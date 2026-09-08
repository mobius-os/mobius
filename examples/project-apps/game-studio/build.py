"""Compile a project-owned game scene into the provider's self-contained player."""
import json
import os
from pathlib import Path

root = Path(os.environ['PROJECT_ROOT']).resolve()
source = (root / os.environ['PROJECT_SOURCE']).resolve()
if not source.is_relative_to(root):
    raise ValueError('The game scene must be inside its project.')
scene = json.loads(source.read_text())
if not isinstance(scene, dict) or not isinstance(scene.get('title'), str):
    raise ValueError('The game scene needs a title.')
encoded = json.dumps(scene).replace('<', '\\u003c')
player = Path(__file__).with_name('player.html').read_text()
output = Path(os.environ['PROJECT_OUTPUT_DIR'])
output.mkdir(parents=True, exist_ok=True)
(output / 'index.html').write_text(player.replace('__SCENE__', encoded))
