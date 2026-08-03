"""Retention coverage for the opt-in field performance probe."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.routes import debug as debug_module


@pytest.fixture
def perf_probe(monkeypatch, tmp_path):
  path = tmp_path / "logs" / "perf-samples.jsonl"
  monkeypatch.setattr(debug_module, "_PERF_SAMPLE_LIMIT", 5)
  monkeypatch.setattr(debug_module, "_PERF_SAMPLE_TRIM_TARGET", 3)
  monkeypatch.setattr(debug_module, "_perf_sample_count", None)
  monkeypatch.setattr(debug_module, "_perf_sample_path", lambda: path)
  return path


def test_capped_perf_ingest_trims_in_batches_not_on_every_write(
  client, auth, monkeypatch, perf_probe,
):
  trim_calls = 0
  original_trim = debug_module._trim_perf_samples

  def count_trim(path):
    nonlocal trim_calls
    trim_calls += 1
    return original_trim(path)

  monkeypatch.setattr(debug_module, "_trim_perf_samples", count_trim)

  for sequence in range(8):
    response = client.post(
      "/api/debug/perf", headers=auth, json={"sequence": sequence},
    )
    assert response.json() == {"stored": True}

  assert trim_calls == 1
  assert len(perf_probe.read_text(encoding="utf-8").splitlines()) == 5

  response = client.post(
    "/api/debug/perf", headers=auth, json={"sequence": 8},
  )
  assert response.json() == {"stored": True}
  assert trim_calls == 2
  assert len(perf_probe.read_text(encoding="utf-8").splitlines()) == 3


def test_restart_and_concurrent_perf_writes_keep_retention_bounded(
  perf_probe,
):
  perf_probe.parent.mkdir(parents=True)
  perf_probe.write_text(
    "".join(json.dumps({"seed": index}) + "\n" for index in range(5)),
    encoding="utf-8",
  )
  # None is the state after process restart: the first writer reconstructs the
  # count from the durable file before deciding whether to trim.
  debug_module._perf_sample_count = None

  with ThreadPoolExecutor(max_workers=8) as pool:
    list(pool.map(
      lambda index: debug_module._append_perf_sample(
        perf_probe, json.dumps({"concurrent": index}),
      ),
      range(40),
    ))

  lines = perf_probe.read_text(encoding="utf-8").splitlines()
  assert len(lines) <= debug_module._PERF_SAMPLE_LIMIT
  assert len({line for line in lines}) == len(lines)
  assert all(isinstance(json.loads(line), dict) for line in lines)
