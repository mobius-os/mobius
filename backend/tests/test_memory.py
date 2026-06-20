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


# --- empty (no published graph) ----------------------------------------


def test_no_ready_sentinel_is_empty(tmp_path):
  shared = tmp_path / "shared"
  shared.mkdir(parents=True)
  # A memory dir without .ready must NOT activate graph mode; with no
  # published graph the injected block is empty (the agent reads on demand).
  (shared / "memory" / "notes").mkdir(parents=True)
  block = memory.build_memory_block(tmp_path)
  assert block.mode == "empty"
  assert block.text == ""
  assert block.loaded == []


def test_empty_when_nothing_present(tmp_path):
  block = memory.build_memory_block(tmp_path)
  assert block.mode == "empty"
  assert block.text == ""
  assert block.loaded == []


# --- graph mode --------------------------------------------------------


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


def test_budget_skips_inbox_when_index_fills_it(tmp_path):
  # A large index can leave no room for the inbox tail. The inbox chunk's
  # header+marker+separator are NOT covered by INBOX_TAIL_BYTES, so without
  # an explicit budget check the chunk would push the block past
  # budget_bytes. Assert it is dropped, keeping the block within budget.
  root = _graph(tmp_path)
  # Index nearly fills the 4000-byte budget (but isn't itself truncated),
  # leaving no room for even a tiny inbox chunk + its marker + separator.
  big_index = "# Home\n" + ("x" * 3990)
  (root / "index.md").write_text(big_index)
  (root / "inbox.md").write_text("- a fresh observation worth keeping")
  (root / ".ready").write_text("")

  block = memory.build_memory_block(tmp_path, budget_bytes=4000)
  assert block.mode == "graph"
  assert "[index truncated" not in block.text  # index itself fits
  assert "fresh observation" not in block.text  # no room left for inbox
  assert "inbox.md" not in block.loaded
  assert len(block.text.encode("utf-8")) <= 4000


def test_truncated_index_stays_within_budget(tmp_path):
  # A too-big index gets truncated + a marker appended. The marker must fit
  # WITHIN budget_bytes, not overrun it by its own length.
  root = _graph(tmp_path)
  (root / "index.md").write_text("# Home\n" + ("x" * 8000))
  (root / ".ready").write_text("")
  block = memory.build_memory_block(tmp_path, budget_bytes=4000)
  assert block.mode == "graph"
  assert "[index truncated" in block.text
  assert len(block.text.encode("utf-8")) <= 4000


def test_empty_published_graph_is_empty(tmp_path):
  # .ready present but the graph has no index/notes/inbox — the injected block
  # is empty (the agent can still Read the graph on demand). No flat-file
  # fallback exists.
  shared = tmp_path / "shared"
  (shared / "memory" / "notes").mkdir(parents=True)
  (shared / "memory" / ".ready").write_text("")
  block = memory.build_memory_block(tmp_path)
  assert block.mode == "empty"
  assert block.text == ""


# --- recent-chats queue --------------------------------------------------


def test_recent_chats_truncates_oldest_first_when_tight(tmp_path):
  # 40 entries at ~40 bytes each ≈ 1.6 KB; a budget that fits the index
  # plus only part of the queue must keep the NEWEST tail and drop the
  # oldest entries behind the omission marker.
  root = _graph(tmp_path)
  (root / "index.md").write_text("# Home")
  lines = [f"- [chat:c{i:02d}] 2026-06-01 — summary {i:02d}" for i in range(40)]
  (root / "recent-chats.md").write_text("\n".join(lines))
  (root / ".ready").write_text("")

  block = memory.build_memory_block(tmp_path, budget_bytes=600)
  assert "recent-chats.md" in block.loaded
  assert "[older recent-chats entries omitted]" in block.text
  assert "summary 39" in block.text  # newest survives
  assert "summary 00" not in block.text  # oldest evicted
  assert len(block.text.encode("utf-8")) <= 600


def test_recent_chats_skipped_when_no_room(tmp_path):
  root = _graph(tmp_path)
  (root / "index.md").write_text("# Home\n" + "x" * 580)
  (root / "recent-chats.md").write_text("- [chat:c1] 2026-06-10 — summary")
  (root / ".ready").write_text("")

  block = memory.build_memory_block(tmp_path, budget_bytes=600)
  assert "recent-chats.md" not in block.loaded
  assert len(block.text.encode("utf-8")) <= 600


def test_recent_chats_not_counted_as_usage(tmp_path):
  memory.record_usage(tmp_path, ["recent-chats.md", "notes/a.md"])
  # The queue is a buffer, not a graph node — no phantom id accrues.
  assert memory.load_usage(tmp_path) == {"a": 1}


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


# --- redirects -----------------------------------------------------------


def _redirect(target):
  return (
    f"---\ntitle: Old slug\ntype: redirect\ntarget: {target}\n---\n"
    f"This content has moved to [[{target}]].\n"
  )


def _kinds(res):
  return {p["kind"] for p in res.problems}


