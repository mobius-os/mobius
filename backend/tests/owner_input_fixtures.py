"""Shared steps and fixtures for tests that ask the owner through the request list.

An agent records a request with `owner_input` (POST input-requests) and keeps
working; the card is shown when its turn ends. These helpers drive that real
path: record, then publish exactly as the terminal path does, either without
ending the run (`publish_now`, the moment between the card commit and the
run's terminal transition) or by completing the turn (`finish_turn` for a
sink the test holds, `complete_turn` for a run it only names).

A test module uses a fixture defined here by importing it by name.
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
import time
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

import pytest

from app import auth as auth_mod, chat as chat_mod, models, transcript_rows
from app.chat_writer import Barrier, ResolvePlatformRestartCard, StartTurn, get_writer
from app.database import SessionLocal


APPROVAL = {
  "kind": "approval",
  "prompt": "Publish the release notes to the public site?",
  "work_key": "site:release-notes:publish",
  "options": [
    {"label": "Publish", "description": "Make the notes public now."},
    {"label": "Not now", "description": "Keep them as a draft."},
  ],
}
QUESTION = {"prompt": "How formal should the changelog be?"}
SECRET_VALUE = "never-record-this-value"
TURN_TEXT = "Here is where things stand."


# ---- Fixtures ---------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_restart_source(monkeypatch, tmp_path):
  """Restart dispatch runs the real preflight against a minimal source tree."""
  root = tmp_path / "platform"
  app = root / "backend" / "app"
  app.mkdir(parents=True)
  (app / "__init__.py").write_text("", encoding="utf-8")
  (app / "main.py").write_text("from app import routes\n", encoding="utf-8")
  (app / "routes.py").write_text(
    "def require_all_routers_loaded():\n  return None\n", encoding="utf-8",
  )
  monkeypatch.setenv("MOBIUS_PLATFORM_DIR", str(root))
  monkeypatch.setattr("app.platform_restart.pending_restart_paths", lambda: [])
  monkeypatch.setattr("app.platform_restart.uncommitted_restart_paths", lambda: [])


def asking_run_id(chat) -> str:
  """The run id of the test chat's turn that records the requests."""
  return f"asking-{chat.id}"


@pytest.fixture
def run_auth(chat, db):
  """A running turn of the test chat and its agent's headers."""
  return start_run(db, chat.id, asking_run_id(chat))


@pytest.fixture
def boot(monkeypatch):
  monkeypatch.setenv("MOBIUS_BOOT_ID", "boot-current")
  return "boot-current"


@pytest.fixture
def no_wake(monkeypatch):
  """Answers queue their continuation without starting a provider."""
  from app.routes import chats_stream

  monkeypatch.setattr(chats_stream, "_schedule_continuation", lambda **kw: (
    chats_stream.discard_starting(kw["chat_id"])))


@pytest.fixture
def consumer(monkeypatch):
  """A sealed consumer that records what it received and exits with ``code``."""
  from app import saved_secure_inputs

  calls = {"seen": [], "code": 0}

  async def consume(spec, values, chat_id):
    calls["seen"].append(dict(values))
    values.clear()
    return calls["code"]

  monkeypatch.setattr(saved_secure_inputs, "_run_consumer", consume)
  return calls


# ---- Agent runs and requests ------------------------------------------------

def agent_auth(db, chat_id: str, run_id: str) -> dict:
  owner = db.query(models.Owner).first()
  token = auth_mod.create_agent_token(
    chat_id=chat_id, owner_username=owner.username,
    token_epoch=owner.token_epoch, run_id=run_id,
    expires_delta=timedelta(minutes=5),
  )
  return {"Authorization": f"Bearer {token}"}


def start_run(db, chat_id: str, run_id: str, content: str = "Ship the release") -> dict:
  """Start an agent turn as the owner's message would; its agent's headers."""
  get_writer().submit(StartTurn(
    chat_id=chat_id, run_token=run_id,
    user_msg={"role": "user", "content": content, "ts": 1},
  )).result(timeout=5)
  return agent_auth(db, chat_id, run_id)


