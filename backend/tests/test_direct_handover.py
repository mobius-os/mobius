"""The provider-free handover a provider switch commits instead of a model briefing."""
import pytest

from app.chat_continuity import apply_checkpoint, checkpoint_coverage
from app.compaction import CompactionError
from app.direct_handover import HANDOVER_MAX_BYTES, build_direct_handover

DIGEST_TITLE = "--- CHAT DIGEST ---"
SUMMARY_TITLE = "--- CHAT SUMMARY ---"
SINCE_DIGEST_TITLE = "--- MESSAGES SINCE THE DIGEST ---"
MAY_REPEAT_TITLE = "--- RECENT MESSAGES (may repeat what the digest covers) ---"
RECENT_TITLE = "--- RECENT MESSAGES ---"
DIGEST_OMITTED = "Older digest entries are omitted here"
MESSAGES_OMITTED = "Earlier messages are omitted here"


def _size(text: str) -> int:
  return len(text.encode("utf-8"))


def _turn(label: str, *, filler: int = 0) -> list[dict]:
  """One owner message and its answer, each carrying ``label`` for searching."""
  return [
    {"role": "user", "content": f"{label} owner message " + "q" * filler},
    {"role": "assistant", "content": f"{label} assistant answer " + "a" * filler},
  ]


def _unbound_note(summary: str, digest: str) -> str:
  """A note like an older chat's: summary and digest, no recorded coverage."""
  return f"---\ntype: chat\n---\n## Summary\n{summary}\n\n## Digest\n{digest}\n"


def _bound_note(covered: list[dict], *, summary: str, digest: str) -> str:
  """A note whose digest is bound to exactly ``covered`` through its checkpoint."""
  return apply_checkpoint(
    None, name="chat", summary=summary, digest=digest,
    coverage=checkpoint_coverage(covered, "no-live-run"),
  )


def _entries(count: int, *, each_bytes: int = 4_000) -> str:
  """Digest entries ENTRY-000.., each one paragraph of about ``each_bytes``."""
  return "\n\n".join(
    f"ENTRY-{index:03d} " + "w" * (each_bytes - 10) for index in range(count)
  )


def test_verified_digest_stands_in_for_covered_messages_and_keeps_the_tail():
  covered = _turn("COVERED")
  digest = "Decision: keep the API stable. Next: wire persistence."
  note = _bound_note(covered, summary="Building feature X.", digest=digest)
  tail = [{"role": "user", "content": "TAIL follow-up after the digest"}]

  handover = build_direct_handover(note, covered + tail)

  assert digest in handover
  assert "Building feature X." in handover
  assert SINCE_DIGEST_TITLE in handover
  assert "TAIL follow-up after the digest" in handover
  assert "COVERED" not in handover
  assert MAY_REPEAT_TITLE not in handover


def test_digest_without_verified_coverage_adds_recent_messages_that_may_repeat_it():
  messages = _turn("EARLIER")
  note = _unbound_note("Short summary.", "Decision: use the API.")

  handover = build_direct_handover(note, messages)

  assert "Decision: use the API." in handover
  assert MAY_REPEAT_TITLE in handover
  assert SINCE_DIGEST_TITLE not in handover
  assert "EARLIER owner message" in handover
  assert "EARLIER assistant answer" in handover


def test_digest_whose_covered_history_was_edited_no_longer_replaces_any_message():
  covered = _turn("COVERED")
  note = _bound_note(covered, summary="s", digest="Decision: keep going.")
  edited = [{**covered[0], "content": "COVERED owner message, edited later"}, covered[1]]

  handover = build_direct_handover(note, edited)

  assert MAY_REPEAT_TITLE in handover
  assert "COVERED owner message, edited later" in handover
  assert "COVERED assistant answer" in handover


def test_chat_without_a_note_hands_over_a_small_transcript_whole():
  messages = _turn("FIRST") + _turn("SECOND")

  handover = build_direct_handover("", messages)

  assert RECENT_TITLE in handover
  assert DIGEST_TITLE not in handover
  assert SUMMARY_TITLE not in handover
  assert MESSAGES_OMITTED not in handover
  for label in ("FIRST", "SECOND"):
    assert f"{label} owner message" in handover
    assert f"{label} assistant answer" in handover
  assert handover.index("FIRST owner message") < handover.index("SECOND owner message")


def test_oversized_digest_keeps_newest_entries_and_the_summary_within_the_budget():
  note = _unbound_note("SUMMARY-MARK still current.", _entries(40))

  handover = build_direct_handover(note, [])

  assert _size(handover) <= HANDOVER_MAX_BYTES
  assert "SUMMARY-MARK still current." in handover
  assert "ENTRY-039" in handover
  assert "ENTRY-000" not in handover
  assert DIGEST_OMITTED in handover
  kept = [i for i in range(40) if f"ENTRY-{i:03d}" in handover]
  assert kept == list(range(kept[0], 40)), "the kept entries must be the newest, unbroken"


def test_oversized_uncovered_tail_keeps_newest_messages_without_crowding_out_the_digest():
  covered = _turn("COVERED")
  digest = _entries(8)
  note = _bound_note(covered, summary="SUMMARY-MARK", digest=digest)
  tail = [
    {"role": "user", "content": f"MSG-{index:03d} " + "y" * 1_000}
    for index in range(80)
  ]

  handover = build_direct_handover(note, covered + tail)

  assert _size(handover) <= HANDOVER_MAX_BYTES
  assert "MSG-079" in handover
  assert "MSG-000" not in handover
  assert MESSAGES_OMITTED in handover
  assert "SUMMARY-MARK" in handover
  assert digest in handover
  assert DIGEST_OMITTED not in handover


