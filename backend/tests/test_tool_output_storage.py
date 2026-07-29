import pytest

from app import tool_output_storage


def test_compressed_tool_output_round_trips_unicode_exactly():
    output = ("plain text αβγ🙂\n" * 2000) + "tail"

    stored = tool_output_storage.encode_tool_output(output)

    assert stored.startswith(tool_output_storage.TOOL_OUTPUT_STORAGE_PREFIX)
    assert len(stored) < len(output.encode("utf-8"))
    assert tool_output_storage.tool_output_length(stored) == len(output)
    assert tool_output_storage.decode_tool_output(stored) == output


def test_preview_decode_does_not_use_the_full_inflate_path(monkeypatch):
    output = "0123456789" * 10_000
    stored = tool_output_storage.encode_tool_output(output)

    def reject_full_inflate(_payload):
        raise AssertionError("preview used full zlib.decompress")

    monkeypatch.setattr(
        tool_output_storage.zlib,
        "decompress",
        reject_full_inflate,
    )

    assert tool_output_storage.decode_tool_output(
        stored,
        max_chars=123,
    ) == output[:123]


def test_preview_decode_keeps_complete_unicode_when_byte_limit_splits_a_codepoint():
    output = "x" + ("🙂" * 10_000)
    stored = tool_output_storage.encode_tool_output(output)

    assert tool_output_storage.decode_tool_output(
        stored,
        max_chars=123,
    ) == output[:123]


def test_plain_legacy_rows_remain_readable():
    assert tool_output_storage.decode_tool_output("legacy", max_chars=3) == "leg"
    assert tool_output_storage.tool_output_length("legacy") == 6


def test_corrupt_compressed_frame_fails_loudly():
    with pytest.raises(tool_output_storage.ToolOutputDecodeError):
        tool_output_storage.decode_tool_output(
            tool_output_storage.TOOL_OUTPUT_STORAGE_PREFIX + "12:not-base64",
        )
