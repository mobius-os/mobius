"""Core chat continuity; optional graph memory belongs to an installed app."""

import os

from app import memory


def _chat_note(root, chat_id, *, description=None, **sections):
  target = root / "chats" / chat_id
  target.mkdir(parents=True, exist_ok=True)
  body = (
    f"---\ntype: chat\ndescription: {description or chat_id}\n---\n"
  )
  for heading, text in sections.items():
    body += f"## {heading}\n{text}\n\n"
  (target / "index.md").write_text(body, encoding="utf-8")
  return target / "index.md"


def test_empty_when_no_chat_summaries_exist(tmp_path):
  block = memory.build_memory_block(tmp_path)
  assert block.mode == "empty"
  assert block.text == ""
  assert block.loaded == []


def test_graph_files_are_never_automatic_chat_context(tmp_path):
  root = tmp_path / "shared" / "memory"
  (root / "notes").mkdir(parents=True)
  (root / "mocs").mkdir()
  (root / "index.md").write_text("SECRET ROUTER", encoding="utf-8")
  (root / "notes" / "fact.md").write_text("SECRET FACT", encoding="utf-8")
  (root / "graph.json").write_text('{"nodes":[]}', encoding="utf-8")
  (root / ".ready").write_text("legacy marker", encoding="utf-8")

  block = memory.build_memory_block(tmp_path)

  assert block.mode == "empty"
  assert "SECRET" not in block.text


def test_chat_digests_are_newest_first_and_budget_bounded(tmp_path):
  root = tmp_path / "shared" / "memory"
  for i in range(6):
    path = _chat_note(
      root, f"c{i:02d}", description=f"chat {i:02d}",
      Digest=f"digest {i:02d} " + "x" * 120,
      Summary=f"private long summary {i:02d}",
    )
    os.utime(path, (1000 + i, 1000 + i))

  block = memory.build_memory_block(tmp_path, budget_bytes=600)

  assert "digest 05" in block.text
  assert "digest 00" not in block.text
  assert block.text.index("digest 05") < block.text.index("digest 04")
  assert "private long summary" not in block.text
  assert len(block.text.encode("utf-8")) <= 600


def test_deleted_or_otherwise_ineligible_chat_note_is_never_injected(tmp_path):
  root = tmp_path / "shared" / "memory"
  active = _chat_note(root, "active", Digest="active context")
  deleted = _chat_note(root, "deleted", Digest="deleted private context")
  os.utime(active, (1000, 1000))
  os.utime(deleted, (2000, 2000))

  block = memory.build_memory_block(
    tmp_path, eligible_chat_ids={"active"},
  )

  assert "active context" in block.text
  assert "deleted private context" not in block.text
  assert block.loaded == ["chats/active/index.md"]


def test_digest_preferred_over_unbounded_summary(tmp_path):
  root = tmp_path / "shared" / "memory"
  _chat_note(
    root, "c1", description="one line", Digest="bounded digest",
    Summary="sensitive cumulative summary " + "x" * 5000,
  )

  block = memory.build_memory_block(tmp_path)

  assert "one line" in block.text
  assert "bounded digest" in block.text
  assert "sensitive cumulative summary" not in block.text


def test_legacy_summary_fallback_excludes_later_fact_sections(tmp_path):
  root = tmp_path / "shared" / "memory"
  path = _chat_note(root, "legacy", Summary="fallback summary")
  with path.open("a", encoding="utf-8") as handle:
    handle.write("## Facts & intent\n- prefers grams\n")

  block = memory.build_memory_block(tmp_path)

  assert "fallback summary" in block.text
  assert "prefers grams" not in block.text


def test_headingless_legacy_note_falls_back_to_bounded_body(tmp_path):
  root = tmp_path / "shared" / "memory"
  path = _chat_note(root, "legacy", description="legacy chat")
  path.write_text(
    "---\ntype: chat\ndescription: legacy chat\n---\nloose legacy body",
    encoding="utf-8",
  )

  block = memory.build_memory_block(tmp_path)

  assert "loose legacy body" in block.text
  assert len(block.text.encode("utf-8")) < memory.DIGEST_MAX_BYTES + 200


def test_parse_frontmatter_supports_chat_description():
  assert memory.parse_frontmatter(
    "---\ndescription: Hello world\ntags: [a, b]\n---\nbody"
  ) == {"description": "Hello world", "tags": ["a", "b"]}
  assert memory.parse_frontmatter("---\nunterminated") == {}
