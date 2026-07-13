"""Offline memory systems under test."""
from __future__ import annotations

import os
import re
from collections.abc import Callable
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


class ProductionInjectionSystem:
  """Exercises the REAL session-start injection (`app.memory.build_memory_block`).

  Unlike the self-contained baselines above, this drives the actual production
  code path: the router `index.md` (full, budget-capped) plus the ~10
  most-recently-modified `chats/<id>/index.md` per-chat notes — Mobius's
  SHORT-TERM memory. It is query-INDEPENDENT (the injected block is the same
  regardless of the query); beating it is the bar for any query-relevant
  retrieval. The deeper graph (`mocs/`, `notes/`) is NOT injected here — that is
  the LONG-TERM arm, reached on demand by the memory-search subagent.

  `data_dir` is the instance root (the parent of `shared/memory/`), not the
  memory tree itself, matching `build_memory_block`'s contract. `selected_node_ids`
  are the graph node ids (`index`, `chat:<id>`) so they line up with the
  read-trace / `usage.json` keying.
  """

  def __init__(self, data_dir: str | Path | None = None):
    self._data_dir = Path(data_dir) if data_dir is not None else None

  def for_tree(self, data_dir: str | Path) -> "ProductionInjectionSystem":
    return type(self)(data_dir)

  def retrieve(self, query: str) -> RetrievalResult:
    from app.memory import build_memory_block
    data_dir = _require_tree(self._data_dir)
    block = build_memory_block(data_dir)
    ids = [
      "chat:" + rel.split("/")[1]
      for rel in block.loaded
      if rel.startswith("chats/") and rel.endswith("/index.md")
    ]
    return RetrievalResult(context=block.text, selected_node_ids=ids)


class MemorySearchSystem:
  """The LONG-TERM arm: the deeper graph reached on demand by memory-search.

  Where `ProductionInjectionSystem` only sees the recently-touched per-chat notes
  (short-term, injected at session start), this represents the recall arm that
  the real platform fires for facts living in `notes/`/`mocs/` or in chat notes
  that have aged past the `RECENT_CHAT_NOTES` window. The real arm is the
  installed Memory app reader; that app-owned executable is LIVE-GATED here
  exactly like `run_live_eval.py`:

  - Unit tests pass an injectable `search_fn(query) -> str` and drive it with a
    DETERMINISTIC stub — never shelling out, fully offline in `wt-pytest`.
  - Only `MemorySearchSystem.live(...)` (or `live=True`, requiring the
    `MEMEVAL_LIVE=1` env to actually invoke) runs the real subagent.

  `selected_node_ids` is whatever the search result reports as its sources (the
  stub returns them explicitly); the live path parses the subagent's read-trace.
  """

  def __init__(
      self,
      search_fn: Callable[[str], "SearchResult | str"],
      *,
      tree_dir: str | Path | None = None,
  ):
    self._search_fn = search_fn
    self._tree_dir = Path(tree_dir) if tree_dir is not None else None

  def for_tree(self, tree_dir: str | Path) -> "MemorySearchSystem":
    return type(self)(self._search_fn, tree_dir=tree_dir)

  @classmethod
  def live(
      cls,
      *,
      tree_dir: str | Path | None = None,
      timeout: int = 180,
  ) -> "MemorySearchSystem":
    """The REAL memory-search subagent. Refuses to run unless `MEMEVAL_LIVE=1`
    is set in the env (mirrors `run_live_eval.py`'s gate) so a unit-test run can
    never accidentally shell `claude`."""
    def _live_search(query: str) -> "SearchResult":
      if os.environ.get("MEMEVAL_LIVE") != "1":
        raise RuntimeError(
          "MemorySearchSystem.live requires MEMEVAL_LIVE=1 (it shells the real "
          "installed Memory reader). Unit tests must pass a deterministic "
          "search_fn instead."
        )
      return _run_real_memory_search(query, tree_dir=tree_dir, timeout=timeout)

    return cls(_live_search, tree_dir=tree_dir)

  def retrieve(self, query: str) -> RetrievalResult:
    result = self._search_fn(query)
    if isinstance(result, SearchResult):
      return RetrievalResult(
        context=result.context, selected_node_ids=list(result.node_ids)
      )
    # A bare string is treated as the synthesized context with no node ids.
    return RetrievalResult(context=str(result), selected_node_ids=[])


@dataclass
class SearchResult:
  """What a `search_fn` returns: the synthesized memory context plus the node
  ids it sourced from (so the runner can score `node_recall` for the long arm)."""

  context: str
  node_ids: list[str]


def _run_real_memory_search(
    query: str, *, tree_dir: str | Path | None, timeout: int
) -> SearchResult:
  """Shell an installed Memory app's confined reader. LIVE only."""
  import subprocess

  script = Path(os.environ.get(
    "MEMORY_SEARCH_SCRIPT", "/data/apps/memory/memory_search.py",
  ))
  if not script.is_file():
    raise RuntimeError(
      "Memory app reader is not installed; set MEMORY_SEARCH_SCRIPT for evals."
    )
  data_dir = Path(tree_dir).parent.parent if tree_dir is not None else Path("/data")
  env = dict(os.environ, DATA_DIR=str(data_dir))
  proc = subprocess.run(
    ["python3", str(script), query[:600]],
    capture_output=True,
    text=True,
    timeout=timeout,
    env=env,
  )
  lines = proc.stdout.strip().splitlines()
  context = "\n".join(line for line in lines if not line.startswith("FILES: "))
  node_ids: list[str] = []
  for line in lines:
    if line.startswith("FILES: "):
      node_ids = [
        Path(rel.strip()).stem
        for rel in line.removeprefix("FILES: ").split(",") if rel.strip()
      ]
  return SearchResult(context=context, node_ids=node_ids)


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
