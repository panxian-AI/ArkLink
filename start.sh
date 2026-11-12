#!/usr/bin/env bash
# ---------------------------------------------------------------
#  ArkLink one-click launcher (Linux / macOS)
#    ./start.sh                 # auto-picks the engine
#    ./start.sh --port 6801     # extra args pass through
#  Headless Linux (Xvfb): run this inside the X session, engine=x11.
# ---------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[X] python3 not found" >&2
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "[1/3] creating .venv ..."
  "$PY" -m venv .venv
  echo "[2/3] installing ..."
  .venv/bin/python -m pip install -q --upgrade pip || true
  .venv/bin/python -m pip install -q -e . || echo "[!] pip install failed (offline?) - trying anyway"
else
  echo "[1/3] venv found, skipping install"
fi

# 引擎自动选择：有 xdotool + DISPLAY 就说明这是「无头 X 里跑 QQ」的场景，
# 用 x11（虚拟屏里点，不抢宿主焦点）；否则用 host（Windows 那种窗口通道）。
ENGINE=${ARKLINK_ENGINE:-}
if [ -z "$ENGINE" ]; then
  if [ -n "${DISPLAY:-}" ] && command -v xdotool >/dev/null 2>&1; then
    ENGINE=x11
  else
    ENGINE=host
  fi
fi

echo "[3/3] starting console (engine=$ENGINE) ..."
echo
exec .venv/bin/python -m arklink start --engine "$ENGINE" "$@"
