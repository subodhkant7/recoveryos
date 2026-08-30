#!/usr/bin/env bash
# ==============================================================================
# RecoveryOS — Automated Judge Demo Video Pipeline (Phase 40)
#
# Generates: artifacts/recoveryos_demo_silent.mp4
# Target Duration: 3:45–3:55 (Exact: 3m 48s)
# Resolution: 1920x1080 (16:9), 30 FPS
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${ROOT_DIR}"

echo "[*] Initializing RecoveryOS Demo Video Generation Pipeline..."

# 1. Check Python virtual environment
if [ -f ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
else
  PYTHON_BIN="python3"
fi

# 2. Check ffmpeg availability
if command -v /opt/homebrew/bin/ffmpeg &>/dev/null; then
  export FFMPEG_BIN="/opt/homebrew/bin/ffmpeg"
elif command -v ffmpeg &>/dev/null; then
  export FFMPEG_BIN="ffmpeg"
else
  echo "[!] Error: ffmpeg is required to encode the judge demonstration video."
  exit 1
fi

echo "[*] Using Python: ${PYTHON_BIN}"
echo "[*] Using FFmpeg: ${FFMPEG_BIN}"

# 3. Execute generator script
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/record_recoveryos_demo.py"

# 4. Final verification
if [ -f "${ROOT_DIR}/artifacts/recoveryos_demo_silent.mp4" ]; then
  echo "[✓] Final Silent Demonstration Video Created: artifacts/recoveryos_demo_silent.mp4"
  ls -lh "${ROOT_DIR}/artifacts/recoveryos_demo_silent.mp4"
else
  echo "[!] Video file was not created."
  exit 1
fi
