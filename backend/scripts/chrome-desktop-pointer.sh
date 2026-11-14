#!/bin/sh
# Launch wrapper for the capture browser at desktop widths.
#
# Headless Chromium has no pointing device, so it reports `hover: none` and
# `pointer: none`; every `@media (hover: hover)` / `(pointer: fine)` style is
# dropped from desktop captures even though the owner's desktop renders them
# (this corrupted published review screenshots). The Blink settings below are
# the only launch surface that fixes the CSS media evaluation itself —
# `Emulation.setEmulatedMedia` from a side CDP session does not stick, and
# `AGENT_BROWSER_ARGS` splits on the commas this switch needs.
#
# agent-screenshot.sh points AGENT_BROWSER_EXECUTABLE_PATH here only when the
# capture viewport is desktop-sized. The browser daemon spawns this wrapper
# with the daemon's environment, not the capture's, so the real executable is
# resolved here from the agent-browser config rather than passed through env.
REAL_BROWSER="${MOBIUS_SCREENSHOT_REAL_BROWSER:-}"
if [ -z "$REAL_BROWSER" ] || [ ! -x "$REAL_BROWSER" ]; then
  REAL_BROWSER="$(python3 - <<'PY'
import json
import os

for path in (os.environ.get("AGENT_BROWSER_CONFIG"), "/app/agent-browser-config.json"):
  if not path:
    continue
  try:
    with open(path) as handle:
      executable = json.load(handle).get("executablePath", "")
  except (OSError, ValueError):
    continue
  if executable and os.access(executable, os.X_OK):
    print(executable)
    break
PY
)"
fi
if [ -z "$REAL_BROWSER" ] || [ ! -x "$REAL_BROWSER" ]; then
  echo "chrome-desktop-pointer.sh: no usable browser executable found" >&2
  exit 1
fi
exec "$REAL_BROWSER" \
  --blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4 \
  "$@"
