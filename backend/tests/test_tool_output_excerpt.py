"""The tool-output carve (contract rule 6): excerpt_tool_output reduces a large
tool result to a bounded excerpt while preserving the failure signal — a
head+tail for plain text (start-anchored 'Exit code N' survives), and a
re-serialized envelope for JSON (stays valid, exit code intact)."""
import json

from app.events import (
    TOOL_OUTPUT_EXCERPT_MAX,
    TOOL_OUTPUT_HEAD,
    TOOL_OUTPUT_INLINE_THRESHOLD,
    excerpt_tool_output,
)


def test_excerpt_is_nonempty_and_preserves_head_and_tail():
    body = "".join(f"line-{i}\n" for i in range(20000))
    assert len(body) > TOOL_OUTPUT_INLINE_THRESHOLD
    excerpt, full_len, exit_code = excerpt_tool_output(body)
    assert full_len == len(body)
    assert exit_code is None
    assert excerpt  # non-empty
    assert len(excerpt) < len(body)
    assert excerpt.startswith(body[:TOOL_OUTPUT_HEAD])
    assert excerpt.endswith(body[-1024:])
    # The marker announces the true size so the reader knows it is a preview.
    assert str(full_len) in excerpt


def test_bash_failure_head_and_exit_code_survive_truncation():
    stderr = "traceback line\n" * 3000
    content = f"Exit code 137\n{stderr}"
    assert len(content) > TOOL_OUTPUT_INLINE_THRESHOLD
    excerpt, full_len, exit_code = excerpt_tool_output(content)
    # The start-anchored failure head survives, so a frontend re-parse still
    # detects the failure; the exit code is also returned as an explicit field.
    assert excerpt.startswith("Exit code 137\n")
    assert exit_code == 137
    assert full_len == len(content)


def test_json_envelope_stays_valid_json_with_exit_code_intact():
    envelope = {
        "stdout": "S" * 200000,
        "stderr": "E" * 50000,
        "exit_code": 2,
    }
    content = json.dumps(envelope)
    assert len(content) > TOOL_OUTPUT_INLINE_THRESHOLD
    excerpt, full_len, exit_code = excerpt_tool_output(content)
    # The carve NEVER breaks the JSON — a naive mid-string cut would.
    parsed = json.loads(excerpt)
    assert parsed["exit_code"] == 2
    assert exit_code == 2
    # The inner streams are truncated (bounded), not dropped.
    assert len(parsed["stdout"]) < 200000
    assert len(excerpt) < len(content)
    assert full_len == len(content)


def test_json_envelope_exitcode_camelcase_is_read():
    content = json.dumps({"stdout": "x" * 100000, "exitCode": 1})
    _, _, exit_code = excerpt_tool_output(content)
    assert exit_code == 1


def test_json_boolean_field_is_not_mistaken_for_exit_code():
    # A boolean exit_code is nonsense; True == 1 in Python must not leak through.
    content = json.dumps({"stdout": "x" * 100000, "exit_code": True})
    excerpt, _, exit_code = excerpt_tool_output(content)
    assert exit_code is None
    assert json.loads(excerpt)  # still valid JSON


def test_json_under_budget_stays_valid_json():
    # A large-but-not-pathological JSON value (its size is one big string, or a
    # handful of fields) stays valid JSON after per-string carving.
    content = json.dumps([{"line": "y" * 500} for _ in range(20)])
    assert len(content) > TOOL_OUTPUT_INLINE_THRESHOLD
    excerpt, _, exit_code = excerpt_tool_output(content)
    assert len(excerpt) <= TOOL_OUTPUT_EXCERPT_MAX
    json.loads(excerpt)  # must not raise
    assert exit_code is None


def test_pathological_json_is_bounded():
    # A JSON value whose bulk is STRUCTURE (many small strings) re-serializes
    # near full size after per-string carving; the ceiling still bounds it (at
    # the cost of the inline preview's JSON validity — the full is one expand
    # away and the exit code is a separate field).
    content = json.dumps([{"line": "y" * 500} for _ in range(2000)])
    assert len(content) > TOOL_OUTPUT_EXCERPT_MAX * 4
    excerpt, full_len, _ = excerpt_tool_output(content)
    assert len(excerpt) <= TOOL_OUTPUT_EXCERPT_MAX
    assert full_len == len(content)


def test_shell_envelope_stays_valid_under_the_ceiling():
    # The common case must NOT trip the ceiling: a stdout+stderr+exit_code
    # envelope stays valid JSON so the terminal rendering + exit code survive.
    content = json.dumps({
        "stdout": "S" * 500000, "stderr": "E" * 500000, "exit_code": 3,
    })
    excerpt, _, exit_code = excerpt_tool_output(content)
    assert len(excerpt) <= TOOL_OUTPUT_EXCERPT_MAX
    parsed = json.loads(excerpt)  # still valid
    assert parsed["exit_code"] == 3
    assert exit_code == 3
