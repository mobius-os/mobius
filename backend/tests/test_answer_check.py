"""The visible-answer check: what a Claude reply hands back without showing."""

from __future__ import annotations

import os

import pytest

from app import answer_check


def _reason(
  blocks, *, before_card, chat_id="c", media_dir="/m", last=None, message_card=False,
):
  return answer_check.reason(
    blocks, chat_id=chat_id, media_dir=media_dir, before_card=before_card,
    last_assistant_message=last, message_card=message_card,
  )


def _asks_for_the_answer(reason: str | None) -> bool:
  return reason is not None and "brief explanation of the conclusion" in reason


# The owner reads only text blocks. This is the stored shape of a reply whose
# answer lived only in its thinking: a closing line after a checkpoint.
_ANSWER_ONLY_IN_THINKING = [
  {"type": "thinking", "content": "Checking the deploy log for the failure."},
  {"type": "text", "content": "I'll check the deploy log before answering."},
  {"type": "tool", "tool": "Bash", "status": "done"},
  {"type": "thinking", "content": "Confirmed: the deploy failed because the "
   "build ran out of memory while bundling the frontend. " * 10},
  {"type": "tool", "tool": "mcp__mobius_control__checkpoint_chat", "status": "done"},
  {"type": "text", "content": "Nothing has been changed."},
]


def test_answer_check_flags_an_answer_written_only_in_thinking_at_the_end_of_a_turn():
  assert _asks_for_the_answer(_reason(_ANSWER_ONLY_IN_THINKING, before_card=False))
  # The CLI's final message may not have reached the sink yet; a short one
  # does not hide the problem.
  assert _asks_for_the_answer(_reason(
    _ANSWER_ONLY_IN_THINKING[:-1], before_card=False, last="Nothing has been changed.",
  ))


def test_answer_check_length_rule_speaks_only_before_any_reminder():
  # After a reminder, a short reply beside long reasoning is the agent judging
  # it has nothing to add, as the reminder allows; a new closing draft still counts.
  assert answer_check.reason(
    _ANSWER_ONLY_IN_THINKING, chat_id="c", media_dir="/m", before_card=False,
    reminded=True,
  ) is None
  assert _asks_for_the_answer(answer_check.reason(
    _closing_draft_in_thinking(), chat_id="c", media_dir="/m", before_card=False,
    reminded=True,
  ))


def test_answer_check_lets_a_short_lead_in_to_a_card_through():
  # The card carries the content, so a short lead-in after long reasoning is
  # the normal shape of a card turn, not a hidden answer.
  lead_in = [
    {"type": "thinking", "content": "Weighing the two layouts and their tradeoffs. " * 18},
    {"type": "text", "content": "Two options, pick one below."},
  ]
  assert _reason(lead_in, before_card=True) is None
  assert _reason(_ANSWER_ONLY_IN_THINKING, before_card=True) is None


def test_answer_check_before_a_card_asks_for_its_message_instead_of_reading_thinking():
  # A question, approval or restart card carries the turn's closing message,
  # so whether one is there decides; closing reasoning is not read at all.
  closing = _closing_draft_in_thinking()
  assert _reason(closing, before_card=True, message_card=True, last="Two options below.") is None
  for blank in (None, "", "   ", 5):
    reason = _reason(
      [{"type": "text", "content": "Two options, pick one below."}],
      before_card=True, message_card=True, last=blank,
    )
    assert reason is not None
    assert reason.startswith("Möbius check before your card is saved.")
    assert "This card has no message" in reason
    assert "card's message" in reason
    assert "brief explanation of the conclusion" not in reason
  # A secret card has no message field; its closing reasoning is not read either.
  assert _reason(closing, before_card=True) is None


def test_answer_check_quotes_what_the_owner_actually_sees():
  # An agent that drafted its answer in thinking believes it is on screen;
  # quoting the visible reply lets it see at once that it is not.
  reason = _reason(_ANSWER_ONLY_IN_THINKING, before_card=False)
  assert ("verbatim: «I'll check the deploy log before answering."
          "Nothing has been changed.»") in reason
  silent = [block for block in _ANSWER_ONLY_IN_THINKING if block["type"] != "text"]
  reason = _reason(silent, before_card=False)
  assert "The owner sees no text at all from this reply." in reason