def start_turn(chat_id: str, run_id: str, content: str = "Carry on", *,
               owner_input: bool = True) -> bool:
  """Start a later turn; False when a barring card refuses it."""
  from app.chat_writer import StartTurnBlockedByPendingQuestion

  result = get_writer().submit(StartTurn(
    chat_id=chat_id, run_token=run_id, owner_input=owner_input,
    user_msg={"role": "user", "content": content, "ts": 10},
  )).result(timeout=5)
  return not isinstance(result, StartTurnBlockedByPendingQuestion)


def edit(client, chat_id: str, headers: dict, **body):
  return client.post(f"/api/chats/{chat_id}/input-requests", json=body, headers=headers)


def secret_body(tmp_path, **extra) -> dict:
  return {
    "kind": "secret", "title": "Connect the mail service",
    "description": "The key goes straight to the local connector.",
    "fields": [{"name": "api_key", "type": "password", "label": "API key"}],
    "command": [sys.executable, "-c", "import json,sys; json.load(sys.stdin)"],
    "cwd": str(tmp_path), **extra,
  }


def request_body(kind: str, tmp_path) -> dict:
  """An `add` body for one owner-only kind."""
  return {"approval": APPROVAL, "restart": {"kind": "restart"},
          "secret": secret_body(tmp_path)}[kind]


async def complete_turn(chat_id: str, monkeypatch, run_id: str, text: str = TURN_TEXT, *,
                        db=None, schedule=None, side_tasks: bool = False, run_gen=None):
  """End a running turn through the real terminal path.

  ``schedule`` replaces the continuation launcher (by default nothing
  starts); ``side_tasks`` also waits for the turn end's notifications;
  ``run_gen`` is the generation the turn owns, as a runner passes it. The
  terminal path closes ``db``.
  """
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_event_sink import ChatEventSink

  monkeypatch.setattr(chat_mod, "_schedule_continuation",
                      schedule if schedule is not None else (lambda **kwargs: None))
  monkeypatch.setattr(chat_mod, "_publish_chat_run_finished", lambda *_: None)
  broadcast = create_broadcast(chat_id)
  sink = ChatEventSink(broadcast, chat_id, run_token=run_id)
  sink.publish({"type": "text", "content": text})
  try:
    result = await chat_mod._complete_turn(
      bc=broadcast, sink=sink, db=db if db is not None else SessionLocal(),
      chat_id=chat_id, run_gen=run_gen,
      provider_id="codex", cost_usd=0, close_browser=False,
    )
    if side_tasks:
      await asyncio.gather(*list(sink._side_tasks))
    return result
  finally:
    remove_broadcast(chat_id)


# ---- Reading what was saved -------------------------------------------------

def transcript(chat_id: str) -> tuple[list[dict], list[dict]]:
  """(every saved card, the transcript) as committed."""
  with SessionLocal() as session:
    history = list(transcript_rows.history(session.get(models.Chat, chat_id)))
  return [block for message in history for block in message.get("blocks") or []
          if block.get("type") == "question"], history


def cards(chat_id: str) -> list[dict]:
  return transcript(chat_id)[0]


def entry(card: dict, key: str) -> dict:
  return next(q for q in card["questions"] if q["request_key"] == key)


def markers(chat_id: str) -> tuple[str | None, str | None]:
  """(barring card, request-list card) as committed."""
  get_writer().submit(Barrier()).result(timeout=5)
  with SessionLocal() as session:
    row = session.get(models.Chat, chat_id)
    return row.pending_question_id, row.open_request_card_id


def request_row(key: str):
  """The newest request recorded under ``key``, detached."""
  with SessionLocal() as session:
    row = session.query(models.OwnerInputRequest).filter_by(key=key).order_by(
      models.OwnerInputRequest.created_at.desc()).first()
    session.expunge(row)
    return row


def wait_row(wait_id: str):
  with SessionLocal() as session:
    row = session.get(models.ChatWait, wait_id)
    session.expunge(row)
    return row


