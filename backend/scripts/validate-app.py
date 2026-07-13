#!/usr/bin/env python3
"""Validate a mini-app source tree against its ``mobius.json`` before push.

Runs the same checks the install path runs (``app.app_source_check``):

  (a) source_files completeness — every relative sibling import reachable from
      the entry and the schedule job is declared in ``source_files`` and
      exists. A miss ships an app that installs from a git clone but breaks on
      every synthetic-fetch install path. This is a hard ERROR (exit 1).
  (b) external-host references — any off-origin http(s) URL in code, which the
      prod ``connect-src 'self'`` CSP blocks silently at runtime. Reported as a
      WARNING; does not fail the run.

Usage:
    python3 backend/scripts/validate-app.py <app-dir> [--manifest path]

Exit code is 1 if any completeness error is found, else 0.
"""

import argparse
import json
import sys
from pathlib import Path

# Put the backend package root (this script's grandparent) on sys.path so the
# stdlib-only checker imports cleanly whether invoked from the repo root, the
# backend dir, or an absolute path — no PYTHONPATH required.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.app_source_check import check_manifest_tree  # noqa: E402

# Read text for anything that could hold an import or a URL; everything else
# (images, fonts, wasm) is recorded as an empty-content key so a relative
# import onto it still resolves. Keeps the walk cheap and encoding-safe.
_TEXT_EXTS = frozenset({
  ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json",
  ".css", ".html", ".htm", ".md", ".txt", ".svg", ".sh",
})
# Directories that never hold hand-written source the manifest declares —
# build output, deps, git, and the installer-managed static tree.
_SKIP_DIRS = frozenset({".git", "node_modules", "dist", ".build", "static"})


def _load_tree(root: Path) -> dict[str, str]:
  files: dict[str, str] = {}
  for path in root.rglob("*"):
    if not path.is_file():
      continue
    rel_parts = path.relative_to(root).parts
    if any(part in _SKIP_DIRS for part in rel_parts[:-1]):
      continue
    rel = "/".join(rel_parts)
    if rel.endswith(".bak"):
      continue
    if path.suffix.lower() in _TEXT_EXTS:
      files[rel] = path.read_text(encoding="utf-8", errors="replace")
    else:
      files[rel] = ""
  return files


def main() -> int:
  parser = argparse.ArgumentParser(
    description="Validate a mini-app source tree against its mobius.json.",
  )
  parser.add_argument("app_dir", help="Path to the app source directory.")
  parser.add_argument(
    "--manifest",
    help="Path to the manifest (default: <app-dir>/mobius.json).",
  )
  args = parser.parse_args()

  root = Path(args.app_dir).resolve()
  if not root.is_dir():
    print(f"error: {root} is not a directory", file=sys.stderr)
    return 2
  manifest_path = (
    Path(args.manifest).resolve() if args.manifest else root / "mobius.json"
  )
  if not manifest_path.is_file():
    print(f"error: manifest not found at {manifest_path}", file=sys.stderr)
    return 2

  try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
  except json.JSONDecodeError as exc:
    print(f"error: {manifest_path} is not valid JSON: {exc}", file=sys.stderr)
    return 2

  result = check_manifest_tree(manifest, _load_tree(root))

  for finding in result.findings:
    stream = sys.stderr if finding.severity == "error" else sys.stdout
    print(finding.format(), file=stream)

  name = manifest.get("name") or manifest.get("id") or root.name
  if result.errors:
    print(
      f"\n{name}: FAIL — {len(result.errors)} error(s), "
      f"{len(result.warnings)} warning(s)",
      file=sys.stderr,
    )
    return 1
  print(
    f"\n{name}: OK — 0 errors, {len(result.warnings)} warning(s)"
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
