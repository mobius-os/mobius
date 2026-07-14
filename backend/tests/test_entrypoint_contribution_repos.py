from pathlib import Path


def test_boot_pruner_preserves_durable_contribution_repositories():
  script = (
    Path(__file__).resolve().parents[1] / "scripts" / "entrypoint.sh"
  ).read_text(encoding="utf-8")

  assert "! -path '/data/contrib/*'" in script
  assert "! -path '/data/contributions/*'" in script
  assert "prepared review cards point at their exact" in script
