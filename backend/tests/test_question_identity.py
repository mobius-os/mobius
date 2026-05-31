"""Task 5: identity-keyed AskUserQuestion answers via `question_id`.

Milestone B threads the `PendingQuestion.question_id` end-to-end (non-
activating, backend only):

  1. Both SDK runners publish `question_id` on the `question` event so the
     id reaches the client over SSE.
  2. `process_event` stamps that id onto the persisted question block.
  3. The answer routes (`chats_stream._apply_answers_to_last_question`
     and the legacy `chats.save_question_answers`) prefer an exact match
     on the block's `question_id` when one is supplied — fixing the
     wrong-block bug when two questions are open — and fall back to the
     existing latest-question search when it is absent (backward-compat).

The actor stays dormant; nothing here routes through it.
"""

from app import models
from app.events import process_event
from app.routes.chats import save_question_answers  # noqa: F401 (import guard)
from app.routes.chats_stream import _apply_answers_to_last_question


# -- (c) the published question event carries question_id (event level) ------
def test_process_event_stamps_question_id_onto_block():
  # When the runner publishes a question event carrying `question_id`, the
  # accumulated block must record it so the answer route can match on it.
  blocks: list = []
  process_event(
    {
      "type": "question",
      "question_id": "qid-abc",
      "questions": [{"id": "q1", "question": "Color?"}],
    },
    blocks,
  )
  assert blocks[0]["type"] == "question"
  assert blocks[0]["question_id"] == "qid-abc"


def test_process_event_omits_question_id_when_event_lacks_it():
  # Backward-compatible: an event with no question_id produces a block with
  # no question_id key (so legacy clients / fallback behavior are unchanged).
  blocks: list = []
  process_event(
    {"type": "question", "questions": [{"question": "Color?"}]},
    blocks,
  )
  assert blocks[0] == {"type": "question", "questions": [{"question": "Color?"}]}
  assert "question_id" not in blocks[0]


def test_process_event_coalesces_partials_by_question_id():
  # Two partial deliveries for the SAME question_id must coalesce into one
  # block even if the sub-question id/text differs between partials.
  blocks: list = []
  process_event(
    {
      "type": "question",
      "question_id": "qid-1",
      "questions": [{"id": "a", "question": "Pick", "options": []}],
    },
    blocks,
  )
  process_event(
    {
      "type": "question",
      "question_id": "qid-1",
      "questions": [
        {"id": "a", "question": "Pick", "options": [{"label": "X"}]},
      ],
    },
    blocks,
  )
  qblocks = [b for b in blocks if b.get("type") == "question"]
  assert len(qblocks) == 1
  assert qblocks[0]["questions"][0]["options"] == [{"label": "X"}]


def test_process_event_distinct_question_ids_append_separate_blocks():
  blocks: list = []
  process_event(
    {"type": "question", "question_id": "qid-1",
     "questions": [{"id": "a", "question": "First?"}]},
    blocks,
  )
  process_event(
    {"type": "question", "question_id": "qid-2",
     "questions": [{"id": "b", "question": "Second?"}]},
    blocks,
  )
  qblocks = [b for b in blocks if b.get("type") == "question"]
  assert len(qblocks) == 2
  assert qblocks[0]["question_id"] == "qid-1"
  assert qblocks[1]["question_id"] == "qid-2"


# -- helpers for the route tests ---------------------------------------------
def _chat_with_two_open_questions():
  """A chat whose last assistant message has TWO open question blocks,
  each carrying its own `question_id`, neither answered yet."""
  return models.Chat(
    id="two-q-chat",
    title="two q",
    messages=[
      {"role": "user", "content": "go"},
      {
        "role": "assistant",
        "content": "asking",
        "blocks": [
          {
            "type": "question",
            "question_id": "qid-first",
            "questions": [{"id": "q-color", "question": "Color?"}],
          },
          {
            "type": "question",
            "question_id": "qid-second",
            "questions": [{"id": "q-size", "question": "Size?"}],
          },
        ],
      },
    ],
  )


