"""A screenshot step names the chat-media picture it saved (``saved_image``),
so opening it in chat downloads only that picture, never the step's whole
stored result. New steps are stamped by the sink; stored steps recover the
name on read from their persisted excerpt."""
import base64
import json

from app import models
from app.agent_activity import EMPTY_AGENT_ACTIVITY_BINDING
from app.chat_transcript import project_messages_for_detail
from app.chat_writer import Barrier, get_writer
from app.events import excerpt_tool_output
from app.screenshot_steps import saved_screenshot_file

CHAT = "c-shot"
CLAUDE_TOOL = "mcp__mobius_control__screenshot"
CODEX_TOOL = "mobius_control:screenshot"
# Long enough that the stored result is reduced to an excerpt, like a real one.
IMAGE_DATA = base64.b64encode(b"\x89PNG" + bytes(range(256)) * 200).decode("ascii")


def _note(chat_id=CHAT, name="shot-1791452221491094445.png"):
    path = f"/data/chats/{chat_id}/media/{name}"
    return (
        f"Saved {path}. To show the owner, paste "
        f"![screenshot](/api/chats/{chat_id}/media/{name}) before describing it."
    )


def _claude_result(note):
    """Claude's stored shape: the image block as JSON, then plain-text lines."""
    image = json.dumps({"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": IMAGE_DATA,
    }})
    return (
        f"{image}\n[Image: source: /tmp/blob.png, original 2560x1271, "
        f"displayed at 2000x993.]\n{note}"
    )


def _codex_result(note):
    """Codex's stored shape: the MCP result as indented ASCII JSON
    (codex_events._format_json), so the note sits inside a quoted string."""
    return json.dumps({
        "content": [
            {"type": "image", "data": IMAGE_DATA, "mimeType": "image/png"},
            {"type": "text", "text": note},
        ],
        "structuredContent": None,
    }, ensure_ascii=True, indent=2)


# -- naming the saved file -----------------------------------------------

def test_claude_and_codex_results_name_the_same_saved_file():
    for tool, output in (
        (CLAUDE_TOOL, _claude_result(_note())),
        (CODEX_TOOL, _codex_result(_note())),
    ):
        assert saved_screenshot_file(tool, output, CHAT) == "shot-1791452221491094445.png"


def test_a_file_saved_in_another_chat_is_never_named():
    output = _claude_result(_note(chat_id="someone-else"))
    assert saved_screenshot_file(CLAUDE_TOOL, output, CHAT) is None


def test_failed_or_unembeddable_captures_name_nothing():
    assert saved_screenshot_file(
        CLAUDE_TOOL, "screenshot failed: browser did not become ready", CHAT,
    ) is None
    assert saved_screenshot_file(
        CLAUDE_TOOL,
        "Saved /tmp/shot.png; it is outside chat media, so it cannot be embedded.",
        CHAT,
    ) is None


def test_only_the_screenshot_tool_can_name_a_saved_picture():
    # A shell step that merely prints the same sentence is not a screenshot step.
    assert saved_screenshot_file("Bash", _claude_result(_note()), CHAT) is None
    assert saved_screenshot_file(None, _claude_result(_note()), CHAT) is None


def test_the_persisted_excerpt_still_names_the_saved_file():
    # Old steps recover the name from their stored excerpt, so the excerpt
    # must keep the note for both providers' result shapes.
    for tool, output in (
        (CLAUDE_TOOL, _claude_result(_note())),
        (CODEX_TOOL, _codex_result(_note())),
    ):
        excerpt, full_len, _ = excerpt_tool_output(output)
        assert len(excerpt) < full_len
        assert saved_screenshot_file(tool, excerpt, CHAT) == "shot-1791452221491094445.png"


# -- new steps: the sink stamps the name ----------------------------------

class _FakeBC:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


def _sink(chat_id=CHAT):
    from app.chat import _ChatEventSink
    return _ChatEventSink(
        _FakeBC(), chat_id, run_token="rt",
        agent_activity_binding=EMPTY_AGENT_ACTIVITY_BINDING,
    )


