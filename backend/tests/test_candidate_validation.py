"""The candidate-validation contract (ONE_WAY_UPGRADES_DESIGN.md §1).

Only the reviewed image-requiring update paths may validate source against the
target image's level instead of the running image's. The level reaches the
validation child alone, bound to the reviewed target's own compat.py; generic
restart callers never pass a target, and every boot probe and the served
process scrub the variables.
"""

import ast
import os
import re
import subprocess
from pathlib import Path

import pytest

from app import compat
from app import platform_update as pu
import app.restart_util as ru

BACKEND = Path(__file__).resolve().parents[1]
ENTRYPOINT = BACKEND / "scripts" / "entrypoint.sh"

# The child reports what it saw through its verdict: it fails unless the
# candidate variables are exactly what the test expects.
_MAIN_PY = (
  "import os\n"
  "expected = os.environ['EXPECT_CANDIDATE']\n"
  "seen = '%s/%s' % (\n"
  "  os.environ.get('MOBIUS_CANDIDATE_VALIDATION', '-'),\n"
  "  os.environ.get('MOBIUS_CANDIDATE_IMAGE_LEVEL', '-'),\n"
  ")\n"
  "assert seen == expected, 'candidate env was ' + seen\n"
)


def _git(cwd: Path, *args: str) -> str:
  return subprocess.run(
    ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args],
    capture_output=True, text=True, check=True,
  ).stdout.strip()


def _source_tree(root: Path, *, routers_ok: bool = True) -> None:
  app = root / "backend" / "app"
  (app / "routes").mkdir(parents=True, exist_ok=True)
  (app / "__init__.py").write_text("")
  (app / "main.py").write_text(_MAIN_PY)
  verdict = "return None" if routers_ok else "raise RuntimeError('router failed')"
  (app / "routes" / "__init__.py").write_text(
    f"def require_all_routers_loaded():\n  {verdict}\n",
  )


def _commit(root: Path, compat_source: str | None) -> str:
  compat_path = root / "backend" / "app" / "compat.py"
  if compat_source is None:
    compat_path.unlink(missing_ok=True)
  else:
    compat_path.write_text(compat_source)
  _git(root, "add", "-A")
  _git(root, "commit", "-q", "--allow-empty", "-m", "target")
  return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def checkout(tmp_path, monkeypatch):
  root = tmp_path / "platform"
  root.mkdir()
  _git(root, "init", "-q", "-b", "main")
  _source_tree(root)
  # This process must never carry the contract itself; a leaked value here
  # proves generic validation strips it from the child.
  monkeypatch.setenv(compat.CANDIDATE_MARKER_ENV, "1")
  monkeypatch.setenv(compat.CANDIDATE_LEVEL_ENV, "99")
  return root


def test_generic_validation_strips_the_candidate_variables(checkout, monkeypatch):
  _commit(checkout, "COMPAT_LEVEL = 4\n")
  monkeypatch.setenv("EXPECT_CANDIDATE", "-/-")

  ru.validate_restart_source(checkout)

  assert os.environ[compat.CANDIDATE_LEVEL_ENV] == "99"  # parent untouched


def test_a_candidate_target_sets_its_own_level_in_the_child_only(
  checkout, monkeypatch,
):
  target = _commit(checkout, "COMPAT_LEVEL = 4\nREQUIRED_IMAGE_LEVEL = 4\n")
  # The working tree may differ from the target (a merged candidate with
  # local edits); the level comes from the reviewed target commit.
  (checkout / "backend" / "app" / "compat.py").write_text("COMPAT_LEVEL = 9\n")
  monkeypatch.setenv("EXPECT_CANDIDATE", "1/4")

  ru.validate_restart_source(checkout, candidate_target=target)
  ok, err = pu._import_probe(checkout, candidate_target=target)
  assert ok, err

  assert os.environ[compat.CANDIDATE_LEVEL_ENV] == "99"


def test_a_target_without_levels_counts_as_level_zero(checkout, monkeypatch):
  target = _commit(checkout, None)
  monkeypatch.setenv("EXPECT_CANDIDATE", "1/0")

  ru.validate_restart_source(checkout, candidate_target=target)
  assert pu._import_probe(checkout, candidate_target=target) == (True, "")

  target = _commit(checkout, "OTHER = 1\n")
  ru.validate_restart_source(checkout, candidate_target=target)