# -- (a) answer keyed to the FIRST question's id hits only that block --------
def test_answer_with_question_id_updates_only_the_matching_block(db):
  chat = _chat_with_two_open_questions()
  db.add(chat)
  db.commit()

  # The latest-question fallback would have hit the SECOND (last) block; the
  # supplied question_id must route the answer to the FIRST instead.
  ok = _apply_answers_to_last_question(
    chat, {"Color?": "red"}, question_id="qid-first",
  )
  assert ok is True
  db.commit()

  db.refresh(chat)
  blocks = chat.messages[-1]["blocks"]
  assert blocks[0]["question_id"] == "qid-first"
  assert blocks[0].get("answers") == {"Color?": "red"}
  # The second (latest) block must be UNTOUCHED — proving we didn't fall
  # back to the latest-question search.
  assert "answers" not in blocks[1]


def test_answer_with_question_id_updates_the_second_block_precisely(db):
  # Symmetric: keying to the SECOND id must hit it and leave the first alone.
  chat = _chat_with_two_open_questions()
  db.add(chat)
  db.commit()

  ok = _apply_answers_to_last_question(
    chat, {"Size?": "m"}, question_id="qid-second",
  )
  assert ok is True
  db.commit()

  db.refresh(chat)
  blocks = chat.messages[-1]["blocks"]
  assert blocks[1].get("answers") == {"Size?": "m"}
  assert "answers" not in blocks[0]


def test_answer_with_unknown_question_id_matches_nothing(db):
  # An id that matches no block must NOT silently fall back to latest — that
  # would re-introduce the wrong-block bug. It returns False (no match).
  chat = _chat_with_two_open_questions()
  db.add(chat)
  db.commit()

  ok = _apply_answers_to_last_question(
    chat, {"Color?": "red"}, question_id="qid-nonexistent",
  )
  assert ok is False
  blocks = chat.messages[-1]["blocks"]
  assert "answers" not in blocks[0]
  assert "answers" not in blocks[1]


# -- (b) no question_id behaves exactly as today (regression-safe) -----------
def test_answer_without_question_id_falls_back_to_latest(db):
  # Backward-compat: when no question_id is supplied, the existing latest-
  # assistant-question behavior is preserved (hits the LAST question block).
  chat = _chat_with_two_open_questions()
  db.add(chat)
  db.commit()

  ok = _apply_answers_to_last_question(chat, {"Size?": "m"})
  assert ok is True
  db.commit()

  db.refresh(chat)
  blocks = chat.messages[-1]["blocks"]
  # Latest = the second block.
  assert blocks[1].get("answers") == {"Size?": "m"}
  assert "answers" not in blocks[0]


def test_answer_without_question_id_single_question_unchanged(db):
  # The common case (one open question, no id): unchanged behavior.
  chat = models.Chat(
    id="one-q-chat",
    title="one q",
    messages=[
      {"role": "user", "content": "go"},
      {
        "role": "assistant",
        "content": "asking",
        "blocks": [{"type": "question", "questions": [{"question": "Color?"}]}],
      },
    ],
  )
  db.add(chat)
  db.commit()

  ok = _apply_answers_to_last_question(chat, {"Color?": "blue"})
  assert ok is True
  db.commit()
  db.refresh(chat)
  blocks = chat.messages[-1]["blocks"]
  assert blocks[0].get("answers") == {"Color?": "blue"}


# -- legacy route (chats.save_question_answers) prefers question_id ----------
def test_legacy_route_with_question_id_hits_matching_block(client, auth, db):
  chat = _chat_with_two_open_questions()
  db.add(chat)
  db.commit()

  res = client.post(
    f"/api/chats/{chat.id}/question-answers",
    json={"answers": {"Color?": "red"}, "question_id": "qid-first"},
    headers=auth,
  )
  assert res.status_code == 200, res.text

  db.refresh(chat)
  blocks = chat.messages[-1]["blocks"]
  assert blocks[0].get("answers") == {"Color?": "red"}
  assert "answers" not in blocks[1]


def test_legacy_route_without_question_id_falls_back_to_latest(client, auth, db):
  chat = _chat_with_two_open_questions()
  db.add(chat)
  db.commit()

  res = client.post(
    f"/api/chats/{chat.id}/question-answers",
    json={"answers": {"Size?": "m"}},
    headers=auth,
  )
  assert res.status_code == 200, res.text

  db.refresh(chat)
  blocks = chat.messages[-1]["blocks"]
  assert blocks[1].get("answers") == {"Size?": "m"}
  assert "answers" not in blocks[0]


