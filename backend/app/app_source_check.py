"""Static checks on a mini-app's source tree against its ``mobius.json``.

Two defect classes a human reviewer used to catch by hand, now mechanical:

  (a) source_files completeness — every RELATIVE sibling module reachable
      from the entry (and the schedule job), transitively, must be declared
      in the manifest's ``source_files``. A miss means the non-clone install
      path (synthetic fetch) writes an incomplete tree, so the app fails to
      compile/load for everyone who did not get it via a full ``git clone``.
      A clone install hides the bug — the whole repo is on disk — which is
      exactly how the Editor app shipped with two imported icons missing from
      ``source_files`` and broke every fetch install. Install-breaking, so
      these are ERRORS.

  (b) external-host references — the prod CSP is ``connect-src 'self'``, so an
      app that references an off-origin http(s) host (esm.sh, a CDN, Google
      Fonts, gstatic) at runtime silently fails. Not install-breaking, so
      these are WARNINGS: the author confirms each hit is vendored same-origin
      or routed through ``/api/proxy`` (the same "grep the build for https://"
      review step the app-building guide already prescribes).

The module is pure and stdlib-only so both the install path
(``app.install``) and the standalone CLI (``scripts/validate-app.py``) can
call it without dragging in FastAPI/httpx. ``check_app_source`` takes the
source tree as an in-memory mapping and never touches the filesystem or the
network; the callers own reading files.
"""

from __future__ import annotations

import os
import re
from collections import deque
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