def test_redirect_resolves_and_keeps_old_links_valid(tmp_path):
  root = _g(tmp_path)
  (root / "index.md").write_text("# Home\n- [[topic]] — the map\n")
  (root / "mocs" / "topic.md").write_text(
    "---\ntitle: Topic\ntype: moc\n---\n- [[new-slug]] — the fact\n"
    "- [[linker]] — still links the old slug\n"
  )
  (root / "notes" / "old-slug.md").write_text(_redirect("new-slug"))
  (root / "notes" / "new-slug.md").write_text(
    _note(mocs=["topic"], title="New")
  )
  (root / "notes" / "linker.md").write_text(
    _note(mocs=["topic"], title="Linker", links="[[old-slug]] — see also")
  )
  res = memory_graph.build_graph(tmp_path)
  assert not res.errors
  by_id = {n["id"]: n for n in res.nodes}
  assert by_id["old-slug"]["type"] == "redirect"
  assert by_id["old-slug"]["target"] == "new-slug"
  # The stub forwards: a redirect-kind edge connects old to new, and the
  # one-hop resolution produces no chain warning.
  kinds = {(e["source"], e["target"], e["kind"]) for e in res.edges}
  assert ("old-slug", "new-slug", "redirect") in kinds
  assert "redirect_chain" not in _kinds(res)
  # linker's [[old-slug]] is NOT dangling, and the stub is not orphaned.
  assert "dangling_link" not in _kinds(res)
  assert "orphan_redirect" not in _kinds(res)


def test_redirect_chain_warns_and_cycle_errors(tmp_path):
  root = _g(tmp_path)
  (root / "index.md").write_text("# Home\n- [[a]] — start of the chain\n")
  (root / "notes" / "a.md").write_text(_redirect("b"))
  (root / "notes" / "b.md").write_text(_redirect("c"))
  (root / "notes" / "c.md").write_text(_note(title="C", mocs=[]))
  res = memory_graph.build_graph(tmp_path)
  chain = [p for p in res.problems if p["kind"] == "redirect_chain"]
  # a -> b -> c is 2 hops (flagged); b -> c is 1 hop (fine).
  assert len(chain) == 1 and chain[0]["severity"] == "warn"
  assert "a resolves in 2 hops" in chain[0]["detail"]

  (root / "notes" / "c.md").write_text(_redirect("a"))  # now a cycle
  res = memory_graph.build_graph(tmp_path)
  assert any(
    p["kind"] == "dangling_redirect" and "cycle" in p["detail"]
    for p in res.errors
  )


def test_redirect_missing_target_is_error(tmp_path):
  root = _g(tmp_path)
  (root / "index.md").write_text("# Home\n- [[stub]] — a broken stub\n")
  (root / "notes" / "stub.md").write_text(_redirect("nowhere"))
  res = memory_graph.build_graph(tmp_path)
  assert any(p["kind"] == "dangling_redirect" for p in res.errors)


def test_orphan_redirect_warns(tmp_path):
  root = _g(tmp_path)
  (root / "index.md").write_text("# Home\n- [[real]] — the live note\n")
  (root / "notes" / "real.md").write_text(_note(title="Real", mocs=[]))
  # Nothing links the stub anymore — its purpose is served.
  (root / "notes" / "stub.md").write_text(_redirect("real"))
  res = memory_graph.build_graph(tmp_path)
  assert any(p["kind"] == "orphan_redirect" for p in res.problems)
  # And it is a warning, not a publish-blocking error.
  assert not res.errors


# --- structure-rule warnings ---------------------------------------------


def test_moc_overfull_warns_past_cap(tmp_path):
  root = _g(tmp_path)
  (root / "index.md").write_text("# Home\n- [[topic]] — the map\n")
  (root / "mocs" / "topic.md").write_text("---\ntype: moc\n---\n")
  for i in range(16):
    (root / "notes" / f"n{i:02d}.md").write_text(
      _note(mocs=["topic"], title=f"N{i}")
    )
  res = memory_graph.build_graph(tmp_path)
  assert any(p["kind"] == "moc_overfull" for p in res.problems)
  # Exactly at the cap is fine.
  (root / "notes" / "n15.md").unlink()
  res = memory_graph.build_graph(tmp_path)
  assert not any(p["kind"] == "moc_overfull" for p in res.problems)


def test_bare_moc_entry_warns_described_entry_does_not(tmp_path):
  root = _g(tmp_path)
  (root / "index.md").write_text("# Home\n- [[topic]] — the map\n")
  (root / "mocs" / "topic.md").write_text(
    "---\ntype: moc\n---\n"
    "- [[described]] — what you will find there\n"
    "- [[bare]]\n"
  )
  for slug in ("described", "bare"):
    (root / "notes" / f"{slug}.md").write_text(
      _note(mocs=["topic"], title=slug)
    )
  res = memory_graph.build_graph(tmp_path)
  bare = [p for p in res.problems if p["kind"] == "bare_moc_entry"]
  assert len(bare) == 1
  assert "topic" in bare[0]["detail"] and "[[bare]]" in bare[0]["detail"]


