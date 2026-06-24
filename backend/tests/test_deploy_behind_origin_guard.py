"""deploy-prod.sh must HARD-BLOCK a checkout that is behind origin/main.

The recurring prod incident: a sibling agent session runs deploy-prod.sh from
a checkout that is BEHIND origin/main. deploy-prod builds from the WORKING TREE,
so the stale checkout bakes an old image and silently REVERTS everyone's pushed
work (the served frontend regresses to an old bundle). The fix is a hard block
(exit 2) gated to prod, with --allow-stale as the deliberate-rollback escape.

The behind guard is the mirror image of the pre-existing unpushed guard, which
blocks the OPPOSITE failure (HEAD ahead of / diverged from origin/main, exit 1).
The two must not collide: the unpushed guard owns ahead + diverged; the behind
guard owns strictly-behind. HEAD == origin/main and HEAD ahead must both PASS
the behind guard.

Two kinds of test here:

1. Text assertions — the script wires the exact predicate, flag, exit code, and
   prod scoping. Cheap regression net, no git or Docker needed.
2. Behavioral — the REAL guard text is sliced out of the script and run against
   live temp git repos covering all four HEAD/origin-main relationships, so the
   test exercises the actual `merge-base --is-ancestor` direction rather than a
   re-derived copy of it.
"""

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

# Repo-root scripts/, not backend/scripts/ — deploy-prod.sh lives at the top.
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy-prod.sh"

# Marker comments that bracket the behind guard in the script. The behavioral
# harness slices the real guard out between them, so the test runs the shipped
# source instead of a copy that could drift from it.
GUARD_START = "# ── behind-origin/main guard"
GUARD_END = "# ── clean-tree guard"


def _read() -> str:
  return SCRIPT.read_text()


def _guard_source() -> str:
  """The real guard block, sliced from the script between its marker comments."""
  text = _read()
  start = text.index(GUARD_START)
  end = text.index(GUARD_END, start)
  block = text[start:end]
  # The guard opens with `if [ -n "$main_sha" ] && ...` and closes with `fi`;
  # slice up to and including that closing `fi` so we eval a complete statement.
  fi = block.rindex("\n    fi")
  return block[: fi + len("\n    fi")]


# ── text assertions ──────────────────────────────────────────────────────


def test_deploy_script_exists():
  assert SCRIPT.is_file(), f"missing {SCRIPT}"


def test_allow_stale_flag_is_parsed():
  text = _read()
  assert "--allow-stale) ALLOW_STALE=1 ;;" in text, \
    "the --allow-stale escape hatch must be parsed in the flag loop"
  assert 'ALLOW_STALE="${ALLOW_STALE:-0}"' in text, \
    "ALLOW_STALE must default off and honor the env override"


def test_behind_guard_uses_origin_main_ancestor_of_head_predicate():
  """The block direction is the crux: origin/main NOT an ancestor of HEAD means
  HEAD lacks commits that are on main (behind/diverged)."""
  guard = _guard_source()
  assert "merge-base --is-ancestor origin/main HEAD" in guard, \
    "behind guard must test `origin/main` is an ancestor of HEAD"
  # Must be the negated form — block when it's NOT an ancestor.
  assert re.search(r"!\s*git[^\n]*merge-base --is-ancestor origin/main HEAD", guard), \
    "behind guard must BLOCK when origin/main is NOT an ancestor of HEAD"


def test_behind_guard_exits_2_and_names_the_remedy():
  guard = _guard_source()
  assert "exit 2" in guard, "behind guard must exit 2 (distinct from unpushed's exit 1)"
  assert "rev-list --count HEAD..origin/main" in guard, \
    "must report how many commits the checkout is missing"
  assert "rebase origin/main" in guard or "pull --ff-only" in guard, \
    "must tell the operator how to bring the checkout current"


def test_behind_guard_skips_when_origin_main_unresolvable():
  """No network/remote: main_sha is empty, the guard must NOT hard-block."""
  guard = _guard_source()
  assert '[ -n "$main_sha" ]' in guard, \
    "behind guard must only run when origin/main resolved (offline => skip, no block)"


def test_behind_guard_is_prod_only():
  """The guard lives inside the `if [ "$TARGET" = "prod" ]` source-safety block,
  so the test target (throwaway checkouts) is exempt."""
  text = _read()
  prod_block = text.index('if [ "$TARGET" = "prod" ]; then', text.index("prod source-safety guard"))
  guard_at = text.index(GUARD_START)
  next_top_level = text.index('# ── step 1: build', guard_at)
  assert prod_block < guard_at < next_top_level, \
    "behind guard must sit inside the prod-only source-safety block"


def test_unpushed_guard_still_present():
  """The behind guard must not regress the pre-existing unpushed (HEAD-ahead)
  protection — both guards coexist."""
  text = _read()
  assert "merge-base --is-ancestor HEAD origin/main" in text, \
    "the unpushed guard (HEAD ancestor of origin/main) must remain"
  assert "--allow-unpushed) ALLOW_UNPUSHED=1 ;;" in text, \
    "the --allow-unpushed escape hatch must remain"


