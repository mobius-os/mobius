"""Assistant messages must carry a STABLE per-turn `ts`.

`build_assistant_message` omits ts, so assistant messages used to persist
with ts=None. That silently defeated the frontend bridge gate
(useBridgePartial keys the kept partial by ts): on reconnect mid-question
the persisted question card AND the replayed stream card both rendered —
the duplicated question/answer bug. `_update_last_assistant_message` must
stamp a ts on first write and hold it stable across every streaming
replace of the same turn.
"""

from app import models
from app import chat as chatmod


def _mk_chat(db, cid, messages):
  chat = models.Chat(id=cid, title="t", messages=messages, pending_messages=[])
  db.add(chat)
  db.commit()
  return chat


def _assistant(text):
  return {
    "role": "assistant",
    "content": text,
    "blocks": [{"type": "text", "content": text}],
  }


def _last(db, cid):
  chat = db.query(models.Chat).filter(models.Chat.id == cid).first()
  return chat.messages[-1]


def test_assistant_message_gets_ts_on_first_write(db):
  _mk_chat(db, "ts-first", [{"role": "user", "content": "hi", "ts": 1000}])
  chatmod._update_last_assistant_message(db, "ts-first", _assistant("a"))
  msg = _last(db, "ts-first")
  assert msg["role"] == "assistant"
  assert msg.get("ts") is not None


def test_assistant_ts_is_stable_across_streaming_replaces(db):
  """The same turn is re-persisted on every throttled save / finalize;
  the ts must NOT change between them or the bridge stops matching."""
  _mk_chat(db, "ts-stable", [{"role": "user", "content": "hi", "ts": 1000}])
  chatmod._update_last_assistant_message(db, "ts-stable", _assistant("a"))
  ts1 = _last(db, "ts-stable")["ts"]
  # streaming replace (same turn) — content grows, ts must hold steady
  chatmod._update_last_assistant_message(db, "ts-stable", _assistant("ab"))
  msg = _last(db, "ts-stable")
  assert msg["content"] == "ab"
  assert msg["ts"] == ts1


def test_assistant_ts_clears_pending_user_ts(db):
  """The assistant ts is allocated against persisted AND pending messages,
  so a queued user message can't share its ts once promoted (which would
  produce duplicate React keys client-side)."""
  chat = models.Chat(
    id="ts-pending", title="t",
    messages=[{"role": "user", "content": "hi", "ts": 1000}],
    pending_messages=[{"role": "user", "content": "queued",
                       "ts": 9_999_999_999_999}],
  )
  db.add(chat)
  db.commit()
  chatmod._update_last_assistant_message(db, "ts-pending", _assistant("a"))
  assert _last(db, "ts-pending")["ts"] > 9_999_999_999_999


def test_legacy_tsless_assistant_message_is_backfilled(db):
  """An assistant message persisted before this fix (ts absent) gets one
  on the next update, so the bridge can start keying on it."""
  _mk_chat(db, "ts-legacy", [
    {"role": "user", "content": "hi", "ts": 1000},
    {"role": "assistant", "content": "old",
     "blocks": [{"type": "text", "content": "old"}]},
  ])
  chatmod._update_last_assistant_message(db, "ts-legacy", _assistant("old+"))
  assert _last(db, "ts-legacy").get("ts") is not None
