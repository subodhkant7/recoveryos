#!/usr/bin/env python3
"""Record the live RecoveryOS local demo in a real browser and export MP4."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
RECORDINGS = ROOT / "recordings"
RAW_DIR = RECORDINGS / "raw"
MP4_DIR = RECORDINGS / "mp4"
META_DIR = RECORDINGS / "metadata"
BASE_URL = os.environ.get("RECOVERYOS_URL", "http://127.0.0.1:8000")

SCENARIOS = [
    ("billing_unavailable", "Billing Provider Outage", "#btn-launch-modal"),
    ("contradictory_evidence", "Contradictory Evidence", "#btn-launch-modal"),
    ("worker_interruption", "Worker Interruption", "#btn-launch-modal"),
]


def ensure_dirs() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    MP4_DIR.mkdir(parents=True, exist_ok=True)
    META_DIR.mkdir(parents=True, exist_ok=True)


def next_available(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    idx = 2
    while True:
        candidate = path.with_name(f"{stem}_{idx}{suffix}")
        if not candidate.exists():
            return candidate
        idx += 1


def fetch_state(page, scenario_name: str, created_after: float | None = None):
    """Return the newest workflow rows for a scenario, ignoring stale prior runs."""
    return page.evaluate(
        """
        async ({ scenarioName, createdAfter }) => {
          const key = 'rec_jwt_operator_tenant-default';
          const token = sessionStorage.getItem(key);
          const headers = token ? { Authorization: 'Bearer ' + token, 'X-Tenant-ID': 'tenant-default'} : { 'X-Tenant-ID': 'tenant-default' };
          try {
            const res = await fetch('/api/workflows?limit=50', { headers });
            if (!res.ok) return { workflows: [] };
            const data = await res.json();
            const workflows = Array.isArray(data.workflows) ? data.workflows : [];
            const matches = workflows.filter((w) => {
              const sameScenario = w.scenario === scenarioName;
              if (!sameScenario) return false;
              if (createdAfter == null) return true;
              const createdAt = new Date(w.created_at || 0).getTime();
              return createdAt >= createdAfter * 1000;
            }).sort((a, b) => {
              const ta = new Date(a.updated_at || a.created_at || 0).getTime();
              const tb = new Date(b.updated_at || b.created_at || 0).getTime();
              return tb - ta;
            });
            return { workflows: matches };
          } catch (error) {
            return { workflows: [] };
          }
        }
        """,
        {"scenarioName": scenario_name, "createdAfter": created_after},
    )


def wait_for_workflow(page, scenario_name: str, expected_states, timeout_seconds: int = 180, created_after: float | None = None) -> dict:
    states = expected_states if isinstance(expected_states, (list, tuple, set)) else [expected_states]
    deadline = time.time() + timeout_seconds
    last = None
    while time.time() < deadline:
        data = fetch_state(page, scenario_name, created_after)
        worked = data.get("workflows") or []
        for wf in worked:
            state = wf.get("state")
            if state in states:
                workflow_id = wf.get("workflow_id")
                if workflow_id:
                    page.evaluate(
                        """
                        ({ workflowId }) => {
                          const cards = [...document.querySelectorAll('.incident-card')];
                          const card = cards.find((el) => el.dataset.id === workflowId) || cards[0];
                          if (card) {
                            card.click();
                          }
                        }
                        """,
                        {"workflowId": workflow_id},
                    )
                    page.wait_for_timeout(1000)
                return wf
            last = wf
        time.sleep(1)
    if last:
        expected = ', '.join(states)
        raise TimeoutError(f"Scenario {scenario_name} reached last state {last.get('state')} instead of {expected}")
    raise TimeoutError(f"Scenario {scenario_name} did not reach any of {states} within {timeout_seconds}s")


def open_scenario(page, scenario_name: str, created_after: float | None = None) -> None:
    page.locator('#btn-launch-modal').click()
    page.locator(f'input[name="scenario_choice"][value="{scenario_name}"]').check()
    page.locator('#btn-execute-scenario').click()
    if created_after is not None:
        page.wait_for_timeout(500)


def hold(page, seconds: float, label: str) -> None:
    print(f"[hold] {label} for {seconds:.1f}s")
    page.wait_for_timeout(int(seconds * 1000))


def ensure_page_ready(page) -> None:
    page.goto(BASE_URL + "/console/", wait_until="networkidle", timeout=120000)
    page.wait_for_selector('#btn-launch-modal', timeout=120000)
    page.wait_for_timeout(2500)


def wait_for_element_text(page, selector: str, expected_texts, timeout_seconds: int = 180) -> None:
    text_variants = expected_texts if isinstance(expected_texts, (list, tuple)) else [expected_texts]
    page.wait_for_function(
        """
        (args) => {
          const el = document.querySelector(args.selector);
          const fullText = (el?.textContent || '').trim();
          return !!el && args.texts.some((candidate) => fullText.toLowerCase().includes(candidate.toLowerCase()));
        }
        """,
        arg={"selector": selector, "texts": text_variants},
        timeout=timeout_seconds * 1000,
    )


def run_recording() -> None:
    ensure_dirs()
    raw_target = next_available(RAW_DIR / "RecoveryOS_Local_Demo.webm")
    final_mp4 = next_available(MP4_DIR / "RecoveryOS_Local_Demo.mp4")

    print(f"[info] Recording raw video to {raw_target}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            device_scale_factor=1,
            ignore_https_errors=True,
            record_video_dir=str(RAW_DIR),
            record_video_size={"width": 1920, "height": 1080},
        )
        page = context.new_page()
        ensure_page_ready(page)

        # Scenario 01: billing_unavailable
        try:
            launch_started = time.time()
            open_scenario(page, "billing_unavailable", created_after=launch_started)
            wait_for_workflow(page, "billing_unavailable", ["COMPLETED"], timeout_seconds=300, created_after=launch_started)
            wait_for_element_text(page, '#graph-stage-label', ['SYSTEM RECOVERED AUTONOMOUSLY', 'RECOVERED'], timeout_seconds=180)
            page.locator('#recovery-proof-certificate').wait_for(state='visible', timeout=60000)
            hold(page, 5.0, 'billing recovery proof')
            hold(page, 4.0, 'billing verification hold')
            hold(page, 4.0, 'billing final recovery hold')
        except Exception as exc:
            print(f"[warning] Scenario 1 did not finish cleanly: {exc}")
            raise

        # Scenario 02: contradictory evidence
        try:
            launch_started = time.time()
            open_scenario(page, "contradictory_evidence", created_after=launch_started)
            wait_for_workflow(page, "contradictory_evidence", ["AWAITING_APPROVAL", "RECOVERING", "EXECUTING"], timeout_seconds=300, created_after=launch_started)
            wait_for_element_text(page, '#graph-stage-label', ['EXECUTING • AGENT ACTIVE', 'AWAITING APPROVAL', 'RECOVERING', 'APPROVAL'], timeout_seconds=180)
            hold(page, 6.0, 'contradictory evidence live execution')
        except Exception as exc:
            print(f"[warning] Scenario 2 did not reach a stable live state: {exc}")
            raise

        # Scenario 03: worker interruption
        try:
            launch_started = time.time()
            open_scenario(page, "worker_interruption", created_after=launch_started)
            wait_for_workflow(page, "worker_interruption", ["COMPLETED"], timeout_seconds=300, created_after=launch_started)
            wait_for_element_text(page, '#graph-stage-label', ['SYSTEM RECOVERED AUTONOMOUSLY', 'RECOVERED'], timeout_seconds=180)
            page.locator('#worker-resilience-card').wait_for(state='visible', timeout=60000)
            hold(page, 5.0, 'worker reconciliation hold')
            hold(page, 5.0, 'worker final verified hold')
        except Exception as exc:
            print(f"[warning] Scenario 3 did not finish cleanly: {exc}")
            raise

        # Historical workflow hydration proof
        all_cards = page.locator('#incident-list-container .incident-card')
        if all_cards.count() > 0:
            all_cards.first.click()
            page.locator('#recovery-proof-certificate').wait_for(state='visible', timeout=60000)
            hold(page, 4.0, 'historical workflow hydration')

        # Let the browser finish and stop video capture.
        page.wait_for_timeout(2000)
        context.close()
        browser.close()

    # Some Playwright versions produce the video under a hidden mp4 container path
    # after context close. Copy the final video to the desired raw target if needed.
    video_candidates = sorted(RAW_DIR.glob("*.webm"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not video_candidates:
        raise RuntimeError("No browser recording was produced in recordings/raw")
    chosen_raw = video_candidates[0]
    if chosen_raw != raw_target:
        shutil.copy2(chosen_raw, raw_target)
    if raw_target.exists() is False:
        raw_target = chosen_raw

    print(f"[info] Raw recording created: {raw_target}")
    ffmpeg_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(raw_target),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(final_mp4),
    ]
    print(f"[exec] {' '.join(ffmpeg_cmd)}")
    subprocess.run(ffmpeg_cmd, check=True)

    probe_cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,size",
        "-show_entries",
        "stream=codec_name,width,height,r_frame_rate",
        "-of",
        "json",
        str(final_mp4),
    ]
    print(f"[exec] {' '.join(probe_cmd)}")
    result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
    metadata = json.loads(result.stdout)
    with (META_DIR / "RecoveryOS_Local_Demo.json").open("w", encoding="utf-8") as handle:
        json.dump({
            "source_raw": str(raw_target),
            "output_mp4": str(final_mp4),
            "probe": metadata,
            "base_url": BASE_URL,
            "scenarios": ["billing_unavailable", "contradictory_evidence", "worker_interruption"],
        }, handle, indent=2)

    print(f"[info] Final MP4 created: {final_mp4}")
    print("[info] FFprobe metadata:")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    try:
        run_recording()
    except Exception as exc:
        print(f"[fatal] {exc}", file=sys.stderr)
        raise
