"""Live chat event ownership and durable stream boundaries.

One ``ChatEventSink`` owns the provider-event reduction, live broadcast, and
single-writer transcript commands for a running turn. Keeping the live-sink
registry beside it makes steering and diagnostics operate on the same owner
rather than reaching through the broader chat scheduler.
"""

import copy
import time
import uuid
from datetime import UTC, datetime

from app.agent_lifecycle import normalize_chat_event
from app.broadcast import get_broadcast
from app.chat_logging import get_logger as _get_logger
from app.chat_writer import (
  AppendSteeredUserMessage,
  Finalize,
  PersistError,
  PersistTranscript,
  QuestionCommit,
  RecordAgentLifecycle,
  StashThinkingTrace,
  StashToolOutput,
  await_ack as _await_ack,
  cid_of,
  get_writer,
)
from app.events import (
  THINKING_INLINE_THRESHOLD,
  TOOL_OUTPUT_INLINE_THRESHOLD,
  blocks_have_renderable_content,
  build_assistant_message,
  capture_question_scrub,
  commit_question_scrub,
  excerpt_tool_output,
  process_event,
  tool_output_exit_code,
  undo_question_scrub,
)
from app.memory_recall import (
  RecallBinding, recall_from_command, settle_recall,
)
from app.runtime_types import ChatEvent


_active_sinks: dict[str, "ChatEventSink"] = {}


def _pause_note(
  message: str,
  *,
  kind: str | None = None,
  resets_at: str | None = None,
  resumable: bool = True,
) -> dict:
  """Build the ONE error-block/event shape every pause producer emits.

  A pause folds its whole classification into a single `pause` descriptor on
  the block: `kind` names the family ('restart' | 'rate_limit' |
  'usage_limit'), and `resets_at` (an explicit-UTC ISO string, present only
  for the limit kinds) is the reset time the card renders. Absorbing the reset
  reason into `kind` keeps the wire at two block keys — `resumable` + `pause` —
  no matter how many pause facts exist, so the passthrough never grows a field
  again (events.ERROR_PASSTHROUGH_FIELDS stays `('resumable', 'pause')`).

  `resumable` is a SEPARATE top-level flag on purpose: it is the orthogonal
  Resume-button affordance, independent of whether the pause is benign — a
  genuine error can be resumable, and a benign pause behind a still-open
  question card is not (its answer is the affordance). A note with no `kind`
  carries no `pause` and renders as the plain error family.
  """
  note: dict = {"type": "error", "message": message}
  if resumable:
    note["resumable"] = True
  if kind is not None:
    pause: dict = {"kind": kind}
    if resets_at is not None:
      pause["resets_at"] = resets_at
    note["pause"] = pause
  return note


def register_active_sink(chat_id: str, sink: "ChatEventSink") -> None:
  """Publish the live sink for `chat_id` so the steer route can reach it."""
  _active_sinks[chat_id] = sink


def get_active_sink(chat_id: str) -> "ChatEventSink | None":
  """Return the live sink for `chat_id`, or None when no turn is streaming."""
  return _active_sinks.get(chat_id)


def active_sink_stream_snapshot(chat_id: str, broadcast) -> list[dict] | None:
  """Freeze the current assistant items for one snapshot-capable subscriber.

  The sink is the reduction authority for a running turn: every content event
  reaches ``assistant_blocks`` before it reaches the broadcast.  Pairing by
  broadcast identity prevents a late sink from seeding a successor turn (or a
  successor sink from seeding a completed broadcast during teardown). ``None``
  means the caller must use ordinary event-log replay; an empty list is a valid
  live snapshot before the assistant has produced its first item.
  """
  sink = get_active_sink(chat_id)
  if sink is None or sink.bc is not broadcast:
    return None
  return copy.deepcopy(sink.assistant_blocks)


def unregister_active_sink(chat_id: str, sink: "ChatEventSink") -> None:
  """Drop the live sink for `chat_id`, identity-keyed.

  Only clears when `sink` still owns the slot, so a turn ending after a
  successor turn already re-registered can't strand the successor's sink.
  """
  if _active_sinks.get(chat_id) is sink:
    _active_sinks.pop(chat_id, None)


def active_sink_memory_diagnostics(*, include_payloads: bool = True) -> list[dict]:
  """Describe live response payloads without exposing their content."""
  from app.memory_observability import estimate_payload_bytes

  diagnostics = []
  for chat_id, sink in list(_active_sinks.items()):
    payload_bytes = None
    if include_payloads:
      try:
        payload_bytes = estimate_payload_bytes(sink.assistant_blocks)
      except RuntimeError:
        pass
    diagnostics.append({
      "chat_id": chat_id,
      "assistant_block_count": len(sink.assistant_blocks),
      "assistant_payload_bytes": payload_bytes,
      "pending_lifecycle_writes": len(sink._lifecycle_writes),
    })
  return diagnostics