@pytest.mark.parametrize("unit", ["🙂", "日本語", "é"])
@pytest.mark.parametrize("offset", [0, 1, 2, 3])
def test_multibyte_text_at_a_cut_boundary_stays_valid_and_within_the_budget(unit, offset):
  # A single unbroken paragraph forces a cut inside the text, wherever
  # ``offset`` happens to put the multi-byte characters relative to it.
  huge = "a" * offset + unit * 80_000
  messages = [{"role": "user", "content": huge}]

  from_transcript = build_direct_handover("", messages)
  from_digest = build_direct_handover(_unbound_note("s", huge), [])

  for handover in (from_transcript, from_digest):
    assert _size(handover) <= HANDOVER_MAX_BYTES
    assert "�" not in handover
    assert unit in handover


def test_earlier_provider_handovers_are_not_copied_into_the_next_one():
  messages = [
    {"role": "user", "content": "REAL owner request"},
    {"role": "assistant", "content": "REAL answer"},
    {"role": "assistant", "kind": "compaction", "content": "STALE-BRIEFING from an earlier switch"},
    {"role": "user", "content": "REAL follow-up"},
  ]

  handover = build_direct_handover("", messages)

  assert "STALE-BRIEFING" not in handover
  for text in ("REAL owner request", "REAL answer", "REAL follow-up"):
    assert text in handover


@pytest.mark.parametrize(
  "messages",
  [
    [],
    [{"role": "assistant", "kind": "compaction", "content": "only an earlier handover"}],
    [{"role": "user", "content": "   "}],
  ],
  ids=["no-messages", "only-an-earlier-handover", "blank-message"],
)
def test_chat_with_no_content_has_nothing_to_hand_over(messages):
  with pytest.raises(CompactionError, match="Nothing to hand over"):
    build_direct_handover("", messages)


def test_summary_alone_is_not_a_handover_when_the_chat_has_no_digest_or_messages():
  with pytest.raises(CompactionError, match="Nothing to hand over"):
    build_direct_handover(_unbound_note("Only a summary.", ""), [])


def test_a_long_uncovered_turn_uses_the_room_a_short_digest_leaves():
  """The reserved share is a floor, not a ceiling: an unusually long final
  turn is cut only when the note really needs the space."""
  covered = _turn("COVERED")
  note = _bound_note(covered, summary="Short.", digest="Decision: keep it small.")
  tail = [
    {"role": "user", "content": f"MSG-{index:03d} " + "y" * 1_000}
    for index in range(40)
  ]

  handover = build_direct_handover(note, covered + tail)

  assert "MSG-000" in handover and "MSG-039" in handover
  assert MESSAGES_OMITTED not in handover
  assert _size(handover) <= HANDOVER_MAX_BYTES


def test_a_row_whose_content_is_not_text_is_skipped_instead_of_failing_the_turn():
  messages = [
    {"role": "user", "content": [{"type": "text", "text": "STRUCTURED"}]},
    {"role": "assistant", "content": None},
    {"role": "user", "content": "PLAIN follow-up"},
  ]

  handover = build_direct_handover("", messages)

  assert "PLAIN follow-up" in handover
  assert "STRUCTURED" not in handover


def test_a_paragraph_starting_exactly_at_the_cut_is_kept_whole():
  from app.direct_handover import _newest_within

  text = "OLD " + "o" * 96 + "\n\n" + "NEW " + "n" * 96

  kept, cut = _newest_within(text, 100)

  assert cut is True
  assert kept == "NEW " + "n" * 96


def test_a_part_cut_away_entirely_still_says_it_was_left_out():
  """A summary far larger than the whole budget leaves the digest no room."""
  note = _unbound_note("s" * 70_000, "DIGEST-MARK decision")

  handover = build_direct_handover(note, _turn("RECENT"))

  assert "DIGEST-MARK" not in handover
  assert DIGEST_TITLE in handover and DIGEST_OMITTED in handover
  assert _size(handover) <= HANDOVER_MAX_BYTES


def test_an_oversized_opening_request_keeps_its_start_and_says_it_was_cut():
  task = "TASK-START audit the runs. " + "t" * 80_000
  messages = [{"role": "user", "content": task}] + [
    {"role": "assistant", "content": f"STEP-{index:02d} " + "p" * 3_000}
    for index in range(30)
  ]

  handover = build_direct_handover("", messages, keep_opening_request=True)

  assert "USER: TASK-START audit the runs." in handover
  assert "Only the start of the opening request is shown" in handover
  assert "STEP-29" in handover
  assert _size(handover) <= HANDOVER_MAX_BYTES


def test_a_kept_opening_request_is_not_repeated_when_recent_messages_show_it():
  messages = [
    {"role": "user", "content": "TASK: audit the three runs"},
    {"role": "assistant", "content": "LATEST progress"},
  ]

  handover = build_direct_handover("", messages, keep_opening_request=True)

  assert handover.count("TASK: audit the three runs") == 1
  assert "--- OPENING REQUEST ---" not in handover


def test_a_note_that_cannot_be_read_counts_as_no_note(tmp_path):
  from app.chat_continuity import note_path, read_note

  assert read_note(tmp_path, "missing-chat") == ""
  damaged = note_path(tmp_path, "damaged-chat")
  damaged.parent.mkdir(parents=True)
  damaged.write_bytes(b"## Digest\n\xff\xfe not utf-8")
  assert read_note(tmp_path, "damaged-chat") == ""