def test_deploy_script_still_parses():
  result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
  assert result.returncode == 0, result.stderr


# ── behavioral tests against a real temp git repo ─────────────────────────


def _git(repo: Path, *args: str) -> str:
  env = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
  }
  return subprocess.run(
    ["git", "-C", str(repo), *args],
    check=True, capture_output=True, text=True, env=env,
  ).stdout.strip()


def _commit(repo: Path, msg: str) -> str:
  (repo / "f").write_text(msg)
  _git(repo, "add", "f")
  _git(repo, "commit", "-q", "-m", msg)
  return _git(repo, "rev-parse", "HEAD")


def _run_guard(
  repo: Path, *, allow_stale: bool, fetch_ok: bool = True
) -> subprocess.CompletedProcess:
  """Run the REAL guard text against `repo` with the same variable scaffolding
  the script provides (REPO_ROOT, main_sha, head_sha, fetch_ok, ALLOW_STALE,
  helpers)."""
  head_sha = _git(repo, "rev-parse", "HEAD")
  main_sha = _git(repo, "rev-parse", "origin/main")
  harness = textwrap.dedent(f"""\
    set -euo pipefail
    warn() {{ printf 'WARN %s\\n' "$1"; }}
    fail() {{ printf 'FAIL %s\\n' "$1" >&2; }}
    info() {{ printf 'INFO %s\\n' "$1"; }}
    REPO_ROOT={repo}
    ALLOW_STALE={1 if allow_stale else 0}
    fetch_ok={1 if fetch_ok else 0}
    head_sha={head_sha}
    main_sha={main_sha}
    """) + _guard_source() + "\n"
  return subprocess.run(
    ["bash", "-c", harness], capture_output=True, text=True,
  )


@pytest.fixture
def repo_with_origin(tmp_path):
  """A repo whose `origin` is a sibling bare-ish remote, so origin/main is real.
  Returns (local_repo, remote_repo) where the local has fetched origin/main."""
  remote = tmp_path / "remote"
  remote.mkdir()
  _git(remote, "init", "-q", "-b", "main")
  base = _commit(remote, "base")
  local = tmp_path / "local"
  _git(tmp_path, "clone", "-q", str(remote), str(local))
  return local, remote, base


def test_head_equals_origin_main_passes(repo_with_origin):
  local, _remote, _base = repo_with_origin
  r = _run_guard(local, allow_stale=False)
  assert r.returncode == 0, f"HEAD==origin/main should PASS, got {r.returncode}: {r.stderr}"


def test_head_ahead_of_origin_main_passes(repo_with_origin):
  """An unpushed feature deploy is legitimate for the behind guard — origin/main
  is still an ancestor of HEAD."""
  local, _remote, _base = repo_with_origin
  _commit(local, "local-only feature")  # HEAD now ahead of origin/main
  r = _run_guard(local, allow_stale=False)
  assert r.returncode == 0, f"HEAD ahead should PASS the behind guard, got {r.returncode}: {r.stderr}"


def test_head_behind_origin_main_blocks_exit_2(repo_with_origin):
  local, remote, _base = repo_with_origin
  # Advance the remote and fetch it. Local HEAD stays at the clone's base, which
  # origin/main now strictly contains plus one more — so HEAD is strictly behind.
  _commit(remote, "newer on main")
  _git(local, "fetch", "-q", "origin")
  r = _run_guard(local, allow_stale=False)
  assert r.returncode == 2, f"strictly-behind should exit 2, got {r.returncode}: {r.stdout}{r.stderr}"
  assert "BEHIND origin/main" in r.stderr


def test_head_behind_with_allow_stale_passes(repo_with_origin):
  local, remote, _base = repo_with_origin
  _commit(remote, "newer on main")
  _git(local, "fetch", "-q", "origin")
  r = _run_guard(local, allow_stale=True)
  assert r.returncode == 0, f"behind + --allow-stale should PASS, got {r.returncode}: {r.stderr}"
  assert "deploying anyway" in r.stdout.lower()


def test_diverged_passes_the_behind_guard(repo_with_origin):
  """Diverged (HEAD has its own commit AND origin/main has one HEAD lacks) is
  owned by the unpushed guard upstream, NOT the behind guard. The behind guard's
  two-sided predicate (HEAD must be an ancestor of origin/main) is false for a
  diverged HEAD, so the behind guard is a no-op here — it must NOT relabel a
  diverged checkout as 'behind' (this is the --allow-unpushed + diverged collision
  the strict-behind predicate exists to avoid)."""
  local, remote, _base = repo_with_origin
  _commit(local, "local divergent")       # HEAD ahead with its own commit
  _commit(remote, "remote divergent")     # origin/main ahead with a different one
  _git(local, "fetch", "-q", "origin")
  r = _run_guard(local, allow_stale=False)
  assert r.returncode == 0, \
    f"behind guard must PASS diverged (unpushed guard owns it), got {r.returncode}: {r.stderr}"