@pytest.mark.parametrize("source", [
  "COMPAT_LEVEL = '3'\n", "COMPAT_LEVEL = -1\n", "COMPAT_LEVEL = 1 +\n",
])
def test_a_malformed_target_level_fails_validation(checkout, monkeypatch, source):
  target = _commit(checkout, source)
  monkeypatch.setenv("EXPECT_CANDIDATE", "1/0")

  with pytest.raises(ru.RestartSourceInvalid, match="image level"):
    ru.validate_restart_source(checkout, candidate_target=target)
  ok, err = pu._import_probe(checkout, candidate_target=target)
  assert not ok and "candidate image level unreadable" in err


def test_an_unknown_target_fails_validation(checkout, monkeypatch):
  _commit(checkout, "COMPAT_LEVEL = 1\n")
  monkeypatch.setenv("EXPECT_CANDIDATE", "1/1")

  with pytest.raises(ru.RestartSourceInvalid):
    ru.validate_restart_source(checkout, candidate_target="f" * 40)
  assert not pu._import_probe(checkout, candidate_target="f" * 40)[0]


def test_both_validators_apply_the_router_registry_verdict(checkout, monkeypatch):
  _commit(checkout, "COMPAT_LEVEL = 0\n")
  monkeypatch.setenv("EXPECT_CANDIDATE", "-/-")
  assert pu._import_probe(checkout) == (True, "")

  _source_tree(checkout, routers_ok=False)

  ok, err = pu._import_probe(checkout)
  assert not ok and "router failed" in err
  with pytest.raises(ru.RestartSourceInvalid, match="router failed"):
    ru.validate_restart_source(checkout)


def _calls_with_candidate_target(tree: ast.AST) -> list[tuple[str, str]]:
  """(enclosing function, callee) for every call naming candidate_target."""
  found = []
  for func in ast.walk(tree):
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
      continue
    for node in ast.walk(func):
      if isinstance(node, ast.Call) and any(
        kw.arg == "candidate_target" for kw in node.keywords
      ):
        callee = node.func
        name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "?")
        found.append((func.name, name))
  return found


def test_only_the_reviewed_update_paths_pass_a_candidate_target():
  """Generic restart callers (Settings/platform restart routes, owner restart
  cards, the preflight script) never name a target; in the updater only
  ``_prepare`` and the reviewed Apply chain do."""
  sources = [
    *BACKEND.joinpath("app").rglob("*.py"),
    *(path for path in BACKEND.joinpath("scripts").iterdir() if path.is_file()),
  ]
  mentions = {
    path.relative_to(BACKEND).as_posix()
    for path in sources
    if "candidate_target" in path.read_text(encoding="utf-8", errors="replace")
  }
  assert mentions == {"app/platform_update.py", "app/restart_util.py"}

  for route in ("app/routes/platform.py", "app/routes/admin.py", "app/chat_writer.py"):
    text = BACKEND.joinpath(route).read_text(encoding="utf-8")
    assert re.findall(r"validate_restart_source\((.*?)\)", text) == [""]

  tree = ast.parse(BACKEND.joinpath("app/platform_update.py").read_text(encoding="utf-8"))
  direct = {
    (func, callee) for func, callee in _calls_with_candidate_target(tree)
    if callee in {"validate_restart_source", "_import_probe"}
  }
  assert direct == {
    ("_prepare", "validate_restart_source"),
    ("_finalize_update", "_import_probe"),
  }
  # _finalize_update receives a target only through the reviewed Apply chain.
  assert set(_calls_with_candidate_target(tree)) == direct | {
    ("_reconcile_pass", "_finalize_update"),
    ("reconcile_clone", "_reconcile_pass"),
    ("_reconcile_under_lock", "reconcile_clone"),
  }


def test_the_entrypoint_scrub_drops_the_candidate_variables_everywhere():
  script = ENTRYPOINT.read_text(encoding="utf-8")
  scrub = re.search(r'^_env_scrub="([^"]*)"$', script, re.M).group(1)
  for name in (compat.CANDIDATE_MARKER_ENV, compat.CANDIDATE_LEVEL_ENV):
    assert f"-u {name} " in scrub + " "
  # The same scrub wraps every import probe, the boot transaction, and both
  # uvicorn execs.
  assert "$_env_scrub timeout 60 python3 -c" in script
  assert "$_env_scrub PYTHONDONTWRITEBYTECODE=1 timeout 900 python3 -m app.platform_boot" in script
  assert script.count("exec $_env_scrub uvicorn app.main:app") == 2
