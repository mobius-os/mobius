"""Builds + lints the knowledge-graph index (`graph.json`) for the viewer.

This is the derived view over `<data_dir>/shared/memory/` — distinct from
`memory.py`, which assembles the *injected* block. The viewer mini-app reads
the `graph.json` this produces; the nightly "dreaming" pass and the agent
regenerate it after editing notes. It is also the lint authority: a publish
step (bootstrap or nightly consolidation) calls `build_graph`, and refuses to
mark the graph `.ready` if `problems` contains an error (Codex review R7).

Node = one note or MOC. Edge = a `mocs:` membership or a body `[[link]]`.
The graph is "healthy" when, from `index`, every MOC is reachable and from
every MOC every note is reachable, with no dangling links and no orphans.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from app.memory import memory_dir, parse_frontmatter

# `[[slug]]` or `[[slug|display text]]` — capture the slug (left of any pipe).
_WIKILINK = re.compile(r"\[\[\s*([^\]|#]+?)\s*(?:[|#][^\]]*)?\]\]")

ROOT_ID = "index"


@dataclass
class GraphResult:
  nodes: list[dict] = field(default_factory=list)
  edges: list[dict] = field(default_factory=list)
  problems: list[dict] = field(default_factory=list)  # {severity, kind, detail}

  @property
  def errors(self) -> list[dict]:
    return [p for p in self.problems if p["severity"] == "error"]

  def to_json(self) -> dict:
    return {
      "version": 1,
      "nodes": self.nodes,
      "edges": self.edges,
      "problems": self.problems,
    }


def _slug(path: Path) -> str:
  return path.stem


def _wikilinks(body: str) -> list[str]:
  return [m.group(1).strip() for m in _WIKILINK.finditer(body)]


def _read(path: Path) -> str:
  try:
    return path.read_text(encoding="utf-8")
  except OSError:
    return ""


def _root(data_dir: str | Path | None, root: str | Path | None) -> Path:
  """A caller passes EITHER a data_dir (resolves to data_dir/shared/memory) OR
  a direct memory `root` (used by the bootstrap to lint a staging tree before
  it lives at the canonical path)."""
  if root is not None:
    return Path(root)
  if data_dir is None:
    raise ValueError("build_graph needs data_dir or root")
  return memory_dir(data_dir)


def build_graph(
  data_dir: str | Path | None = None, *, root: str | Path | None = None
) -> GraphResult:
  """Walks `notes/` + `mocs/` + `index.md`, parses frontmatter + `[[links]]`,
  and returns nodes, edges, and lint problems. Pure: never writes."""
  root = _root(data_dir, root)
  res = GraphResult()
  nodes_by_id: dict[str, dict] = {}

  def add_node(path: Path, ntype: str) -> None:
    sid = _slug(path)
    text = _read(path)
    fm = parse_frontmatter(text)
    if sid in nodes_by_id:
      res.problems.append(
        {"severity": "error", "kind": "duplicate_id",
         "detail": f"{sid} ({path.name})"}
      )
      return
    nodes_by_id[sid] = {
      "id": sid,
      "title": str(fm.get("title") or sid),
      "type": ntype,
      "path": str(path.relative_to(root)),
      "size_bytes": len(text.encode("utf-8")),
      "importance": fm.get("importance") if isinstance(fm.get("importance"), int) else 1,
      "access_count": fm.get("access_count") if isinstance(fm.get("access_count"), int) else 0,
      "mocs": fm.get("mocs") if isinstance(fm.get("mocs"), list) else [],
      "tags": fm.get("tags") if isinstance(fm.get("tags"), list) else [],
      "_links": _wikilinks(text),
    }

  index_path = root / "index.md"
  if index_path.is_file():
    add_node(index_path, "moc")
    # The index node's id is its slug ("index"); normalize to ROOT_ID.
    if "index" in nodes_by_id:
      nodes_by_id[ROOT_ID] = nodes_by_id.pop("index")
      nodes_by_id[ROOT_ID]["id"] = ROOT_ID
  else:
    res.problems.append(
      {"severity": "error", "kind": "missing_index", "detail": "index.md"}
    )

  for sub, ntype in (("mocs", "moc"), ("notes", "note")):
    d = root / sub
    if not d.is_dir():
      continue
    for fp in sorted(d.glob("*.md")):
      add_node(fp, ntype)

  # Edges: membership (note.mocs -> moc) + lateral/body links. A note's
  # `mocs:` backlink and the MOC body's `[[link]]` describe the same
  # relationship, so dedupe on the unordered pair (membership wins its kind)
  # to keep the viewer graph clean while still validating every raw link.
  ids = set(nodes_by_id)
  seen_pairs: set[frozenset[str]] = set()

  def add_edge(source: str, target: str, kind: str) -> None:
    pair = frozenset((source, target))
    if pair in seen_pairs:
      return
    seen_pairs.add(pair)
    res.edges.append({"source": source, "target": target, "kind": kind})

  # Pass 1: membership edges first, so a note's declared MOC wins the pair's
  # `kind` regardless of file iteration order.
  for sid, node in nodes_by_id.items():
    for moc in node.get("mocs", []):
      target = moc.strip()
      if target in ids:
        add_edge(sid, target, "moc")
      else:
        res.problems.append(
          {"severity": "error", "kind": "dangling_moc",
           "detail": f"{sid} -> {target}"}
        )
  # Pass 2: body `[[links]]` (validated for every link; deduped for the graph).
  for sid, node in nodes_by_id.items():
    for link in node.pop("_links"):
      if link in ids:
        add_edge(sid, link, "link")
      elif link != sid:
        res.problems.append(
          {"severity": "warn", "kind": "dangling_link",
           "detail": f"{sid} -> {link}"}
        )

  # Orphan = a note reachable from nothing and pointing nowhere.
  linked = {e["source"] for e in res.edges} | {e["target"] for e in res.edges}
  for sid, node in nodes_by_id.items():
    if node["type"] == "note" and sid not in linked:
      res.problems.append(
        {"severity": "warn", "kind": "orphan", "detail": sid}
      )

  # Reachability from the root index (undirected — a note is "reachable" if it
  # shares any edge with the connected component containing index).
  adj: dict[str, set[str]] = {sid: set() for sid in nodes_by_id}
  for e in res.edges:
    adj[e["source"]].add(e["target"])
    adj[e["target"]].add(e["source"])
  seen: set[str] = set()
  if ROOT_ID in nodes_by_id:
    q = deque([ROOT_ID])
    seen.add(ROOT_ID)
    while q:
      cur = q.popleft()
      for nxt in adj.get(cur, ()):
        if nxt not in seen:
          seen.add(nxt)
          q.append(nxt)
  for sid in nodes_by_id:
    if sid not in seen:
      res.problems.append(
        {"severity": "warn", "kind": "unreachable", "detail": sid}
      )

  res.nodes = list(nodes_by_id.values())
  return res


def write_graph(
  data_dir: str | Path | None = None, *, root: str | Path | None = None
) -> GraphResult:
  """Builds the graph and, if it has no ERROR-severity problems, writes
  `graph.json`. On error, leaves the previous `graph.json` in place
  (last-known-good) and returns the result so the caller can abort a publish.
  """
  base = _root(data_dir, root)
  res = build_graph(root=base)
  if res.errors:
    return res
  out = base / "graph.json"
  tmp = out.with_suffix(".json.tmp")
  tmp.write_text(json.dumps(res.to_json(), ensure_ascii=False, indent=2),
                 encoding="utf-8")
  tmp.replace(out)  # atomic
  return res
