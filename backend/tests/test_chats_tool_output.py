"""Lazy tool-output truncation (card 163): large tool outputs are dropped to an
output_truncated marker on chat load (the collapsed block already shows its
top-line summary), fetched in full on expand. Tests the pure truncation helper,
including the no-mutation invariant (it must NOT corrupt the stored message
dicts)."""
from app.routes.chats import (
    _truncate_large_tool_outputs,
    _TOOL_OUTPUT_INLINE_THRESHOLD,
)


def _tool_msg(output, ts=1):
    return {
        "role": "assistant", "ts": ts,
        "blocks": [{"type": "tool", "tool": "Read", "input": "f", "output": output}],
    }


def test_large_tool_output_is_dropped_to_marker():
    big = "x" * (_TOOL_OUTPUT_INLINE_THRESHOLD + 5000)
    blk = _truncate_large_tool_outputs([_tool_msg(big)])[0]["blocks"][0]
    assert blk["output_truncated"] is True
    assert blk["output_full_len"] == len(big)
    # No preview is shipped — the collapsed block's top-line summary is enough;
    # the full output is fetched on expand via /tool-output.
    assert blk["output"] == ""


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
