"""Server-side stash of large tool outputs (contract rule 6): the StashToolOutput
actor command writes the `tool_outputs` side table keyed by (chat_id,
tool_use_id); the sink reduces the wire event and submits the stash; the
GET /tool-output/{tool_use_id} endpoint serves a bounded expansion preview and
the exact text on explicit copy. Also covers the reducer
carrying tool identity + truncation metadata onto the persisted block."""
import json
import uuid

from sqlalchemy import Text, cast, event as sqlalchemy_event, text as sql_text

from app import models
from app.chat_transcript import project_messages_for_detail
from app.chat_writer import (
    Barrier,
    ReplaceTranscript,
    StashToolOutput,
    get_writer,
)
from app.chat_event_sink import ChatEventSink
from app.events import (
    TOOL_OUTPUT_INLINE_THRESHOLD,
    process_event,
)
from app.routes.chats import TOOL_OUTPUT_PREVIEW_CHARS
from app.memory_recall import EMPTY_RECALL_BINDING
from app.tool_output_storage import (
    TOOL_OUTPUT_STORAGE_PREFIX,
    compress_legacy_tool_output_batch,
    decode_tool_output,
)


def _flush_writer():
    """Barrier proves the fire-and-forget stash already processed."""
    get_writer().submit(Barrier()).result(timeout=5)


def _raw_tool_output(db, chat_id, tool_use_id):
    return db.execute(
        models.ToolOutput.__table__.select()
        .with_only_columns(cast(models.ToolOutput.output, Text))
        .where(
            models.ToolOutput.chat_id == chat_id,
            models.ToolOutput.tool_use_id == tool_use_id,
        )
    ).scalar_one()


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
    stored = _raw_tool_output(db, "c1", "tu_1")
    assert stored.startswith(TOOL_OUTPUT_STORAGE_PREFIX)
    assert decode_tool_output(stored) == big


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


def test_legacy_tool_output_backfill_is_bounded_and_idempotent(db):
    big = "legacy\n" * 5000
    db.execute(sql_text(
        "INSERT INTO tool_outputs (chat_id, tool_use_id, output) "
        "VALUES (:chat_id, :tool_use_id, :output)"
    ), {"chat_id": "c1", "tool_use_id": "legacy", "output": big})
    db.commit()

    from app.database import SessionLocal
    first = compress_legacy_tool_output_batch(
        SessionLocal,
        batch_size=1,
    )
    assert first["compressed"] == 1
    db.expire_all()
    stored = _raw_tool_output(db, "c1", "legacy")
    assert stored.startswith(TOOL_OUTPUT_STORAGE_PREFIX)
    assert decode_tool_output(stored) == big

    second = compress_legacy_tool_output_batch(
        SessionLocal,
        batch_size=1,
    )
    assert second["compressed"] == 0


def test_stash_ignores_empty_key(db):
    fut = get_writer().submit(
        StashToolOutput(chat_id="", tool_use_id="tu_1", output="x")
    )
    assert fut.result(timeout=5) is False


def test_sink_stashes_full_edit_diff_and_keeps_private_text_off_wire(db):
    class Bus:
        def __init__(self):
            self.events = []

        def publish(self, event):
            self.events.append(json.loads(json.dumps(event)))

    bus = Bus()
    sink = ChatEventSink(
        bus,
        "chat-edit",
        recall_binding=EMPTY_RECALL_BINDING,
    )
    full = "diff --git a/a b/a\n" + ("+large line\n" * 2500)
    event = {
        "type": "tool_start",
        "tool": "Edit",
        "tool_use_id": "edit-1",
        "edit_preview": {
            "diff": full[:20000],
            "truncated": True,
            "_full_diff": full,
        },
    }

    sink.publish(event)
    _flush_writer()

    preview = bus.events[0]["edit_preview"]
    assert "_full_diff" not in preview
    assert preview["full_id"].startswith("edit-diff-")
    assert sink.assistant_blocks[0]["edit_preview"] == preview
    assert decode_tool_output(_raw_tool_output(
        db, "chat-edit", preview["full_id"],
    )) == full


# -- endpoint -------------------------------------------------------------
def test_chat_edit_diffs_endpoint_expands_only_linked_sidecars(client, auth, db):
    chat_id = str(uuid.uuid4())
    full_id = "edit-diff-" + ("a" * 64)
    orphan_id = "edit-diff-" + ("b" * 64)
    preview = "diff --git a/a b/a\n@@ -1 +1 @@\n-old\n+partial"
    full = preview + " complete"
    db.add(models.Chat(
        id=chat_id,
        title="t",
        messages=[{
            "role": "assistant",
            "ts": 123,
            "blocks": [{
                "type": "tool",
                "tool": "Edit",
                "tool_use_id": "edit-1",
                "edit_preview": {
                    "diff": preview,
                    "truncated": True,
                    "full_id": full_id,
                },
            }],
        }],
    ))
    db.add(models.ToolOutput(chat_id=chat_id, tool_use_id=full_id, output=full))
    db.add(models.ToolOutput(chat_id=chat_id, tool_use_id=orphan_id, output="secret orphan"))
    db.commit()

    response = client.get(f"/api/chats/{chat_id}/edit-diffs", headers=auth)

    assert response.status_code == 200
    assert response.json() == {"entries": [{
        "id": "edit-1",
        "tool": "Edit",
        "ts": 123,
        "preview": {
            "diff": full,
            "truncated": False,
            "full_id": full_id,
        },
    }]}
    assert "secret orphan" not in response.text


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


