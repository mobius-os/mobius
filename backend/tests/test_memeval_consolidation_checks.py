from pathlib import Path

from memeval.consolidation_checks import (
  bootstrap_retired,
  broken_router_target_count,
  dangling_wikilink_count,
  duplicate_normalized_title_count,
  hub_fan_in,
  missing_directory_index_count,
  orphan_count,
)
from memeval.corpus import CONSOLIDATION_FIXTURES, write_corpus


def _fixture(fixture_id: str):
  return next(item for item in CONSOLIDATION_FIXTURES if item.id == fixture_id)


def test_clean_fixture_has_no_basic_defects(tmp_path: Path):
  tree = write_corpus(_fixture("clean-cooking"), tmp_path)
  assert orphan_count(tree) == 0
  assert duplicate_normalized_title_count(tree) == 0
  assert dangling_wikilink_count(tree) == 0
  assert missing_directory_index_count(tree) == 0
  assert broken_router_target_count(tree) == 0
  assert bootstrap_retired(tree)


def test_orphan_fixture_is_detected(tmp_path: Path):
  tree = write_corpus(_fixture("defect-orphan"), tmp_path)
  assert orphan_count(tree) == 1


def test_duplicate_title_fixture_is_detected(tmp_path: Path):
  tree = write_corpus(_fixture("defect-duplicate-title"), tmp_path)
  assert duplicate_normalized_title_count(tree) == 1


def test_dangling_link_fixture_is_detected(tmp_path: Path):
  tree = write_corpus(_fixture("defect-dangling-link"), tmp_path)
  assert dangling_wikilink_count(tree) == 1


def test_hub_fan_in_fixture_counts_links(tmp_path: Path):
  tree = write_corpus(_fixture("defect-bad-hub-fan-in"), tmp_path)
  assert hub_fan_in(tree)["hubs/units.md"] == 3


def test_missing_directory_index_fixture_is_detected(tmp_path: Path):
  tree = write_corpus(_fixture("defect-missing-dir-index"), tmp_path)
  assert missing_directory_index_count(tree) == 1


def test_bootstrap_fixture_is_not_retired(tmp_path: Path):
  tree = write_corpus(_fixture("defect-bootstrap-not-retired"), tmp_path)
  assert not bootstrap_retired(tree)


def test_broken_router_target_is_detected(tmp_path: Path):
  (tmp_path / "index.md").write_text(
    "# Router\n\n- Missing [open](missing.md)\n", encoding="utf-8"
  )
  assert broken_router_target_count(tmp_path) == 1
