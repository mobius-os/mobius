"""Forward-migration for prod chats (card-221, B1 + B2).

Covers the pure planner (`plan_chat_migration`), the `MigrateChat` actor command
(cid backfill + fat-tool-output extraction under an optimistic CAS), the
round-trip verification + rollback-on-failure contract, and the
`scripts/migrate_chat_identity` orchestration.
"""
import pytest
from sqlalchemy import select, update

from app import models
from app.chat_writer import (
    _PersistFailed,
    cid_of,
    get_writer,
    MigrateChat,
    plan_chat_migration,
)
from app.events import (
    TOOL_OUTPUT_INLINE_THRESHOLD,
    excerpt_tool_output,
)
from scripts import migrate_chat_identity


# -- helpers --------------------------------------------------------------
def _big(extra: int = 500) -> str:
    """A tool output comfortably over the inline threshold."""
    return "HEAD\n" + ("x" * (TOOL_OUTPUT_INLINE_THRESHOLD + extra)) + "\nTAIL"


def _fat_tool_msg(ts=100, full=None, **blk_extra) -> dict:
    blk = {"type": "tool", "tool": "Bash", "input": "run", "status": "done",
           "output": full if full is not None else _big()}
    blk.update(blk_extra)
    return {"role": "assistant", "ts": ts, "content": "", "blocks": [blk]}


def _make_chat(db, cid, messages, pending=None, run_status=None,
               deleted_at=None):
    chat = models.Chat(
        id=cid, title="t", messages=messages, pending_messages=pending or [],
        run_status=run_status, deleted_at=deleted_at,
    )
    db.add(chat)
    db.commit()
    return chat


def _reread(db, cid):
    db.expire_all()
    return db.query(models.Chat).filter_by(id=cid).first()


def _migrate(chat_id, dry_run=False, timeout=5):
    return get_writer().submit(
        MigrateChat(chat_id=chat_id, dry_run=dry_run)
    ).result(timeout=timeout)


# -- B2: cid backfill (pure planner) --------------------------------------
def test_backfill_cid_is_legacy_ts():
    row = {"role": "user", "content": "hi", "ts": 5}
    plan = plan_chat_migration([row], [])
    migrated = plan.new_messages[0]
    assert migrated["cid"] == "legacy-5"
    # The migrated row now carries an explicit cid, so cid_of reads it back
    # directly (no read-time derivation from ts).
    assert cid_of(migrated) == "legacy-5"
    assert plan.backfilled == 1
    assert plan.changed is True


def test_backfill_ts_zero_is_legacy_zero():
    # ts is demoted display metadata; ts==0 is valid and the planner must
    # backfill legacy-0 (it guards on `ts is not None`, not truthiness).
    plan = plan_chat_migration([{"role": "user", "ts": 0}], [])
    assert plan.new_messages[0]["cid"] == "legacy-0"
    assert plan.backfilled == 1


def test_present_cid_left_untouched():
    row = {"role": "user", "ts": 5, "cid": "client-abc"}
    plan = plan_chat_migration([row], [])
    assert plan.new_messages[0]["cid"] == "client-abc"
    assert plan.backfilled == 0
    assert plan.changed is False


def test_user_row_no_cid_no_ts_is_unfixable():
    plan = plan_chat_migration([{"role": "user", "content": "orphan"}], [])
    assert plan.backfilled == 0
    assert plan.changed is False
    assert plan.unfixable == [{"kind": "user_no_cid_no_ts", "index": 0}]
    assert "cid" not in plan.new_messages[0]


def test_pending_messages_user_rows_are_backfilled():
    plan = plan_chat_migration([], [{"role": "user", "ts": 9}])
    assert plan.new_pending[0]["cid"] == "legacy-9"
    assert plan.backfilled == 1


def test_assistant_rows_not_given_a_cid():
    plan = plan_chat_migration([{"role": "assistant", "ts": 3, "blocks": []}], [])
    assert "cid" not in plan.new_messages[0]
    assert plan.backfilled == 0


# -- B1: tool-output extraction (pure planner) ----------------------------
def test_extract_fat_block_mints_id_and_excerpts():
    full = _big()
    plan = plan_chat_migration([_fat_tool_msg(ts=100, full=full)], [])
    blk = plan.new_messages[0]["blocks"][0]
    assert blk["tool_use_id"] == "legacy-100-0"
    assert blk["output_truncated"] is True
    assert blk["output_full_len"] == len(full)
    # Excerpt is byte-identical to the shared reducer used on the live + read
    # paths, so migrated and un-migrated blocks look the same to the client.
    expected_excerpt, _, _ = excerpt_tool_output(full)
    assert blk["output"] == expected_excerpt
    assert plan.stashes == [("legacy-100-0", full)]
    assert plan.extracted == 1
    assert plan.bytes_moved == len(full)


