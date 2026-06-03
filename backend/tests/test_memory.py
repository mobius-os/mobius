"""Knowledge-graph memory: injection assembly (memory.py) + indexer lint
(memory_graph.py). Fully isolated via tmp_path — no global DATA_DIR."""

from pathlib import Path

from app import memory, memory_graph


def _note(importance=1, access=0, title="T", mocs=None, links="", body="body"):
  mocs = mocs if mocs is not None else ["m"]
  fm = (
    f"---\ntitle: {title}\ntype: note\nimportance: {importance}\n"
    f"access_count: {access}\nmocs: [{', '.join(mocs)}]\n---\n"
  )
  return fm + body + ("\n" + links if links else "")


def _graph(tmp: Path, *, ready=True):
  root = tmp / "shared" / "memory"
  (root / "notes").mkdir(parents=True, exist_ok=True)
  (root / "mocs").mkdir(parents=True, exist_ok=True)
  return root


# --- legacy fallback ---------------------------------------------------


def test_legacy_fallback_when_no_ready_sentinel(tmp_path):
  shared = tmp_path / "shared"
  shared.mkdir(parents=True)
  (shared / "agent-experience.md").write_text("legacy memory here")
  # A memory dir without .ready must NOT activate graph mode.
  (shared / "memory" / "notes").mkdir(parents=True)
  block = memory.build_memory_block(tmp_path)
  assert block.mode == "legacy"
  assert "legacy memory here" in block.text
  assert block.loaded == ["agent-experience.md"]


def test_empty_when_nothing_present(tmp_path):
  block = memory.build_memory_block(tmp_path)
  assert block.mode == "empty"
  assert block.text == ""
  assert block.loaded == []


# --- graph mode --------------------------------------------------------


def test_graph_mode_injects_index_hot_notes_and_inbox(tmp_path):
  root = _graph(tmp_path)
  (root / "index.md").write_text("# Home\n\n- [[working]]\n")
  (root / "notes" / "aaa.md").write_text(_note(importance=5, title="hot"))
  (root / "notes" / "bbb.md").write_text(_note(importance=1, title="cold"))
  (root / "inbox.md").write_text("- saw the user do X today")
  (root / ".ready").write_text("")

  block = memory.build_memory_block(tmp_path)
  assert block.mode == "graph"
  assert "# Home" in block.text
  assert "notes/aaa.md" in block.text  # path marker present
  assert "saw the user do X" in block.text  # inbox tail injected
  assert "index.md" in block.loaded
  assert "notes/aaa.md" in block.loaded
  assert "inbox.md" in block.loaded


def test_hot_notes_selected_by_score_rendered_in_path_order(tmp_path):
  root = _graph(tmp_path)
  (root / "index.md").write_text("# Home")
  # zzz is hottest by importance, but rendered order must be path-sorted.
  (root / "notes" / "zzz.md").write_text(_note(importance=5, title="z"))
  (root / "notes" / "aaa.md").write_text(_note(importance=4, title="a"))
  (root / "notes" / "mmm.md").write_text(_note(importance=3, title="m"))
  (root / ".ready").write_text("")

  block = memory.build_memory_block(tmp_path, max_notes=2)
  # max_notes=2 selects the two highest scores (zzz=5, aaa=4); mmm excluded.
  assert "notes/zzz.md" in block.text
  assert "notes/aaa.md" in block.text
  assert "notes/mmm.md" not in block.text
  # Rendered path order: aaa before zzz (stable across score changes).
  assert block.text.index("notes/aaa.md") < block.text.index("notes/zzz.md")


def test_budget_skips_hot_notes_when_index_fills_it(tmp_path):
  root = _graph(tmp_path)
  big_index = "# Home\n" + ("x" * 5000)
  (root / "index.md").write_text(big_index)
  (root / "notes" / "n.md").write_text(_note(body="y" * 2000))
  (root / ".ready").write_text("")

  block = memory.build_memory_block(tmp_path, budget_bytes=4000)
  assert block.mode == "graph"
  assert "[index truncated" in block.text
  assert "notes/n.md" not in block.text  # no room left
  assert "notes/n.md" not in block.loaded


# --- frontmatter parsing ----------------------------------------------


def test_parse_frontmatter_scalars_and_lists():
  fm = memory.parse_frontmatter(
    "---\ntitle: Hello world\nimportance: 4\nmocs: [a, b, c]\n---\nbody"
  )
  assert fm["title"] == "Hello world"
  assert fm["importance"] == 4
  assert fm["mocs"] == ["a", "b", "c"]


def test_parse_frontmatter_garbage_is_empty():
  assert memory.parse_frontmatter("no frontmatter here") == {}
  assert memory.parse_frontmatter("---\nunterminated") == {}


# --- indexer + lint ----------------------------------------------------


