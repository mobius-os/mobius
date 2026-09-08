"""Core project templates compose with installed providers through the same registry."""
from pathlib import Path

TEMPLATE_ROOT = Path(__file__).with_suffix('')
LINKED_APP_GUIDANCE = (
  "This Project edits the installed app's existing source folder, not a copy. "
  "Saving source never updates the running app. The owner's explicit Build & update app "
  "action uses the ordinary app apply workflow; a failed build keeps the last working app. "
  "Open the installed app for its real runtime, theme and data; do not create a duplicate "
  "App Creation or standalone app preview. Collaborators edit the same linked files, "
  "but project membership does not grant app runtime, private data or update authority."
)


def linked_app_id(template):
  """Identify explicit linked-app ownership, excluding historical imported copies."""
  imported = template.get("imported_from", {}) if isinstance(template, dict) else {}
  if not isinstance(imported, dict) or imported.get("management") != "linked" or imported.get("kind") != "app":
    return None
  value = imported.get("id")
  return str(value) if value is not None and str(value) else None


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