def test_extract_carries_exit_code_when_present():
    full = "Exit code 2\n" + ("e" * TOOL_OUTPUT_INLINE_THRESHOLD)
    plan = plan_chat_migration([_fat_tool_msg(ts=7, full=full)], [])
    blk = plan.new_messages[0]["blocks"][0]
    assert blk["output_exit_code"] == 2


def test_small_block_left_inline():
    small = "tiny output"
    plan = plan_chat_migration([_fat_tool_msg(ts=1, full=small)], [])
    blk = plan.new_messages[0]["blocks"][0]
    assert blk["output"] == small
    assert "output_truncated" not in blk
    assert plan.extracted == 0
    assert plan.changed is False


def test_tsless_block_uses_message_index():
    # A legacy assistant message with no ts still gets a unique, fetchable id
    # (message index + block index) — the by-id endpoint does not need ts, so
    # this FIXES blocks the legacy ?ts=&i= reducer had to keep fully inline.
    msg = _fat_tool_msg(full=_big())
    del msg["ts"]
    plan = plan_chat_migration([{"role": "user", "ts": 1}, msg], [])
    blk = plan.new_messages[1]["blocks"][0]
    assert blk["tool_use_id"] == "legacy-m1-0"
    assert blk["output_truncated"] is True


def test_minted_id_suffixed_on_collision_with_existing_id():
    # A real (runner-assigned) block already keys tool_outputs under the id the
    # mint would produce — the synthetic id must not collide with it.
    collide = _fat_tool_msg(ts=100, full=_big())
    collide["blocks"][0]["tool_use_id"] = "legacy-100-0"  # a pre-tagged block
    fresh = _fat_tool_msg(ts=100, full=_big())  # would mint legacy-100-0
    plan = plan_chat_migration([collide, fresh], [])
    # collide already tagged -> not re-extracted; fresh mints a suffixed id.
    assert plan.extracted == 1
    assert plan.new_messages[1]["blocks"][0]["tool_use_id"] == "legacy-100-0-2"


def test_already_reduced_block_is_noop():
    msg = _fat_tool_msg(ts=5, full=_big())
    msg["blocks"][0]["output_truncated"] = True
    msg["blocks"][0]["tool_use_id"] = "tu_live"
    plan = plan_chat_migration([msg], [])
    assert plan.extracted == 0
    assert plan.changed is False


def test_tagged_but_not_truncated_block_not_extracted():
    # Defensive: a block carrying a tool_use_id (already stashed by the funnel)
    # is left alone even if big and lacking output_truncated.
    msg = _fat_tool_msg(ts=5, full=_big())
    msg["blocks"][0]["tool_use_id"] = "tu_live"
    plan = plan_chat_migration([msg], [])
    assert plan.extracted == 0


def test_planner_does_not_mutate_inputs():
    msgs = [{"role": "user", "ts": 5}, _fat_tool_msg(ts=6)]
    orig_repr = repr(msgs)
    plan_chat_migration(msgs, [])
    assert repr(msgs) == orig_repr  # deep-copied, caller's list untouched


# -- MigrateChat actor command (end-to-end through the writer) ------------
def test_migrate_writes_backfill_extract_and_round_trips(db):
    full = _big()
    _make_chat(db, "c1", [
        {"role": "user", "content": "hi", "ts": 1},
        _fat_tool_msg(ts=2, full=full),
    ])
    res = _migrate("c1")
    assert res["status"] == "migrated"
    assert res["backfilled"] == 1
    assert res["extracted"] == 1
    assert res["bytes_moved"] == len(full)

    reread = _reread(db, "c1")
    assert reread.messages[0]["cid"] == "legacy-1"
    blk = reread.messages[1]["blocks"][0]
    assert blk["output_truncated"] is True
    assert blk["tool_use_id"] == "legacy-2-0"
    assert len(blk["output"]) < len(full)

    # The full text round-trips through the side table (what the by-id endpoint
    # serves), byte-identical.
    row = db.query(models.ToolOutput).filter_by(
        chat_id="c1", tool_use_id="legacy-2-0").first()
    assert row is not None
    assert row.output == full

    # The read path serves the migrated block as-is: it already carries the
    # bounded excerpt (< full) and the tool_use_id, so loading the chat ships the
    # excerpt and the by-id fetch path serves the full text on expand.
    assert len(blk["output"]) < len(full)