def _g(tmp_path):
  root = tmp_path / "shared" / "memory"
  (root / "notes").mkdir(parents=True)
  (root / "mocs").mkdir(parents=True)
  return root


def test_build_graph_healthy(tmp_path):
  root = _g(tmp_path)
  (root / "index.md").write_text("# Home\n[[topic]]")
  (root / "mocs" / "topic.md").write_text(
    "---\ntitle: Topic\ntype: moc\n---\n[[fact]]"
  )
  (root / "notes" / "fact.md").write_text(_note(mocs=["topic"], title="Fact"))
  res = memory_graph.build_graph(tmp_path)
  ids = {n["id"] for n in res.nodes}
  assert ids == {"index", "topic", "fact"}
  assert not res.errors
  # fact -> topic via mocs membership + index -> topic via body link.
  kinds = {(e["source"], e["target"], e["kind"]) for e in res.edges}
  assert ("fact", "topic", "moc") in kinds
  assert ("index", "topic", "link") in kinds


def test_usage_counter_accrues_and_merges_into_graph(tmp_path):
  """The Mind 'Used' column read access_count, which nothing incremented —
  so it was always 0. record_usage now accrues loads into usage.json, and
  build_graph merges them onto the frontmatter baseline. Also exercises the
  new children_count breadth metadata on a MOC."""
  root = _g(tmp_path)
  (root / "index.md").write_text("# Home\n[[topic]]")
  (root / "mocs" / "topic.md").write_text("---\ntitle: Topic\ntype: moc\n---\n")
  (root / "notes" / "a.md").write_text(_note(mocs=["topic"], title="A"))
  (root / "notes" / "b.md").write_text(_note(mocs=["topic"], title="B"))
  # Nothing loaded yet → the old uniformly-zero state.
  res0 = memory_graph.build_graph(tmp_path)
  assert all(n["access_count"] == 0 for n in res0.nodes)
  # Two loads of 'a', one of 'b' + the index. inbox/unknown are ignored.
  memory.record_usage(tmp_path, ["notes/a.md", "index.md"])
  memory.record_usage(tmp_path, ["notes/a.md", "notes/b.md", "inbox.md"])
  assert memory.load_usage(tmp_path) == {"a": 2, "b": 1, "index": 1}
  res1 = memory_graph.build_graph(tmp_path)
  by_id = {n["id"]: n for n in res1.nodes}
  assert by_id["a"]["access_count"] == 2
  assert by_id["b"]["access_count"] == 1
  assert by_id["index"]["access_count"] == 1
  # The MOC exposes its breadth: 2 member notes.
  assert by_id["topic"]["children_count"] == 2
  assert set(by_id["topic"]["children"]) == {"a", "b"}


def test_hot_note_selection_reflects_live_usage(tmp_path):
  """Live usage breaks the tie between equal-importance notes, so a note the
  agent keeps loading rises into the injected hot set even with a 0 baseline."""
  root = _g(tmp_path)
  (root / ".ready").write_text("")
  (root / "index.md").write_text("# Home")
  (root / "notes" / "low.md").write_text(_note(importance=1, title="Low", mocs=[]))
  (root / "notes" / "high.md").write_text(_note(importance=1, title="High", mocs=[]))
  memory.record_usage(tmp_path, ["notes/high.md"] * 3)
  block = memory.build_memory_block(tmp_path, max_notes=1)
  assert "notes/high.md" in block.loaded
  assert "notes/low.md" not in block.loaded


def test_build_graph_flags_dangling_link_and_orphan(tmp_path):
  root = _g(tmp_path)
  (root / "index.md").write_text("# Home")
  (root / "notes" / "lonely.md").write_text(
    _note(mocs=["ghost"], links="[[nowhere]]", title="Lonely")
  )
  res = memory_graph.build_graph(tmp_path)
  kinds = {p["kind"] for p in res.problems}
  assert "dangling_moc" in kinds  # ghost moc doesn't exist -> error
  assert "dangling_link" in kinds  # [[nowhere]] -> warn
  assert any(p["kind"] == "dangling_moc" and p["severity"] == "error"
             for p in res.problems)


def test_build_graph_duplicate_id_is_error(tmp_path):
  root = _g(tmp_path)
  (root / "index.md").write_text("# Home")
  (root / "mocs" / "dup.md").write_text("---\ntype: moc\n---\nx")
  (root / "notes" / "dup.md").write_text(_note(title="dup"))
  res = memory_graph.build_graph(tmp_path)
  assert any(p["kind"] == "duplicate_id" for p in res.errors)


def test_write_graph_skips_on_errors(tmp_path):
  root = _g(tmp_path)
  (root / "index.md").write_text("# Home")
  (root / "notes" / "bad.md").write_text(_note(mocs=["ghost"]))  # dangling moc
  res = memory_graph.write_graph(tmp_path)
  assert res.errors
  assert not (root / "graph.json").exists()  # not written on error