# Modules whose imports we follow transitively and whose bodies we scan.
_SOURCE_EXTS = frozenset(
  {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
)
# Extension resolution order for a bare relative specifier, mirroring how
# esbuild resolves `./x` -> `./x.jsx` / `./x/index.jsx`. The empty string is
# first so an already-suffixed specifier (`./x.js`) matches exactly.
_RESOLVE_EXTS = ("", ".jsx", ".js", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".css")
_INDEX_BASENAMES = ("index.jsx", "index.js", "index.ts", "index.tsx")
# Files worth scanning for external hosts: code + the markup/style that can
# pull an off-origin asset. README/markdown/plain text are deliberately
# excluded — a URL mentioned in prose is not a runtime reference.
_SCANNED_EXTS = frozenset(
  {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".css", ".html", ".htm"}
)

# Hosts that appear in source as XML/SVG *namespaces*, never fetched over the
# network, so a match on them is always a false positive for the CSP concern.
_NAMESPACE_HOSTS = frozenset({"w3.org", "www.w3.org"})

# Relative-import specifiers. `from '...'` covers both `import ... from` and
# `export ... from` (and tolerates multi-line named-import lists, since the
# match anchors on `from`); the others cover side-effect, dynamic, and
# CommonJS forms. Newlines are excluded from the quoted span so a stray quote
# can't run the match across statements.
_RE_FROM = re.compile(r"""\bfrom\s*['"]([^'"\n]+)['"]""")
_RE_SIDE_EFFECT = re.compile(r"""\bimport\s*['"]([^'"\n]+)['"]""")
_RE_DYNAMIC = re.compile(r"""\bimport\s*\(\s*['"]([^'"\n]+)['"]\s*\)""")
_RE_REQUIRE = re.compile(r"""\brequire\s*\(\s*['"]([^'"\n]+)['"]\s*\)""")
_IMPORT_RES = (_RE_FROM, _RE_SIDE_EFFECT, _RE_DYNAMIC, _RE_REQUIRE)

# Any http(s) URL. Runs on comment-stripped, string-preserving source so it
# fires on real references (URLs live in string literals) and not on URLs
# mentioned in a comment.
_RE_URL = re.compile(r"""https?://([^\s'"`)\]}>]+)""")


@dataclass(frozen=True)
class Finding:
  """One problem found in the source tree.

  ``severity`` is ``"error"`` (install-breaking) or ``"warning"`` (runtime
  quality). ``code`` is a stable machine tag; ``path`` is the source file the
  finding is about; ``detail`` is a one-line, human-facing explanation.
  """

  severity: str
  code: str
  path: str
  detail: str

  def format(self) -> str:
    tag = "ERROR" if self.severity == "error" else "WARN "
    return f"[{tag}] {self.path}: {self.detail}"


@dataclass(frozen=True)
class SourceCheckResult:
  findings: list[Finding] = field(default_factory=list)

  @property
  def errors(self) -> list[Finding]:
    return [f for f in self.findings if f.severity == "error"]

  @property
  def warnings(self) -> list[Finding]:
    return [f for f in self.findings if f.severity == "warning"]

  @property
  def ok(self) -> bool:
    """True when nothing install-breaking was found (warnings are allowed)."""
    return not self.errors


def _norm(path: str) -> str:
  """Normalize a posix-ish relative path: drop `.`/empty segments and
  collapse `..` against the preceding segment. A leading `..` that escapes
  the tree is preserved (it will simply resolve to nothing)."""
  parts: list[str] = []
  for seg in path.replace("\\", "/").split("/"):
    if seg in ("", "."):
      continue
    if seg == "..":
      if parts and parts[-1] != "..":
        parts.pop()
      else:
        parts.append("..")
    else:
      parts.append(seg)
  return "/".join(parts)


def _parent(path: str) -> str:
  return path.rsplit("/", 1)[0] if "/" in path else ""


def _ext(path: str) -> str:
  return os.path.splitext(path)[1].lower()


def _strip_comments(text: str) -> str:
  """Blank out `//` and `/* */` comments while preserving string literals.

  A single string-aware pass, not a regex: a `//` or `/*` inside a quoted
  string (a URL, a path like ``ui/*.jsx`` written in a comment) must not be
  taken for a comment delimiter, and a comment must not be seen inside a
  string. Regex stripping gets this wrong — a `//` line comment that mentions
  ``/*`` opens a phantom block comment that eats the real imports below it.
  String bodies are kept verbatim (that is where URLs live); comment bodies
  are dropped, newlines preserved so line structure survives.
  """
  out: list[str] = []
  i = 0
  n = len(text)
  quote: str | None = None  # active string delimiter, if any
  while i < n:
    ch = text[i]
    if quote is not None:
      out.append(ch)
      if ch == "\\" and i + 1 < n:
        out.append(text[i + 1])
        i += 2
        continue
      if ch == quote:
        quote = None
      i += 1
      continue
    pair = text[i : i + 2]
    if pair == "//":
      while i < n and text[i] != "\n":
        i += 1
      continue
    if pair == "/*":
      i += 2
      while i < n and text[i : i + 2] != "*/":
        if text[i] == "\n":
          out.append("\n")
        i += 1
      i += 2
      continue
    if ch in ("'", '"', "`"):
      quote = ch
    out.append(ch)
    i += 1
  return "".join(out)


def _relative_specifiers(source: str) -> list[str]:
  """Return the relative (`./` or `../`) import specifiers in ``source``."""
  code = _strip_comments(source)
  specs: list[str] = []
  for pattern in _IMPORT_RES:
    for spec in pattern.findall(code):
      if spec.startswith("./") or spec.startswith("../"):
        specs.append(spec)
  return specs


def _resolve(importer: str, spec: str, keys: Collection[str]) -> str | None:
  """Resolve a relative ``spec`` imported from ``importer`` to a key in the
  tree, trying esbuild-style extension and directory-index resolution. Returns
  the resolved key or ``None`` when nothing in the tree matches."""
  base = _norm(_parent(importer) + "/" + spec)
  for ext in _RESOLVE_EXTS:
    candidate = base + ext
    if candidate in keys:
      return candidate
  for name in _INDEX_BASENAMES:
    candidate = _norm(base + "/" + name)
    if candidate in keys:
      return candidate
  return None


def _static_source_paths(value) -> list[str]:
  """Map logical static destinations to their installed source-tree paths.

  The installer serves logical destination ``x.js`` from ``static/x.js`` on
  disk. Shape errors are the manifest validator's job; tolerate and skip them.
  """
  if not value:
    return []
  if isinstance(value, list):
    return [f"static/{p}" for p in value if isinstance(p, str)]
  if isinstance(value, dict):
    return [f"static/{d}" for d in value if isinstance(d, str)]
  return []


def check_app_source(
  files: Mapping[str, str],
  *,
  entry: str,
  source_files: Iterable[str] = (),
  job: str | None = None,
  static_assets: Iterable[str] = (),
) -> SourceCheckResult:
  """Check a mini-app source tree against its manifest declarations.

  Args:
    files: The source tree as ``{relative_posix_path: text_content}``. For an
      install this is exactly what the fetch path would write (entry + declared
      source_files + job + static-asset dests); for the CLI it is the whole
      repo. An import that resolves to a key here is "present"; one that does
      not is missing.
    entry: The entry file's key in ``files`` (``index.jsx`` on the synthetic
      fetch path, whatever the repo calls it on a clone / in the CLI).
    source_files: The manifest's declared ``source_files``.
    job: The manifest's ``schedule.job`` bare filename, if any. It is a root of
      the import graph but is intentionally NOT part of ``source_files``.
    static_assets: Static assets' installed source-tree paths — allowed import
      targets that the installer writes separately from ``source_files``.

  Returns:
    A ``SourceCheckResult``; ``.errors`` are install-breaking completeness
    problems, ``.warnings`` are external-host (CSP) references.
  """
  entry = _norm(entry)
  declared_sources = [_norm(s) for s in source_files]
  job = _norm(job) if job else None
  static = [_norm(s) for s in static_assets]
  # Every path the fetch install materializes and that an import may legally
  # land on: the entry, each declared source file, the job script, and the
  # static-asset dests. An import resolving to anything OUTSIDE this set is the
  # completeness defect — present in a clone, absent from a fetch install.
  allowed = {entry, *declared_sources, *static}
  if job:
    allowed.add(job)

  findings: list[Finding] = []

  # A declared source file that isn't in the tree at all (typo, deleted file).
  for rel in declared_sources:
    if rel not in files:
      findings.append(Finding(
        "error", "declared_source_missing", rel,
        "listed in source_files but not present in the app source tree",
      ))

  if entry not in files:
    findings.append(Finding(
      "error", "entry_missing", entry,
      "manifest `entry` is not present in the app source tree",
    ))

  roots = [entry]
  if job:
    if job in files:
      roots.append(job)
    else:
      findings.append(Finding(
        "error", "job_missing", job,
        "manifest `schedule.job` is not present in the app source tree",
      ))

  # BFS the relative-import graph from the roots, flagging any reachable import
  # that is undeclared (present in the tree but not in `allowed`) or missing
  # (no file for the specifier anywhere in the tree).
  visited: set[str] = set()
  queue: deque[str] = deque(r for r in roots if r in files)
  seen_missing: set[tuple[str, str]] = set()
  seen_undeclared: set[str] = set()
  while queue:
    current = queue.popleft()
    if current in visited:
      continue
    visited.add(current)
    if _ext(current) not in _SOURCE_EXTS:
      continue
    for spec in _relative_specifiers(files[current]):
      target = _resolve(current, spec, files.keys())
      if target is None:
        if (current, spec) not in seen_missing:
          seen_missing.add((current, spec))
          findings.append(Finding(
            "error", "missing_import", current,
            f"imports {spec!r}, which resolves to no file the install would "
            "have — declare it in `source_files` (and make sure the file "
            "exists)",
          ))
        continue
      if target not in allowed and target not in seen_undeclared:
        seen_undeclared.add(target)
        findings.append(Finding(
          "error", "undeclared_source", target,
          f"imported by {current} but not listed in `source_files` — a "
          "non-clone install would fetch an incomplete tree and fail to load",
        ))
      if _ext(target) in _SOURCE_EXTS and target not in visited:
        queue.append(target)

  # Scope the external-host scan to the module graph actually reachable from
  # the entry — the code that ships to the browser under the CSP. Scanning the
  # whole tree would flag build scripts and test fixtures (localhost, x.test),
  # which never run under `connect-src 'self'`.
  findings.extend(_external_host_findings(files, visited))
  # Stable order: errors first, then by path, so CLI output and test
  # assertions are deterministic.
  findings.sort(key=lambda f: (f.severity != "error", f.path, f.code))
  return SourceCheckResult(findings)


def _external_host_findings(
  files: Mapping[str, str], scope: Collection[str]
) -> list[Finding]:
  findings: list[Finding] = []
  for rel in sorted(scope):
    if _ext(rel) not in _SCANNED_EXTS or rel not in files:
      continue
    code = _strip_comments(files[rel])
    seen: set[str] = set()
    for match in _RE_URL.finditer(code):
      host = re.split(r"[/:?#]", match.group(1), maxsplit=1)[0].lower()
      if not host or host in _NAMESPACE_HOSTS or host in seen:
        continue
      seen.add(host)
      findings.append(Finding(
        "warning", "external_host", rel,
        f"references external host {host!r}; the prod CSP is "
        "`connect-src 'self'`, so a direct load fails silently — vendor it "
        "same-origin or route it through `/api/proxy`",
      ))
  return findings


def check_manifest_tree(
  manifest: Mapping, files: Mapping[str, str]
) -> SourceCheckResult:
  """Convenience wrapper: derive the check inputs from a raw manifest dict.

  Used by the CLI and any caller holding a parsed ``mobius.json``. The install
  path calls ``check_app_source`` directly because it already has the fetched
  tree and can report findings before materialization.
  """
  sched = manifest.get("schedule")
  job = sched.get("job") if isinstance(sched, Mapping) else None
  return check_app_source(
    files,
    entry=manifest.get("entry") or "index.jsx",
    source_files=manifest.get("source_files") or [],
    job=job,
    static_assets=_static_source_paths(manifest.get("static_assets")),
  )
