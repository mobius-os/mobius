"""Hermetic contracts for the opt-in live trace collector."""
import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "continuity_eval", Path(__file__).resolve().parents[2] / "scripts/chat-continuity-eval.py",
)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


@pytest.mark.parametrize("name,count", [("short", 3), ("medium", 10), ("long", 30)])
def test_progressive_scenarios(name, count):
    prompts = evaluation.scenario(name)
    assert len(prompts) == count
    assert "0042" in prompts[0]
    assert "superseded" in prompts[-1]


def test_dry_run_never_contacts_platform(monkeypatch, capsys):
    def fail(*args, **kwargs):
        pytest.fail("dry run contacted platform")
    monkeypatch.setattr(evaluation, "api", fail)
    assert evaluation.main(["--scenario", "short"]) == 0
    assert "Juniper" in capsys.readouterr().out


def test_fixture_is_explicit_empty_and_model_matched():
    chat = {"title": "Continuity eval: candidate", "messages": [], "provider": "codex",
            "effective_agent_settings": {"model": "chosen-model"}}
    evaluation.validate_fixture(chat, "codex", "chosen-model")
    for patch in [{"title": "Real owner work"}, {"messages": [{"role": "user"}]},
                  {"running": True}, {"provider": "claude"},
                  {"effective_agent_settings": {"model": "wrong"}}]:
        with pytest.raises(ValueError):
            evaluation.validate_fixture({**chat, **patch}, "codex", "chosen-model")


def test_old_idle_run_is_not_new_turn_completion():
    runtime = {"run_id": "old", "run_status": "completed", "running": False}
    assert not evaluation.settled(runtime, "old")
    assert evaluation.settled({**runtime, "run_id": "new"}, "old")
    assert not evaluation.settled({**runtime, "run_id": "new", "pending_messages": [{}]}, "old")


@pytest.mark.parametrize("status", ["parked", "resume_pending", "parked_notified"])
def test_parked_continuation_is_recordable_but_not_a_success(status):
    assert evaluation.settled(
        {"run_id": "new", "run_status": status, "running": False}, "old",
    )


def test_baseline_publication_requires_current_coverage():
    assert not evaluation.baseline_published("## Digest\nOld summary", 2)
    assert not evaluation.baseline_published("source_message_count: 2\n", 4)
    assert evaluation.baseline_published("source_message_count: 4\n", 4)


def test_collector_records_receipts_without_claiming_behavior_pass(tmp_path, monkeypatch):
    chat_id = "11111111-1111-4111-8111-111111111111"
    root = f"/api/chats/{chat_id}"
    sends = []

    def fake_api(path, body=None):
        if path == root:
            return {"title": "Continuity eval: short", "messages": [],
                    "provider": "codex", "effective_agent_settings": {"model": "fixture-model"}}
        if path == root + "/messages":
            sends.append(body)
            return {"accepted": True}
        if path == root + "/runtime":
            return {"run_id": f"run-{len(sends)}", "run_status": "completed", "running": False}
        if path.startswith(root + "/continuity"):
            return {"revision": len(sends), "summary": "Fixture summary", "entries": []}
        if path == root + "?limit=500":
            return {"messages": []}
        if path == root + "/usage":
            return {"totals": {"output_tokens": None}}
        if path == "/api/debug/status":
            return {"memory": {"cgroup": {"current_bytes": 123}}, "active_sdk_sessions": []}
        pytest.fail(f"unexpected API call: {path}")

    monkeypatch.setattr(evaluation, "api", fake_api)
    assert evaluation.main(["--execute", "--chat-id", chat_id, "--provider", "codex",
                            "--model", "fixture-model", "--output", str(tmp_path)]) == 0
    output = next(tmp_path.iterdir())
    result = json.loads((output / "result.json").read_text())
    assert result["state"] == "collected"
    assert result["quality"] == "not graded"
    assert len(sends) == 3
    assert json.loads((output / "03-continuity.json").read_text())["revision"] == 3


def test_collector_records_parked_turn_without_waiting_or_retrying(tmp_path, monkeypatch):
    chat_id = "22222222-2222-4222-8222-222222222222"
    root = f"/api/chats/{chat_id}"
    sends = []
    runtime_reads = 0

    def fake_api(path, body=None):
        nonlocal runtime_reads
        if path == root:
            return {"title": "Continuity eval: parked", "messages": [],
                    "provider": "claude", "effective_agent_settings": {"model": "fixture-model"}}
        if path == root + "/messages":
            sends.append(body)
            return {"accepted": True}
        if path == root + "/runtime":
            runtime_reads += 1
            if runtime_reads == 1:
                return {"run_id": "old", "run_status": "completed", "running": False}
            return {"run_id": "parked-run", "run_status": "parked", "running": False,
                    "runtime_revision": 7, "pending_messages": []}
        if path == root + "/continuity":
            return {"revision": 1, "summary": "Before the provider limit", "entries": []}
        if path == root + "?limit=500":
            return {"messages": [{"role": "user", "content": "fixture"}]}
        if path == root + "/usage":
            return {"runs": [{"id": "parked-run", "status": "parked",
                               "usage": {"limit": "session"}}]}
        if path == "/api/debug/status":
            return {"memory": {"cgroup": {"current_bytes": 456}},
                    "active_sdk_sessions": []}
        pytest.fail(f"unexpected API call: {path}")

    monkeypatch.setattr(evaluation, "api", fake_api)
    monkeypatch.setattr(evaluation.time, "sleep", lambda _: pytest.fail("parked turn was polled again"))

    with pytest.raises(RuntimeError, match="Turn ended parked"):
        evaluation.main(["--execute", "--scenario", "toolburst", "--chat-id", chat_id,
                         "--provider", "claude", "--model", "fixture-model",
                         "--output", str(tmp_path)])

    output = next(tmp_path.iterdir())
    assert len(sends) == 1
    assert runtime_reads == 2
    assert json.loads((output / "01-runtime.json").read_text())["run_status"] == "parked"
    assert json.loads((output / "01-chat.json").read_text())["messages"]
    assert json.loads((output / "01-usage.json").read_text())["runs"][0]["status"] == "parked"
    result = json.loads((output / "result.json").read_text())
    assert result["state"] == "incomplete"
    assert "Turn ended parked" in result["error"]


def test_toolburst_stays_one_turn_and_local():
    prompts = evaluation.scenario("toolburst")
    assert len(prompts) == 1
    assert "/tmp/mobius-continuity-fixture-$CHAT_ID" in prompts[0]
    assert "Run the failing test" in prompts[0]