def test_answer_check_leaves_ordinary_replies_alone():
  answered = [*_ANSWER_ONLY_IN_THINKING, {
    "type": "text",
    "content": "The deploy failed: the build ran out of memory. " * 10,
  }]
  assert _reason(answered, before_card=False) is None
  # An answer the stream reader has not dispatched yet still counts.
  assert _reason(
    _ANSWER_ONLY_IN_THINKING, before_card=False, last=answered[-1]["content"],
  ) is None
  # A short answer after brief reasoning is a normal short reply.
  assert _reason([
    {"type": "thinking", "content": "Simple greeting."},
    {"type": "text", "content": "Hi!"},
  ], before_card=False) is None
  for malformed in (None, "blocks", [None, {"type": "thinking", "content": 5}]):
    assert _reason(malformed, before_card=False) is None
    assert answer_check.after_step_reason(malformed) is None


_DRAFT = ("The preview build still fails: increase the memory limit in the "
          "build settings, then retry the deploy.")


def _closing_draft_in_thinking() -> list[dict]:
  """A reply that drafted its closing words in thinking, then handed back.

  Summarized thinking keeps such a draft as its own paragraph after blank
  lines; the visible reply is long enough that length alone looks fine.
  """
  return [
    {"type": "text", "content": "Checking whether the preview build succeeds "
     "now, in case the memory limit was already increased. " * 6},
    {"type": "tool", "tool": "Bash", "status": "done"},
    {"type": "thinking", "content": "Same failure, so the memory limit is still too low."
     f"\n\n\n\n{_DRAFT}\n"},
  ]


@pytest.mark.parametrize("blocks", [_closing_draft_in_thinking(), _ANSWER_ONLY_IN_THINKING])
def test_answer_check_requests_a_summary_without_reproducing_thinking(blocks):
  # Both detection paths must still prompt for a missing answer, but must not
  # send thinking excerpts back as instructions to reproduce them visibly.
  reason = _reason(blocks, before_card=False)
  assert reason is not None
  assert reason.startswith("Möbius end-of-turn check.")
  assert "brief explanation of the conclusion or a summary of actions taken" in reason
  assert "verified task evidence" in reason
  assert _DRAFT not in reason
  assert "Same failure" not in reason
  assert "build ran out of memory" not in reason
  assert "Everything the owner sees from this reply, verbatim:" in reason


def test_answer_check_leaves_said_and_superseded_drafts_alone():
  blocks = _closing_draft_in_thinking()
  assert _asks_for_the_answer(_reason(blocks, before_card=False))
  # Said in the visible reply, even re-wrapped across lines.
  said = [*blocks[:2], {"type": "text", "content": _DRAFT.replace(" in the ", "\nin the ")},
          blocks[2]]
  assert _reason(said, before_card=False) is None
  # Followed by visible text, which supersedes it.
  later = [*blocks, {"type": "text", "content": "Increase the memory limit, then retry the deploy."}]
  assert _reason(later, before_card=False) is None
  # Final text the stream reader has not dispatched yet also comes after it.
  assert _reason(
    blocks, before_card=False, last="Increase memory, then retry the deploy.",
  ) is None
  # A stale final message already in the blocks does not hide the draft.
  assert _asks_for_the_answer(_reason(blocks, before_card=False, last=blocks[0]["content"]))
  # Ordinary reasoning: one paragraph, or a closing fragment too short to be
  # words for the owner.
  for content in ("Same failure, so the memory limit is still too low.",
                  "Same failure.\n\n\n\nOk, asking."):
    assert _reason(
      [*blocks[:2], {"type": "thinking", "content": content}], before_card=False,
    ) is None


def test_answer_check_reads_only_the_reasoning_nearest_the_hand_back():
  # Mid-turn reasoning with a closing paragraph, followed by the steps it
  # planned and newer reasoning, was working toward those steps.
  first, step, planning = _closing_draft_in_thinking()
  later = {"type": "thinking", "content": "The retry shows the same failure."}
  blocks = [first, step, planning, {"type": "tool", "tool": "Bash", "status": "done"}, later]
  assert _reason(blocks, before_card=False) is None
  # A draft followed only by a progress note still closes the reply.
  note = {"type": "tool", "tool": "mcp__mobius_control__checkpoint_chat", "status": "done"}
  assert _asks_for_the_answer(_reason([first, step, planning, note], before_card=False))


def test_answer_check_bounds_visible_quotes_without_exposing_closing_drafts():
  blocks = _closing_draft_in_thinking()
  blocks[0] = {"type": "text", "content": "Earlier progress. " * 1000 + "Latest progress."}
  reason = _reason(blocks, before_card=False)

  assert reason is not None
  assert "The end of the owner's visible reply, verbatim:" in reason
  assert "Latest progress." in reason
  assert len(reason) < 1500
  assert _DRAFT not in reason


def _media_dir(chat_id: str) -> str:
  from app.config import get_settings
  from app.viewed_images import chat_media_dir

  return os.path.normpath(str(chat_media_dir(get_settings().data_dir, chat_id)))