def test_migrate_coalesces_and_extracts_large_legacy_thinking(db):
    first = "α" * 700
    second = "β" * 700
    _make_chat(db, "thinking-old", [{
        "role": "assistant", "ts": 8, "blocks": [
            {"type": "thinking", "content": first, "duration_ms": 100},
            {"type": "thinking", "content": second, "duration_ms": 250},
        ],
    }])
    res = _migrate("thinking-old")
    assert res["status"] == "migrated"
    assert res["thinking_extracted"] == 1
    block = _reread(db, "thinking-old").messages[0]["blocks"][0]
    assert block == {
        "type": "thinking",
        "thinking_id": "legacy-thinking-8-0",
        "thinking_deferred": True,
        "thinking_revision": 1400,
        "thinking_complete": True,
        "duration_ms": 350,
    }
    row = db.query(models.ThinkingTrace).filter_by(
        chat_id="thinking-old", thinking_id="legacy-thinking-8-0",
    ).one()
    assert row.content == first + second
    assert row.complete is True

def test_migrate_dry_run_reports_without_writing(db):
    _make_chat(db, "c2", [
        {"role": "user", "ts": 1},
        _fat_tool_msg(ts=2),
    ])
    res = _migrate("c2", dry_run=True)
    assert res["status"] == "dry_run"
    assert res["backfilled"] == 1
    assert res["extracted"] == 1

    reread = _reread(db, "c2")
    assert "cid" not in reread.messages[0]              # not written
    assert "output_truncated" not in reread.messages[1]["blocks"][0]
    assert db.query(models.ToolOutput).filter_by(chat_id="c2").count() == 0


def test_migrate_is_idempotent(db):
    _make_chat(db, "c3", [
        {"role": "user", "ts": 1},
        _fat_tool_msg(ts=2),
    ])
    first = _migrate("c3")
    assert first["status"] == "migrated"
    second = _migrate("c3")
    assert second["status"] == "noop"
    assert second["backfilled"] == 0
    assert second["extracted"] == 0
    assert db.query(models.ToolOutput).filter_by(chat_id="c3").count() == 1


def test_migrate_reports_unfixable_rows(db):
    _make_chat(db, "c4", [
        {"role": "user", "content": "orphan"},   # no cid, no ts
        {"role": "user", "ts": 3},               # fixable
    ])
    res = _migrate("c4")
    assert res["status"] == "migrated"
    assert res["backfilled"] == 1
    assert res["unfixable"] == [{"kind": "user_no_cid_no_ts", "index": 0}]
    reread = _reread(db, "c4")
    assert "cid" not in reread.messages[0]
    assert reread.messages[1]["cid"] == "legacy-3"


def test_migrate_skips_active_chat(db):
    _make_chat(db, "c5", [
        {"role": "user", "ts": 1},
        _fat_tool_msg(ts=2),
    ], run_status="running")
    res = _migrate("c5")
    assert res["status"] == "skipped_active"
    reread = _reread(db, "c5")
    assert "cid" not in reread.messages[0]                    # untouched
    assert "output_truncated" not in reread.messages[1]["blocks"][0]
    assert db.query(models.ToolOutput).filter_by(chat_id="c5").count() == 0


def test_migrate_missing_chat(db):
    res = _migrate("does-not-exist")
    assert res["status"] == "missing"


def test_migrate_includes_soft_deleted_chat(db):
    from datetime import UTC, datetime
    _make_chat(db, "c6", [{"role": "user", "ts": 1}, _fat_tool_msg(ts=2)],
               deleted_at=datetime.now(UTC))
    res = _migrate("c6")
    assert res["status"] == "migrated"
    reread = _reread(db, "c6")
    assert reread.messages[0]["cid"] == "legacy-1"
    assert reread.deleted_at is not None      # NOT resurrected


# -- optimistic CAS guard (the cross-process safety mechanism) ------------
def test_cas_guard_run_status_and_updated_at(db):
    from datetime import timedelta
    _make_chat(db, "cx", [], [])
    snap = db.execute(
        select(models.Chat.updated_at).where(models.Chat.id == "cx")
    ).scalar_one()
    assert snap is not None

    # exact snapshot + idle -> applies (rowcount 1)
    r1 = db.execute(
        update(models.Chat)
        .where(models.Chat.id == "cx", models.Chat.run_status.is_(None),
               models.Chat.updated_at == snap)
        .values(messages=[{"a": 1}])
    )
    assert r1.rowcount == 1
    db.commit()

    # stale updated_at (a concurrent write happened) -> no-op (rowcount 0)
    stale = snap - timedelta(seconds=1)
    r2 = db.execute(
        update(models.Chat)
        .where(models.Chat.id == "cx", models.Chat.updated_at == stale)
        .values(messages=[{"b": 2}])
    )
    assert r2.rowcount == 0
    db.rollback()

    # run_status set (a live turn started) -> no-op even with fresh updated_at
    db.execute(update(models.Chat).where(models.Chat.id == "cx")
               .values(run_status="running"))
    db.commit()
    snap2 = db.execute(
        select(models.Chat.updated_at).where(models.Chat.id == "cx")
    ).scalar_one()
    r3 = db.execute(
        update(models.Chat)
        .where(models.Chat.id == "cx", models.Chat.run_status.is_(None),
               models.Chat.updated_at == snap2)
        .values(messages=[{"c": 3}])
    )
    assert r3.rowcount == 0
    db.rollback()


