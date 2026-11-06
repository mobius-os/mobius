"""A retried Stop learns what the original cleared.

The client resends exactly the queued messages Stop reports it cleared. When
that reply is lost, a retry runs after the original (one Stop per chat at a
time) and would find nothing left to clear, so the queued messages would
vanish. A retry under the same request id gets the original outcome instead.
"""


def queue(db, chat, *cids):
  chat.pending_messages = [
    {"role": "user", "content": f"queued {cid}", "cid": cid, "ts": index + 1}
    for index, cid in enumerate(cids)
  ]
  db.commit()


def stop(client, auth, chat, request_id=None):
  body = {"chat_id": chat.id}
  if request_id is not None:
    body["request_id"] = request_id
  response = client.post("/api/chat/stop", json=body, headers=auth)
  assert response.status_code == 200, response.text
  return response.json()


def test_a_retry_with_the_same_request_id_gets_the_original_cleared_set(client, auth, db, chat):
  queue(db, chat, "first", "second")

  original = stop(client, auth, chat, "stop-1")
  assert original["cleared_pending_cids"] == ["first", "second"]

  retry = stop(client, auth, chat, "stop-1")
  assert retry["cleared_pending_cids"] == ["first", "second"]
  assert retry["stop_receipt"] == original["stop_receipt"]


def test_a_new_request_id_is_a_new_stop(client, auth, db, chat):
  queue(db, chat, "first")
  assert stop(client, auth, chat, "stop-1")["cleared_pending_cids"] == ["first"]

  queue(db, chat, "later")
  assert stop(client, auth, chat, "stop-2")["cleared_pending_cids"] == ["later"]
  # Only the chat's last answered Stop is replayed.
  assert stop(client, auth, chat, "stop-1")["cleared_pending_cids"] == []


def test_a_stop_without_a_request_id_is_never_replayed(client, auth, db, chat):
  queue(db, chat, "first")
  assert stop(client, auth, chat)["cleared_pending_cids"] == ["first"]
  assert stop(client, auth, chat)["cleared_pending_cids"] == []
