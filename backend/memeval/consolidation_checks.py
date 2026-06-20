"""Offline checks for v2 memory-tree consolidation fixtures.

These checks are intentionally mechanical: pure markdown file reads, no LLM, no
production app imports.
"""
from __future__ import annotations

import re
from collections import Counter, deque
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_TITLE_RE = re.compile(r"^title:\s*(.+?)\s*$", re.MULTILINE)
_TYPE_RE = re.compile(r"^type:\s*(.+?)\s*$", re.MULTILINE)


def orphan_count(tree_dir: str | Path) -> int:
  tree = Path(tree_dir)
  reachable = _reachable_from_indexes(tree)
  count = 0
  for path in _markdown_files(tree):
    if _is_non_note_file(path):
      continue
    if _node_id(tree, path) not in reachable:
      count += 1
  return count


def duplicate_normalized_title_count(tree_dir: str | Path) -> int:
  titles = [_normalize_title(_title_for(path)) for path in _markdown_files(Path(tree_dir))]
  counts = Counter(title for title in titles if title)
  return sum(count - 1 for count in counts.values() if count > 1)


def dangling_wikilink_count(tree_dir: str | Path) -> int:
  tree = Path(tree_dir)
  count = 0
  for path in _markdown_files(tree):
    for target in _linked_paths(path, tree):
      if target is not None and not target.exists():
        count += 1
  return count


def hub_fan_in(tree_dir: str | Path) -> dict[str, int]:
  tree = Path(tree_dir)
  hubs = {
    _node_id(tree, path): path
    for path in _markdown_files(tree)
    if _frontmatter_value(path, "type") == "hub" or "hubs" in path.relative_to(tree).parts
  }
  counts = {node_id: 0 for node_id in hubs}
  target_to_id = {path.resolve(): node_id for node_id, path in hubs.items()}
  for source in _markdown_files(tree):
    if _is_non_note_file(source):
      continue
    for target in _linked_paths(source, tree):
      if target is None:
        continue
      node_id = target_to_id.get(target.resolve())
      if node_id and target.resolve() != source.resolve():
        counts[node_id] += 1
  return counts


def missing_directory_index_count(tree_dir: str | Path) -> int:
  tree = Path(tree_dir)
  return sum(
    1
    for path in tree.rglob("*")
    if path.is_dir() and path != tree and not (path / "index.md").is_file()
  )


def broken_router_target_count(tree_dir: str | Path) -> int:
  tree = Path(tree_dir)
  count = 0
  for index in tree.rglob("index.md"):
    for target in _linked_paths(index, tree):
      if target is not None and not target.exists():
        count += 1
  return count


def bootstrap_retired(tree_dir: str | Path) -> bool:
  tree = Path(tree_dir)
  return not any(path.is_file() for path in tree.glob("bootstrap*.md"))


def _reachable_from_indexes(tree: Path) -> set[str]:
  roots = [path for path in tree.rglob("index.md") if path.is_file()]
  seen_paths = {path.resolve() for path in roots}
  queue = deque(roots)
  reachable: set[str] = set()
  while queue:
    path = queue.popleft()
    for target in _linked_paths(path, tree):
      if target is None or not target.exists() or not _is_in_tree(target, tree):
        continue
      if _is_non_note_file(target):
        continue
      reachable.add(_node_id(tree, target))
      resolved = target.resolve()
      if resolved not in seen_paths:
        seen_paths.add(resolved)
        queue.append(target)
  return reachable


def _markdown_files(tree: Path) -> list[Path]:
  return sorted(path for path in tree.rglob("*.md") if path.is_file())


def _is_non_note_file(path: Path) -> bool:
  return path.name in {"index.md", "README.md", "log.md"}


def _linked_paths(path: Path, tree: Path) -> list[Path | None]:
  text = path.read_text(encoding="utf-8")
  targets: list[Path | None] = []
  for raw in _extract_link_targets(text):
    targets.append(_resolve_link(path, tree, raw))
  return targets


def _extract_link_targets(text: str) -> list[str]:
  targets = [match.group(1) for match in _MARKDOWN_LINK_RE.finditer(text)]
  targets.extend(match.group(1) for match in _WIKILINK_RE.finditer(text))
  return targets


def _resolve_link(source: Path, tree: Path, raw_target: str) -> Path | None:
  target = raw_target.split("#", 1)[0].strip()
  if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
    return None
  candidate = (source.parent / target).resolve()
  if candidate.suffix == "":
    candidate = candidate.with_suffix(".md")
  if candidate.is_dir():
    candidate = candidate / "index.md"
  try:
    candidate.relative_to(tree.resolve())
  except ValueError:
    return candidate
  return candidate


def _title_for(path: Path) -> str:
  frontmatter_title = _frontmatter_value(path, "title")
  if frontmatter_title:
    return frontmatter_title
  for line in path.read_text(encoding="utf-8").splitlines():
    if line.startswith("# "):
      return line[2:].strip()
  return path.stem


def _frontmatter_value(path: Path, key: str) -> str:
  text = path.read_text(encoding="utf-8")
  match = _FRONTMATTER_RE.match(text)
  if not match:
    return ""
  pattern = _TITLE_RE if key == "title" else _TYPE_RE
  value = pattern.search(match.group(1))
  return value.group(1).strip().strip('"\'') if value else ""


def _normalize_title(title: str) -> str:
  return " ".join(re.sub(r"[^a-z0-9]+", " ", title.casefold()).split())


def _node_id(tree: Path, path: Path) -> str:
  return path.relative_to(tree).as_posix()


def _is_in_tree(path: Path, tree: Path) -> bool:
  try:
    path.resolve().relative_to(tree.resolve())
  except ValueError:
    return False
  return True
