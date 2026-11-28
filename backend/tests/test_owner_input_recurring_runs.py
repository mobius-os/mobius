"""Work that restarts by itself carries on around an open request card.

A recurring wake (here a re-armed timer Wait) must not be stopped by a
question its chat asked earlier: each run completes, may revise what it asks,
and its end shows the current list. The owner is pushed only when that list
holds something they have not been shown. A barring card still holds wakes.
"""
import pytest

from app import chat as chat_mod, chat_waits, models
from app import owner_input_requests as requests
from app.chat_writer import StartTurn, get_writer
from app.database import SessionLocal
from app.timeutil import now_naive_utc
from tests import owner_input_fixtures as inputs


PROMPT = "Which environment should the nightly sync write to?"
CHOICE = {
  "prompt": PROMPT,
  "options": [
    {"label": "Staging", "description": "Write to staging only."},
    {"label": "Production", "description": "Write to production."},
  ],
}
REVISED = "Which environment should the nightly sync write to from now on?"


@pytest.fixture
def pushes(db, monkeypatch):
  owner = db.query(models.Owner).first()
  db.add(models.PushSubscription(
    id="sub-recurring", owner_id=owner.id, endpoint="https://push.example/recurring",
    p256dh="p256", auth="auth",
  ))
  db.commit()
  sent = []
  monkeypatch.setattr(
    "app.push.send_push", lambda subscription, payload: sent.append(payload) or True,
  )
  return sent


async def _complete(chat_id, run_id, text, monkeypatch):
  """End one run through the real terminal path, owner notification included."""
  await inputs.complete_turn(chat_id, monkeypatch, run_id, text,
                             schedule=lambda **_kw: True, side_tasks=True)


def _arm_and_fire_timer(chat_id, run_id):
  """A run re-arms its timer; time passes and it comes due."""
  with SessionLocal() as session:
    row = chat_waits.declare_wait(
      session, chat_id=chat_id, description="the next nightly sync",
      kind="timer", delay_secs=3600, created_by_run_id=run_id,
    )
    row.status = "met"
    row.met_at = now_naive_utc()
    session.commit()
    return row.id


async def _wake(chat_id, wait_id, monkeypatch):
  """Deliver the fired Wait; return the run it started, if any."""
  monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **_kw: True)
  delivered = await chat_waits._deliver_resume(wait_id)
  chat_mod.discard_starting(chat_id)
  if not delivered:
    return None
  with SessionLocal() as session:
    run = session.get(models.ChatRun, f"wait-resume-{wait_id}")
    assert run is not None and run.status == "running"
    return run.id


def _card_notifications(chat_id):
  with SessionLocal() as session:
    return session.query(models.Notification).filter(
      models.Notification.source_id == chat_id,
    ).count()


async def _first_run_asks(client, chat, db, monkeypatch):
  """An owner turn sets up the recurring work and asks one question."""
  run_id = f"setup-{chat.id}"
  get_writer().submit(StartTurn(
    chat_id=chat.id, run_token=run_id,
    user_msg={"role": "user", "content": "Run the sync every night", "ts": 1},
  )).result(timeout=5)
  response = client.post(
    f"/api/chats/{chat.id}/input-requests",
    json={"op": "add", "key": "sync-target", **CHOICE,
          "blocks": {"work": "Writing the synced rows"}},
    headers=inputs.agent_auth(db, chat.id, run_id),
  )
  assert response.status_code == 200, response.text
  wait_id = _arm_and_fire_timer(chat.id, run_id)
  await _complete(chat.id, run_id, "Scheduled; I need one answer first.", monkeypatch)
  return wait_id


