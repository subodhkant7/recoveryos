# RecoveryOS — Phase 39 Video Recording Setup & Technical Configuration

This document specifies the exact technical requirements and step-by-step procedures for recording the final hackathon presentation video.

---

## 1. Technical Specifications

| Parameter | Standard | Target |
|:---|:---|:---|
| **Video Resolution** | 1080p (Full HD) | `1920 x 1080` (16:9 Aspect Ratio) |
| **Frame Rate** | 60 FPS or 30 FPS constant | 60 FPS preferred for fluid animations |
| **Video Bitrate** | 8,000 – 12,000 kbps | H.264 (MP4 container) |
| **Audio Sample Rate** | 48.0 kHz | 24-bit / Stereo or Mono |
| **Audio Target Level** | -14 LUFS (Peak at -3.0 dB) | Clear human narration, zero background hiss |
| **Maximum Duration** | **4 minutes (240 seconds)** | **Target: 3:45 – 3:55** |

---

## 2. Browser Environment Preparation

1. **Browser Profile**:
   - Use Google Chrome, Brave, or Safari in a clean dedicated profile.
   - Disable all browser extensions (especially ad blockers or password managers with badges).
   - Hide Bookmarks Bar (`Cmd + Shift + B` on macOS).
   - Set browser zoom to exactly **100%** (`Cmd + 0`).
2. **Viewport Size**:
   - Window size: `1920 x 1080` or maximized on a 1080p display.
   - URL: `http://localhost:8000/console/`
3. **OS Notifications**:
   - Enable **Do Not Disturb** / Focus Mode on macOS.
   - Hide Dock (`Cmd + Option + D`).
   - Close all background messengers (Slack, Discord, Telegram, Email).

---

## 3. Server Startup Procedure

Before starting the screen recording, launch the clean local server:

```bash
# 1. Open Terminal and activate virtualenv
cd "Recovery OS"
source .venv/bin/activate

# 2. Run clean secret scan & test verification
python -m pytest tests/test_phase33_final_judge_attack.py -q

# 3. Launch the FastAPI server
uvicorn backend.api.server:app --host 127.0.0.1 --port 8000
```

Verify in browser:
- Open `http://localhost:8000/console/`
- Top health indicator shows: `RECOVERY CONTROL PLANE • ONLINE`
- Stream pill shows: `● LIVE EXECUTION`
- Zero red console errors in browser DevTools.

---

## 4. OBS Studio Configuration (Recommended)

1. **Canvas & Output**:
   - Base Canvas Resolution: `1920x1080`
   - Output (Scaled) Resolution: `1920x1080`
   - Downscale Filter: Lanczos (36 samples)
   - Common FPS Values: 60
2. **Sources**:
   - **Window Capture**: Target the browser window (`RecoveryOS — Recovery-First Control Plane`).
   - **Audio Input Capture**: Target your USB condenser/dynamic microphone.
3. **Audio Filters on Microphone**:
   - *Noise Suppression*: RNNoise (high quality).
   - *Compressor*: Ratio 3:1, Threshold -18dB, Attack 6ms, Release 60ms.
   - *Limiter*: Threshold -2.0dB, Release 60ms.

---

## 5. macOS QuickTime Screen Recording (Alternative)

1. Open **QuickTime Player** → `File` → `New Screen Recording` (`Cmd + Shift + 5`).
2. Select **Options**:
   - Microphone: Select external USB microphone.
   - Show Mouse Clicks: Optional (keep subtle).
   - Timer: 5 seconds countdown.
3. Drag selection box to fill the entire 1920x1080 browser window.
4. Click **Record**.

---

## 6. Pre-Recording Rehearsal Checklist

- [ ] Local server running on `http://127.0.0.1:8000`
- [ ] No git unstaged files, clean working tree
- [ ] Microphone tested and levels calibrated (-12dB to -6dB peak)
- [ ] Script printed or open on a second monitor ([`docs/PHASE_39_DEMO_RECORDING_SCRIPT.md`](PHASE_39_DEMO_RECORDING_SCRIPT.md))
- [ ] Stop-watch ready on phone to monitor target time (3:45 – 3:55)
- [ ] Rehearsed one full run-through of Scenarios 01, 02, and 03