def steered_into_turn_event(stored_messages: list[dict]) -> dict:
  """Build the `steered_into_turn` SSE payload for a batch of steered rows.

  `steered_into_turn` is the AUTHORITATIVE CUT and the client's ONLY "seal the
  live stream here and re-base it" signal. It means the transcript split has
  COMMITTED: A1 sealed, these rows appended after it, the sink reset for A2. So
  it may only be published from the instant the split really happens — by the
  live provider handle through the owning sink, after provider acknowledgement
  and ``split_for_steer`` both complete.

  Publishing it at HTTP arrival on a deferred path is what made a
  steer paint duplicated output for the rest of the turn: every block the runner
  streamed between arrival and the real seal was accumulated into the sealed A1
  AND left at the head of the client's freshly re-based stream. The deferred
  path publishes NOTHING at arrival — the 202's own `pending_messages` is what
  keeps the accepted row visible until the cut, so there is no second channel
  reconciling the same tray.
  """
  return {
    "type": "steered_into_turn",
    "messages": [
      {
        "role": "user",
        "ts": msg.get("ts"),
        "cid": cid_of(msg),
        "content": msg.get("content", ""),
        **({"attachments": msg.get("attachments")} if msg.get("attachments") else {}),
      }
      for msg in stored_messages
    ],
    # Backward-compatible shape for any existing client still expecting a
    # single steered row.
    "ts": stored_messages[-1].get("ts"),
    "content": stored_messages[-1].get("content", ""),
  }


def steer_delivery_failed_event(consume_pending_cids: list[str]) -> dict:
  """Tell the client that an admitted steer stayed in the durable queue.

  Provider steering is settled by the live runner after the HTTP request has
  returned. If that provider-side delivery fails or the turn ends first, the
  rows remain in ``pending_messages`` and are still safe, but the client must
  release their temporary "being steered" presentation reservation and show
  them as ordinary queued work again.
  """
  return {
    "type": "steer_delivery_failed",
    "consume_pending_cids": list(consume_pending_cids),
  }