def test_failed_fetch_warns_not_blocks_when_current(repo_with_origin):
  """Fetch failed (offline) and HEAD matches the cached origin/main: must NOT
  hard-block (offline-is-a-warning contract), only warn staleness is unverified."""
  local, _remote, _base = repo_with_origin
  r = _run_guard(local, allow_stale=False, fetch_ok=False)
  assert r.returncode == 0, f"failed-fetch must not hard-block, got {r.returncode}: {r.stderr}"
  assert "UNVERIFIED" in r.stdout


def test_failed_fetch_does_not_block_even_when_cached_ref_is_ahead(repo_with_origin):
  """The strongest offline case (Finding 2/3): the cached origin/main is AHEAD of
  HEAD (the checkout looks strictly behind), but the fetch FAILED — so the ref is
  untrusted and we must WARN, never hard-block. Hard-blocking is gated on
  fetch_ok=1, so this exits 0 with the unverified warning."""
  local, remote, _base = repo_with_origin
  _commit(remote, "newer on main")
  _git(local, "fetch", "-q", "origin")   # cached origin/main now ahead of HEAD
  r = _run_guard(local, allow_stale=False, fetch_ok=False)
  assert r.returncode == 0, \
    f"offline must not hard-block even when the cached ref looks ahead, got {r.returncode}: {r.stderr}"
  assert "UNVERIFIED" in r.stdout
  assert "BEHIND origin/main" not in r.stderr


# ── combined-guards ordering: both real guards run together ────────────────


def _both_guards_source() -> str:
  """The unpushed guard + the behind guard, sliced together from the script, so
  a combined harness can verify their interaction (which exits first, which
  message fires) — Finding 4 from the adversarial review."""
  text = _read()
  start = text.index("# ── unpushed-commit guard")
  end = text.index(GUARD_END, start)
  block = text[start:end]
  fi = block.rindex("\n    fi")
  return block[: fi + len("\n    fi")]


def _run_both_guards(
  repo: Path, *, allow_unpushed: bool, allow_stale: bool
) -> subprocess.CompletedProcess:
  harness = textwrap.dedent(f"""\
    set -euo pipefail
    warn() {{ printf 'WARN %s\\n' "$1"; }}
    fail() {{ printf 'FAIL %s\\n' "$1" >&2; }}
    info() {{ printf 'INFO %s\\n' "$1"; }}
    REPO_ROOT={repo}
    ALLOW_UNPUSHED={1 if allow_unpushed else 0}
    ALLOW_STALE={1 if allow_stale else 0}
    fetch_ok=1
    head_sha=$(git -C "$REPO_ROOT" rev-parse HEAD)
    main_sha=$(git -C "$REPO_ROOT" rev-parse origin/main)
    """) + _both_guards_source() + "\n"
  return subprocess.run(["bash", "-c", harness], capture_output=True, text=True)


def test_combined_diverged_exits_1_via_unpushed_guard(repo_with_origin):
  """Diverged with no overrides: the unpushed guard fires FIRST (exit 1), the
  behind guard never runs — so the operator gets the unpushed message, not a
  misleading 'behind' one."""
  local, remote, _base = repo_with_origin
  _commit(local, "local divergent")
  _commit(remote, "remote divergent")
  _git(local, "fetch", "-q", "origin")
  r = _run_both_guards(local, allow_unpushed=False, allow_stale=False)
  assert r.returncode == 1, f"diverged should exit 1 (unpushed guard), got {r.returncode}"
  assert "not on origin/main" in r.stderr


def test_combined_diverged_with_allow_unpushed_is_not_relabeled_behind(repo_with_origin):
  """The Finding 1 regression: --allow-unpushed makes the unpushed guard
  warn-and-continue on diverged; the behind guard must then NOT block it as
  'behind' (its two-sided predicate excludes diverged)."""
  local, remote, _base = repo_with_origin
  _commit(local, "local divergent")
  _commit(remote, "remote divergent")
  _git(local, "fetch", "-q", "origin")
  r = _run_both_guards(local, allow_unpushed=True, allow_stale=False)
  assert r.returncode == 0, \
    f"--allow-unpushed + diverged must pass both guards, got {r.returncode}: {r.stderr}"
  assert "BEHIND origin/main" not in r.stderr, "diverged must not be relabeled 'behind'"


def test_combined_strictly_behind_exits_2_via_behind_guard(repo_with_origin):
  """Strictly behind with no overrides: the unpushed guard PASSES (HEAD is an
  ancestor of origin/main), then the behind guard fires (exit 2)."""
  local, remote, _base = repo_with_origin
  _commit(remote, "newer on main")
  _git(local, "fetch", "-q", "origin")
  r = _run_both_guards(local, allow_unpushed=False, allow_stale=False)
  assert r.returncode == 2, f"strictly behind should exit 2 (behind guard), got {r.returncode}"
  assert "BEHIND origin/main" in r.stderr


def test_combined_head_ahead_passes_both(repo_with_origin):
  """An unpushed feature deploy with --allow-unpushed must clear both guards."""
  local, _remote, _base = repo_with_origin
  _commit(local, "local-only feature")
  r = _run_both_guards(local, allow_unpushed=True, allow_stale=False)
  assert r.returncode == 0, f"HEAD ahead + --allow-unpushed should pass, got {r.returncode}: {r.stderr}"
