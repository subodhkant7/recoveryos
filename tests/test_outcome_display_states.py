"""Regression tests for the single-source required-outcome display state."""

import json
import subprocess
from pathlib import Path


APP_JS = Path(__file__).parents[1] / "backend" / "api" / "static" / "app.js"


def derive_states(outcomes, evidence=None, events=None, steps=None):
    payload = json.dumps({
        "outcomes": outcomes,
        "evidence": evidence or [],
        "events": events or [],
        "steps": steps or [],
    })
    script = f"""
const fs = require('fs');
const vm = require('vm');
const context = {{
  window: {{ addEventListener() {{}}, deriveOutcomeDisplayStates: null }},
  document: {{ getElementById() {{ return null; }}, querySelector() {{ return null; }},
    querySelectorAll() {{ return []; }} }},
  setInterval() {{}}, setTimeout() {{}}, console, fetch() {{}},
}};
vm.createContext(context);
vm.runInContext(fs.readFileSync({json.dumps(str(APP_JS))}, 'utf8'), context);
const input = {payload};
process.stdout.write(JSON.stringify(context.window.deriveOutcomeDisplayStates(
  input.outcomes, input.evidence, input.events, input.steps
)));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


OUTCOMES = [{"outcome_id": outcome} for outcome in (
    "identity_verified", "documents_validated", "risk_assessed", "billing_configured"
)]


def test_outcome_display_states_fresh_workflow():
    states = derive_states(OUTCOMES)
    assert all(state == {"status": "pending", "icon": "○", "label": "PENDING"} for state in states.values())


def test_outcome_display_states_partial_verification():
    states = derive_states(
        OUTCOMES,
        evidence=[{
            "evidence_type": "VERIFICATION",
            "source": "verify:identity_verified",
            "data": {"target": "identity_verified", "passed": True},
        }],
        steps=[{"target_outcome_id": "documents_validated", "status": "RUNNING"}],
    )
    assert states["identity_verified"]["label"] == "VERIFIED"
    assert states["identity_verified"]["icon"] == "✓"
    assert states["documents_validated"]["label"] == "IN PROGRESS"
    assert states["documents_validated"]["icon"] == "◐"
    assert states["risk_assessed"]["label"] == "PENDING"


def test_outcome_display_states_fully_completed_requires_independent_evidence():
    evidence = [{
        "evidence_type": "VERIFICATION",
        "source": f"verify:{outcome['outcome_id']}",
        "data": {"target": outcome["outcome_id"], "passed": True},
    } for outcome in OUTCOMES]
    states = derive_states(OUTCOMES, evidence=evidence)
    assert all(state["status"] == "verified" and state["icon"] == "✓" and state["label"] == "VERIFIED"
               for state in states.values())


def test_outcome_display_states_failed_does_not_affect_later_outcomes():
    states = derive_states(
        OUTCOMES,
        evidence=[{
            "evidence_type": "VERIFICATION",
            "source": "verify:billing_configured",
            "data": {"target": "billing_configured", "passed": False},
        }],
    )
    assert states["billing_configured"]["status"] == "failed"
    assert states["billing_configured"]["icon"] == "✕"
    assert states["billing_configured"]["label"] == "FAILED"
    assert states["risk_assessed"]["status"] == "pending"


def test_historical_completed_hydration_uses_evidence_not_workflow_state():
    evidence = [{
        "evidence_type": "VERIFICATION",
        "source": f"verify:{outcome['outcome_id']}",
        "data": {"target": outcome["outcome_id"], "passed": True},
    } for outcome in OUTCOMES]
    states = derive_states(OUTCOMES, evidence=evidence)
    assert all(state["icon"] == "✓" and state["label"] == "VERIFIED" for state in states.values())


def test_outcome_display_state_icon_and_label_are_consistent():
    states = derive_states(OUTCOMES)
    expected = {"pending": ("○", "PENDING"), "in_progress": ("◐", "IN PROGRESS"),
                "verified": ("✓", "VERIFIED"), "failed": ("✕", "FAILED")}
    assert all((state["icon"], state["label"]) == expected[state["status"]]
               for state in states.values())