def _viewed_before_after(chat_id: str) -> list[dict]:
  """A reply that promises a comparison, views it, and never embeds it."""
  return [
    {"type": "text", "content": "I'll put the two versions side by side."},
    {"type": "tool", "tool": "Bash", "status": "done"},
    {"type": "tool", "tool": "Read", "status": "done",
     "input": f"{_media_dir(chat_id)}/before-after.png"},
  ]


def test_answer_check_flags_a_viewed_chat_image_the_reply_never_shows():
  chat_id = "image-check-chat"
  reason = _reason(
    _viewed_before_after(chat_id), before_card=True,
    chat_id=chat_id, media_dir=_media_dir(chat_id),
  )
  assert reason is not None
  assert reason.startswith("Möbius check before your card is saved. The card was NOT saved.")
  assert "before-after.png" in reason
  assert f"/api/chats/{chat_id}/media/" in reason
  assert "call the same card tool again" in reason

  # A screenshot step names its saved picture instead of a path.
  shot = [
    {"type": "text", "content": "Here is how it looks now."},
    {"type": "tool", "tool": "mcp__mobius_control__screenshot",
     "status": "done", "saved_image": "shot-1.png"},
  ]
  reason = _reason(shot, before_card=False, chat_id=chat_id, media_dir=_media_dir(chat_id))
  assert reason is not None and "shot-1.png" in reason
  assert reason.startswith("Möbius end-of-turn check.")


def test_answer_check_leaves_shown_and_private_images_alone():
  chat_id = "image-check-chat"
  media = _media_dir(chat_id)

  def reason_for(blocks, last=None):
    return _reason(blocks, before_card=True, chat_id=chat_id, media_dir=media, last=last)

  embed = f"![Before and after](/api/chats/{chat_id}/media/before-after.png)"
  shown = [*_viewed_before_after(chat_id), {"type": "text", "content": embed}]
  assert reason_for(shown) is None
  # An embed the stream reader has not dispatched yet still counts.
  assert reason_for(_viewed_before_after(chat_id), embed) is None
  # Verification frames outside chat media, the owner's uploads, another
  # chat's media, and non-image reads are not something the owner awaits.
  for path in (
    "/tmp/check.png",
    f"{os.path.dirname(media)}/uploads/image.png",
    f"{_media_dir('another-chat')}/before-after.png",
    f"{media}/notes.md",
  ):
    assert reason_for([
      {"type": "text", "content": "Checked it."},
      {"type": "tool", "tool": "Read", "status": "done", "input": path},
    ]) is None, path


_CHECKOUT = "https://checkout.stripe.com/c/pay/cs_test_example#fid"


def _link_only_in_tool_output(text: str, output: str = f"checkout ready\n{_CHECKOUT}\n") -> list[dict]:
  """A reply that fetched a payment link and pointed to it without showing it."""
  return [
    {"type": "text", "content": "Starting the test purchase."},
    {"type": "tool", "tool": "Bash", "status": "done", "output": output},
    {"type": "text", "content": text},
  ]


def test_answer_check_flags_a_link_the_reply_points_to_but_never_shows():
  reason = _reason(
    _link_only_in_tool_output("Pay on the test checkout page linked above."),
    before_card=True,
  )
  assert reason is not None
  assert reason.startswith("Möbius check before your card is saved.")
  assert "checkout.stripe.com" in reason
  # The address itself stays out of the reason; the agent already has it.
  assert "cs_test_" not in reason


def test_answer_check_names_only_the_host_of_a_link_with_credentials():
  # A clone URL with a token in its userinfo must not carry the token into
  # the reason, which is stored with the declined card.
  output = "remote: https://x-access-token:ghs_SECRET123@github.com/o/r.git\n"
  reason = _reason(
    _link_only_in_tool_output("Open the repository from the link below.", output),
    before_card=True,
  )
  assert reason is not None
  assert "(github.com)" in reason
  assert "ghs_SECRET123" not in reason.lower() and "ghs_secret123" not in reason.lower()
  assert "x-access-token" not in reason


def test_answer_check_leaves_shown_and_unmentioned_links_alone():
  def reason_for(blocks, last=None):
    return _reason(blocks, before_card=False, last=last)

  # The link is shown, or the reply never mentions one.
  assert reason_for(_link_only_in_tool_output(f"[Open the checkout]({_CHECKOUT})")) is None
  assert reason_for(_link_only_in_tool_output("Checked the balance: 0.00.")) is None
  # A link the stream reader has not dispatched yet still counts.
  assert reason_for(
    _link_only_in_tool_output("Here is the link:"), f"<{_CHECKOUT}>",
  ) is None
  # Loopback addresses are the agent's plumbing, not something to open.
  assert reason_for([
    {"type": "tool", "tool": "Bash", "status": "done",
     "output": "GET http://127.0.0.1:8000/api/apps/ 200\nhttp://localhost:5173/"},
    {"type": "text", "content": "The broken link in your app is fixed."},
  ]) is None


