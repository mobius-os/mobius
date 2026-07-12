"""Server-side stash of large tool outputs (contract rule 6): the StashToolOutput
actor command writes the `tool_outputs` side table keyed by (chat_id,
tool_use_id); the sink reduces the wire event and submits the stash; the
GET /tool-output/{tool_use_id} endpoint serves the full text on expand and 404s
when absent (so the frontend keeps the inline excerpt). Also covers the reducer
carrying tool identity + truncation metadata onto the persisted block."""
import uuid

from app import models
from app.chat_writer import Barrier, StashToolOutput, get_writer
from app.events import (
    TOOL_OUTPUT_INLINE_THRESHOLD,
    process_event,
)


def _flush_writer():
    """Barrier proves the fire-and-forget stash already processed."""
    get_writer().submit(Barrier()).result(timeout=5)


# -- actor round-trip -----------------------------------------------------
def test_stash_round_trip_insert_and_read_back(db):
    big = "z" * (TOOL_OUTPUT_INLINE_THRESHOLD + 100)
    get_writer().submit(
        StashToolOutput(chat_id="c1", tool_use_id="tu_1", output=big)
    ).result(timeout=5)
    row = db.query(models.ToolOutput).filter(
        models.ToolOutput.chat_id == "c1",
        models.ToolOutput.tool_use_id == "tu_1",
    ).first()
    assert row is not None
    assert row.output == big


def test_stash_upsert_last_write_wins(db):
    get_writer().submit(
        StashToolOutput(chat_id="c1", tool_use_id="tu_1", output="first")
    ).result(timeout=5)
    get_writer().submit(
        StashToolOutput(chat_id="c1", tool_use_id="tu_1", output="second")
    ).result(timeout=5)
    rows = db.query(models.ToolOutput).filter(
        models.ToolOutput.chat_id == "c1",
        models.ToolOutput.tool_use_id == "tu_1",
    ).all()
    assert len(rows) == 1
    assert rows[0].output == "second"


def test_stash_ignores_empty_key(db):
    fut = get_writer().submit(
        StashToolOutput(chat_id="", tool_use_id="tu_1", output="x")
    )
    assert fut.result(timeout=5) is False


# -- endpoint -------------------------------------------------------------
def test_tool_output_by_id_endpoint_serves_full_text(client, auth, db):
    chat_id = str(uuid.uuid4())
    db.add(models.Chat(id=chat_id, title="t", messages=[]))
    db.commit()
    big = "hello world\n" * 5000
    get_writer().submit(
        StashToolOutput(chat_id=chat_id, tool_use_id="tu_x", output=big)
    ).result(timeout=5)
    r = client.get(f"/api/chats/{chat_id}/tool-output/tu_x", headers=auth)
    assert r.status_code == 200
    assert r.text == big


def test_tool_output_by_id_endpoint_404_when_absent(client, auth, db):
    chat_id = str(uuid.uuid4())
    db.add(models.Chat(id=chat_id, title="t", messages=[]))
    db.commit()
    r = client.get(f"/api/chats/{chat_id}/tool-output/missing", headers=auth)
    assert r.status_code == 404


def test_tool_output_by_id_endpoint_requires_owner(client, db):
    chat_id = str(uuid.uuid4())
    db.add(models.Chat(id=chat_id, title="t", messages=[]))
    db.commit()
    r = client.get(f"/api/chats/{chat_id}/tool-output/tu_x")
    assert r.status_code == 401


# -- sink reduction + stash ----------------------------------------------
class _FakeBC:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


def _sink(chat_id="c-sink"):
    from app.chat import _ChatEventSink
    return _ChatEventSink(_FakeBC(), chat_id, run_token="rt")


def test_sink_reduces_large_tagged_output_and_stashes_full(db):
    sink = _sink()
    big = "Exit code 1\n" + ("err\n" * 4000)
    assert len(big) > TOOL_OUTPUT_INLINE_THRESHOLD
    event = {"type": "tool_output", "content": big, "tool_use_id": "tu_big"}
    sink._reduce_tool_output(event)
    # Event rewritten to a bounded excerpt with metadata; tool_use_id intact.
    assert event["content"] != big
    assert len(event["content"]) < len(big)
    assert event["output_truncated"] is True
    assert event["output_full_len"] == len(big)
    assert event["output_exit_code"] == 1
    assert event["tool_use_id"] == "tu_big"
    # Full text stashed under the tool_use_id.
    _flush_writer()
    row = db.query(models.ToolOutput).filter(
        models.ToolOutput.chat_id == "c-sink",
        models.ToolOutput.tool_use_id == "tu_big",
    ).first()
    assert row is not None and row.output == big


def test_sink_passes_through_small_output(db):
    sink = _sink()
    small = "ok"
    event = {"type": "tool_output", "content": small, "tool_use_id": "tu_s"}
    sink._reduce_tool_output(event)
    assert event["content"] == small
    assert "output_truncated" not in event
    _flush_writer()
    assert db.query(models.ToolOutput).filter(
        models.ToolOutput.tool_use_id == "tu_s"
    ).first() is None


def test_sink_passes_through_untagged_large_output(db):
    # No tool_use_id -> nothing to key a stash by; leave the full output inline
    # so it rides the legacy ?ts=&i= path against Chat.messages.
    sink = _sink()
    big = "y" * (TOOL_OUTPUT_INLINE_THRESHOLD + 100)
    event = {"type": "tool_output", "content": big}
    sink._reduce_tool_output(event)
    assert event["content"] == big
    assert "output_truncated" not in event
    _flush_writer()
    assert db.query(models.ToolOutput).count() == 0


# -- reducer carries identity + metadata onto the block -------------------
def test_reducer_carries_tool_use_id_from_tool_start():
    blocks = []
    process_event(
        {"type": "tool_start", "tool": "Bash", "input": "ls", "tool_use_id": "tu_9"},
        blocks,
    )
    assert blocks[0]["tool_use_id"] == "tu_9"


def test_reducer_carries_truncation_metadata_from_tool_output():
    blocks = []
    process_event(
        {"type": "tool_start", "tool": "Bash", "input": "ls", "tool_use_id": "tu_9"},
        blocks,
    )
    process_event(
        {
            "type": "tool_output",
            "content": "excerpt…",
            "tool_use_id": "tu_9",
            "output_truncated": True,
            "output_full_len": 123456,
            "output_exit_code": 2,
        },
        blocks,
    )
    blk = blocks[0]
    assert blk["output"] == "excerpt…"
    assert blk["tool_use_id"] == "tu_9"
    assert blk["output_truncated"] is True
    assert blk["output_full_len"] == 123456
    assert blk["output_exit_code"] == 2


def test_reducer_leaves_block_shape_unchanged_without_id_or_metadata():
    blocks = []
    process_event({"type": "tool_start", "tool": "Bash", "input": "ls"}, blocks)
    process_event({"type": "tool_output", "content": "small"}, blocks)
    assert blocks[0] == {
        "type": "tool", "tool": "Bash", "input": "ls",
        "output": "small", "status": "running",
    }