def test_tool_output_preview_inflates_only_the_bounded_prefix(client, auth, db):
    chat_id = str(uuid.uuid4())
    db.add(models.Chat(id=chat_id, title="t", messages=[]))
    db.commit()
    big = "0123456789" * (TOOL_OUTPUT_PREVIEW_CHARS // 10 + 1000)
    get_writer().submit(
        StashToolOutput(chat_id=chat_id, tool_use_id="tu_preview", output=big)
    ).result(timeout=5)

    r = client.get(
        f"/api/chats/{chat_id}/tool-output/tu_preview?preview=1",
        headers=auth,
    )

    assert r.status_code == 200
    assert r.text == big[:TOOL_OUTPUT_PREVIEW_CHARS]
    assert r.headers["x-tool-output-complete"] == "0"
    assert r.headers["cache-control"] == "private, no-store"
    db.expire_all()
    stored = _raw_tool_output(db, chat_id, "tu_preview")
    assert stored.startswith(TOOL_OUTPUT_STORAGE_PREFIX)
    assert len(stored) < len(big)


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

    statements = []
    engine = db.get_bind()

    def capture_sql(_, __, statement, *args):
        statements.append(statement.lower())

    sqlalchemy_event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        detail = client.get(f"/api/chats/{chat_id}", headers=auth)
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", capture_sql)

    assert detail.status_code == 200
    sidecar_index_sql = next(
        statement
        for statement in statements
        if "from tool_outputs" in statement
    )
    assert "tool_outputs.tool_use_id in" in sidecar_index_sql
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


def test_chat_detail_recovers_a_legacy_memory_receipt_from_its_sidecar(
    client, auth, db,
):
    chat_id = str(uuid.uuid4())
    command = (
        "/bin/bash -lc 'python3 /data/apps/memory/memory_search.py "
        '"where is the navigation decision" "$CHAT_ID"\''
    )
    block = {
        "type": "tool",
        "tool": "Bash",
        "input": command,
        "output": "bounded excerpt without the final receipt",
        "status": "done",
        "tool_use_id": "tu_legacy_memory",
        "output_truncated": True,
        "output_full_len": 50000,
        "output_exit_code": 0,
    }
    # The legacy receipt is only citable because an installed app actually
    # holds shared-memory authority at that path. Recall used to be granted by
    # a slug-shaped regex, so this row was not needed and the platform would
    # happily cite a provider that was never installed.
    db.add(models.App(
        name="Memory",
        description="graph",
        jsx_source="export default function App() { return <div/> }",
        slug="memory",
        source_dir="/data/apps/memory",
        capability_contract={"data": {"shared_memory": "write"}},
    ))
    db.add(models.Chat(
        id=chat_id,
        title="t",
        messages=[{"role": "assistant", "blocks": [block]}],
    ))
    db.add(models.ToolOutput(
        chat_id=chat_id,
        tool_use_id="tu_legacy_memory",
        output=(
            "Selected note contents\n"
            'MOBIUS_MEMORY_RESULT_V1:{"status":"hit","notes":['
            '{"id":"navigation-decision","path":"notes/navigation-decision.md",'
            '"title":"Navigation decision"}]}\n'
        ),
    ))
    db.commit()

    statements = []
    engine = db.get_bind()

    def capture_sql(_, __, statement, *args):
        statements.append(statement.lower())

    sqlalchemy_event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        detail = client.get(
            f"/api/chats/{chat_id}?compact=1",
            headers=auth,
        )
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", capture_sql)

    assert detail.status_code == 200
    projected = detail.json()["messages"][0]["blocks"][0]
    assert "output" not in projected
    assert projected["recall"] == {
        "status": "hit",
        "app_slug": "memory",
        "query": "where is the navigation decision",
        "notes": [{
            "id": "navigation-decision",
            "path": "notes/navigation-decision.md",
            "title": "Navigation decision",
            "app_slug": "memory",
        }],
    }
    memory_tail_sql = [
        statement for statement in statements
        if "from tool_outputs" in statement and "substr(" in statement
    ]
    assert memory_tail_sql, "legacy recovery reads a bounded result tail in SQL"


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
    return _ChatEventSink(
      _FakeBC(), chat_id, run_token="rt",
      recall_binding=EMPTY_RECALL_BINDING,
    )


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


def test_sink_keeps_a_runner_supplied_exit_code_on_large_plain_output(db):
    sink = _sink()
    sink.publish({
        "type": "tool_start", "tool": "Bash", "input": "run",
        "tool_use_id": "tu_typed",
    })
    big = "plain output\n" + ("x" * (TOOL_OUTPUT_INLINE_THRESHOLD + 100))
    event = {
        "type": "tool_output", "content": big, "tool_use_id": "tu_typed",
        "output_exit_code": 7,
    }

    sink.publish(event)

    assert event["output_truncated"] is True
    assert event["output_exit_code"] == 7
    assert sink.assistant_blocks[-1]["output_exit_code"] == 7


def test_sink_parses_a_large_json_envelope_once(monkeypatch, db):
    import app.events as events

    sink = _sink()
    big = json.dumps({
        "stdout": "x" * (TOOL_OUTPUT_INLINE_THRESHOLD + 100),
        "exit_code": 3,
    })
    loads = events.json.loads
    calls = 0

    def counting_loads(value):
        nonlocal calls
        calls += 1
        return loads(value)

    monkeypatch.setattr(events.json, "loads", counting_loads)
    event = {"type": "tool_output", "content": big, "tool_use_id": "tu_json"}
    sink.publish(event)

    assert calls == 1
    assert event["output_exit_code"] == 3


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