class ChatEventSink:
  """Bridges SDK-runner events to broadcast + the chat-writer actor.

  SDK runners publish Möbius events via `sink.publish(event)`. The
  sink forwards each event to the real broadcast, accumulates assistant
  content blocks for the message-in-progress, captures
  `session_id` + `cost_usd` from terminal events, and routes every
  transcript write through the single-writer actor (`chat_writer`) keyed
  on `(chat_id, run_token)`. This keeps SDK runners pure (one-way SDK →
  events) while the chat-side state stays here.

  Lifetime: one sink per `_run_chat_impl` call. After the runner
  returns, the chat-impl wrapper awaits `finalize()`, which submits the
  terminal `Finalize` to the actor and awaits its ack before the turn's
  queue-drain / continuation runs.

  Why the actor (this is the C2 activation): the streaming save is a
  `db.commit()` against SQLite. With `busy_timeout=5000` (database.py) a
  commit under write contention can block its thread for up to 5s.
  Running that on the event loop stalled every other chat's SSE; running
  it inline on the request session re-introduced the lost-update race
  (the actor builds a snapshot from an old read while a request commits
  an answer, then the actor's stale snapshot clobbers it). The actor is
  the SOLE runtime mutator of both JSON blobs (`messages`,
  `pending_messages`), so the blocking commit is off-loop AND
  serialized: no lost update, no SSE stall.

  Write semantics:
    - ordinary events (text/tool/etc.) → `PersistTranscript`
      (coalescible, fire-and-forget; a later snapshot or Finalize
      repairs a dropped write — a done-callback logs an exception so a
      failure is visible);
    - `error` → `PersistError` (fire-and-forget, non-coalescing);
    - `question` is REJECTED by `publish()` — it must go through
      `publish_question()` (save-before-broadcast), so a runner can't
      bypass the QuestionCommit barrier;
    - private helper lifecycle facts are buffered and fenced (with one retry)
      by `finalize()` because no transcript snapshot can reconstruct them;
    - `finalize()` → `Finalize` (commit-before-ack: the queue only
      drains / a continuation only schedules once the terminal state is
      durable).
  """

  _SAVE_INTERVAL_SECS = 1.0
  # Subset of app.events.EventType that forces a save so the user does
  # not reconnect into a stale transcript mid-turn. Each is a
  # fire-and-forget PersistTranscript / PersistError. (`question` is not
  # here: publish() rejects question events outright — they go through
  # publish_question()'s save-before-broadcast barrier instead.)
  _IMMEDIATE_SAVE_TYPES = frozenset(
    {"tool_start", "tool_end", "task_start", "task_done", "error"}
  )

  def __init__(
    self,
    bc,
    chat_id: str,
    run_token: str | None = None,
    *,
    recall_binding: RecallBinding,
  ):
    self.bc = bc
    self.chat_id = chat_id
    # Which app's recall receipts this turn will honor, resolved ONCE by the
    # caller while its session is live. Required rather than defaulted: a sink
    # that silently fell back to "no provider" would drop every citation for
    # the turn and look identical to "the agent never looked".
    self._recall_binding = recall_binding
    # Per-turn run identity, allocated by the scheduler and threaded in
    # via `_run_chat_impl`. The sink stamps it on every writer-actor
    # command so the actor coalesces/fences this turn's snapshots under
    # `(chat_id, run_token)`. `""` for a tokenless legacy/test caller —
    # the actor tolerates an empty token (its own key).
    self.run_token = run_token
    self.assistant_blocks: list = []
    self.session_id: str | None = None
    self.cost_usd: float | None = None
    self._last_save = 0.0
    # The last error message published via publish() during this turn, or None.
    # Used by finalize(): a turn that errors before accumulating any content
    # (auth failure, connect timeout) leaves assistant_blocks empty, so the
    # normal finalize no-op fires and the error is never persisted — it exists
    # only in the 30s in-memory event log. On reconnect the failure is
    # invisible. When blocks are empty but _last_error is set, finalize()
    # synthesizes a minimal error block so the turn is durable.
    self._last_error: str | None = None
    # True only for the duration of `split_for_steer`. While set, `publish()`
    # still broadcasts and accumulates the continuation's blocks, but does NOT
    # submit a transcript snapshot — a snapshot landing mid-split would target
    # the still-trailing pre-steer assistant message (A1) and overwrite it
    # with continuation text before A1 is sealed and the steered user row is
    # appended. Cleared once the split's transcript writes have committed, so
    # the next snapshot (or the terminal finalize) appends the continuation as
    # a fresh assistant message.
    self._steering = False
    self._lifecycle_writes: list[tuple[RecordAgentLifecycle, object]] = []

  def _prepare_thinking_event(self, event: ChatEvent) -> None:
    """Give a reasoning run stable identity before reducer + broadcast."""
    if event.get("type") != "thinking":
      return
    last = self.assistant_blocks[-1] if self.assistant_blocks else None
    if (
      last
      and last.get("type") == "thinking"
      and not last.get("_thinking_closed")
      and last.get("thinking_id")
    ):
      event["thinking_id"] = last["thinking_id"]
    else:
      event["thinking_id"] = event.get("thinking_id") or (
        f"think-{uuid.uuid4().hex}"
      )

  def _deferred_snapshot(
    self, blocks: list, *, complete_all: bool = False,
  ) -> tuple[dict, list[StashThinkingTrace]]:
    """Build a bounded transcript snapshot plus its full-text sidecars."""
    snapshot = copy.deepcopy(build_assistant_message(blocks))
    stashes: list[StashThinkingTrace] = []
    for source, persisted in zip(
      [b for b in blocks if b.get("type") != "text_boundary"],
      snapshot.get("blocks") or [],
    ):
      if source.get("type") != "thinking":
        continue
      content = str(source.get("content") or "")
      if len(content) <= THINKING_INLINE_THRESHOLD:
        continue
      thinking_id = source.get("thinking_id") or f"think-{uuid.uuid4().hex}"
      source["thinking_id"] = thinking_id
      revision = len(content)
      complete = bool(complete_all or source.get("_thinking_closed"))
      persisted.clear()
      persisted.update({
        "type": "thinking",
        "thinking_id": thinking_id,
        "thinking_deferred": True,
        "thinking_revision": revision,
        "thinking_complete": complete,
        "duration_ms": source.get("duration_ms", 0),
      })
      stashes.append(StashThinkingTrace(
        chat_id=self.chat_id,
        thinking_id=thinking_id,
        content=content,
        revision=revision,
        complete=complete,
      ))
    return snapshot, stashes

  def _submit_fire_and_forget(self, cmd) -> None:
    """Submit a fire-and-forget transcript write; log a failed ack.

    `PersistTranscript` / `PersistError` are coalescible / non-terminal:
    a dropped write is repaired by a later snapshot or the terminal
    `Finalize`, so the caller does NOT await the ack. But a silently
    failing ack would hide a real persistence problem, so attach a
    done-callback that logs the exception (a `None` result — a coalesced
    snapshot superseded before it committed — is the normal drop and is
    not logged).
    """
    ack = get_writer().submit(cmd)

    def _log_if_failed(fut, _kind=type(cmd).__name__, _cid=self.chat_id):
      try:
        fut.result()
      except Exception:
        _get_logger().exception(
          "chat writer %s ack failed chat_id=%s (a later snapshot/"
          "Finalize repairs)", _kind, _cid,
        )

    ack.add_done_callback(_log_if_failed)

  def _memory_recall_for_tool(self, tool_use_id) -> dict | None:
    """Return the input-time Memory marker for this tool, if there is one.

    Read-only by design: resolving the block is a question, not the place to
    adopt a legacy id (`process_event` still owns that a moment later). Only a
    tool whose COMMAND named memory_search may go on to cite notes, so output
    text alone can never mint a citation.
    """
    for blk in reversed(self.assistant_blocks):
      if blk.get("type") != "tool":
        continue
      if tool_use_id:
        if blk.get("tool_use_id") == tool_use_id:
          recall = blk.get("recall")
          return recall if isinstance(recall, dict) else None
        continue
      # Legacy events without an id: the newest still-open tool is the only
      # safe candidate, matching `_tool_block_for_event`'s fallback.
      if blk.get("status") != "done":
        recall = blk.get("recall")
        return recall if isinstance(recall, dict) else None
    return None

  def _tool_was_memory_recall(self, tool_use_id) -> bool:
    return self._memory_recall_for_tool(tool_use_id) is not None

  def _stamp_memory_recall(self, event: ChatEvent) -> None:
    """Name a Memory-app recall on the event, in two lifecycle phases.

    The documented simple command identifies the lookup, so the live turn can
    say it is remembering while the search runs. Only the final output event
    settles it from the Memory app's structured result; streaming deltas cannot
    prematurely claim success, emptiness, or failure.
    """
    if event.get("type") in ("tool_start", "tool_input"):
      if event.get("type") == "tool_start" and event.get("tool") != "Bash":
        return
      # Both a tool_start AND a tool_input can arrive for one tool call on the
      # Claude runner. Stamp the command-derived marker exactly once per block:
      # if the block for this tool_use_id already carries a recall marker, leave
      # it settled and skip. (Codex has no tool_input; Claude's tool_start input
      # is empty, so in practice only one phase produces a marker — this keeps a
      # future runner that populates both from double-stamping.)
      if self._tool_was_memory_recall(event.get("tool_use_id")):
        return
      recall = recall_from_command(event.get("input"), self._recall_binding)
      if recall is not None:
        event["recall"] = recall
      return
    pending = self._memory_recall_for_tool(event.get("tool_use_id"))
    if event.get("output_complete") and pending is not None:
      event["recall"] = settle_recall(
        pending, event.get("content"), event.get("output_exit_code"),
      )

  def _reduce_tool_output(self, event: ChatEvent) -> bool:
    """Move a large tool_output's full text OFF the wire (contract rule 6).

    This is the single funnel where the live SSE push, the catch-up event_log,
    and the persisted Chat.messages blob all branch from one event object, so
    rewriting the event here bounds all three at once and keeps the live and
    replayed excerpts byte-identical by construction.

    Rewrites the event to a bounded head+tail excerpt and stamps
    `output_truncated` / `output_full_len` / `output_exit_code` / `tool_use_id`,
    then stashes the FULL text via the writer actor keyed by tool_use_id.

    One pass-through leaves the event unchanged: a small output (<= threshold),
    where a fetch round-trip costs more than the bytes.

    Both SDK runners tag every tool block's id now, so a large tool_output with
    NO tool_use_id is unexpected. Rather than keep the full text inline (the
    retired dual-read `?ts=&i=` fallback), mint a stash id, log loudly, and still
    reduce+stash under it — the smallest behavior that can't silently ship a fat
    block with no fetchable full text.

    The stash is submitted UNCONDITIONALLY — never gated on `_steering` (unlike
    the transcript save in `publish`) — so a tool that completes during a steer
    split does not strand a truncated block with no fetchable full text."""
    content = event.get("content")
    if (not isinstance(content, str)
        or len(content) <= TOOL_OUTPUT_INLINE_THRESHOLD):
      return False
    if not self.chat_id:
      # No chat to key a stash by (a detached/synthetic sink — chat_id is always
      # set on the live path). Can't move the text off-wire safely, so leave it.
      return False
    tool_use_id = event.get("tool_use_id")
    if not tool_use_id:
      # Unexpected post-card-221: mint a stash id and stamp it on the event so
      # the block fetches the full text by id via /tool-output/{tool_use_id}.
      tool_use_id = f"synth-{uuid.uuid4().hex}"
      event["tool_use_id"] = tool_use_id
      _get_logger().warning(
        "large tool_output arrived with no tool_use_id chat_id=%s; "
        "minted stash id %s", self.chat_id, tool_use_id,
      )
    full = content
    excerpt, full_len, parsed_exit_code = excerpt_tool_output(full)
    event["content"] = excerpt
    event["output_truncated"] = True
    event["output_full_len"] = full_len
    # Codex can supply a typed exit code independently of its display text.
    # That runner-owned fact outranks best-effort parsing of the excerpt.
    typed_exit_code = event.get("output_exit_code")
    if not isinstance(typed_exit_code, int) or isinstance(typed_exit_code, bool):
      event["output_exit_code"] = parsed_exit_code
    self._submit_fire_and_forget(
      StashToolOutput(
        chat_id=self.chat_id, tool_use_id=tool_use_id, output=full,
      )
    )
    return True

  def record_lifecycle(self, event: dict) -> None:
    """Queue private lifecycle metadata without broadcasting it.

    Unlike coalescible transcript snapshots, these append-only facts cannot be
    reconstructed by Finalize. Their acknowledgements are retained and fenced
    at turn finalization, with one idempotent retry on failure.
    """
    if not (self.chat_id and self.run_token):
      return
    lifecycle = normalize_chat_event(
      chat_id=self.chat_id,
      chat_run_id=self.run_token,
      event=event,
      observed_at=datetime.now(UTC),
    )
    if lifecycle is not None:
      cmd = RecordAgentLifecycle(values=lifecycle)
      ack = get_writer().submit(cmd)
      self._lifecycle_writes.append((cmd, ack))

  async def _flush_lifecycle(self) -> None:
    pending = self._lifecycle_writes
    self._lifecycle_writes = []
    for cmd, ack in pending:
      try:
        await _await_ack(ack)
      except Exception:
        _get_logger().warning(
          "agent lifecycle write failed; retrying once chat_id=%s",
          self.chat_id, exc_info=True,
        )
        # RecordAgentLifecycle is event-key idempotent, so retrying after an
        # ambiguous timeout cannot duplicate a committed fact. A fresh command
        # is essential: writer commands own their ack Future, and resubmitting
        # the failed object would just return that already-failed Future.
        retry = RecordAgentLifecycle(values=cmd.values)
        await _await_ack(get_writer().submit(retry))

  def publish(self, event: ChatEvent) -> bool:
    """Publishes an ordinary event and routes any due save to the actor.

    Live broadcast is best-effort independent from persistence and
    always happens here, synchronously, so SSE ordering is preserved.
    The blocking `db.commit()` runs on the actor thread (off-loop,
    serialized), submitted fire-and-forget so the loop never waits.

    `question` events are a programming error here — they must go
    through `publish_question()` so the QuestionCommit save-before-
    broadcast barrier can't be bypassed. Returns True (the bool is
    vestigial now that no commit runs inline; kept so the runner's
    call-site contract is unchanged).
    """
    event_type = event.get("type")
    assert event_type != "question", (
      "question events must go through publish_question(), not publish()"
    )

    # Contract rule 6: reduce a large tool_output to a bounded excerpt and stash
    # its full text BEFORE process_event (which copies content onto the block)
    # and before the broadcast below, so the rewritten event is the single
    # source feeding the persisted block, the live wire, and the catch-up log.
    #
    # Reduce first so a large JSON envelope is parsed only once. The app prints
    # its bounded structured Memory result last, so the carved tail still
    # contains the line that settles a recognized lookup.
    output_reduced = False
    if event_type == "tool_output":
      output_reduced = self._reduce_tool_output(event)
      if not output_reduced and event.get("output_exit_code") is None:
        exit_code = tool_output_exit_code(event.get("content"))
        if exit_code is not None:
          event["output_exit_code"] = exit_code
    if event_type in ("tool_start", "tool_input", "tool_output"):
      self._stamp_memory_recall(event)
    if event_type == "thinking":
      self._prepare_thinking_event(event)

    # Accumulate the event into assistant_blocks and decide whether a
    # save is due (immediate for save-triggering types, throttled
    # otherwise).
    accumulated = process_event(event, self.assistant_blocks)
    if event_type == "thinking" and self.assistant_blocks:
      thought = self.assistant_blocks[-1]
      content = str(thought.get("content") or "")
      if len(content) > THINKING_INLINE_THRESHOLD:
        # The reducer keeps the full run privately for persistence. The public
        # event log/SSE gets only stable identity + version after the cutoff;
        # this crossing event tells the client to discard its <=1KB prefix.
        event["content"] = ""
        event["thinking_deferred"] = True
        event["thinking_revision"] = len(content)
        event["duration_ms"] = thought.get("duration_ms", 0)
    # `not self._steering`: a snapshot submitted mid-split would replace the
    # still-trailing pre-steer assistant message (A1) with continuation text
    # before A1 is sealed and the steered user row is appended. The split's
    # own transcript writes carry the durable state across this window; once
    # it completes the next snapshot appends the continuation cleanly.
    needs_save = accumulated and self.chat_id and self.run_token and (
      not self._steering
    ) and (
      event_type in self._IMMEDIATE_SAVE_TYPES
      or time.monotonic() - self._last_save >= self._SAVE_INTERVAL_SECS
    )

    # Track the most recent error message so finalize() can synthesize a
    # durable error block when the turn produced no assistant content at all
    # (e.g. auth failure or connect timeout before any text arrived).
    if event_type == "error":
      self._last_error = event.get("message") or "An error occurred."

    self.bc.publish(event)

    # done: capture cost.
    if event_type == "done":
      self.cost_usd = event.get("cost_usd")

    # Route the due save to the actor AFTER broadcast. An `error` is a
    # non-coalescing PersistError (it must not be collapsed away by a
    # later text snapshot); everything else is a coalescible
    # PersistTranscript. Both fire-and-forget — the off-loop commit can't
    # stall the stream, and a dropped write is repaired by a later
    # snapshot or the terminal Finalize.
    #
    # Deep-copy is load-bearing: build_assistant_message can alias the
    # live block dicts (process_event mutates those dicts in place). The
    # actor reads the snapshot on its own thread; copying here means it
    # reads a frozen value no later publish()/process_event on the loop
    # can mutate underneath it. Snapshots are <=1/sec (throttle) and
    # tiny next to a commit, so the copy is free.
    if needs_save:
      self._last_save = time.monotonic()
      snapshot, stashes = self._deferred_snapshot(self.assistant_blocks)
      if event_type == "error":
        self._submit_fire_and_forget(
          PersistError(
            chat_id=self.chat_id, run_token=self.run_token, snapshot=snapshot,
            thinking_stashes=stashes,
          )
        )
      else:
        self._submit_fire_and_forget(
          PersistTranscript(
            chat_id=self.chat_id, run_token=self.run_token, snapshot=snapshot,
            thinking_stashes=stashes,
          )
        )
    return True

  async def finalize(self) -> None:
    """Submit the terminal assistant-message write and await its ack.

    Runs once per turn AFTER the runner's stream loop returns, BEFORE the
    queue drain / continuation. `Finalize` is commit-before-ack and
    must-persist: the actor force-completes any running tool block and
    writes the terminal snapshot, raising (failing the ack) if the write
    did not land. The caller (`_run_chat_impl`) awaits this and, on a
    failed ack, emits a transport-only error + `done` and does NOT
    promote the queue or schedule a continuation (the run marker is left
    set for reconciliation to repair) — see the design's failure
    semantics. No fallback direct write.

    No-op when there's nothing to finalize (no chat_id, no token, and
    no accumulated blocks AND no recorded error — a truly empty turn).
    When blocks are empty but _last_error is set (a turn that errored before
    any content arrived — auth failure, connect timeout, provider error),
    synthesize a minimal error block so the turn is durably persisted rather
    than vanishing from the transcript after the 30s in-memory event log expires.
    The error block shape matches the renderer's "error" branch (see
    reconcile_interrupted_chats and MsgContent.jsx: keyed on block["message"]).
    """
    if not (self.chat_id and self.run_token):
      return
    await self._flush_lifecycle()
    if not blocks_have_renderable_content(self.assistant_blocks):
      if self._last_error:
        # Synthesize an error block so the failure is durable in the transcript.
        blocks = [{"type": "error", "message": self._last_error}]
      elif getattr(self, "_lost_reply_marker", False):
        # Defense-in-depth: a normally-owned run reached a CLEAN provider
        # terminal but produced zero renderable content (a Claude synthetic-
        # resume no-op, or a codex message whose text was lost). The runner-side
        # fixes stop those at the source; this guarantees the turn is never a
        # SILENT user->user gap — persist a neutral marker the client can retry.
        #
        # Built via _pause_note so the marker carries `resumable` — the flag
        # MsgContent gates the one-tap Resume button on. No `kind`, so no
        # `pause` descriptor: a lost reply is a failure, not a benign pause,
        # and stays in the Error family. The button is the affordance; the
        # message just states the fact.
        blocks = [_pause_note("This turn ended without a response.")]
      else:
        # Genuinely empty turn (no content, no error, not a lost reply) —
        # nothing to persist.
        return
    else:
      blocks = self.assistant_blocks
    snapshot, stashes = self._deferred_snapshot(blocks, complete_all=True)
    ack = get_writer().submit(
      Finalize(
        chat_id=self.chat_id, run_token=self.run_token, snapshot=snapshot,
        thinking_stashes=stashes,
      )
    )
    await _await_ack(ack)

  async def split_for_steer(
    self, user_msg: dict | list[dict], consume_pending_cids: list[str],
  ) -> dict:
    """Split the streaming turn at a steer boundary so reload order is
    Q1, A1, Q2, A2.

    Deterministic for Claude: its steer is interrupt + re-query, a real turn
    boundary, so the sealed A1 is exactly the pre-interrupt text. For Codex,
    `turn.steer()` injects into the SAME running turn with no boundary, so the
    A1/A2 cut is best-effort — a continuation delta already in flight when the
    steer lands can be sealed as the tail of A1 rather than the head of A2.
    The split still imposes Möbius-side ordering (seal A1-so-far, append Q2,
    accumulate A2 fresh); only the exact cut point is upstream-determined for
    Codex. Stop (interrupt + fresh turn) is the path with a real boundary on
    both providers.

    Called by the live provider handle after steering delivery is acknowledged:
    Claude at its interrupt boundary, Codex when ``turn.steer()`` returns.
    Both run on the one FastAPI event loop, so the cut is serialized with this
    sink's ``publish()`` snapshots. The pre-steer assistant text (A1) becomes
    its own trailing assistant message, the steered user message (Q2) is
    appended at the END, and the sink resets its blocks so the post-steer
    continuation (A2) accumulates fresh and the next snapshot appends it as a
    NEW assistant message. The reset is what preserves the durable order A1,
    Q2, A2: without it A1+A2 persist as one message with Q2 inserted before
    them, reloading as Q1, Q2, A1A2.

    Race-free without a lock: `_steering` is set and the blocks captured +
    reset SYNCHRONOUSLY before the first `await`, so any continuation delta
    arriving during the awaited writes broadcasts and accumulates into the
    fresh block list but submits no snapshot (publish gates on `_steering`).
    The two transcript writes run as fenced actor commands, so a coalescible
    snapshot enqueued earlier cannot clobber them. Returns the steered append
    result (`stored` + remaining `pending`).

    When the pre-steer segment has no renderable content (an empty pre-steer
    turn, or only an empty/whitespace token streamed before the cut) the seal
    step is skipped — there is no A1 worth committing — and Q2 is simply
    appended; the trailing assistant message, if any, is already the
    in-progress one the next snapshot will replace, matching the no-partial
    seed case. Keeping that empty A1 would leave a stray empty assistant row
    before Q2 on reload (card 166).
    """
    self._steering = True
    try:
      sealed_blocks = self.assistant_blocks
      # Reset BEFORE the first await so the continuation accumulates into a
      # fresh list the instant the steer lands.
      self.assistant_blocks = []
      # Skip the seal when the pre-steer segment has no renderable content — a
      # steer that lands before the assistant emitted any real output would
      # otherwise commit a stray empty assistant message (A1) before the
      # steered user row, the durable twin of card 166's orphaned fragment. A
      # single REAL token ("I ") still seals; only the empty/whitespace case is
      # dropped (no A1 to commit, matching the no-partial seed case).
      if (
        self.chat_id
        and self.run_token
        and blocks_have_renderable_content(sealed_blocks)
      ):
        snapshot, stashes = self._deferred_snapshot(
          sealed_blocks, complete_all=True,
        )
        ack = get_writer().submit(
          Finalize(
            chat_id=self.chat_id,
            run_token=self.run_token,
            snapshot=snapshot,
            thinking_stashes=stashes,
          )
        )
        try:
          await _await_ack(ack)
        except Exception:
          # Finalize ack failed: assistant_blocks was already reset. Restore
          # the sealed blocks before re-raising so the turn-end Finalize
          # carries A1+A2 rather than only the post-steer continuation.
          # Continuation deltas that arrived during the await are already in
          # self.assistant_blocks (the reset list); prepend the sealed content
          # so the combined snapshot is complete.
          self.assistant_blocks = sealed_blocks + self.assistant_blocks
          raise
      user_msgs = user_msg if isinstance(user_msg, list) else [user_msg]
      ack = get_writer().submit(
        AppendSteeredUserMessage(
          chat_id=self.chat_id,
          run_token="",
          user_msgs=user_msgs,
          consume_pending_cids=consume_pending_cids,
        )
      )
      return await _await_ack(ack)
    finally:
      self._steering = False

  async def commit_steer_cut(
    self, user_msg: dict | list[dict], consume_pending_cids: list[str],
  ) -> dict:
    """Commit and publish one authoritative steering cut.

    The sink owns both sides of the boundary: ``split_for_steer`` persists
    Q1/A1/Q2 ordering, then this method publishes ``steered_into_turn`` on the
    same broadcast before any caller can mistake provider acceptance for a
    durable cut. Provider runners call this only after their control channel
    has acknowledged the steering input.
    """
    user_msgs = user_msg if isinstance(user_msg, list) else [user_msg]
    stored_result = await self.split_for_steer(
      user_msgs, consume_pending_cids,
    )
    stored_messages = (
      stored_result.get("stored_messages")
      if isinstance(stored_result, dict)
      else None
    )
    if not isinstance(stored_messages, list) or not stored_messages:
      stored_messages = user_msgs
    try:
      self.bc.publish(steered_into_turn_event(stored_messages))
    except Exception:
      # The transcript cut already committed. A lost notification must not be
      # reported as a delivery failure (which would falsely claim the rows are
      # still queued and invite a duplicate retry). Turn completion/refetch
      # repairs the client; durability and exactly-once ownership win here.
      _get_logger().exception(
        "publishing the steer cut failed chat_id=%s; the split committed but "
        "the client must refetch", self.chat_id,
      )
    return stored_result

  async def publish_question(self, event: ChatEvent) -> None:
    """Save-before-broadcast for an AskUserQuestion card.

    A question is a protocol barrier: its `question_id` MUST be durably
    persisted before the SSE card is shown, or a fast user Submit races
    the DB write and the answer is lost. So this does NOT go through the
    coalescible `publish()` path; it:

      1. accumulates the question into `assistant_blocks` (so the saved
         snapshot carries the card), then
      2. submits a `QuestionCommit` and AWAITS its ack — a distinct,
         non-coalescing writer-actor command that commits the full
         assistant-message snapshot before resolving, and
      3. ONLY THEN broadcasts the event.

    On a failed commit the actor's ack raises (missing row / empty
    transcript / dropped commit); this method propagates that and does
    NOT broadcast the card. The runner catches it and ends the turn with
    a transport-only error (Claude → PermissionResultDeny, Codex →
    _BridgeError) — no fallback direct write, no unpersisted card on the
    wire. `deepcopy` freezes the snapshot the actor reads so a later
    same-loop event can't mutate the block list out from under it.
    """
    assert event.get("type") == "question", (
      "publish_question only accepts question events; ordinary events go "
      "through publish()"
    )
    # Capture EXACTLY what process_event will do to assistant_blocks BEFORE
    # it runs, so a failed commit can be reverted by identity (not the old
    # tail-slice, which was wrong when process_event COALESCED into an
    # existing block or when a concurrent same-loop append landed after the
    # slice point). The receipt records APPENDED (a new object to delete by
    # identity) vs COALESCED (an existing block whose touched fields we
    # restore, guarded by equality-still-holds).
    receipt = capture_question_scrub(event, self.assistant_blocks)
    process_event(event, self.assistant_blocks)
    commit_question_scrub(receipt, self.assistant_blocks)
    snapshot, stashes = self._deferred_snapshot(self.assistant_blocks)
    ack = get_writer().submit(
      QuestionCommit(
        chat_id=self.chat_id, run_token=self.run_token or "", snapshot=snapshot,
        thinking_stashes=stashes,
      )
    )
    try:
      await _await_ack(ack)
    except Exception:
      # The commit did not land (missing row / empty transcript / dropped
      # commit / wedged writer past the timeout). `process_event` either
      # appended a new question block or coalesced into an existing one; if
      # that survives, a later `Finalize` would persist an UNANSWERABLE card
      # (a question card with no live pending future — reload shows a card
      # that can never be answered). Revert by exact identity before
      # propagating so the terminal Finalize can't persist the orphan and a
      # concurrent same-loop block is never collaterally deleted. The runner
      # catches the re-raised error and ends the turn with a transport-only
      # error (Claude → PermissionResultDeny, Codex → _BridgeError); the
      # card is NOT broadcast.
      undo_question_scrub(receipt, self.assistant_blocks)
      raise
    # Committed durably — now (and only now) show the card.
    self.bc.publish(event)
    # The card is persisted: record the save time so a subsequent throttled
    # snapshot in publish() doesn't redundantly re-commit the same state
    # immediately after.
    self._last_save = time.monotonic()


