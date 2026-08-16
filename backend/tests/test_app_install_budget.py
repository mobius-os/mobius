import json
from types import SimpleNamespace
from unittest.mock import patch

from app.install import candidate_install_bytes


def _candidate(**overrides):
  values = {
    "manifest": {"id": "tiny", "entry": "index.jsx"},
    "entry_bytes": b"export default 1",
    "icon_processed": None,
    "bundled_job": None,
    "static_assets": {},
    "source_files": {},
    "seeds": {},
  }
  values.update(overrides)
  return SimpleNamespace(**values)


def test_candidate_install_estimate_has_repository_and_bundle_floor():
  assert candidate_install_bytes(_candidate()) == 1024 * 1024


def test_candidate_install_estimate_counts_every_fetched_payload_threefold():
  candidate = _candidate(
    entry_bytes=b"e" * 400_000,
    icon_processed=b"i" * 10_000,
    bundled_job=b"j" * 10_000,
    static_assets={"hero.png": b"a" * 20_000},
    source_files={"ui.jsx": b"s" * 20_000},
    seeds={"welcome.json": b"d" * 10_000},
  )
  manifest_bytes = len(
    json.dumps(
      candidate.manifest, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
  )

  assert candidate_install_bytes(candidate) == (
    manifest_bytes + 470_000
  ) * 3


def test_candidate_install_estimate_uses_source_icon_and_normalization_reserve():
  candidate = _candidate(
    manifest={"id": "icon-app", "entry": "index.jsx", "icon": "icon.png"},
    entry_bytes=b"e" * 400_000,
    icon_processed=b"p" * 900_000,
    icon_source_bytes=10_000,
  )
  manifest_bytes = len(
    json.dumps(
      candidate.manifest, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
  )

  assert candidate_install_bytes(candidate) == (
    manifest_bytes + 400_000 + 10_000 + 256 * 1024
  ) * 3


def test_install_budget_endpoint_does_not_fetch_a_package(client, auth):
  budget = {
    "total_bytes": 10_000,
    "free_bytes": 4_000,
    "reserve_bytes": 1_000,
    "available_bytes": 3_000,
  }
  with patch("app.resource_pressure.app_install_storage_budget", return_value=budget):
    response = client.get("/api/apps/install-budget", headers=auth)

  assert response.status_code == 200, response.text
  assert response.json() == budget
