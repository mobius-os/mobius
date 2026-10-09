#!/usr/bin/env python3
"""Validate a mini-app source tree against its ``mobius.json`` before push.

Runs the same checks the install path runs (``app.app_source_check``):

  (a) source_files completeness — every relative sibling import reachable from
      the entry and the schedule job is declared in ``source_files`` and
      exists. A miss ships an app that installs from a git clone but breaks on
      every synthetic-fetch install path. This is a hard ERROR (exit 1).
  (b) exact production compilation — bundle the declared entry with the same
      JSX mode, browser target, format, injected runtime, and pinned dependency
      graph used by the installer. A compile failure is a hard ERROR (exit 1).
  (c) external-host references — any off-origin http(s) URL in code, which the
      prod ``connect-src 'self'`` CSP blocks silently at runtime. Reported as a
      WARNING; does not fail the run.

Usage:
    python3 backend/scripts/validate-app.py <app-dir> [--manifest path]

Exit code is 1 if any completeness error is found, else 0.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Put the backend package root (this script's grandparent) on sys.path so the
# stdlib-only checker imports cleanly whether invoked from the repo root, the
# backend dir, or an absolute path — no PYTHONPATH required.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.app_source_check import check_manifest_tree  # noqa: E402
from app.app_compile_contract import (  # noqa: E402
  ROLLDOWN_TIMEOUT_SECS,
  rolldown_command,
  rolldown_report_contract_error,
)
from app.build_admission import build_lease  # noqa: E402
from app.manifest_contract import (  # noqa: E402
  MANIFEST_MAX_BYTES,
  PACKAGE_MAX_BYTES,
  SKILL_MAX_BYTES,
  SYSTEM_PROMPT_MAX_BYTES,
  ManifestContractError,
  package_bytes_on_disk,
  package_limit_message,
  size_on_disk,
  static_asset_entries,
  validate_manifest_contract,
)

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


def _compile(
  root: Path, manifest: dict, static_assets: dict[str, str],
) -> str | None:
  """Compile the exact tree a synthetic-fetch install would materialize."""
  entry = manifest["entry"]
  entry_path = root / entry
  try:
    source = entry_path.read_text(encoding="utf-8")
  except OSError as exc:
    return f"cannot read manifest entry {entry!r}: {exc}"
  if not source.strip():
    return "JSX source is empty"
  with tempfile.TemporaryDirectory(prefix="mobius-validate-") as tmp:
    staged_root = Path(tmp) / "app"
    declared = [entry, *(manifest.get("source_files") or [])]
    schedule = manifest.get("schedule") or {}
    if schedule.get("job"):
      declared.append(schedule["job"])
    for rel in dict.fromkeys(declared):
      target = staged_root / rel
      target.parent.mkdir(parents=True, exist_ok=True)
      shutil.copy2(root / rel, target)
    for dest, source_path in static_assets.items():
      target = staged_root / "static" / dest
      target.parent.mkdir(parents=True, exist_ok=True)
      shutil.copy2(root / source_path, target)
    report_path = Path(tmp) / "report.json"
    command = rolldown_command(
      staged_root / entry, Path(tmp) / "app.js", report=report_path,
    )
    try:
      # In a running container this is the same lease shell Vite and mini-app
      # compilation take; without a runtime directory it is a no-op, so the
      # CLI stays zero-configuration in a developer checkout.
      with build_lease():
        result = subprocess.run(
          command, capture_output=True, text=True,
          timeout=ROLLDOWN_TIMEOUT_SECS, check=False,
        )
    except FileNotFoundError:
      return (
        "Node.js is not installed or not on PATH; install it before validating "
        "an app"
      )
    except subprocess.TimeoutExpired:
      return f"Rolldown timed out after {ROLLDOWN_TIMEOUT_SECS} seconds"
    if result.returncode != 0:
      detail = " ".join(result.stderr.strip().splitlines())
      return detail or f"Rolldown exited {result.returncode}"
    try:
      report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
      return f"cannot read Rolldown report: {exc}"
    return rolldown_report_contract_error(report)


def _symlink_component(root: Path, rel: str) -> Path | None:
  current = root
  for part in Path(rel).parts:
    current /= part
    if current.is_symlink():
      return current
  return None


def _referenced_file_findings(
  root: Path, manifest: dict,
) -> tuple[list[str], list[str]]:
  errors: list[str] = []
  warnings: list[str] = []

  references: list[tuple[str, str]] = [("entry", manifest["entry"])]
  references.extend(
    ("source file", path) for path in (manifest.get("source_files") or [])
  )
  schedule = manifest.get("schedule") or {}
  if schedule.get("job"):
    references.append(("scheduled job", schedule["job"]))
  icon = manifest.get("icon")
  if isinstance(icon, str) and icon:
    references.append(("icon", icon))
  static_assets = static_asset_entries(manifest.get("static_assets") or {})
  references.extend(("static asset source", source) for source in static_assets.values())
  references.extend(
    ("storage seed source", value)
    for value in (manifest.get("storage_seeds") or {}).values()
    if isinstance(value, str)
  )
  for label, rel in dict.fromkeys(references):
    symlink = _symlink_component(root, rel)
    if symlink is not None:
      errors.append(
        f"{label} {rel!r} traverses symlink "
        f"{symlink.relative_to(root).as_posix()!r}; package files must be regular files"
      )

  icon_path = root / icon if isinstance(icon, str) and icon else None
  if icon_path is not None and _symlink_component(root, icon) is None:
    if not icon_path.is_file():
      errors.append(
        f"manifest icon {icon!r} is missing; local apply rejects the revision"
      )
  for source in static_assets.values():
    if _symlink_component(root, source) is None and not (root / source).is_file():
      errors.append(f"static asset source {source!r} is missing")
  for value in (manifest.get("storage_seeds") or {}).values():
    if (
      isinstance(value, str)
      and _symlink_component(root, value) is None
      and not (root / value).is_file()
    ):
      errors.append(f"storage seed source {value!r} is missing")
  return errors, warnings


def _package_size_errors(root: Path, manifest_path: Path, manifest: dict) -> list[str]:
  errors: list[str] = []

  if manifest_path.stat().st_size > MANIFEST_MAX_BYTES:
    errors.append(f"manifest exceeds {MANIFEST_MAX_BYTES} bytes")

  package_total = package_bytes_on_disk(root, manifest)
  if package_total > PACKAGE_MAX_BYTES:
    errors.append(package_limit_message(package_total))

  for skill in manifest.get("skills") or []:
    if isinstance(skill, str) and size_on_disk(root, skill) > SKILL_MAX_BYTES:
      errors.append(f"skill {skill!r} exceeds {SKILL_MAX_BYTES} bytes")
  prompt = manifest.get("system_prompt")
  if isinstance(prompt, str) and size_on_disk(root, prompt) > SYSTEM_PROMPT_MAX_BYTES:
    errors.append(
      f"system_prompt {prompt!r} exceeds {SYSTEM_PROMPT_MAX_BYTES} bytes"
    )
  return errors


def _load_tree(root: Path) -> dict[str, str]:
  files: dict[str, str] = {}
  for path in root.rglob("*"):
    if path.is_symlink() or not path.is_file():
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
    Path(args.manifest).absolute() if args.manifest else root / "mobius.json"
  )
  if manifest_path.is_symlink():
    print(
      f"[ERROR] mobius.json: manifest must not be a symlink: {manifest_path}",
      file=sys.stderr,
    )
    return 1
  if not manifest_path.is_file():
    print(f"error: manifest not found at {manifest_path}", file=sys.stderr)
    return 2

  try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
  except json.JSONDecodeError as exc:
    print(f"error: {manifest_path} is not valid JSON: {exc}", file=sys.stderr)
    return 2

  try:
    validate_manifest_contract(manifest)
  except ManifestContractError as exc:
    print(f"[ERROR] mobius.json: {exc}", file=sys.stderr)
    return 1

  tree = _load_tree(root)
  static_assets = static_asset_entries(manifest.get("static_assets") or {})
  for destination in static_assets:
    tree.setdefault(f"static/{destination}", "")
  result = check_manifest_tree(manifest, tree)

  for finding in result.findings:
    stream = sys.stderr if finding.severity == "error" else sys.stdout
    print(finding.format(), file=stream)

  reference_errors, reference_warnings = _referenced_file_findings(root, manifest)
  reference_errors.extend(_package_size_errors(root, manifest_path, manifest))
  for detail in reference_errors:
    print(f"[ERROR] mobius.json: {detail}", file=sys.stderr)
  for detail in reference_warnings:
    print(f"[WARN ] mobius.json: {detail}")

  compile_error = None
  entry = manifest.get("entry")
  if not result.errors and not reference_errors:
    if not isinstance(entry, str) or not entry:
      compile_error = "manifest `entry` must be a non-empty string"
    else:
      compile_error = _compile(root, manifest, static_assets)
  if compile_error:
    print(f"[ERROR] {entry or 'mobius.json'}: compile failed: {compile_error}", file=sys.stderr)

  name = manifest.get("name") or manifest.get("id") or root.name
  error_count = len(result.errors) + len(reference_errors) + (1 if compile_error else 0)
  warning_count = len(result.warnings) + len(reference_warnings)
  if error_count:
    print(
      f"\n{name}: FAIL — {error_count} error(s), "
      f"{warning_count} warning(s)",
      file=sys.stderr,
    )
    return 1
  print(
    f"\n{name}: OK — 0 errors, {warning_count} warning(s)"
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
