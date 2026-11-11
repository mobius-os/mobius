"""Whether a Claude reply hands control back without showing the owner its point.

The owner reads only a turn's text blocks; thinking and tool activity are
folded away. Three shapes leave the owner without what the turn was for:

- an answer drafted in thinking: the reasoning that closes the reply (its
  last thinking, after its last visible text) ends with words written for the
  owner, set apart by blank lines (summarized reasoning keeps such a draft as
  its own closing paragraph), and the agent then ends as if it had said them;
  or, with no such paragraph, a short visible reply after long reasoning, such
  as "Nothing has been changed.";
- an image the agent saved to this chat's media and viewed (a screenshot or a
  Read), then never embedded: viewing is private, so a reply that says "here's
  the before and after" shows nothing;
- a link the agent fetched into tool output and then pointed to ("pay on the
  page linked above") without ever writing it in visible text.

`reason` checks these where a turn hands control back. When the agent ends its
turn, all three count. Just before it saves an owner card (a saved card ends
the turn, so a later check could never reopen it), only images and links do:
a question, approval or restart card carries the turn's closing message in its
`message`, so one without it is declined outright, with no reading of
thinking. `after_step_reason` is the narrower check run after each work step,
for words written as an extra thinking part. Both return text for the agent,
or None. They never quote thinking back: asking the model to reproduce even
summarized thinking can trigger a reasoning-extraction refusal, so the
reminders quote only the visible reply and ask for a task-grounded summary.

``blocks`` is the chat sink's current assistant segment (dicts with ``type``
``text`` / ``thinking`` / ``tool``); anything malformed reads as no evidence.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from urllib.parse import urlsplit

from app.screenshot_steps import SAVED_IMAGE_FIELD, SCREENSHOT_TOOLS

# A model that writes its answer into thinking often does it again after the
# reminder, so the caller re-arms on new evidence instead of speaking once per
# turn. The cap bounds the extra model steps if a reply keeps tripping it.
MAX_REMINDERS = 3

_VISIBLE_MAX_CHARS = 400
_VISIBLE_QUOTE_MAX_CHARS = 700
_THINKING_MIN_CHARS = 600
_DRAFT_BREAK = re.compile(r"\n[ \t]*\n[ \t]*\n")
_DRAFT_MIN_CHARS = 40
# Five words in a row shared with the visible reply mean the draft was said.
_SHARED_WORDS = 5
_IMAGE_PATH = re.compile(r"\.(?:gif|jpe?g|png|webp)$", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s\"'<>()\[\]\\`]+")
# A markdown link target shows a link, relative ones such as `/shell/?app=3`
# included.
_MARKDOWN_LINK = re.compile(r"\]\(\s*[^)\s]")
# A reply that points at where a link is ("the link below", "linked above")
# rather than one that talks about links ("I added a link to the footer",
# "the Link component").
_POINTS_TO_A_LINK = re.compile(
  r"\b(?:(?:links?|urls?)\s+(?:above|below)\b|linked\s+(?:above|below|here)\b"
  r"|following\s+(?:links?|urls?)\b)",
  re.IGNORECASE,
)
# An XML namespace (`xmlns="http://www.w3.org/2000/svg"`) is an identifier,
# never a page to open.
_XML_NAMESPACE_BEFORE = re.compile(r"xmlns(?::[\w.-]+)?\s*=\s*[\"']?$")
# Loopback addresses in tool output are the agent's own plumbing, never a link
# the owner could open.
_PRIVATE_HOST = re.compile(r"^(?:localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|::1)$")

_END_OF_TURN = (
  "Möbius end-of-turn check. The owner sees only your visible reply; your "
  "thinking and tool activity are hidden from them."
)
_BEFORE_CARD = (
  "Möbius check before your card is saved. The card was NOT saved. The owner "
  "sees only your visible reply; your thinking and tool activity are hidden "
  "from them."
)
_MISSING_ANSWER = (
  "Your visible reply may not yet answer the owner's latest message or "
  "report the result. {seen} Re-read that message and the verified task "
  "evidence. If something is missing, write a brief explanation of the "
  "conclusion or a summary of actions taken. If it already answers the "
  "owner, leave it."
)
_SEEN = "Everything the owner sees from this reply, verbatim: «{visible}»."
_SEEN_EXCERPT = "The end of the owner's visible reply, verbatim: «…{visible}»."
_SEEN_NOTHING = "The owner sees no text at all from this reply."
_NO_CARD_MESSAGE = (
  "This card has no message, and it is saved only with one. Saving it ends "
  "your turn, so its `message` carries your closing message, shown to the "
  "owner above the card: what you did, where things stand, and what the card "
  "decides."
)
_IMAGES = (
  "You viewed {images} this turn, but your visible reply never shows {them}. "
  "If the owner should see {them} (you described {them}, promised to show "
  "{them}, or are asking about {them}), embed each now in visible text as "
  "![short description](/api/chats/{chat_id}/media/<file name>). If {them_was} "
  "only for your own checking, leave {them} out."
)
_LINKS = (
  "Your visible reply mentions a link but shows none, while this turn's tool "
  "output contained one ({hosts}). Tool output is hidden from the owner, so a "
  "link there is not \"above\" for them. If the owner needs a link, write it "
  "in visible text as [short label](full address). If you meant something "
  "else, leave it."
)
_END_OF_TURN_NEXT = (
  "Do not mention this check. If nothing is missing, end your turn without "
  "writing anything more."
)
_BEFORE_CARD_NEXT = (
  "Do not mention this check. Then call the same card tool again with the "
  "same arguments; this check speaks again only about something new."
)
_BEFORE_MESSAGE_CARD_NEXT = (
  "Do not mention this check. Put any needed explanation, image or link in "
  "the card's message and call the same card tool again, keeping its decision "
  "arguments unchanged; this check speaks again only about something new."
)
# Attached to a work step's result (never blocking it), so a note the model
# wrote as thinking mid-reply is caught without costing an extra model step.
_AFTER_STEP = (
  "Möbius check after this step. The owner sees only your visible reply; your "
  "thinking and tool activity are hidden from them. Before this step you may "
  "have meant to tell the owner something, but no visible text reached them. "
  "{seen} If the owner should know something from that step, write a brief "
  "visible note now, then continue. Otherwise just continue. Do not mention "
  "this check."
)


def reason(
  blocks: object,
  *,
  chat_id: str,
  media_dir: str,
  before_card: bool,
  reminded: bool = False,
  last_assistant_message: object = None,
  message_card: bool = False,
) -> str | None:
  """What the owner may be missing from this reply, phrased for the agent.

  ``last_assistant_message`` is pending visible text: the CLI's final text at
  Stop, or a card's closing message before its save. ``message_card`` marks a
  card that carries that message (all but secret cards): one without it is
  declined, and repairs are directed into it. Before a card, thinking is not
  read at all; the message rule covers a closing message left out. Once a
  reminder has spoken this turn (``reminded``), the length rule stays quiet:
  it has no new evidence, and a reply that ends with more reasoning and
  nothing more to say, as the reminder allows, must not be returned again for
  it.
  """
  if not isinstance(blocks, list):
    return None
  dispatched = _dispatched_text(blocks)
  visible = _with_undispatched_tail(dispatched, last_assistant_message)
  parts: list[str] = []
  if before_card:
    if message_card and not (
      isinstance(last_assistant_message, str) and last_assistant_message.strip()
    ):
      parts.append(_NO_CARD_MESSAGE)
  elif (
    # Final text the stream reader has not dispatched yet comes after every
    # thinking block here, so nothing in thinking closes the reply.
    (visible == dispatched and _has_unsaid_draft(
      _closing_thinking(blocks), visible, _closing_draft_candidates,
    ))
    or (not reminded and _answer_may_be_only_in_thinking(blocks, visible))
  ):
    parts.append(_MISSING_ANSWER.format(seen=_seen(visible)))
  images = _unshown_viewed_images(blocks, media_dir, visible)
  if images:
    one = len(images) == 1
    parts.append(_IMAGES.format(
      images=", ".join(images),
      them="it" if one else "them",
      them_was="it was" if one else "they were",
      chat_id=chat_id,
    ))
  link_hosts = _tool_output_link_hosts_the_reply_points_to(blocks, visible)
  if link_hosts:
    parts.append(_LINKS.format(hosts=", ".join(link_hosts)))
  if not parts:
    return None
  if before_card:
    next_step = _BEFORE_MESSAGE_CARD_NEXT if message_card else _BEFORE_CARD_NEXT
    return " ".join([_BEFORE_CARD, *parts, next_step])
  return " ".join([_END_OF_TURN, *parts, _END_OF_TURN_NEXT])


def after_step_reason(blocks: object) -> str | None:
  """A note for the agent when the step it just took swallowed its words.

  Mid-reply only the narrow two-thinking-parts signal counts: long reasoning,
  unshown images, and unmentioned links are normal before a reply is done.
  ``blocks`` should already be narrowed to thinking no check has spoken about.
  """
  if not isinstance(blocks, list):
    return None
  visible = _dispatched_text(blocks)
  if not _has_unsaid_draft(_thinking_since_text(blocks), visible, _extra_thinking_part):
    return None
  return _AFTER_STEP.format(seen=_seen(visible))


def unreviewed_evidence(
  blocks: object, reviewed_through: int, thinking_reviewed_through: int = 0,
) -> object:
  """This segment as a check after an earlier one spoke should see it.

  Every text block stays, because all of it is on the owner's screen. Blocks
  before ``reviewed_through`` were covered by a reminder, and thinking before
  ``thinking_reviewed_through`` by any check that spoke (a reminder or an
  after-step note); both are dropped, so a check speaks again only about
  something new and one swallowed draft is pointed out once.
  """
  if not isinstance(blocks, list) or max(reviewed_through, thinking_reviewed_through) <= 0:
    return blocks
  return [
    block for index, block in enumerate(blocks)
    if _is_text(block) or (
      index >= reviewed_through
      and not (index < thinking_reviewed_through and _is_thinking(block))
    )
  ]


def _is_text(block: object) -> bool:
  return (
    isinstance(block, dict) and block.get("type") == "text"
    and isinstance(block.get("content"), str)
  )


def _is_thinking(block: object) -> bool:
  return (
    isinstance(block, dict) and block.get("type") == "thinking"
    and isinstance(block.get("content"), str)
  )


def _dispatched_text(blocks: list) -> str:
  return "".join(block["content"] for block in blocks if _is_text(block))


def _with_undispatched_tail(dispatched: str, last_assistant_message: object) -> str:
  if isinstance(last_assistant_message, str):
    tail = last_assistant_message.strip()
    if tail and tail not in dispatched:
      return dispatched + "\n" + tail
  return dispatched


def _seen(visible: str) -> str:
  """What the owner can read of this reply, quoted (bounded) for a reminder."""
  visible = " ".join(visible.split())
  if not visible:
    return _SEEN_NOTHING
  template = _SEEN_EXCERPT if len(visible) > _VISIBLE_QUOTE_MAX_CHARS else _SEEN
  return template.format(visible=visible[-_VISIBLE_QUOTE_MAX_CHARS:])


def _closing_thinking(blocks: list) -> list[dict]:
  """The reasoning that closes the reply: its last thinking, after its last text.

  Earlier reasoning followed by more steps and more reasoning was working
  toward those steps, not replying. Steps after the last thinking do not
  disqualify it: an agent often drafts its answer, saves a progress note, and
  then hands back.
  """
  closing = _thinking_since_text(blocks)
  return closing[-1:]


def _thinking_since_text(blocks: list) -> list[dict]:
  last = max(
    (index for index, block in enumerate(blocks)
     if _is_text(block) and block["content"].strip()),
    default=-1,
  )
  return [block for block in blocks[last + 1:] if _is_thinking(block)]


def _has_unsaid_draft(
  thinking: list[dict], visible: str, drafts_in: Callable[[dict], list[str]],
) -> bool:
  """Whether a draft in these thinking blocks has not reached the visible reply.

  A draft counts as said when any run of a few words from it appears in the
  visible reply, whitespace aside. Return only the signal, never thinking
  excerpts that a reminder could inadvertently send back to the model.
  """
  visible = " ".join(visible.split())
  for block in thinking:
    for draft in drafts_in(block):
      if len(draft) < _DRAFT_MIN_CHARS:
        continue
      words = draft.split()
      runs = (
        " ".join(words[start:start + _SHARED_WORDS])
        for start in range(max(1, len(words) - _SHARED_WORDS + 1))
      )
      if not any(run in visible for run in runs):
        return True
  return False


def _closing_draft_candidates(block: dict) -> list[str]:
  """Words in one thinking block that may have been meant for the owner.

  The closing paragraph set apart by blank lines, plus the answer written as
  an extra thinking part (`_extra_thinking_part`).
  """
  candidates: list[str] = []
  paragraphs = _DRAFT_BREAK.split(block["content"].strip())
  if len(paragraphs) >= 2:
    candidates.append(" ".join(paragraphs[-1].split()))
  return candidates + _extra_thinking_part(block)


def _extra_thinking_part(block: dict) -> list[str]:
  """The last thinking part of a response that thought twice without replying.

  The narrowest swallowed-answer signal: in the raw Claude sessions examined,
  every response with two thinking parts back to back and no reply text was an
  answer the owner never saw, which makes it precise enough to check after
  every work step. The live block keeps those parts in ``_thinking_segments``
  (stripped before persistence).
  """
  segments = block.get("_thinking_segments")
  if not isinstance(segments, list) or len(segments) < 2:
    return []
  last = segments[-1]
  if isinstance(last, (list, tuple)) and len(last) == 2 and isinstance(last[1], str):
    return [" ".join(last[1].split())]
  return []


def _answer_may_be_only_in_thinking(blocks: list, visible: str) -> bool:
  """Whether this reply's reasoning dwarfs what the owner can actually read."""
  if len(visible.strip()) >= _VISIBLE_MAX_CHARS:
    return False
  longest_thinking = max(
    (len(block["content"].strip()) for block in blocks if _is_thinking(block)),
    default=0,
  )
  return longest_thinking >= _THINKING_MIN_CHARS