async def commit_steer_cut(
  chat_id: str,
  user_msg: dict | list[dict],
  consume_pending_cids: list[str],
  *,
  sink=None,
) -> dict:
  """Commit a provider-acknowledged cut through the owning live sink.

  Production runners pass their exact sink so the cut lands on the same event
  log as the streamed A1/A2 blocks. The fallback is for out-of-band callers and
  isolated tests that have no sink: it still consumes the durable rows and
  publishes on the chat broadcast, preserving the same exactly-once contract.
  """
  target = sink or get_active_sink(chat_id)
  commit = getattr(target, "commit_steer_cut", None)
  if callable(commit):
    return await commit(user_msg, consume_pending_cids)

  user_msgs = user_msg if isinstance(user_msg, list) else [user_msg]
  ack = get_writer().submit(
    AppendSteeredUserMessage(
      chat_id=chat_id,
      run_token="",
      user_msgs=user_msgs,
      consume_pending_cids=consume_pending_cids,
    )
  )
  stored_result = await _await_ack(ack)
  raw_bc = get_broadcast(chat_id)
  if raw_bc is not None:
    stored_messages = stored_result.get("stored_messages")
    if not isinstance(stored_messages, list) or not stored_messages:
      stored_messages = user_msgs
    try:
      raw_bc.publish(steered_into_turn_event(stored_messages))
    except Exception:
      _get_logger().exception(
        "publishing the fallback steer cut failed chat_id=%s; the split "
        "committed but the client must refetch", chat_id,
      )
  return stored_result