# -- round-trip verification + rollback-on-failure ------------------------
def test_verify_round_trip_detects_mismatch(db):
    actor = get_writer()
    db.add(models.ToolOutput(chat_id="cv", tool_use_id="t", output="stored"))
    db.commit()
    # exact match -> passes silently
    actor._verify_round_trip(db, "cv", [("t", "stored")])
    # different bytes -> raises
    with pytest.raises(_PersistFailed):
        actor._verify_round_trip(db, "cv", [("t", "stored-but-longer")])
    # missing row -> raises
    with pytest.raises(_PersistFailed):
        actor._verify_round_trip(db, "cv", [("absent", "x")])


def test_migrate_rolls_back_on_verify_failure(db, monkeypatch):
    _make_chat(db, "cf", [{"role": "user", "ts": 1}, _fat_tool_msg(ts=2)])
    actor = get_writer()

    def boom(_db, _chat_id, _stashes):
        raise _PersistFailed("injected round-trip failure")

    monkeypatch.setattr(actor, "_verify_round_trip", boom)
    with pytest.raises(_PersistFailed):
        actor.submit(MigrateChat(chat_id="cf")).result(timeout=5)

    # Loud failure, NO silent corruption: block still fat, no stash, cid not set.
    reread = _reread(db, "cf")
    blk = reread.messages[1]["blocks"][0]
    assert "output_truncated" not in blk
    assert "cid" not in reread.messages[0]
    assert db.query(models.ToolOutput).filter_by(chat_id="cf").count() == 0


def test_migrate_defers_on_lock_contention(db, monkeypatch):
    # C1: a transient SQLite lock during the first write (flush / verify / CAS)
    # must surface as a RE-RUNNABLE deferral (deferred_locked), not a hard
    # failure that would exit-1 the migration run — and it must leave NO partial
    # write behind (the flushed stashes roll back with the aborted transaction).
    from sqlalchemy.exc import OperationalError

    _make_chat(db, "cl", [{"role": "user", "ts": 1}, _fat_tool_msg(ts=2)])
    actor = get_writer()

    def locked(_db, _chat_id, _stashes):
        raise OperationalError("stmt", {}, Exception("database is locked"))

    monkeypatch.setattr(actor, "_verify_round_trip", locked)
    res = actor.submit(MigrateChat(chat_id="cl")).result(timeout=5)
    assert res["status"] == "deferred_locked"

    reread = _reread(db, "cl")
    blk = reread.messages[1]["blocks"][0]
    assert "output_truncated" not in blk           # block still fat
    assert "cid" not in reread.messages[0]          # no backfill
    assert db.query(models.ToolOutput).filter_by(chat_id="cl").count() == 0


# -- script orchestration -------------------------------------------------
def test_run_migrates_all_chats(db):
    _make_chat(db, "s1", [{"role": "user", "ts": 1}, _fat_tool_msg(ts=2)])
    _make_chat(db, "s2", [{"role": "user", "ts": 3}])
    results = migrate_chat_identity.run()
    by_id = {r["chat_id"]: r for r in results}
    assert by_id["s1"]["status"] == "migrated"
    assert by_id["s2"]["status"] == "migrated"
    assert _reread(db, "s1").messages[0]["cid"] == "legacy-1"
    assert db.query(models.ToolOutput).filter_by(chat_id="s1").count() == 1


def test_run_single_chat_id(db):
    _make_chat(db, "s3", [{"role": "user", "ts": 1}])
    _make_chat(db, "s4", [{"role": "user", "ts": 2}])
    results = migrate_chat_identity.run(chat_id="s3")
    assert [r["chat_id"] for r in results] == ["s3"]
    assert "cid" not in _reread(db, "s4").messages[0]   # untouched


def test_run_dry_run_does_not_write(db):
    _make_chat(db, "s5", [{"role": "user", "ts": 1}, _fat_tool_msg(ts=2)])
    results = migrate_chat_identity.run(dry_run=True)
    assert results[0]["status"] == "dry_run"
    assert "cid" not in _reread(db, "s5").messages[0]
    assert db.query(models.ToolOutput).filter_by(chat_id="s5").count() == 0


def test_summarize_returns_nonzero_on_failure(capsys):
    code = migrate_chat_identity.summarize(
        [{"chat_id": "x", "status": "failed", "error": "boom"}], dry_run=False)
    assert code == 1
    code_ok = migrate_chat_identity.summarize(
        [{"chat_id": "y", "status": "migrated", "backfilled": 1,
          "extracted": 0, "bytes_moved": 0, "unfixable": []}], dry_run=False)
    assert code_ok == 0