def test_oversized_note_counts_prose_not_structure(tmp_path):
  root = _g(tmp_path)
  (root / "index.md").write_text("# Home")
  # 31 prose lines → split candidate.
  (root / "notes" / "fat.md").write_text(
    _note(title="Fat", mocs=[], body="\n".join(f"line {i}" for i in range(31)))
  )
  # 31 lines of headings + link bullets + blanks → NOT prose, no warning.
  structure = "\n".join(
    ["## h", "", "- [[x]] — desc"] * 10 + ["one real prose line"]
  )
  (root / "notes" / "lists.md").write_text(
    _note(title="Lists", mocs=[], body=structure)
  )
  res = memory_graph.build_graph(tmp_path)
  fat = [p for p in res.problems if p["kind"] == "oversized_note"]
  assert len(fat) == 1 and "fat" in fat[0]["detail"]


def test_moc_candidate_warns_at_five_outbound_links(tmp_path):
  root = _g(tmp_path)
  (root / "index.md").write_text("# Home")
  links5 = " ".join(f"[[t{i}]]" for i in range(5))
  links4 = " ".join(f"[[t{i}]]" for i in range(4))
  (root / "notes" / "hub.md").write_text(
    _note(title="Hub", mocs=[], links=links5)
  )
  (root / "notes" / "plain.md").write_text(
    _note(title="Plain", mocs=[], links=links4)
  )
  for i in range(5):
    (root / "notes" / f"t{i}.md").write_text(_note(title=f"T{i}", mocs=[]))
  res = memory_graph.build_graph(tmp_path)
  cand = [p for p in res.problems if p["kind"] == "moc_candidate"]
  assert len(cand) == 1 and "hub" in cand[0]["detail"]


def test_as_of_supersedes_source_tolerated_and_validated(tmp_path):
  root = _g(tmp_path)
  (root / "index.md").write_text("# Home")
  (root / "notes" / "good.md").write_text(
    "---\ntitle: Good\ntype: note\nas-of: 2026-06-11\n"
    "supersedes: [old-one]\nsource: [chat:abc, chat:def]\nmocs: []\n---\nbody"
  )
  (root / "notes" / "bad.md").write_text(
    "---\ntitle: Bad\ntype: note\nas-of: yesterday\n"
    "supersedes: 7\nmocs: []\n---\nbody"
  )
  res = memory_graph.build_graph(tmp_path)
  kinds = _kinds(res)
  assert "bad_as_of" in kinds
  assert "bad_supersedes" in kinds
  bad = [p for p in res.problems if p["kind"] in ("bad_as_of", "bad_supersedes")]
  assert all("bad" in p["detail"] for p in bad)  # only the bad note flagged
  assert all(p["severity"] == "warn" for p in bad)
  by_id = {n["id"]: n for n in res.nodes}
  # The well-formed fields surface in graph.json (scalar supersedes would
  # be normalized to a list).
  assert by_id["good"]["as_of"] == "2026-06-11"
  assert by_id["good"]["supersedes"] == ["old-one"]
  assert by_id["good"]["source"] == ["chat:abc", "chat:def"]


def test_seed_graph_lints_clean_under_new_warnings():
  """The shipped seed must not warn — bare entries or oversized seed notes
  would teach every fresh instance to ignore the worklist."""
  seed = Path(__file__).resolve().parents[1] / "scripts" / "seed-memory"
  res = memory_graph.build_graph(root=seed)
  assert not res.errors
  assert not res.problems, res.problems


def test_graph_mode_injects_router_recency_inbox_no_notes(tmp_path):
  # v2: build_memory_block injects the router (index) + recency + inbox, and NO
  # notes — the agent traverses from the router's scent lines on demand.
  root = _graph(tmp_path)
  (root / "index.md").write_text("# Home\n\n- [[working]]\n")
  (root / "notes" / "aaa.md").write_text(_note(title="a"))
  (root / "notes" / "bbb.md").write_text(_note(title="b"))
  (root / "inbox.md").write_text("- saw the user do X today")
  (root / ".ready").write_text("")

  block = memory.build_memory_block(tmp_path)
  assert block.mode == "graph"
  assert "# Home" in block.text
  assert "saw the user do X" in block.text
  assert "index.md" in block.loaded
  assert "inbox.md" in block.loaded
  assert not any(x.startswith("notes/") for x in block.loaded)
  assert "notes/" not in block.text


def test_recent_chats_injected_after_index_before_inbox_v2(tmp_path):
  root = _graph(tmp_path)
  (root / "index.md").write_text("# Home")
  (root / "recent-chats.md").write_text(
    "- [chat:c1] 2026-06-10 — built the Habits app\n"
  )
  (root / "inbox.md").write_text("- a fresh observation")
  (root / ".ready").write_text("")

  block = memory.build_memory_block(tmp_path)
  assert "recent-chats.md" in block.loaded
  assert "built the Habits app" in block.text
  assert (
    block.text.index("# Home")
    < block.text.index("recent-chats.md")
    < block.text.index("inbox.md")
  )