@pytest.mark.asyncio
async def test_a_recurring_wake_runs_past_an_open_request_card_and_shows_the_revision(
  client, auth, chat, db, pushes, monkeypatch,
):
  wait_id = await _first_run_asks(client, chat, db, monkeypatch)
  [first], _ = inputs.transcript(chat.id)

  # The timer fires while the card is open: the wake runs, not queues.
  wake_run = await _wake(chat.id, wait_id, monkeypatch)
  assert wake_run is not None
  # The background run revises the question through the same tool and
  # re-arms its schedule; nothing about the open card stops either.
  response = client.post(
    f"/api/chats/{chat.id}/input-requests",
    json={"op": "update", "key": "sync-target", "prompt": REVISED},
    headers=inputs.agent_auth(db, chat.id, wake_run),
  )
  assert response.status_code == 200, response.text
  next_wait = _arm_and_fire_timer(chat.id, wake_run)
  await _complete(chat.id, wake_run, "Synced the read-only tables.", monkeypatch)

  cards, history = inputs.transcript(chat.id)
  latest = cards[-1]
  assert len(cards) == 2 and latest["question_id"] != first["question_id"]
  assert [q["question"] for q in latest["questions"]] == [REVISED]
  assert latest["questions"][0]["request_revision"] == 2
  # The revised card is the last thing the run shows, after all its output.
  assert history[-1]["blocks"][0]["content"] == "Synced the read-only tables."
  assert history[-1]["blocks"][-1]["question_id"] == latest["question_id"]
  assert inputs.markers(chat.id) == (None, latest["question_id"])
  # The old card stops accepting answers; the revision is what is asked.
  stale = client.post(f"/api/chats/{chat.id}/messages", json={
    "content": "- Staging", "hidden": True, "answers": {PROMPT: "Staging"},
    "question_id": first["question_id"],
  }, headers=auth)
  assert stale.status_code == 410, stale.text
  with SessionLocal() as session:
    [row] = requests.open_requests(session, chat.id)
    assert row.revision == 2 and row.published_question_id == latest["question_id"]

  # The re-armed timer keeps firing on schedule past the open card.
  assert await _wake(chat.id, next_wait, monkeypatch) is not None


@pytest.mark.asyncio
async def test_an_unchanged_republish_does_not_push_again_but_a_revision_does(
  client, chat, db, pushes, monkeypatch,
):
  wait_id = await _first_run_asks(client, chat, db, monkeypatch)
  assert _card_notifications(chat.id) == 1 and len(pushes) == 1

  quiet_run = await _wake(chat.id, wait_id, monkeypatch)
  next_wait = _arm_and_fire_timer(chat.id, quiet_run)
  await _complete(chat.id, quiet_run, "Nothing new tonight.", monkeypatch)

  # Republished as the run's last item, but the owner was already told.
  cards, _ = inputs.transcript(chat.id)
  assert len(cards) == 2 and inputs.markers(chat.id) == (None, cards[-1]["question_id"])
  assert _card_notifications(chat.id) == 1 and len(pushes) == 1

  revising_run = await _wake(chat.id, next_wait, monkeypatch)
  with SessionLocal() as session:
    requests.update_request(session, chat_id=chat.id, ref="sync-target",
                            spec={**CHOICE, "prompt": REVISED})
  await _complete(chat.id, revising_run, "The schema changed.", monkeypatch)

  assert _card_notifications(chat.id) == 2 and len(pushes) == 2
  assert pushes[-1]["body"] == REVISED


@pytest.mark.asyncio
async def test_a_barring_card_still_holds_a_recurring_wake(
  client, chat, db, pushes, monkeypatch,
):
  wait_id = await _first_run_asks(client, chat, db, monkeypatch)
  with SessionLocal() as session:
    session.get(models.Chat, chat.id).pending_question_id = "approval-card"
    session.commit()

  assert await _wake(chat.id, wait_id, monkeypatch) is None

  with SessionLocal() as session:
    row = session.get(models.ChatWait, wait_id)
    assert row.resume_delivered_at is None and row.result_disposition is None
    assert session.get(models.ChatRun, f"wait-resume-{wait_id}") is None
    assert chat_waits.wait_resume_blocker(session, row) == "owner_input"
    # Queued behind the barring card, exactly as before request lists existed.
    queued = session.get(models.Chat, chat.id).pending_messages or []
    assert [message.get("cid") for message in queued] == [f"wait-result-{wait_id}"]
