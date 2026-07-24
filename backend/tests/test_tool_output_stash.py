"""Server-side stash of large tool outputs (contract rule 6): the StashToolOutput
actor command writes the `tool_outputs` side table keyed by (chat_id,
tool_use_id); the sink reduces the wire event and submits the stash; the
GET /tool-output/{tool_use_id} endpoint serves a bounded expansion preview and
the exact text on explicit copy. Also covers the reducer
carrying tool identity + truncation metadata onto the persisted block."""
import uuid

from sqlalchemy import event as sqlalchemy_event

from app import models
from app.chat_transcript import project_messages_for_detail
from app.chat_writer import (
    Barrier,
    ReplaceTranscript,
    StashToolOutput,
    get_writer,
)
from app.events import (
    TOOL_OUTPUT_INLINE_THRESHOLD,
    process_event,
)
from app.routes.chats import TOOL_OUTPUT_PREVIEW_CHARS


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
    assert r.headers["cache-control"] == "private, no-store"


def test_tool_output_preview_is_sliced_in_database(client, auth, db):
    chat_id = str(uuid.uuid4())
    db.add(models.Chat(id=chat_id, title="t", messages=[]))
    db.commit()
    big = "0123456789" * (TOOL_OUTPUT_PREVIEW_CHARS // 10 + 1000)
    get_writer().submit(
        StashToolOutput(chat_id=chat_id, tool_use_id="tu_preview", output=big)
    ).result(timeout=5)

    statements = []
    engine = db.get_bind()

    def capture_sql(_, __, statement, *args):
        statements.append(statement.lower())

    sqlalchemy_event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        r = client.get(
            f"/api/chats/{chat_id}/tool-output/tu_preview?preview=1",
            headers=auth,
        )
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", capture_sql)

    assert r.status_code == 200
    assert r.text == big[:TOOL_OUTPUT_PREVIEW_CHARS]
    assert r.headers["x-tool-output-complete"] == "0"
    assert r.headers["cache-control"] == "private, no-store"
    preview_sql = next(
        statement for statement in statements
        if "from tool_outputs" in statement
    )
    assert "substr(" in preview_sql
    assert "length(" not in preview_sql


def test_tool_output_barrier_observes_latest_queued_stash(client, auth, db):
    chat_id = str(uuid.uuid4())
    db.add(models.Chat(id=chat_id, title="t", messages=[]))
    db.add(models.ToolOutput(
        chat_id=chat_id,
        tool_use_id="tu_latest",
        output="intermediate",
    ))
    db.commit()

    # Do not await this write. The endpoint's own Barrier must queue behind it
    # and prevent the already-committed intermediate row from winning the read.
    get_writer().submit(StashToolOutput(
        chat_id=chat_id,
        tool_use_id="tu_latest",
        output="final",
    ))
    r = client.get(
        f"/api/chats/{chat_id}/tool-output/tu_latest?preview=1",
        headers=auth,
    )

    assert r.status_code == 200
    assert r.text == "final"
    assert r.headers["x-tool-output-complete"] == "1"


def test_tool_output_by_id_endpoint_404_when_absent(client, auth, db):
    chat_id = str(uuid.uuid4())
    db.add(models.Chat(id=chat_id, title="t", messages=[]))
    db.commit()
    r = client.get(f"/api/chats/{chat_id}/tool-output/missing", headers=auth)
    assert r.status_code == 404


def test_tool_output_by_id_endpoint_202_while_chat_is_running(
    client, auth, db, monkeypatch,
):
    chat_id = str(uuid.uuid4())
    db.add(models.Chat(id=chat_id, title="t", messages=[]))
    db.commit()
    monkeypatch.setattr("app.routes.chats.is_chat_running", lambda _: True)

    r = client.get(
        f"/api/chats/{chat_id}/tool-output/missing?preview=1",
        headers=auth,
    )

    assert r.status_code == 202
    assert r.headers["retry-after"] == "1"


def test_tool_output_by_id_endpoint_requires_owner(client, db):
    chat_id = str(uuid.uuid4())
    db.add(models.Chat(id=chat_id, title="t", messages=[]))
    db.commit()
    r = client.get(f"/api/chats/{chat_id}/tool-output/tu_x")
    assert r.status_code == 401


def test_settled_chat_detail_uses_lazy_sidecar_for_large_output(
    client, auth, db,
):
    chat_id = str(uuid.uuid4())
    db.add(models.Chat(id=chat_id, title="t", messages=[]))
    db.commit()
    block = {
        "type": "tool",
        "tool": "Bash",
        "input": "long command",
        "output": "bounded excerpt",
        "status": "complete",
        "tool_use_id": "tu_detail",
        "output_truncated": True,
        "output_full_len": 40000,
        "output_exit_code": 0,
    }
    get_writer().submit(ReplaceTranscript(
        chat_id=chat_id,
        messages=[{"role": "assistant", "blocks": [block]}],
    )).result(timeout=5)
    get_writer().submit(StashToolOutput(
        chat_id=chat_id,
        tool_use_id="tu_detail",
        output="complete output",
    )).result(timeout=5)

    detail = client.get(f"/api/chats/{chat_id}", headers=auth)

    assert detail.status_code == 200
    projected = detail.json()["messages"][0]["blocks"][0]
    assert "output" not in projected
    assert projected["tool_use_id"] == "tu_detail"
    assert projected["output_truncated"] is True
    preview = client.get(
        f"/api/chats/{chat_id}/tool-output/tu_detail?preview=1",
        headers=auth,
    )
    assert preview.status_code == 200
    assert preview.text == "complete output"


def test_running_chat_detail_strips_history_but_keeps_live_excerpt(
    client, auth, db, monkeypatch,
):
    chat_id = str(uuid.uuid4())
    historical_block = {
        "type": "tool",
        "output": "historical excerpt",
        "tool_use_id": "tu_history",
        "output_truncated": True,
    }
    live_block = {
        "type": "tool",
        "output": "live excerpt",
        "tool_use_id": "tu_live",
        "output_truncated": True,
    }
    db.add(models.Chat(
        id=chat_id,
        title="t",
        messages=[
            {"role": "assistant", "blocks": [historical_block], "ts": 1},
            {"role": "user", "content": "continue", "ts": 2},
        ],
        live_assistant={"role": "assistant", "blocks": [live_block], "ts": 3},
    ))
    db.commit()
    get_writer().submit(StashToolOutput(
        chat_id=chat_id,
        tool_use_id="tu_history",
        output="complete historical output",
    )).result(timeout=5)
    monkeypatch.setattr("app.routes.chats.is_chat_running", lambda _: True)

    detail = client.get(f"/api/chats/{chat_id}", headers=auth)

    assert detail.status_code == 200
    messages = detail.json()["messages"]
    assert "output" not in messages[0]["blocks"][0]
    assert messages[-1]["blocks"][0]["output"] == "live excerpt"


def test_settled_detail_omits_fetchable_large_output_excerpt_without_mutation():
    messages = [{
        "role": "assistant",
        "blocks": [{
            "type": "tool",
            "tool": "Bash",
            "input": "long command",
            "output": "bounded excerpt",
            "status": "complete",
            "tool_use_id": "tu_large",
            "output_truncated": True,
            "output_full_len": 50000,
            "output_exit_code": 1,
        }],
    }]

    projected = project_messages_for_detail(
        messages,
        fetchable_tool_output_ids={"tu_large"},
    )

    assert projected is not messages
    assert projected[0] is not messages[0]
    assert projected[0]["blocks"] is not messages[0]["blocks"]
    block = projected[0]["blocks"][0]
    assert "output" not in block
    assert block["input"] == "long command"
    assert block["tool_use_id"] == "tu_large"
    assert block["output_truncated"] is True
    assert block["output_full_len"] == 50000
    assert block["output_exit_code"] == 1
    assert messages[0]["blocks"][0]["output"] == "bounded excerpt"


def test_detail_projection_keeps_only_live_and_unfetchable_outputs_inline():
    messages = [
        {
            "role": "assistant",
            "blocks": [
                {
                    "type": "tool",
                    "output": "historical excerpt",
                    "tool_use_id": "tu_history",
                    "output_truncated": True,
                },
                {"type": "tool", "output": "small", "tool_use_id": "tu_small"},
                {
                    "type": "tool",
                    "output": "legacy excerpt",
                    "output_truncated": True,
                },
            ],
        },
        {
            "role": "assistant",
            "blocks": [{
                "type": "tool",
                "output": "live excerpt",
                "tool_use_id": "tu_live",
                "output_truncated": True,
            }],
        },
    ]

    projected = project_messages_for_detail(
        messages,
        fetchable_tool_output_ids={"tu_history"},
        live_message=messages[-1],
    )

    assert projected is not messages
    assert "output" not in projected[0]["blocks"][0]
    assert projected[0]["blocks"][1]["output"] == "small"
    assert projected[0]["blocks"][2]["output"] == "legacy excerpt"
    assert projected[1] is messages[1]
    assert projected[1]["blocks"][0]["output"] == "live excerpt"


def test_detail_projection_with_only_a_live_message_is_identity_stable():
    live = {
        "role": "assistant",
        "blocks": [{
            "type": "tool",
            "output": "live excerpt",
            "tool_use_id": "tu_live",
            "output_truncated": True,
        }],
    }
    messages = [live]

    assert project_messages_for_detail(
        messages,
        fetchable_tool_output_ids={"tu_live"},
        live_message=live,
    ) is messages


def test_detail_projection_keeps_excerpt_when_sidecar_is_missing():
    messages = [{
        "role": "assistant",
        "blocks": [{
            "type": "tool",
            "output": "still useful excerpt",
            "tool_use_id": "tu_missing",
            "output_truncated": True,
        }],
    }]

    assert project_messages_for_detail(
        messages,
        fetchable_tool_output_ids=set(),
    ) is messages


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


def test_sink_mints_id_and_stashes_untagged_large_output(db):
    # A large tool_output with no tool_use_id is unexpected post-card-221 (both
    # runners tag universally). Rather than strand the text inline (the retired
    # dual-read ?ts=&i= fallback), the sink mints a stash id, stamps it on the
    # event, reduces the wire event, and stashes the full text so it stays
    # fetchable by id.
    sink = _sink()
    big = "y" * (TOOL_OUTPUT_INLINE_THRESHOLD + 100)
    event = {"type": "tool_output", "content": big}
    sink._reduce_tool_output(event)
    assert event["output_truncated"] is True
    assert event["content"] != big
    minted = event["tool_use_id"]
    assert minted  # a synthetic id was stamped on the event
    _flush_writer()
    row = db.query(models.ToolOutput).filter(
        models.ToolOutput.chat_id == "c-sink",
        models.ToolOutput.tool_use_id == minted,
    ).first()
    assert row is not None and row.output == big


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
