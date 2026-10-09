"""Manual synthesis batches preserve every interval without relaxing handoffs."""

import asyncio

import pytest

from app import compaction
from app import providers


class _Provider:
  def check_auth(self, _data_dir):
    return None

  async def ensure_auth(self, _data_dir):
    return None


@pytest.fixture
def fake_auth(monkeypatch):
  monkeypatch.setattr(providers, "get_provider", lambda _id: _Provider())


def _batch_kwargs(checkpoint):
  return dict(data_dir="/unused", provider_id="codex", checkpoint=checkpoint)


@pytest.mark.asyncio
async def test_explicit_batches_cover_over_640kb_in_two_calls_and_resume(fake_auth, monkeypatch):
  source = "".join(chr(65 + i) * compaction._SYNTHESIS_CHUNK_BYTES for i in range(11))
  chunks = list(compaction._utf8_chunks(source, compaction._SYNTHESIS_CHUNK_BYTES))
  seen = []
  saved = []

  async def run(prompt, **_kwargs):
    index = len(seen)
    assert prompt.endswith(chunks[index])
    seen.append(index)
    return f"briefing {index}"

  async def checkpoint(cursor, briefing):
    saved.append((cursor, briefing))

  monkeypatch.setattr(compaction, "_run_provider_summarize_turn", run)
  first = await compaction.summarize_batch(source, **_batch_kwargs(checkpoint))
  assert first == dict(next_chunk=8, briefing="briefing 7", complete=False,
                       total_chunks=11, source_bytes=len(source))
  assert len(saved) == 8
  assert seen == list(range(8))

  second = await compaction.summarize_batch(
    source, start_chunk=first["next_chunk"], briefing=first["briefing"],
    **_batch_kwargs(checkpoint),
  )
  assert second["complete"] is True
  assert second["next_chunk"] == 11
  assert seen == list(range(11))
  assert saved[-1] == (11, "briefing 10")


@pytest.mark.asyncio
async def test_utf8_chunks_are_lossless_bounded_and_lazy(fake_auth, monkeypatch):
  source = "a😀é中" * 30000
  chunks = compaction._utf8_chunks(source, 101)
  assert iter(chunks) is chunks
  parts = list(chunks)
  assert "".join(parts) == source
  assert all(len(part.encode("utf-8")) <= 101 for part in parts)
  assert all(part for part in parts)


@pytest.mark.asyncio
async def test_checkpoint_failure_stops_before_next_provider_call(fake_auth, monkeypatch):
  calls = []
  async def run(_prompt, **_kwargs):
    calls.append(1)
    return "valid"
  async def checkpoint(_cursor, _briefing):
    raise RuntimeError("storage failed")
  monkeypatch.setattr(compaction, "_run_provider_summarize_turn", run)
  with pytest.raises(RuntimeError, match="storage failed"):
    await compaction.summarize_batch("x" * 170000, **_batch_kwargs(checkpoint))
  assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("provider failed"), asyncio.CancelledError()])
async def test_failure_or_cancellation_keeps_earlier_checkpoints(fake_auth, monkeypatch, failure):
  calls = []
  saved = []
  async def run(_prompt, **_kwargs):
    calls.append(1)
    if len(calls) == 2:
      raise failure
    return "kept"
  async def checkpoint(cursor, briefing):
    saved.append((cursor, briefing))
  monkeypatch.setattr(compaction, "_run_provider_summarize_turn", run)
  with pytest.raises(type(failure)):
    await compaction.summarize_batch("x" * 170000, **_batch_kwargs(checkpoint))
  assert saved == [(1, "kept")]
  assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["", "  ", "x" * 60001])
async def test_invalid_provider_output_never_checkpointed(fake_auth, monkeypatch, answer):
  async def run(_prompt, **_kwargs):
    return answer
  saved = []
  async def checkpoint(cursor, briefing):
    saved.append((cursor, briefing))
  monkeypatch.setattr(compaction, "_run_provider_summarize_turn", run)
  with pytest.raises(compaction.CompactionError):
    await compaction.summarize_batch("source", **_batch_kwargs(checkpoint))
  assert saved == []


@pytest.mark.asyncio
async def test_batch_deadline_caps_slow_provider(fake_auth, monkeypatch):
  monkeypatch.setattr(compaction, "_SYNTHESIS_TOTAL_TIMEOUT_SECS", 0.01)
  async def run(_prompt, **_kwargs):
    await asyncio.sleep(1)
    return "late"
  async def checkpoint(_cursor, _briefing):
    raise AssertionError("should not checkpoint")
  monkeypatch.setattr(compaction, "_run_provider_summarize_turn", run)
  with pytest.raises(compaction.CompactionError, match="overall time limit"):
    await compaction.summarize_batch("source", **_batch_kwargs(checkpoint))


@pytest.mark.asyncio
async def test_batch_rejects_oversized_prompt_before_provider_call(fake_auth, monkeypatch):
  async def run(_prompt, **_kwargs):
    raise AssertionError("oversized prompt must not reach provider")
  async def checkpoint(_cursor, _briefing):
    raise AssertionError("oversized prompt must not checkpoint")
  monkeypatch.setattr(compaction, "_run_provider_summarize_turn", run)
  with pytest.raises(compaction.CompactionError, match="briefing is too large"):
    await compaction.summarize_batch(
      "source", custom_instructions="x" * 160000,
      **_batch_kwargs(checkpoint),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor,briefing", [(-1, None), (1, None), (2, "old"), (0, "old"), (1, " ")])
async def test_invalid_resume_state_rejected_before_provider(fake_auth, cursor, briefing):
  async def checkpoint(_cursor, _briefing):
    raise AssertionError("unexpected")
  with pytest.raises(compaction.CompactionError):
    await compaction.summarize_batch(
      "x" * 90000, start_chunk=cursor, briefing=briefing,
      **_batch_kwargs(checkpoint),
    )


@pytest.mark.asyncio
async def test_existing_auto_handoff_still_rejects_oversized_source(fake_auth, monkeypatch):
  async def run(_prompt, **_kwargs):
    raise AssertionError("oversized source must not reach provider")
  monkeypatch.setattr(compaction, "_run_provider_summarize_turn", run)
  with pytest.raises(compaction.CompactionError, match="too large"):
    await compaction.summarize_chat(
      [{"role": "user", "content": "x" * 641000}],
      data_dir="/unused", provider_id="codex",
    )


def test_source_builder_preserves_digest_and_transcript_markers():
  source = compaction.build_synthesis_source(
    [{"role": "user", "content": "hello"}], " digest ",
  )
  assert source == (
    "--- FULL CHAT DIGEST ---\ndigest\n\n"
    "--- CURRENT CHAT TRANSCRIPT ---\nUSER: hello"
  )
  assert compaction.build_synthesis_source(
    [{"role": "user", "content": "hello"}]
  ) == "--- LEGACY CHAT TRANSCRIPT ---\nUSER: hello"
