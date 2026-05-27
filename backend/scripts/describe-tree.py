#!/usr/bin/env python3
"""Walk a directory and print each file's filename + first description.

Why this exists
---------------
Hand-curated directory tables in the agent's skill/seed (the list
that says "ChatView.jsx — chat messages, streaming, scroll") drift
out of date the moment a file is renamed or removed. We've hit this
twice already — once with a stale `ChatInput.jsx` reference that
the agent kept trying to edit, and once with the storage envelope
docs that silently misled 18 mini-apps into corrupting data.

The fix: have files self-describe via their first docstring/comment
and have the agent QUERY this script at runtime instead of trusting
a snapshot. Stale info becomes impossible because the data IS the
filesystem state.

What it does
------------
For each file in the target directory (recursive, up to a depth
cap), extract the first comment/docstring block and print one
line: `<relative path> — <first sentence of doc>`.

Supported file types (extension-based dispatch):
  .py            triple-quoted module docstring
  .jsx .js .ts .tsx
                 leading // line block OR leading /* */ block
  .sh .bash      leading #-prefixed line block (after shebang)
  .css           leading /* */ block
  .md            first heading + first paragraph (skip frontmatter)

Files without a leading description show `(no description)`.

Usage
-----
  describe-tree.py <path>            # tree of <path>
  describe-tree.py <path> --depth 3  # cap recursion depth
  describe-tree.py <path> --quiet    # skip "(no description)" rows

Output is plain text. The agent reads stdout directly.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# File extensions we know how to extract a description from. Anything
# else gets skipped (no point dumping ".png — ..." lines).
_KNOWN_EXTS = {
  ".py", ".jsx", ".js", ".ts", ".tsx",
  ".sh", ".bash", ".css", ".md",
}

# Patterns that mark "this is a generated/vendor/junk dir; don't recurse."
_SKIP_DIRS = {
  "node_modules", ".git", "__pycache__", ".pytest_cache",
  "dist", "build", ".venv", "venv", ".mypy_cache",
}

# Cap how much of the file we read for description extraction. Files
# rarely have meaningful docs past the first ~2 KB; reading more
# wastes IO + memory on huge bundled JS files.
_HEAD_BYTES = 4096


def _first_sentence(text: str) -> str:
  """Trim a description to the first sentence or line, whichever ends
  first. Strips leading/trailing whitespace and collapses internal
  whitespace to single spaces so the output table stays one-line-
  per-file regardless of how the source formats its docstring.
  """
  if not text:
    return ""
  # Stop at the first period followed by space/end-of-string OR the
  # first newline-newline (paragraph break). Whichever comes first.
  text = text.strip()
  para_break = text.find("\n\n")
  if para_break != -1:
    text = text[:para_break]
  # Collapse internal whitespace.
  text = re.sub(r"\s+", " ", text).strip()
  # First sentence — period followed by whitespace or end.
  m = re.search(r"\.(?:\s|$)", text)
  if m:
    text = text[: m.start() + 1]
  return text


def _extract_python(head: str) -> str:
  """Module docstring: first triple-quoted string at the top.

  Skips a shebang line and any leading comments. Returns empty if
  the file doesn't start with a docstring (which is the convention
  this script encourages — files SHOULD start with one).

  Anchors the closing delimiter to the SAME triple-quote type the
  docstring opened with: a `\"\"\"`-opened docstring containing a
  `'''` substring (e.g. SQL examples) must not be cut at the
  embedded `'''`. Two-branch alternation rather than one mixed
  regex achieves this without needing a regex backreference.
  """
  # Skip shebang + leading comments + blank lines.
  lines = head.split("\n")
  i = 0
  while i < len(lines) and (
    lines[i].startswith("#") or not lines[i].strip()
  ):
    i += 1
  remainder = "\n".join(lines[i:]).lstrip()
  # Try each delimiter type explicitly; pick whichever opens first.
  for opener, closer in (('"""', '"""'), ("'''", "'''")):
    if remainder.startswith(opener):
      end = remainder.find(closer, len(opener))
      if end == -1:
        return ""
      return _first_sentence(remainder[len(opener):end])
  return ""


def _extract_js_ts(head: str) -> str:
  """JS/TS leading comment block.

  Supports both /* ... */ and a run of // lines. The agent's
  convention is to use a /* ... */ at the top of mini-app and
  shell files; the // form is here for tolerance with existing
  code that uses it.
  """
  head = head.lstrip()
  if head.startswith("/*"):
    m = re.match(r"/\*+\s*(.*?)\*/", head, re.DOTALL)
    if m:
      # Strip leading * from each line that the multi-line comment
      # might use ("* description ...").
      body = re.sub(r"^[ \t]*\*[ \t]?", "", m.group(1), flags=re.MULTILINE)
      return _first_sentence(body)
    return ""
  if head.startswith("//"):
    lines = []
    for line in head.split("\n"):
      stripped = line.lstrip()
      if not stripped.startswith("//"):
        break
      lines.append(stripped[2:].strip())
    return _first_sentence(" ".join(lines))
  return ""


def _extract_shell(head: str) -> str:
  """Shell script header: lines starting with `#` (after the shebang)."""
  lines = head.split("\n")
  # Skip shebang.
  if lines and lines[0].startswith("#!"):
    lines = lines[1:]
  collected = []
  for line in lines:
    stripped = line.lstrip()
    if not stripped:
      if collected:
        break
      continue
    if not stripped.startswith("#"):
      break
    collected.append(stripped.lstrip("#").strip())
  return _first_sentence(" ".join(collected))


def _extract_css(head: str) -> str:
  """CSS files: first /* ... */ block."""
  m = re.search(r"/\*+\s*(.*?)\*/", head, re.DOTALL)
  if not m:
    return ""
  return _first_sentence(m.group(1))


def _extract_md(head: str) -> str:
  """Markdown: first heading line, then first paragraph if needed.

  Skips a YAML frontmatter block (`---\n...\n---`) at the top, which
  many of our .pm/ tickets use.

  The frontmatter end marker is `\\n---\\n` (newline-dash-dash-dash-
  newline) anchored as a WHOLE line, not just any substring `---`.
  Without that anchor, a frontmatter containing a YAML block scalar
  with `---` on its own line (e.g. `description: |\\n  ---\\n`)
  would be cut short and the script would extract YAML mid-block
  as the description. Claude reviewer caught this edge case.
  """
  text = head.lstrip()
  if text.startswith("---\n") or text.startswith("---\r\n"):
    # Search for the closing `---` AS A WHOLE LINE. Anchor with both
    # leading and trailing newline so a `---` inside a YAML block
    # scalar (indented or with trailing content) doesn't match.
    for marker in ("\n---\n", "\n---\r\n"):
      end = text.find(marker, 4)
      if end != -1:
        text = text[end + len(marker):].lstrip()
        break
    else:
      # File starts with `---\n` but has no proper closing line.
      # Skip past the opening marker line so we extract a real
      # content line below instead of returning `---` itself.
      # Codex review caught this.
      text = text.split("\n", 1)[1] if "\n" in text else ""
  # Prefer the first heading.
  for line in text.split("\n"):
    line = line.strip()
    if line.startswith("#"):
      return _first_sentence(line.lstrip("#").strip())
    if line:
      return _first_sentence(line)
  return ""


_EXTRACTORS = {
  ".py": _extract_python,
  ".jsx": _extract_js_ts,
  ".js": _extract_js_ts,
  ".ts": _extract_js_ts,
  ".tsx": _extract_js_ts,
  ".sh": _extract_shell,
  ".bash": _extract_shell,
  ".css": _extract_css,
  ".md": _extract_md,
}


def describe_file(path: Path) -> str:
  """Returns the first-sentence description for `path`, or '' if none."""
  ext = path.suffix.lower()
  extractor = _EXTRACTORS.get(ext)
  if not extractor:
    return ""
  try:
    with path.open("rb") as f:
      raw = f.read(_HEAD_BYTES)
    head = raw.decode("utf-8", errors="replace")
  except OSError:
    return ""
  return extractor(head)


def walk(
  root: Path, depth: int, quiet: bool,
) -> list[tuple[str, str]]:
  """Walk `root` up to `depth` levels deep; return [(relpath, desc)]."""
  results: list[tuple[str, str]] = []
  root = root.resolve()
  if not root.exists():
    print(f"describe-tree: {root}: not found", file=sys.stderr)
    return results
  if root.is_file():
    # Single-file mode.
    desc = describe_file(root)
    if desc or not quiet:
      results.append((root.name, desc or "(no description)"))
    return results
  for current, dirs, files in os.walk(root):
    # Prune skip dirs in-place so os.walk doesn't descend into them.
    dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
    # Depth check.
    rel_current = Path(current).resolve().relative_to(root)
    if depth >= 0 and len(rel_current.parts) > depth:
      dirs[:] = []
      continue
    for fname in sorted(files):
      ext = Path(fname).suffix.lower()
      if ext not in _KNOWN_EXTS:
        continue
      fpath = Path(current) / fname
      relpath = fpath.relative_to(root).as_posix()
      desc = describe_file(fpath)
      if desc:
        results.append((relpath, desc))
      elif not quiet:
        results.append((relpath, "(no description)"))
  return results


def main() -> int:
  parser = argparse.ArgumentParser(
    description="Walk a dir and print first-sentence file descriptions."
  )
  parser.add_argument("path", help="Directory (or single file) to describe.")
  parser.add_argument(
    "--depth", type=int, default=4,
    help="Max recursion depth. Default 4. -1 = unlimited.",
  )
  parser.add_argument(
    "--quiet", action="store_true",
    help="Skip rows for files without a description.",
  )
  args = parser.parse_args()

  rows = walk(Path(args.path), args.depth, args.quiet)
  if not rows:
    return 0
  # Width-align the paths for readability. Cap at 60 cols to avoid
  # blowing up on deeply-nested paths.
  width = min(60, max(len(p) for p, _ in rows))
  for path, desc in rows:
    print(f"{path:<{width}}  {desc}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
