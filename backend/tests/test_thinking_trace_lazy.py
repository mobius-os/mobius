"""Deferred reasoning storage, bounded wire events, and lazy read endpoint."""
import time
import uuid

from app import models
from app.chat import _ChatEventSink
from app.chat_writer import Barrier, StashThinkingTrace, get_writer
from app.events import THINKING_INLINE_THRESHOLD
from app.routes.chats import THINKING_TRACE_PREVIEW_CHARS
from app.memory_recall import EMPTY_RECALL_BINDING


class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(dict(event))


def test_thinking_stash_is_revision_monotonic(db):
    get_writer().submit(StashThinkingTrace(
        chat_id="trace-chat", thinking_id="think-1",
        content="newest", revision=10, complete=False,
    )).result(timeout=5)
    get_writer().submit(StashThinkingTrace(
        chat_id="trace-chat", thinking_id="think-1",
        content="old", revision=3, complete=True,
    )).result(timeout=5)
    row = db.query(models.ThinkingTrace).one()
    assert row.content == "newest"
    assert row.revision == 10
    assert row.complete is True


def test_sink_streams_thinking_live_and_defers_only_persistence(db):
    bus = _Bus()
    sink = _ChatEventSink(bus, "trace-chat", run_token="rt", recall_binding=EMPTY_RECALL_BINDING)
    # Keep this unit test off the periodic transcript path; exercise the
    # snapshot/stash helper explicitly after checking the public events.
    sink._last_save = time.monotonic()
    first = "a" * (THINKING_INLINE_THRESHOLD - 100)
    second = "b" * 200
    sink.publish({"type": "thinking", "content": first, "ts": 1000})
    sink.publish({"type": "thinking", "content": second, "ts": 1100})

    # The live wire streams the raw deltas token-by-token, even past the size
    # cutoff — never blanked, never flagged deferred. The client appends them the
    # same way it appends answer-text deltas (the typewriter).
    assert bus.events[0]["content"] == first
    assert bus.events[1]["content"] == second
    assert "thinking_deferred" not in bus.events[1]
    assert bus.events[0]["thinking_id"] == bus.events[1]["thinking_id"]
    assert sink.assistant_blocks[0]["content"] == first + second

    # Persistence still defers past the cutoff: the durable transcript carries a
    # bounded reference and the full trace is stashed out-of-band, so the
    # transcript stays lean and a reopened thought is fetched on demand.
    snapshot, stashes = sink._deferred_snapshot(sink.assistant_blocks)
    block = snapshot["blocks"][0]
    assert "content" not in block
    assert block["thinking_deferred"] is True
    assert block["thinking_revision"] == len(first + second)
    for stash in stashes:
        get_writer().submit(stash)
    get_writer().submit(Barrier()).result(timeout=5)
    row = db.query(models.ThinkingTrace).one()
    assert row.content == first + second


def test_broadcast_coalesces_thinking_deltas_in_replay_log():
    from app.broadcast import ChatBroadcast

    bc = ChatBroadcast("coalesce-chat")
    # Same thought + same segment: deltas merge into ONE bounded log entry, so a
    # long thought is a handful of entries instead of thousands on reconnect.
    bc.publish({"type": "thinking", "content": "aa", "thinking_id": "t1", "segment_id": "s1"})
    bc.publish({"type": "thinking", "content": "bb", "thinking_id": "t1", "segment_id": "s1"})
    # A new segment of the same thought stays a separate entry so replayed
    # reasoning keeps its paragraph breaks.
    bc.publish({"type": "thinking", "content": "cc", "thinking_id": "t1", "segment_id": "s2"})
    # A different thought is its own entry.
    bc.publish({"type": "thinking", "content": "dd", "thinking_id": "t2", "segment_id": "s1"})

    thinking = [e["content"] for e in bc.event_log if e.get("type") == "thinking"]
    assert thinking == ["aabb", "cc", "dd"]


def test_thinking_trace_endpoint_serves_exact_full_text(client, auth, db):
    chat_id = str(uuid.uuid4())
    db.add(models.Chat(id=chat_id, title="t", messages=[]))
    db.add(models.ThinkingTrace(
        chat_id=chat_id, thinking_id="think-x", content="full reasoning",
        revision=14, complete=True,
    ))
    db.commit()
    r = client.get(
        f"/api/chats/{chat_id}/thinking-trace/think-x?revision=14",
        headers=auth,
    )
    assert r.status_code == 200
    assert r.text == "full reasoning"
    assert r.headers["x-thinking-revision"] == "14"
    assert r.headers["x-thinking-complete"] == "1"
    assert r.headers["cache-control"] == "private, no-store"


def test_thinking_trace_endpoint_bounds_expansion_preview(client, auth, db):
    chat_id = str(uuid.uuid4())
    content = "reasoning\n" * (THINKING_TRACE_PREVIEW_CHARS // 10 + 1000)
    db.add(models.Chat(id=chat_id, title="t", messages=[]))
    db.add(models.ThinkingTrace(
        chat_id=chat_id,
        thinking_id="think-large",
        content=content,
        revision=len(content),
        complete=True,
    ))
    db.commit()

    r = client.get(
        f"/api/chats/{chat_id}/thinking-trace/think-large"
        f"?revision={len(content)}&preview=1",
        headers=auth,
    )

    assert r.status_code == 200
    assert r.text == content[:THINKING_TRACE_PREVIEW_CHARS]
    assert r.headers["x-thinking-preview-complete"] == "0"
    assert r.headers["x-thinking-complete"] == "1"


def test_thinking_trace_endpoint_404s_when_settled_and_missing(
    client, auth, db,
):
    chat_id = str(uuid.uuid4())
    db.add(models.Chat(id=chat_id, title="t", messages=[]))
    db.commit()
    r = client.get(
        f"/api/chats/{chat_id}/thinking-trace/missing?revision=1",
        headers=auth,
    )
    assert r.status_code == 404
