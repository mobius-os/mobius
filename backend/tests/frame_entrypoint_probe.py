"""Run frame behavior against its real inline declarations, not a copied loader."""

import json
import subprocess
from pathlib import Path


def run_frame_entrypoint(html: str, **options) -> dict:
  probe = Path(__file__).with_name("frame_entrypoint_probe.mjs")
  result = subprocess.run(
    ["node", "--max-old-space-size=96", str(probe)],
    input=json.dumps({"html": html, **options}),
    capture_output=True,
    text=True,
    timeout=15,
  )
  assert result.returncode == 0, result.stderr or result.stdout
  return json.loads(result.stdout)