def test_a_finished_screenshot_step_names_its_saved_picture(db):
    sink = _sink()
    sink.publish({
        "type": "tool_start", "tool": CLAUDE_TOOL, "input": "app_id=15",
        "tool_use_id": "tu-shot",
    })
    event = {
        "type": "tool_output", "content": _claude_result(_note()),
        "tool_use_id": "tu-shot", "output_complete": True,
    }

    sink.publish(event)

    assert event["output_truncated"] is True
    assert event["saved_image"] == "shot-1791452221491094445.png"
    block = sink.assistant_blocks[-1]
    assert block["saved_image"] == "shot-1791452221491094445.png"
    get_writer().submit(Barrier()).result(timeout=5)
    assert db.query(models.ToolOutput).filter(
        models.ToolOutput.chat_id == CHAT,
        models.ToolOutput.tool_use_id == "tu-shot",
    ).first() is not None


def test_a_shell_step_printing_the_same_note_is_not_stamped(db):
    sink = _sink()
    sink.publish({
        "type": "tool_start", "tool": "Bash", "input": "cat note.txt",
        "tool_use_id": "tu-bash",
    })
    event = {
        "type": "tool_output", "content": _claude_result(_note()),
        "tool_use_id": "tu-bash", "output_complete": True,
    }

    sink.publish(event)

    assert "saved_image" not in event
    assert "saved_image" not in sink.assistant_blocks[-1]


# -- stored steps: the history read recovers the name ---------------------

def _stored_screenshot_step(tool_use_id, output, *, tool=CLAUDE_TOOL):
    excerpt, full_len, _ = excerpt_tool_output(output)
    return {
        "type": "tool", "tool": tool, "input": "app_id=15", "status": "done",
        "tool_use_id": tool_use_id, "output": excerpt,
        "output_truncated": True, "output_full_len": full_len,
    }


def test_a_stored_step_recovers_its_saved_picture_without_rewriting_history():
    stored = _stored_screenshot_step("tu-old", _claude_result(_note()))
    messages = [{"role": "assistant", "blocks": [stored]}]

    projected = project_messages_for_detail(
        messages, chat_id=CHAT, fetchable_tool_output_ids={"tu-old"},
    )

    block = projected[0]["blocks"][0]
    assert block["saved_image"] == "shot-1791452221491094445.png"
    assert "output" not in block
    assert "saved_image" not in stored
    assert "output" in stored


def test_a_stored_step_without_its_full_result_still_names_its_picture():
    stored = _stored_screenshot_step("tu-orphan", _codex_result(_note()), tool=CODEX_TOOL)

    projected = project_messages_for_detail(
        [{"role": "assistant", "blocks": [stored]}],
        chat_id=CHAT, fetchable_tool_output_ids=set(),
    )

    block = projected[0]["blocks"][0]
    assert block["saved_image"] == "shot-1791452221491094445.png"
    assert block["output"] == stored["output"]


def test_a_stored_step_never_names_another_chats_file():
    stored = _stored_screenshot_step("tu-foreign", _claude_result(_note(chat_id="other")))

    projected = project_messages_for_detail(
        [{"role": "assistant", "blocks": [stored]}],
        chat_id=CHAT, fetchable_tool_output_ids={"tu-foreign"},
    )

    assert "saved_image" not in projected[0]["blocks"][0]


def test_a_recorded_name_is_kept_and_the_live_turn_is_untouched():
    recorded = {
        **_stored_screenshot_step("tu-new", _claude_result(_note(name="shot-2.png"))),
        "saved_image": "shot-2.png",
    }
    live = {"role": "assistant", "blocks": [
        _stored_screenshot_step("tu-live", _claude_result(_note())),
    ]}
    messages = [{"role": "assistant", "blocks": [recorded]}, live]

    projected = project_messages_for_detail(
        messages, chat_id=CHAT,
        fetchable_tool_output_ids={"tu-new", "tu-live"}, live_message=live,
    )

    assert projected[0]["blocks"][0]["saved_image"] == "shot-2.png"
    assert projected[1] is live
