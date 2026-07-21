"""Deferred reasoning storage, bounded wire events, and lazy read endpoint."""
import time
import uuid

from app import models
from app.chat import _ChatEventSink
from app.chat_writer import Barrier, StashThinkingTrace, get_writer
from app.events import THINKING_INLINE_THRESHOLD
from app.routes.chats import THINKING_TRACE_PREVIEW_CHARS


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


def test_sink_bounds_wire_and_snapshot_after_cutoff(db):
    bus = _Bus()
    sink = _ChatEventSink(bus, "trace-chat", run_token="rt")
    # Keep this unit test off the periodic transcript path; exercise the
    # snapshot/stash helper explicitly after checking the public events.
    sink._last_save = time.monotonic()
    first = "a" * (THINKING_INLINE_THRESHOLD - 100)
    second = "b" * 200
    sink.publish({"type": "thinking", "content": first, "ts": 1000})
    sink.publish({"type": "thinking", "content": second, "ts": 1100})

    assert bus.events[0]["content"] == first
    assert bus.events[1]["content"] == ""
    assert bus.events[1]["thinking_deferred"] is True
    assert bus.events[1]["thinking_revision"] == len(first + second)
    assert bus.events[0]["thinking_id"] == bus.events[1]["thinking_id"]
    assert sink.assistant_blocks[0]["content"] == first + second

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
