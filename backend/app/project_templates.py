"""Core project templates compose with installed providers through the same registry."""
from pathlib import Path

TEMPLATE_ROOT = Path(__file__).with_suffix('')
CORE_TEMPLATES = [
  {
    "id": "blank", "name": "Blank project", "kind": "blank",
    "description": "Start with an empty folder.",
    "guidance": "Work only inside this project's root unless the user asks otherwise.",
    "files": {},
  },
  {
    "id": "app", "name": "App project", "kind": "mini-app",
    "description": "Build a Möbius mini-app with editable source and a live preview.",
    "guidance": (
      "Edit index.jsx and mobius.json inside this project. Build the App Creation "
      "for a source preview. This preview is not an installed app and has no "
      "app-scoped storage or host permissions. Use the ordinary app apply workflow "
      "only when the owner asks to install or update the app; never publish implicitly."
    ),
    "skills": ["building-apps-quickstart", "visual-testing", "notifications"],
    "files": {"index.jsx": "app/index.jsx", "mobius.json": "app/mobius.json"},
    "previews": [{"id": "app", "name": "App", "kind": "html", "path": "index.jsx"}],
  },
]