# -- (c) both SDK runners publish question_id on the question event ----------
def test_claude_runner_publishes_question_id_on_question_event(monkeypatch):
  # The Claude runner's `can_use_tool` callback must publish the
  # PendingQuestion.question_id on the `question` event so the id reaches
  # the client over SSE. We capture the callback the runner hands to the
  # SDK options, invoke it, and assert the published event.
  import asyncio

  from app import claude_sdk_runner

  captured: dict = {}

  class _FakeOptions:
    def __init__(self, **kwargs):
      captured["can_use_tool"] = kwargs.get("can_use_tool")

  class _FakeClient:
    def __init__(self, options):
      pass

    async def connect(self):
      # Return early from the turn (after the callback was captured) by
      # raising the same timeout the runner already handles gracefully.
      raise asyncio.TimeoutError()

    async def disconnect(self):
      pass

  monkeypatch.setattr(claude_sdk_runner, "ClaudeAgentOptions", _FakeOptions)
  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", _FakeClient)

  events: list = []

  class _Bc:
    def publish(self, event):
      events.append(event)

  pending_registry: dict = {}

  async def go():
    # Run the turn; it captures can_use_tool then bails on connect timeout.
    await claude_sdk_runner.run_claude_sdk_turn(
      user_message="hi",
      session_id=None,
      base_env={},
      cwd="/tmp",
      chat_id="cqid",
      skill_text="",
      bc=_Bc(),
      pending_questions=pending_registry,
      db=None,
    )
    cut = captured["can_use_tool"]
    assert cut is not None, "runner did not hand can_use_tool to the SDK"

    # Invoke the captured callback; resolve its future so it returns.
    task = asyncio.create_task(
      cut("AskUserQuestion", {"questions": [{"id": "q1", "question": "Color?"}]}, None)
    )
    # Let the callback publish + register the pending question.
    for _ in range(50):
      await asyncio.sleep(0.01)
      pending = pending_registry.get("cqid")
      if pending is not None:
        break
    assert pending is not None
    pending.future.set_result({"Color?": "red"})
    await task

    qevents = [e for e in events if e.get("type") == "question"]
    assert len(qevents) == 1
    assert qevents[0]["question_id"] == pending.question_id
    assert qevents[0]["question_id"]  # non-empty

  asyncio.run(go())


def test_codex_runner_publishes_question_id_on_question_event():
  # The Codex bridge's `park_question` must publish question_id too. Drive
  # the installed sync handler from a worker thread, marshaling onto the
  # runner loop, and assert the published `question` event carries the id.
  import asyncio
  import threading

  from app import codex_sdk_runner

  events: list = []
  events_lock = threading.Lock()

  class _Bc:
    def publish(self, event):
      with events_lock:
        events.append(dict(event))

  class _SyncClient:
    _approval_handler = None

  class _Inner:
    _sync = _SyncClient()

  class _FakeCodex:
    _client = _Inner()

  pending_registry: dict = {}

  async def go():
    loop = asyncio.get_running_loop()
    codex_sdk_runner._install_request_user_input_handler(
      _FakeCodex(),
      loop=loop,
      chat_id="cqid2",
      bc=_Bc(),
      pending_questions=pending_registry,
      db=None,
    )
    handler = _FakeCodex._client._sync._approval_handler
    assert handler is not None, "bridge did not install the approval handler"

    # The sync handler blocks on fut.result(); run it off-loop so this
    # coroutine can resolve the parked future once it registers.
    result_holder: dict = {}

    def call_handler():
      result_holder["res"] = handler(
        "item/tool/requestUserInput",
        {"questions": [{"id": "q1", "question": "Color?"}]},
      )

    t = threading.Thread(target=call_handler)
    t.start()

    # Wait for park_question to register the pending question, then answer.
    for _ in range(100):
      await asyncio.sleep(0.01)
      pending = pending_registry.get("cqid2")
      if pending is not None:
        break
    assert pending is not None
    pending.future.set_result({"Color?": "red"})

    await loop.run_in_executor(None, t.join, 5)

    qevents = [e for e in events if e.get("type") == "question"]
    assert len(qevents) == 1
    assert qevents[0]["question_id"] == pending.question_id
    assert qevents[0]["question_id"]

  asyncio.run(go())