@pytest.mark.parametrize("text", [
  "Pay on the test checkout page linked above.",
  "Use the link below to finish the purchase.",
  "Open the following link to pay.",
])
def test_answer_check_flags_each_way_of_pointing_at_an_unshown_link(text):
  reason = _reason(_link_only_in_tool_output(text), before_card=True)
  assert reason is not None and "checkout.stripe.com" in reason


@pytest.mark.parametrize("text", [
  # Talking about a link, or a component named Link, is not pointing at one.
  "I added a link to the footer.",
  "Check the Link component.",
  # A relative markdown link is shown, even though it has no web address.
  "Open [the link](/shell/?app=3) below.",
], ids=["a-link", "link-component", "relative-markdown-link"])
def test_answer_check_leaves_ordinary_link_wording_alone(text):
  assert _reason(_link_only_in_tool_output(text), before_card=True) is None


def test_answer_check_leaves_link_words_and_xml_namespaces_alone():
  svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
  # Using the word "link" is not pointing at one.
  assert _reason(
    _link_only_in_tool_output("I fixed the nav link color.", svg), before_card=False,
  ) is None
  # Pointing at a link while tool output held only a namespace identifier.
  assert _reason(
    _link_only_in_tool_output("Follow the link above.", svg), before_card=False,
  ) is None


def _answer_as_second_thinking_part(answer: str = _DRAFT) -> dict:
  """One model response that thought, then wrote its answer as more thinking.

  The reducer joins the response's thinking parts with a single paragraph
  break and keeps each part in ``_thinking_segments``; no reply text follows.
  """
  reasoning = "Same failure, so the memory limit is still too low; asking the owner."
  return {
    "type": "thinking",
    "content": f"{reasoning}\n\n{answer}",
    "_thinking_segments": [["m1:0", reasoning], ["m1:1", answer]],
  }


def test_answer_check_flags_an_answer_written_as_a_second_thinking_part():
  earlier = _closing_draft_in_thinking()[:2]
  assert _asks_for_the_answer(
    _reason([*earlier, _answer_as_second_thinking_part()], before_card=False),
  )
  # Said in the visible reply after all, or a lone thinking part: ordinary.
  said = [{"type": "text", "content": _DRAFT}, earlier[1], _answer_as_second_thinking_part()]
  assert _reason(said, before_card=False) is None
  lone = {"type": "thinking", "content": _DRAFT, "_thinking_segments": [["m1:0", _DRAFT]]}
  assert _reason([*earlier, lone], before_card=False) is None
  assert _reason([*earlier, _answer_as_second_thinking_part("Ok.")], before_card=False) is None


def test_unreviewed_answer_evidence_keeps_all_visible_text_but_only_new_work():
  blocks = [
    {"type": "text", "content": "Checking."},
    {"type": "thinking", "content": "reported already"},
    {"type": "tool", "tool": "mcp__mobius_control__request_question"},
    {"type": "text", "content": "Here is the answer."},
    {"type": "thinking", "content": "new"},
  ]
  assert answer_check.unreviewed_evidence(blocks, 0) is blocks
  assert answer_check.unreviewed_evidence(blocks, 3) == [blocks[0], blocks[3], blocks[4]]
  assert answer_check.unreviewed_evidence(None, 3) is None
  # Thinking an after-step note already spoke about is dropped; tools stay.
  assert answer_check.unreviewed_evidence(blocks, 0, 3) == [
    blocks[0], blocks[2], blocks[3], blocks[4],
  ]


def test_answer_check_after_a_step_flags_only_words_written_as_extra_thinking():
  progress = {"type": "text", "content": "Checking the preview build."}
  step = {"type": "tool", "tool": "Bash", "status": "done"}
  note = answer_check.after_step_reason([progress, _answer_as_second_thinking_part(), step])
  assert note is not None
  assert note.startswith("Möbius check after this step.")
  assert "verbatim: «Checking the preview build.»" in note
  assert _DRAFT not in note and "Same failure" not in note
  # Ordinary mid-reply shapes are left alone: one thinking part, even long or
  # with a closing paragraph, and an extra part the agent then said.
  closing_paragraph = {"type": "thinking", "content": f"Same failure.\n\n\n\n{_DRAFT}"}
  assert answer_check.after_step_reason([progress, closing_paragraph, step]) is None
  assert answer_check.after_step_reason([
    progress, _answer_as_second_thinking_part(), {"type": "text", "content": _DRAFT}, step,
  ]) is None
