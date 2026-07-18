#!/usr/bin/env python3
"""Validate a mini-app source tree against its ``mobius.json`` before push.

Runs the same checks the install path runs (``app.app_source_check``):

  (a) source_files completeness — every relative sibling import reachable from
      the entry and the schedule job is declared in ``source_files`` and
      exists. A miss ships an app that installs from a git clone but breaks on
      every synthetic-fetch install path. This is a hard ERROR (exit 1).
  (b) exact production compilation — bundle the declared entry with the same
      JSX mode, browser target, format, and external runtime libraries used by
      the installer. A compile failure is a hard ERROR (exit 1).
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
  ESBUILD_TIMEOUT_SECS,
  esbuild_command,
  esbuild_environment,
  esbuild_metafile_contract_error,
)
from app.manifest_contract import (  # noqa: E402
  ENTRY_MAX_BYTES,
  ICON_MAX_BYTES,
  MANIFEST_MAX_BYTES,
  SEED_MAX_BYTES,
  SEEDS_COUNT_MAX,
  SEEDS_TOTAL_MAX,
  SKILL_MAX_BYTES,
  SOURCE_FILES_TOTAL_MAX,
  STATIC_ASSET_MAX_BYTES,
  STATIC_ASSETS_COUNT_MAX,
  STATIC_ASSETS_TOTAL_MAX,
  SYSTEM_PROMPT_MAX_BYTES,
  ManifestContractError,
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
    metadata_path = Path(tmp) / "meta.json"
    command = esbuild_command(
      staged_root / entry, Path(tmp) / "app.js", metafile=metadata_path,
    )
    try:
      result = subprocess.run(
        command, capture_output=True, text=True,
        timeout=ESBUILD_TIMEOUT_SECS, check=False,
        env=esbuild_environment(),
      )
    except FileNotFoundError:
      return (
        "esbuild is not installed or not on PATH; install it before "
        "validating an app"
      )
    except subprocess.TimeoutExpired:
      return f"esbuild timed out after {ESBUILD_TIMEOUT_SECS} seconds"
    if result.returncode != 0:
      detail = " ".join(result.stderr.strip().splitlines())
      return detail or f"esbuild exited {result.returncode}"
    try:
      metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
      return f"cannot read esbuild metadata: {exc}"
    return esbuild_metafile_contract_error(metadata)


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
      warnings.append(
        f"manifest icon {icon!r} is missing; install uses the fallback icon"
      )
    elif icon_path.stat().st_size > ICON_MAX_BYTES:
      warnings.append(
        f"manifest icon {icon!r} exceeds {ICON_MAX_BYTES} bytes; "
        "install uses the fallback icon"
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

  def size(path: Path, label: str, maximum: int) -> int:
    try:
      value = path.stat().st_size
    except OSError:
      return 0
    if value > maximum:
      errors.append(f"{label} exceeds {maximum} bytes")
    return value

  size(manifest_path, "manifest", MANIFEST_MAX_BYTES)
  size(root / manifest["entry"], "entry", ENTRY_MAX_BYTES)
  schedule = manifest.get("schedule") or {}
  job = schedule.get("job")
  if isinstance(job, str) and (root / job).is_file():
    size(root / job, f"scheduled job {job!r}", ENTRY_MAX_BYTES)
  source_total = sum(
    size(root / path, f"source file {path!r}", ENTRY_MAX_BYTES)
    for path in (manifest.get("source_files") or [])
    if isinstance(path, str) and (root / path).is_file()
  )
  if source_total > SOURCE_FILES_TOTAL_MAX:
    errors.append(f"source_files exceed {SOURCE_FILES_TOTAL_MAX} bytes total")

  static = static_asset_entries(manifest.get("static_assets") or {})
  if len(static) > STATIC_ASSETS_COUNT_MAX:
    errors.append(f"static_assets exceed {STATIC_ASSETS_COUNT_MAX} files")
  static_total = sum(
    size(root / source, f"static asset {source!r}", STATIC_ASSET_MAX_BYTES)
    for source in static.values() if (root / source).is_file()
  )
  if static_total > STATIC_ASSETS_TOTAL_MAX:
    errors.append(f"static_assets exceed {STATIC_ASSETS_TOTAL_MAX} bytes total")

  seeds = manifest.get("storage_seeds") or {}
  if len(seeds) > SEEDS_COUNT_MAX:
    errors.append(f"storage_seeds exceed {SEEDS_COUNT_MAX} entries")
  seed_total = 0
  for destination, value in seeds.items():
    if isinstance(value, str) and (root / value).is_file():
      seed_total += size(root / value, f"storage seed {value!r}", SEED_MAX_BYTES)
    elif not isinstance(value, str):
      seed_total += len(json.dumps(value).encode("utf-8"))
  if seed_total > SEEDS_TOTAL_MAX:
    errors.append(f"storage_seeds exceed {SEEDS_TOTAL_MAX} bytes total")

  for skill in manifest.get("skills") or []:
    if isinstance(skill, str) and (root / skill).is_file():
      size(root / skill, f"skill {skill!r}", SKILL_MAX_BYTES)
  prompt = manifest.get("system_prompt")
  if isinstance(prompt, str) and (root / prompt).is_file():
    size(root / prompt, f"system_prompt {prompt!r}", SYSTEM_PROMPT_MAX_BYTES)
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
