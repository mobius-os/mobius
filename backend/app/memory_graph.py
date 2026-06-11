"""Builds + lints the knowledge-graph index (`graph.json`) for the viewer.

This is the derived view over `<data_dir>/shared/memory/` — distinct from
`memory.py`, which assembles the *injected* block. The viewer mini-app reads
the `graph.json` this produces; the nightly "dreaming" pass and the agent
regenerate it after editing notes. It is also the lint authority: a publish
step (bootstrap or nightly consolidation) calls `build_graph`, and refuses to
mark the graph `.ready` if `problems` contains an error (Codex review R7).

Node = one note, MOC, or redirect stub (`type: redirect` + `target:`
frontmatter — the forwarding pointer a move/rename leaves behind so old
`[[links]]` keep resolving). Edge = a `mocs:` membership, a body `[[link]]`,
or a redirect's forward to its target. The graph is "healthy" when, from
`index`, every MOC is reachable and from every MOC every note is reachable,
with no dangling links and no orphans.

Beyond the publish-blocking errors, the lint emits WARNINGS that encode the
graph's structure rules (mind.md owns the prose; the thresholds live here):
a MOC over 15 children, a bare `[[slug]]` MOC entry with no one-line
description, a note body over ~30 prose lines (split candidate), a note
with 5+ outbound links (MOC-promotion candidate), redirect chains and
orphaned stubs, and malformed `as-of:`/`supersedes:`/`source:` fields.
Warnings never block `.ready` — they are the nightly Dreaming pass's
reorganization worklist.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from app.memory import _read_usage_file, memory_dir, parse_frontmatter

# `[[slug]]` or `[[slug|display text]]` — capture the slug (left of any pipe).
_WIKILINK = re.compile(r"\[\[\s*([^\]|#]+?)\s*(?:[|#][^\]]*)?\]\]")
# A MOC entry bullet that is ONLY a link — no one-line description after it.
_BARE_ENTRY = re.compile(r"^\s*[-*]\s*\[\[[^\]]+\]\]\s*$")
# Any link-list bullet (described or bare) — excluded from prose-line counts,
# the way WP:SIZERULE measures readable prose rather than navigation lists.
_LINK_BULLET = re.compile(r"^\s*[-*]\s*\[\[")
_AS_OF_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ROOT_ID = "index"

# Structure-rule thresholds (the prose rules live in mind.md; Dreaming acts
# on the warnings these produce).
MOC_CHILDREN_CAP = 15
NOTE_PROSE_LINE_CAP = 30
MOC_PROMOTION_LINKS = 5


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


def _split_body(text: str) -> str:
  """Returns the text after the YAML frontmatter (or all of it)."""
  if not text.startswith("---"):
    return text
  end = text.find("\n---", 3)
  if end == -1:
    return text
  nl = text.find("\n", end + 1)
  return text[nl + 1:] if nl != -1 else ""


def _prose_line_count(body: str) -> int:
  """Counts the body lines that are actual prose.

  Blank lines, headings, and link-list bullets don't count — the split
  trigger measures readable content, not navigation structure (a MOC-ish
  note full of described links is handled by the promotion warning, not
  the size one)."""
  count = 0
  for line in body.splitlines():
    s = line.strip()
    if not s or s.startswith("#") or _LINK_BULLET.match(line):
      continue
    count += 1
  return count


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
    # A `type: redirect` stub overrides the directory-derived type: the
    # stub lives wherever the moved file lived, but it is neither a note
    # nor a MOC — just a forwarding pointer.
    if fm.get("type") == "redirect":
      ntype = "redirect"
    node = {
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
      "_body": _split_body(text),
    }
    if ntype == "redirect":
      target = fm.get("target")
      node["target"] = target.strip() if isinstance(target, str) else ""
    # Staleness/supersession metadata: tolerated on any node, validated in
    # the shape-lint pass below, surfaced in graph.json for the viewer and
    # the Dreaming staleness sweep.
    for fm_key, node_key in (
      ("as-of", "as_of"), ("supersedes", "supersedes"), ("source", "source"),
    ):
      if fm_key in fm:
        node[node_key] = fm[fm_key]
    nodes_by_id[sid] = node

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

  # Per-node shape lint (warnings only — never blocks a publish). These
  # encode the structure rules the Dreaming pass enforces: every MOC entry
  # carries a one-line description, a note over ~30 prose lines is a split
  # candidate, a note with 5+ outbound links is doing a MOC's job, and
  # time-sensitive metadata must be well-formed enough to act on.
  for sid, node in nodes_by_id.items():
    body = node.pop("_body")
    if node["type"] == "moc":
      bare = [ln.strip() for ln in body.splitlines() if _BARE_ENTRY.match(ln)]
      if bare:
        res.problems.append(
          {"severity": "warn", "kind": "bare_moc_entry",
           "detail": f"{sid}: {len(bare)} entry line(s) without a one-line "
                     f"description (first: {bare[0]})"}
        )
    elif node["type"] == "note":
      prose = _prose_line_count(body)
      if prose > NOTE_PROSE_LINE_CAP:
        res.problems.append(
          {"severity": "warn", "kind": "oversized_note",
           "detail": f"{sid}: {prose} prose lines "
                     f"(> {NOTE_PROSE_LINE_CAP} — split candidate)"}
        )
      outbound = {l for l in node["_links"] if l != sid}
      if len(outbound) >= MOC_PROMOTION_LINKS:
        res.problems.append(
          {"severity": "warn", "kind": "moc_candidate",
           "detail": f"{sid}: {len(outbound)} outbound links "
                     f"(promote to a MOC?)"}
        )
    as_of = node.get("as_of")
    if as_of is not None and not (
      isinstance(as_of, str) and _AS_OF_DATE.match(as_of)
    ):
      res.problems.append(
        {"severity": "warn", "kind": "bad_as_of",
         "detail": f"{sid}: as-of {as_of!r} is not YYYY-MM-DD"}
      )
    sup = node.get("supersedes")
    if sup is not None:
      if isinstance(sup, str):
        node["supersedes"] = [sup]
      elif not (
        isinstance(sup, list) and all(isinstance(x, str) for x in sup)
      ):
        res.problems.append(
          {"severity": "warn", "kind": "bad_supersedes",
           "detail": f"{sid}: supersedes must be a slug or list of slugs"}
        )
      # No referential check on purpose: a superseded note is often pruned
      # later, and a pointer into git history is not a lint problem.
    src = node.get("source")
    if src is not None and not isinstance(src, (str, list)):
      res.problems.append(
        {"severity": "warn", "kind": "bad_source",
         "detail": f"{sid}: source must be a list of chat ids"}
      )

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
  # Redirect forwarding edges, BEFORE body links so the pair's kind reads
  # "redirect" (a stub's body conventionally also [[links]] its target).
  # Traversal — and the reachability lint — flows through the stub, so
  # moved content stays reachable from every note still linking the old
  # slug.
  for sid, node in nodes_by_id.items():
    if node["type"] == "redirect" and node.get("target") in ids:
      add_edge(sid, node["target"], "redirect")

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

  # Redirect resolution. Every stub must terminate at a real, non-redirect
  # node: a missing target or a cycle never resolves (error — any link
  # routed through the stub dead-ends). A chain that does resolve but takes
  # more than one hop is legal yet flagged for Dreaming to collapse
  # (A -> B -> C becomes A -> C, Wikipedia's double-redirect bot rule).
  redirects = {
    sid: node.get("target") or ""
    for sid, node in nodes_by_id.items()
    if node["type"] == "redirect"
  }
  for sid, first in redirects.items():
    hops = 0
    chain = {sid}
    cur = first
    while True:
      if not cur or cur not in ids:
        res.problems.append(
          {"severity": "error", "kind": "dangling_redirect",
           "detail": f"{sid} -> {cur or '(no target)'}"}
        )
        break
      hops += 1
      if cur not in redirects:
        if hops > 1:
          res.problems.append(
            {"severity": "warn", "kind": "redirect_chain",
             "detail": f"{sid} resolves in {hops} hops (collapse to {cur})"}
          )
        break
      if cur in chain:
        res.problems.append(
          {"severity": "error", "kind": "dangling_redirect",
           "detail": f"{sid} -> redirect cycle"}
        )
        break
      chain.add(cur)
      cur = redirects[cur]

  # A stub nothing points at anymore has served its purpose: every inbound
  # link to the old slug has been updated, so Dreaming can purge it.
  inbound = {e["target"] for e in res.edges}
  for sid in redirects:
    if sid not in inbound:
      res.problems.append(
        {"severity": "warn", "kind": "orphan_redirect", "detail": sid}
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

  # Breadth metadata + live usage. children_count lets the agent (and the
  # Mind app) see a map's breadth at a glance — "this MOC has N members, fan
  # out and read them" — without parsing the body. A MOC's children are the
  # notes that declare it (membership edges); the root index's children are
  # the MOCs it links to (body links). access_count merges the live usage
  # counter onto the frontmatter baseline so "Used" reflects real load counts.
  children: dict[str, set[str]] = {sid: set() for sid in nodes_by_id}
  for e in res.edges:
    if e["kind"] == "moc" and e["target"] in children:
      children[e["target"]].add(e["source"])
    elif e["kind"] == "link" and e["source"] in children:
      children[e["source"]].add(e["target"])
  usage = _read_usage_file(root / "usage.json")
  for sid, node in nodes_by_id.items():
    if node["type"] == "moc":
      kids = sorted(children.get(sid, ()))
      node["children"] = kids
      node["children_count"] = len(kids)
      if len(kids) > MOC_CHILDREN_CAP:
        res.problems.append(
          {"severity": "warn", "kind": "moc_overfull",
           "detail": f"{sid}: {len(kids)} children "
                     f"(cap {MOC_CHILDREN_CAP} — split into sub-MOCs)"}
        )
    node["access_count"] = node.get("access_count", 0) + usage.get(sid, 0)

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
