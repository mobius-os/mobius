#!/usr/bin/env python3
"""Registers a mini-app from a JSX file via the Ultimate API.

Usage:
  register_app.py <name> <description> <jsx_file_path>

Environment:
  AGENT_TOKEN   JWT bearer token for the Ultimate API.
  API_BASE_URL  Base URL of the Ultimate backend (default: http://localhost:8000).

Prints the created or updated app JSON to stdout.
"""

import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

# Reuse the same local-manifest projection as the live source watcher. The
# script normally runs by absolute path, so add the backend package root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.app_capabilities import local_manifest_runtime_fields  # noqa: E402
from app import app_git  # noqa: E402


def _call(url: str, token: str, method: str, data: dict | None = None):
  body = json.dumps(data).encode() if data is not None else None
  req = urllib.request.Request(
    url,
    data=body,
    headers={
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
    },
    method=method,
  )
  try:
    with urllib.request.urlopen(req) as resp:
      body = resp.read()
      return json.loads(body) if body else None
  except urllib.error.HTTPError as exc:
    print(f"API error {exc.code}: {exc.read().decode()}", file=sys.stderr)
    sys.exit(1)


def _slugify_for_match(value: str | None) -> str:
  slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
  return slug or "app"


def _row_matches_name_or_slug(app: dict, name: str) -> bool:
  expected = _slugify_for_match(name)
  return (
    app.get("name") == name
    or app.get("slug") == expected
    or _slugify_for_match(app.get("name")) == expected
  )


def _find_existing(
  apps: list,
  source_dir: str,
  name: str,
  *,
  legacy_source_dirs: list[str] | None = None,
):
  """Find the app this (re-)registration refers to, by STABLE identity.

  Identity is source_dir, not the display name. An app renamed in place keeps the
  same /data/apps/<slug>/ source dir, so matching on name would miss the existing
  row and CREATE a duplicate.
  The file watcher already keys edits on the exact source_dir, so it is the
  canonical per-app key. Fall back to the name only for legacy rows that
  predate source_dir being populated (NULL/absent in the API response), where
  there is no stable key to compare.
  """
  by_dir = next(
    (a for a in apps if a.get("source_dir") == source_dir), None
  )
  if by_dir is not None:
    return by_dir
  legacy_dirs = set(legacy_source_dirs or [])
  if legacy_dirs:
    by_legacy_dir = next(
      (
        a for a in apps
        if a.get("source_dir") in legacy_dirs
        and _row_matches_name_or_slug(a, name)
      ),
      None,
    )
    if by_legacy_dir is not None:
      return by_legacy_dir
  return next(
    (a for a in apps if not a.get("source_dir") and a.get("name") == name),
    None,
  )


def _notify(token: str, base: str, event_type: str, **kwargs):
  """Best-effort notification — failures are not fatal."""
  try:
    data = {"type": event_type, **kwargs}
    _call(f"{base}/api/notify", token, "POST", data)
  except SystemExit:
    pass  # notify is best-effort; don't abort on failure


def _read_manifest_registration(source_dir: str) -> dict:
  """Read safe local-registration fields from the manifest beside index.jsx.

  `mobius.json` is the source of truth for local app capabilities. Registration
  used to read only the nested `capabilities` object while silently ignoring
  top-level `offline_capable`; every ordinary app then needed a diagnostic
  PATCH to make the live row match its manifest. Keep the create/update payload
  aligned here instead.

  A missing manifest preserves the historical bare-app behavior. When the
  manifest exists, omitted optional fields remain omitted on PATCH rather than
  resetting an existing row.
  """
  manifest_path = os.path.join(source_dir, "mobius.json")
  try:
    with open(manifest_path, encoding="utf-8") as f:
      manifest = json.load(f)
  except FileNotFoundError:
    return {"capabilities": {}}
  except (OSError, json.JSONDecodeError) as exc:
    print(f"Cannot read mobius.json: {exc}", file=sys.stderr)
    sys.exit(1)
  try:
    return local_manifest_runtime_fields(manifest)
  except ValueError as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(1)


def _read_capabilities(source_dir: str) -> dict:
  """Backward-compatible helper used by older callers and focused tests."""
  return _read_manifest_registration(source_dir)["capabilities"]


def _finalize_source(source_dir: str, *, created: bool) -> None:
  """Record the source accepted by registration in the per-app repository.

  Registration owns the initial commit; the watcher owns later save commits.
  ``commit_local`` treats an already-clean tree as success, so a delayed
  watcher event cannot turn successful registration into a failing empty
  ``git commit``.
  """
  message = "create app" if created else "register app update"
  try:
    app_git.commit_local(source_dir, message)
  except Exception as exc:  # noqa: BLE001 - surface an actionable CLI failure
    print(
      "App registration succeeded, but its source commit failed: "
      f"{exc}. Re-run register_app.py to retry safely.",
      file=sys.stderr,
    )
    sys.exit(1)


def main() -> None:
  if len(sys.argv) != 4:
    print(
      "Usage: register_app.py <name> <description> <jsx_file_path>",
      file=sys.stderr,
    )
    sys.exit(1)

  name, description, jsx_path = sys.argv[1], sys.argv[2], sys.argv[3]

  try:
    with open(jsx_path, encoding="utf-8") as f:
      jsx_source = f.read()
  except OSError as exc:
    print(f"Cannot read JSX file: {exc}", file=sys.stderr)
    sys.exit(1)

  # Absolute directory of the JSX file — sent to the API so the file
  # watcher can resolve `<app_dir>/index.jsx` change events back to
  # this app's DB row exactly, without slugify-guessing the name.
  source_dir = os.path.dirname(os.path.abspath(jsx_path))
  manifest_registration = _read_manifest_registration(source_dir)

  token = os.environ.get("AGENT_TOKEN")
  if not token:
    print("AGENT_TOKEN environment variable is not set.", file=sys.stderr)
    sys.exit(1)

  base = os.environ.get("API_BASE_URL", "http://localhost:8000")
  # Tag the app with the current chat so errors can be routed back to it.
  chat_id = os.environ.get("CHAT_ID") or None
  legacy_source_dirs = [
    os.path.abspath(p)
    for p in os.environ.get("MOBIUS_REGISTER_LEGACY_SOURCE_DIRS", "").split(
      os.pathsep
    )
    if p
  ]

  # Trailing slash required — FastAPI redirects /api/apps → /api/apps/ and
  # urllib does not follow POST redirects, so use the canonical URL directly.
  apps = _call(f"{base}/api/apps/", token, "GET")
  # Match on the stable source_dir, not the display name: a rename keeps the
  # source dir but changes the name, and a name-only match would create a
  # duplicate row for the renamed app (feature 097).
  existing = _find_existing(
    apps, source_dir, name, legacy_source_dirs=legacy_source_dirs,
  )

  if existing:
    app = _call(
      f"{base}/api/apps/{existing['id']}",
      token,
      "PATCH",
      {
        "name": name,
        "description": description,
        "jsx_source": jsx_source,
        "chat_id": chat_id,
        "source_dir": source_dir,
        **manifest_registration,
      },
    )
  else:
    app = _call(
      f"{base}/api/apps/",
      token,
      "POST",
      {
        "name": name,
        "description": description,
        "jsx_source": jsx_source,
        "chat_id": chat_id,
        "source_dir": source_dir,
        **manifest_registration,
      },
    )

  _finalize_source(source_dir, created=existing is None)
  print(json.dumps(app))
  _notify(token, base, "app_updated", appId=str(app["id"]))


if __name__ == "__main__":
  main()
