"""Legacy-row tool-output reducer (contract rule 6). New turns are reduced at
the event funnel (chat.py _ChatEventSink) so their persisted blocks already
carry a bounded excerpt; `_truncate_large_tool_outputs` now only trims the FEW
pre-migration transcripts whose tool blocks still hold the full output inline,
carving them to the SAME head+tail excerpt (non-empty) the funnel produces. A
block already marked output_truncated is left untouched. Tests the pure reducer
plus its no-mutation invariant."""
from app.events import TOOL_OUTPUT_HEAD
from app.routes.chats import (
    _truncate_large_tool_outputs,
    _TOOL_OUTPUT_INLINE_THRESHOLD,
)


def _tool_msg(output, ts=1, extra=None):
    blk = {"type": "tool", "tool": "Read", "input": "f", "output": output}
    if extra:
        blk.update(extra)
    return {"role": "assistant", "ts": ts, "blocks": [blk]}


def test_large_tool_output_is_reduced_to_a_nonempty_excerpt():
    big = "x" * (_TOOL_OUTPUT_INLINE_THRESHOLD + 5000)
    blk = _truncate_large_tool_outputs([_tool_msg(big)])[0]["blocks"][0]
    assert blk["output_truncated"] is True
    assert blk["output_full_len"] == len(big)
    # The excerpt is NON-EMPTY (the head+tail is shown inline; the full output is
    # fetched on expand), and the HEAD is preserved verbatim so a start-anchored
    # failure signal / the first lines survive the carve.
    assert blk["output"] != ""
    assert blk["output"].startswith("x" * TOOL_OUTPUT_HEAD)


def test_bash_failure_head_survives_the_legacy_carve():
    # A legacy Claude-bash failure ("Exit code N\n<stderr>") stays detectable:
    # the start-anchored head is preserved and the exit code is stamped as a
    # field so the frontend chip reads a field, not a re-parse of the excerpt.
    stderr = "boom\n" * 2000
    big = f"Exit code 1\n{stderr}"
    assert len(big) > _TOOL_OUTPUT_INLINE_THRESHOLD
    blk = _truncate_large_tool_outputs([_tool_msg(big)])[0]["blocks"][0]
    assert blk["output"].startswith("Exit code 1\n")
    assert blk["output_exit_code"] == 1


def test_already_truncated_block_is_left_untouched():
    # A block reduced at the funnel already carries output_truncated + a bounded
    # excerpt; the legacy reducer must not re-carve it (or blank it).
    excerpt = "head…[999999 B total]…tail"
    blk = _truncate_large_tool_outputs([
        _tool_msg(excerpt, extra={
            "output_truncated": True,
            "output_full_len": 999999,
            "tool_use_id": "tu_1",
        })
    ])[0]["blocks"][0]
    assert blk["output"] == excerpt
    assert blk["output_full_len"] == 999999
    assert blk["tool_use_id"] == "tu_1"


def test_small_tool_output_stays_inline():
    small = "x" * 100
    blk = _truncate_large_tool_outputs([_tool_msg(small)])[0]["blocks"][0]
    assert blk["output"] == small
    assert "output_truncated" not in blk


def test_threshold_boundary_stays_inline():
    exact = "x" * _TOOL_OUTPUT_INLINE_THRESHOLD
    blk = _truncate_large_tool_outputs([_tool_msg(exact)])[0]["blocks"][0]
    assert "output_truncated" not in blk


def test_non_tool_and_user_messages_untouched():
    big = "x" * (_TOOL_OUTPUT_INLINE_THRESHOLD + 1)
    msgs = [
        {"role": "user", "ts": 1, "blocks": [{"type": "text", "content": big}]},
        {"role": "assistant", "ts": 2, "blocks": [{"type": "text", "content": big}]},
    ]
    assert _truncate_large_tool_outputs(msgs) == msgs


def test_does_not_mutate_input():
    big = "x" * (_TOOL_OUTPUT_INLINE_THRESHOLD + 1)
    msg = _tool_msg(big)
    _truncate_large_tool_outputs([msg])
    assert msg["blocks"][0]["output"] == big
    assert "output_truncated" not in msg["blocks"][0]


def test_legacy_reduction_strips_tool_use_id():
    # A legacy fat block with full output inline (no side-table stash) must NOT
    # keep a stray tool_use_id, or the frontend would call the by-id endpoint
    # (404, no stash) instead of the legacy ?ts=&i= endpoint. The reduced block
    # carries the excerpt + output_truncated but no id.
    big = "x" * (_TOOL_OUTPUT_INLINE_THRESHOLD + 5000)
    blk = _truncate_large_tool_outputs([
        _tool_msg(big, extra={"tool_use_id": "stray_id"})
    ])[0]["blocks"][0]
    assert blk["output_truncated"] is True
    assert "tool_use_id" not in blk
