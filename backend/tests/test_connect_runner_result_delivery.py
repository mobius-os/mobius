"""A finished command's result must never wedge the machine.

The runner retains each result and retries it across reconnects. If a retry
could loop forever on a payload the server will always refuse, one finished
command would pin the single command slot and block every new one. These tests
pin the two behaviors that prevent that: cap the output the runner sends, and
drop a permanently rejected result while still retrying a transient failure.
"""

import io
import urllib.error

from app import connect_runner


def _http_error(code):
    return urllib.error.HTTPError(
        "https://x/api/connect/result", code, "no", {}, io.BytesIO(b""),
    )


def test_cap_output_truncates_keeping_head_and_tail():
    big = "A" * 500_000 + "B" * 800_000
    capped, truncated = connect_runner._cap_output(big)
    assert len(capped) == connect_runner._MAX_RESULT_STREAM
    assert "…[output truncated by runner]…" in capped
    assert capped.startswith("A")
    assert capped.endswith("B")
    assert truncated is True


def test_cap_output_leaves_small_output_untouched():
    assert connect_runner._cap_output("hello") == ("hello", False)
    assert connect_runner._cap_output("") == ("", False)
    assert connect_runner._cap_output(None) == ("", False)


def test_flush_drops_a_permanently_rejected_result(monkeypatch):
    runner = connect_runner._CommandRunner("https://x", "t")
    runner.outbox.append({"type": "result", "request_id": "r1"})
    attempts = []

    def fake_post(url, payload, token=None):
        attempts.append(payload["request_id"])
        raise _http_error(422)

    monkeypatch.setattr(connect_runner, "_post", fake_post)

    # The payload is attempted once and dropped. The runner also asks its stream
    # loop to reconnect immediately, which lets the server clear the command.
    assert runner.flush_pending_results() is True
    assert attempts == ["r1"]
    assert list(runner.outbox) == []
    assert runner.take_reconcile_request() is True
    assert runner.take_reconcile_request() is False


def test_flush_keeps_a_transiently_failed_result(monkeypatch):
    runner = connect_runner._CommandRunner("https://x", "t")
    runner.outbox.append({"type": "result", "request_id": "r1"})

    def fake_post(url, payload, token=None):
        raise _http_error(503)

    monkeypatch.setattr(connect_runner, "_post", fake_post)

    # A server-side or rate-limit failure is recoverable, so the result is
    # retained for the next reconnect rather than discarded.
    assert runner.flush_pending_results() is False
    assert [m["request_id"] for m in runner.outbox] == ["r1"]
    assert runner.take_reconcile_request() is False


def test_flush_retries_a_transient_client_status(monkeypatch):
    runner = connect_runner._CommandRunner("https://x", "t")
    runner.outbox.append({"type": "result", "request_id": "r1"})
    monkeypatch.setattr(
        connect_runner, "_post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(_http_error(425)),
    )

    assert runner.flush_pending_results() is False
    assert [m["request_id"] for m in runner.outbox] == ["r1"]
    assert runner.take_reconcile_request() is False


def test_flush_keeps_a_result_through_a_network_error(monkeypatch):
    runner = connect_runner._CommandRunner("https://x", "t")
    runner.outbox.append({"type": "result", "request_id": "r1"})

    def fake_post(url, payload, token=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(connect_runner, "_post", fake_post)

    assert runner.flush_pending_results() is False
    assert [m["request_id"] for m in runner.outbox] == ["r1"]


def test_post_result_marks_runner_truncation(monkeypatch):
    runner = connect_runner._CommandRunner("https://x", "t")
    monkeypatch.setattr(runner, "flush_pending_results", lambda: False)

    runner._post_result(
        "r1", "x" * (connect_runner._MAX_RESULT_STREAM + 1), "", 0,
        "completed",
    )

    [message] = runner.pending_messages()
    assert len(message["stdout"]) == connect_runner._MAX_RESULT_STREAM
    assert message["truncated"] is True