def press_restart(chat_id: str, card: dict, option: str | None = None):
  """Resolve the card's restart entry as its Restart now press does."""
  restart = entry(card, "restart")
  return get_writer().submit(ResolvePlatformRestartCard(
    chat_id=chat_id, question_id=card["question_id"], request_id=restart["id"],
    selected_option_id=option or restart["platform_action"]["restart_option_id"],
  )).result(timeout=5)


def submit_secret(client, headers: dict, chat_id: str, request_id: str,
                  value: str = SECRET_VALUE, *, shown: dict | None = None):
  """Submit a secret entry as the tab showing it does: naming its card and
  revision, by default those of the newest card that shows the request."""
  if shown is None:
    card = next((card for card in reversed(cards(chat_id)) if any(
      question.get("id") == request_id for question in card["questions"])), None)
    question = next((question for question in card["questions"]
                     if question.get("id") == request_id), {}) if card else {}
    shown = {"question_id": card and card["question_id"],
             "request_revision": question.get("request_revision")}
  return client.post(f"/api/secure-inputs/{chat_id}/{request_id}/submit",
                     headers=headers, json={**shown, "fields": {"api_key": value}})


def wait_for_secret(request_id: str, timeout: float = 5.0) -> str:
  """Wait until a submitted secret's consumer has settled; its final status.

  A submission returns once its consumer task is owned; the consumer runs
  and settles in the background.
  """
  deadline = time.monotonic() + timeout
  while True:
    with SessionLocal() as session:
      status = session.get(models.SavedSecureInput, request_id).status
    if status not in ("pending", "consuming"):
      return status
    if time.monotonic() >= deadline:
      raise AssertionError(
        f"the sealed consumer for {request_id} did not settle within {timeout}s "
        f"(still {status})")
    time.sleep(0.02)


def approval_body(prompt: dict) -> dict:
  """An approval request from the old saved-card prompt shape."""
  return {
    "kind": "approval", "prompt": prompt["question"],
    "options": prompt["options"], "work_key": prompt["work_key"],
  }


def add(client, chat_id: str, headers: dict, key: str, **body):
  return edit(client, chat_id, headers, op="add", key=key, **body)


def _ensure_reply(sink, text: str) -> None:
  # A turn with no renderable output is the lost-reply failure, which never
  # publishes; a real turn says where things stand before it ends.
  if not any(block.get("type") == "text" for block in sink.assistant_blocks):
    sink.publish({"type": "text", "content": text})


def publish_now(sink, text: str = TURN_TEXT) -> None:
  """Show the open requests as this turn's end does, leaving the run running."""
  _ensure_reply(sink, text)
  asyncio.run(chat_mod._publish_open_owner_requests(sink.chat_id, sink))


def finish_turn(sink, text: str = TURN_TEXT, *, provider="claude"):
  """Complete the sink's turn through the real terminal path."""
  _ensure_reply(sink, text)

  async def finish():
    with SessionLocal() as db:
      return await chat_mod._complete_turn(
        bc=sink.bc, sink=sink, db=db, chat_id=sink.chat_id, run_gen=None,
        provider_id=provider, cost_usd=0, close_browser=False,
      )

  return asyncio.run(finish())


def last_card(chat_id: str) -> dict | None:
  with SessionLocal() as db:
    chat = db.get(models.Chat, chat_id)
    return next((
      block for message in reversed(list(transcript_rows.history(chat)))
      for block in reversed(message.get("blocks") or [])
      if block.get("type") == "question"
    ), None)


