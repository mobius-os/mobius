"""Offline memory systems under test."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class RetrievalResult:
  context: str
  selected_node_ids: list[str]


class MemorySystem(Protocol):
  def retrieve(self, query: str) -> RetrievalResult: ...


class NoMemorySystem:
  def retrieve(self, query: str) -> RetrievalResult:
    return RetrievalResult(context="", selected_node_ids=[])

  def for_tree(self, tree_dir: str | Path) -> "NoMemorySystem":
    return type(self)()


class FixedBundleBaselineSystem:
  """Query-independent baseline that injects every markdown file in the tree."""

  def __init__(self, tree_dir: str | Path | None = None):
    self._tree_dir = Path(tree_dir) if tree_dir is not None else None

  def for_tree(self, tree_dir: str | Path) -> "FixedBundleBaselineSystem":
    return type(self)(tree_dir)

  def retrieve(self, query: str) -> RetrievalResult:
    tree = _require_tree(self._tree_dir)
    files = _markdown_files(tree)
    return RetrievalResult(
      context=_join_files(files, tree),
      selected_node_ids=[_node_id(tree, path) for path in files],
    )


class FlatInboxSystem:
  """Reads every note in a flat ``notes/`` directory."""

  def __init__(self, tree_dir: str | Path | None = None):
    self._tree_dir = Path(tree_dir) if tree_dir is not None else None

  def for_tree(self, tree_dir: str | Path) -> "FlatInboxSystem":
    return type(self)(tree_dir)

  def retrieve(self, query: str) -> RetrievalResult:
    tree = _require_tree(self._tree_dir)
    notes_dir = tree / "notes"
    if not notes_dir.is_dir():
      return RetrievalResult(context="", selected_node_ids=[])
    files = sorted(path for path in notes_dir.glob("*.md") if path.is_file())
    return RetrievalResult(
      context=_join_files(files, tree),
      selected_node_ids=[_node_id(tree, path) for path in files],
    )


class V2RouterOneHopSystem:
  """Self-contained router traversal for memory-v2 fixtures.

  It reads root scent lines, follows matching router targets, descends one level
  through a matching directory ``index.md``, then opens direct see-also targets
  from selected notes. There is no ranking and no production import.
  """

  def __init__(self, tree_dir: str | Path | None = None):
    self._tree_dir = Path(tree_dir) if tree_dir is not None else None

  def for_tree(self, tree_dir: str | Path) -> "V2RouterOneHopSystem":
    return type(self)(tree_dir)

  def retrieve(self, query: str) -> RetrievalResult:
    tree = _require_tree(self._tree_dir)
    root_index = tree / "index.md"
    if not root_index.is_file():
      return RetrievalResult(context="", selected_node_ids=[])

    selected: list[Path] = []
    for line in _router_lines(root_index):
      if not _scent_matches(query, line):
        continue
      for target in _line_targets(root_index, tree, line):
        if target.name == "index.md":
          selected.extend(_matching_targets_from_index(target, tree, query))
        elif target.is_file():
          selected.append(target)

    selected = _dedupe_paths(selected)
    with_see_also = _dedupe_paths([
      *selected,
      *[
        target
        for source in selected
        for target in _see_also_targets(source, tree)
        if target.is_file()
      ],
    ])
    return RetrievalResult(
      context=_join_files(with_see_also, tree),
      selected_node_ids=[_node_id(tree, path) for path in with_see_also],
    )


StaticInjectionSystem = FixedBundleBaselineSystem

_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_STOPWORDS = {
  "about",
  "and",
  "are",
  "for",
  "from",
  "not",
  "or",
  "the",
  "this",
  "that",
  "what",
  "when",
  "where",
  "with",
  "you",
  "your",
}


def _matching_targets_from_index(index_path: Path, tree: Path, query: str) -> list[Path]:
  selected: list[Path] = []
  for line in _router_lines(index_path):
    if not _scent_matches(query, line):
      continue
    selected.extend(
      target for target in _line_targets(index_path, tree, line) if target.is_file()
    )
  return selected


def _router_lines(index_path: Path) -> list[str]:
  return [
    line.strip()
    for line in index_path.read_text(encoding="utf-8").splitlines()
    if line.lstrip().startswith(("-", "*")) and (_MARKDOWN_LINK_RE.search(line) or _WIKILINK_RE.search(line))
  ]


def _scent_matches(query: str, scent_line: str) -> bool:
  query_text = _normalize_text(query)
  scent_text = _normalize_text(scent_line)
  if not query_text or not scent_text:
    return False
  if query_text in scent_text or scent_text in query_text:
    return True
  query_terms = _terms(query_text)
  scent_terms = _terms(scent_text)
  return bool(query_terms & scent_terms)


def _terms(text: str) -> set[str]:
  return {term for term in text.split() if len(term) >= 3 and term not in _STOPWORDS}


def _normalize_text(text: str) -> str:
  return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())


def _line_targets(source: Path, tree: Path, line: str) -> list[Path]:
  return [
    target
    for target in (_resolve_link(source, tree, raw) for raw in _extract_link_targets(line))
    if target is not None
  ]


def _see_also_targets(source: Path, tree: Path) -> list[Path]:
  targets: list[Path] = []
  for line in source.read_text(encoding="utf-8").splitlines():
    if "see also" not in line.casefold() and "see-also" not in line.casefold():
      continue
    targets.extend(_line_targets(source, tree, line))
  return targets


def _extract_link_targets(text: str) -> list[str]:
  targets = [match.group(1) for match in _MARKDOWN_LINK_RE.finditer(text)]
  targets.extend(match.group(1) for match in _WIKILINK_RE.finditer(text))
  return targets


def _resolve_link(source: Path, tree: Path, raw_target: str) -> Path | None:
  target = raw_target.split("#", 1)[0].strip()
  if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
    return None
  candidate = source.parent / target
  if candidate.suffix == "":
    candidate = candidate.with_suffix(".md")
  if candidate.is_dir():
    candidate = candidate / "index.md"
  try:
    resolved = candidate.resolve()
    resolved.relative_to(tree.resolve())
  except ValueError:
    return None
  return resolved


def _markdown_files(tree: Path) -> list[Path]:
  return sorted(path for path in tree.rglob("*.md") if path.is_file())


def _join_files(files: list[Path], tree: Path) -> str:
  chunks = []
  for path in files:
    chunks.append(f"<!-- {_node_id(tree, path)} -->\n{path.read_text(encoding='utf-8').strip()}")
  return "\n\n".join(chunks)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
  seen: set[Path] = set()
  out: list[Path] = []
  for path in paths:
    resolved = path.resolve()
    if resolved in seen:
      continue
    seen.add(resolved)
    out.append(path)
  return out


def _node_id(tree: Path, path: Path) -> str:
  return path.relative_to(tree).as_posix()


def _require_tree(tree_dir: Path | None) -> Path:
  if tree_dir is None:
    raise ValueError("tree_dir is required for this memory system")
  return tree_dir