def _viewed_media_image(block: object, media_dir: str) -> str | None:
  """The chat-media image file name this tool block showed the agent, if any.

  A screenshot step names the picture it saved; a Read names a path, which
  counts only when it is an image directly in this chat's media — the files
  that exist to be shown. Images elsewhere (``/tmp`` verification frames, the
  owner's own uploads) are not something the owner is waiting to see.
  """
  if not isinstance(block, dict) or block.get("type") != "tool":
    return None
  tool = block.get("tool")
  if tool in SCREENSHOT_TOOLS:
    saved = block.get(SAVED_IMAGE_FIELD)
    return saved if isinstance(saved, str) and saved else None
  if tool != "Read":
    return None
  raw = block.get("input")
  if isinstance(raw, dict):
    raw = raw.get("file_path")
  if not isinstance(raw, str) or not _IMAGE_PATH.search(raw):
    return None
  path = os.path.normpath(raw)
  if os.path.dirname(path) != media_dir:
    return None
  return os.path.basename(path)


def _unshown_viewed_images(blocks: list, media_dir: str, visible: str) -> list[str]:
  """Chat-media images the agent viewed this segment but never embedded."""
  unshown: list[str] = []
  for block in blocks:
    name = _viewed_media_image(block, media_dir)
    if name and f"/media/{name}" not in visible and name not in unshown:
      unshown.append(name)
  return unshown


def _tool_output_link_hosts_the_reply_points_to(blocks: list, visible: str) -> list[str]:
  """Hosts of links seen only in tool output when the reply points to a link.

  Fires only when the visible reply points at a link yet shows none at all (no
  web address, no markdown link): tool output routinely holds addresses nobody
  needs, so their mere presence says nothing; a reply that points to one and
  shows none does. Only the host is named, never userinfo such as a token in
  ``https://user:token@host``.
  """
  if (
    not _POINTS_TO_A_LINK.search(visible)
    or _URL.search(visible) or _MARKDOWN_LINK.search(visible)
  ):
    return []
  hosts: list[str] = []
  for block in blocks:
    if not isinstance(block, dict) or block.get("type") != "tool":
      continue
    output = block.get("output")
    if not isinstance(output, str):
      continue
    for match in _URL.finditer(output):
      if _XML_NAMESPACE_BEFORE.search(output[max(0, match.start() - 40):match.start()]):
        continue
      try:
        host = urlsplit(match.group(0)).hostname
      except ValueError:
        continue
      if host and not _PRIVATE_HOST.match(host) and host not in hosts:
        hosts.append(host)
  return hosts[:3]