def _save_released_card(
  sink, payload: dict, *, identity: object, secure_request: dict | None = None,
  activation_requirement: dict | None = None,
) -> str:
  """Commit a barring card as earlier releases saved it; its id.

  The card has no request row behind it: it sets `pending_question_id` and
  settles through its own answer, restart or sealed path.
  """
  from app import owner_input_cards as cards
  from app.owner_input import publish_owner_input_changed

  question_id = str(uuid5(NAMESPACE_URL, json.dumps(
    [sink.chat_id, sink.run_token, identity], sort_keys=True,
  )))
  payload = copy.deepcopy(payload)
  for question in payload.get("questions", []):
    for index, option in enumerate(question.get("options", [])):
      option.setdefault("id", str(index))
  activation_wait = None
  if activation_requirement is not None:
    with SessionLocal() as db:
      run = db.get(models.ChatRun, sink.run_token)
      activation_wait = cards.activation_wait_for(question_id, run, activation_requirement)
    payload["platform_action"]["wait_id"] = activation_wait["id"]
  event = {"type": "question", "question_id": question_id,
           "response_mode": "continuation", **payload}
  asyncio.run(sink.publish_question(event, **(
    {"secure_request": secure_request} if secure_request is not None
    else {"activation_wait": activation_wait} if activation_wait is not None
    else {}
  )))
  publish_owner_input_changed(sink.chat_id, "question", question_id=question_id)
  return question_id


def save_barring_question(sink, questions: list[dict]) -> str:
  """Save a barring Q&A card exactly as earlier releases did.

  Ordinary questions are now shown on the non-barring request-list card, but
  chats keep their saved barring Q&A cards and those stay answerable; this
  saves one to exercise that answer lifecycle.
  """
  grouped = len(questions) > 1
  payload = {"questions": [{
    **question,
    "id": question.get("id") or f"question-{index}",
    "header": question.get("header") or (f"Question {index}" if grouped else "Your choice"),
  } for index, question in enumerate(questions, 1)]}
  _ensure_reply(sink, "One question first.")
  return _save_released_card(sink, payload, identity=payload)


def save_legacy_approval(sink, prompt: dict) -> str:
  """Save a barring Approval card as earlier releases did; its id.

  As then, the chat claims the card's exact action before the card is saved.
  """
  from app.agent_work_claims import claim_work

  with SessionLocal() as db:
    owner = db.query(models.Owner).first()
    claim_work(db, owner_id=owner.id, chat_id=sink.chat_id, run_id=sink.run_token,
               work_key=prompt["work_key"], summary=prompt["question"][:500])
  payload = {
    "questions": [{"id": "approval", "header": "Approval",
                   "question": prompt["question"], "options": prompt["options"]}],
    "action_key": prompt["work_key"],
  }
  _ensure_reply(sink, TURN_TEXT)
  return _save_released_card(sink, payload, identity=prompt)


def save_legacy_restart(sink) -> str:
  """Save a barring Restart card as earlier releases did; its id."""
  from app import owner_input_cards as cards, restart_ledger

  entry = cards.restart_entry(sink.run_token, restart_ledger.current_boot_id(), "")
  action = {name: value for name, value in entry["platform_action"].items() if name != "wait_id"}
  requirement = action["requirement"]
  payload = {
    "questions": [{"id": "restart", **{name: entry[name] for name in
                                       ("header", "question", "options")}}],
    "platform_action": action,
  }
  _ensure_reply(sink, TURN_TEXT)
  return _save_released_card(
    sink, payload, activation_requirement=requirement,
    identity={"type": "request_restart", "source_boot_id": requirement["source_boot_id"]},
  )


def save_legacy_secret(sink, spec: dict) -> str:
  """Save a barring Secure input card and its private consumer as earlier
  releases did; its id, which is also the sealed request id."""
  from app.saved_secure_inputs import validate_consumer_spec

  consumer = validate_consumer_spec({**spec, "action": spec.get("action", "run")})
  card = {
    "questions": [{"id": "secure_input", "header": "Secure input",
                   "question": spec["title"], "options": []}],
    "secure_input": {"title": spec["title"], "description": spec.get("description", ""),
                     "mode": "sealed", "fields": copy.deepcopy(spec["fields"])},
  }
  _ensure_reply(sink, TURN_TEXT)
  return _save_released_card(sink, card, secure_request=consumer,
                             identity={"card": card, "consumer": consumer})
