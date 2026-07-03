"""Tests for recovery version + update-available (Phase 2).

Phase 2 of the recovery self-updating feature (see
docs/superpowers/plans/2026-07-03-recovery-self-updating.md). recoveryd
learns its own version and whether the pinned upstream has a newer
release — read-only surfacing, no apply yet.

These follow the recovery_env fixture pattern in test_recoveryd.py: the
env is set BEFORE re-importing so module-scope constants (DATA_DIR,
LIVE_DIR) pick it up, and RECOVERY_SKIP_INTEGRITY=1 gates the test-only
upstream-URL override. The upstream tests stand up a LOCAL bare git repo
with tags so no network is ever touched — offline is the default.
"""

import importlib
import io
import subprocess
import sys
from email.message import Message
from pathlib import Path

import pytest

# The frozen bundle ships at backend/recovery/. Put it on sys.path so the
# stdlib-only modules import the same way they do inside /app/recovery.
_RECOVERY_DIR = Path(__file__).resolve().parents[1] / "recovery"
if str(_RECOVERY_DIR) not in sys.path:
  sys.path.insert(0, str(_RECOVERY_DIR))


@pytest.fixture()
def recoveryd(monkeypatch, tmp_path):
  """Freshly-imported recoveryd bound to an isolated DATA_DIR.

  Mirrors the recovery_env fixture in test_recoveryd.py: sets the env
  BEFORE re-importing so module-scope path constants (DATA_DIR, LIVE_DIR)
  pick it up. RECOVERY_SKIP_INTEGRITY=1 both bypasses the SELF check and
  gates the test-only upstream-URL override honored by _upstream_url.
  """
  data_dir = tmp_path
  (data_dir / "db").mkdir()
  db_path = data_dir / "db" / "ultimate.db"
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
  monkeypatch.setenv("RECOVERY_SKIP_INTEGRITY", "1")
  # A stray override from another test's environment must not leak in.
  monkeypatch.delenv("RECOVERY_UPSTREAM_URL_TEST", raising=False)
  monkeypatch.delenv("MOBIUS_RECOVERY_EXECED", raising=False)
  for mod in ("recovery_auth", "recovery_db", "recovery_pages", "recoveryd"):
    sys.modules.pop(mod, None)
  return importlib.import_module("recoveryd")


def _run_git(*args: str) -> None:
  """Runs a git command, raising with captured output on failure."""
  subprocess.run(
    ["git", *args],
    check=True,
    capture_output=True,
    text=True,
  )


def _init_upstream(tmp_path: Path, tags) -> str:
  """Creates a bare git repo with `tags` pushed to it, returns its file:// URL.

  The launcher's upstream check runs `git ls-remote --tags --refs <url>`;
  a local bare repo reachable over file:// exercises the real command with
  zero network. Tags are created in a scratch working clone and pushed to
  the bare repo (a bare repo has no worktree to tag against directly).
  """
  bare = tmp_path / "upstream.git"
  _run_git("init", "--bare", "-q", str(bare))
  work = tmp_path / "upstream-work"
  _run_git("init", "-q", str(work))
  _run_git("-C", str(work), "config", "user.email", "t@example.com")
  _run_git("-C", str(work), "config", "user.name", "Test")
  (work / "VERSION").write_text("seed\n", encoding="utf-8")
  _run_git("-C", str(work), "add", "-A")
  _run_git("-C", str(work), "commit", "-q", "-m", "init")
  for tag in tags:
    _run_git("-C", str(work), "tag", tag)
  _run_git("-C", str(work), "remote", "add", "origin", str(bare))
  _run_git("-C", str(work), "push", "-q", "origin", "--tags")
  return f"file://{bare}"


def _init_recovery_upstream(tmp_path: Path, version: str, *,
                            body: str = "# recovery live\nprint('ok')\n") -> str:
  """Creates a bare upstream repo carrying a real recoveryd.py + VERSION,
  tagged v<version>, and returns its file:// URL.

  pull_latest_recovery clones the release tag, so the fixture repo must
  contain the files a real recovery release has (an entrypoint + a VERSION),
  not just the placeholder VERSION `_init_upstream` uses for tag-listing.
  """
  bare = tmp_path / "upstream.git"
  _run_git("init", "--bare", "-q", str(bare))
  work = tmp_path / "upstream-work"
  _run_git("init", "-q", str(work))
  _run_git("-C", str(work), "config", "user.email", "t@example.com")
  _run_git("-C", str(work), "config", "user.name", "Test")
  (work / "recoveryd.py").write_text(body, encoding="utf-8")
  (work / "VERSION").write_text(f"{version}\n", encoding="utf-8")
  _run_git("-C", str(work), "add", "-A")
  _run_git("-C", str(work), "commit", "-q", "-m", "release")
  _run_git("-C", str(work), "tag", f"v{version}")
  _run_git("-C", str(work), "remote", "add", "origin", str(bare))
  _run_git("-C", str(work), "push", "-q", "origin", "HEAD")
  _run_git("-C", str(work), "push", "-q", "origin", "--tags")
  return f"file://{bare}"


def _trusted_live(recoveryd, monkeypatch, version: str | None) -> Path:
  """Populates LIVE_DIR with a recoveryd.py (+ optional VERSION) and makes
  ownership look root-owned (0644) so bundle_is_trusted passes and
  resolve_run_dir() selects it as the running bundle."""
  live = recoveryd.LIVE_DIR
  live.mkdir(parents=True)
  (live / "recoveryd.py").write_text("# live\n", encoding="utf-8")
  if version is not None:
    (live / "VERSION").write_text(version, encoding="utf-8")
  monkeypatch.setattr(recoveryd, "_uid_and_mode", lambda p: (0, 0o644))
  return live


# -- Task 2.1: running_version ---------------------------------------------

def test_running_version_reads_version_file(recoveryd, monkeypatch):
  # A trusted live copy carrying VERSION becomes the running bundle, and
  # running_version reads that file's trimmed contents.
  _trusted_live(recoveryd, monkeypatch, "1.2.3\n")
  assert recoveryd.running_version() == "1.2.3"


def test_running_version_defaults_when_absent(recoveryd, monkeypatch):
  # A running bundle with no VERSION file resolves to the lowest version so
  # any tagged upstream release looks newer.
  _trusted_live(recoveryd, monkeypatch, None)
  assert recoveryd.running_version() == "0.0.0"


def test_running_version_defaults_when_empty(recoveryd, monkeypatch):
  # A blank VERSION file is not a valid version — fall back to "0.0.0".
  _trusted_live(recoveryd, monkeypatch, "   \n")
  assert recoveryd.running_version() == "0.0.0"


def test_baked_bundle_ships_version(recoveryd):
  # The baked floor (no live copy on a fresh volume) must carry a VERSION
  # file so a stock recovery reports a real version, not the "0.0.0"
  # default.
  assert recoveryd.running_version() == "0.1.0"


# -- Task 2.2: _upstream_url seam ------------------------------------------

def test_upstream_url_is_pinned_constant(recoveryd, monkeypatch):
  # With no test override, the pinned constant is used.
  monkeypatch.delenv("RECOVERY_UPSTREAM_URL_TEST", raising=False)
  assert recoveryd._upstream_url() == recoveryd._RECOVERY_UPSTREAM_URL
  assert recoveryd._RECOVERY_UPSTREAM_URL == (
    "https://github.com/mobius-os/recovery.git"
  )


def test_upstream_url_override_honored_under_skip_integrity(recoveryd,
                                                            monkeypatch):
  # The test-only override is honored ONLY together with
  # RECOVERY_SKIP_INTEGRITY=1 (set by the fixture).
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", "file:///tmp/whatever")
  assert recoveryd._upstream_url() == "file:///tmp/whatever"


def test_upstream_url_override_ignored_without_skip_integrity(recoveryd,
                                                              monkeypatch):
  # Production never sets RECOVERY_SKIP_INTEGRITY, so the override is inert
  # and the pinned constant wins — the URL can never be repointed in prod.
  monkeypatch.delenv("RECOVERY_SKIP_INTEGRITY", raising=False)
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", "file:///tmp/evil")
  assert recoveryd._upstream_url() == recoveryd._RECOVERY_UPSTREAM_URL


# -- Task 2.2: latest_upstream_version + update_available ------------------

def test_latest_upstream_version_reads_tags(recoveryd, monkeypatch, tmp_path):
  url = _init_upstream(tmp_path / "up", ["v0.1.0", "v0.2.0"])
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  # Highest tag wins, returned without the leading 'v'.
  assert recoveryd.latest_upstream_version() == "0.2.0"


def test_latest_upstream_version_accepts_unprefixed_tags(recoveryd, monkeypatch,
                                                         tmp_path):
  url = _init_upstream(tmp_path / "up", ["0.1.0", "0.3.0"])
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  assert recoveryd.latest_upstream_version() == "0.3.0"


def test_latest_upstream_version_semver_double_digit(recoveryd, monkeypatch,
                                                     tmp_path):
  # String sort would rank 0.9.0 above 0.10.0; semver compare must not.
  url = _init_upstream(tmp_path / "up", ["v0.9.0", "v0.10.0"])
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  assert recoveryd.latest_upstream_version() == "0.10.0"


def test_latest_upstream_version_ignores_non_semver_tags(recoveryd, monkeypatch,
                                                         tmp_path):
  url = _init_upstream(tmp_path / "up", ["v0.1.0", "nightly", "release-2"])
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  assert recoveryd.latest_upstream_version() == "0.1.0"


def test_latest_upstream_version_none_when_unreachable(recoveryd, monkeypatch,
                                                       tmp_path):
  # A file:// path with no repo behind it fails ls-remote; offline-safe ->
  # None, never a raise.
  missing = tmp_path / "nonexistent.git"
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", f"file://{missing}")
  assert recoveryd.latest_upstream_version() is None


def test_latest_upstream_version_none_on_timeout(recoveryd, monkeypatch):
  # A ls-remote that exceeds the timeout must resolve to None, never hang
  # or raise (offline-safe).
  def boom(*a, **k):
    raise subprocess.TimeoutExpired(cmd="git", timeout=0.01)

  monkeypatch.setattr(recoveryd.subprocess, "run", boom)
  assert recoveryd.latest_upstream_version() is None


def test_update_available_true_when_running_older(recoveryd, monkeypatch,
                                                  tmp_path):
  url = _init_upstream(tmp_path / "up", ["v0.1.0", "v0.2.0"])
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  monkeypatch.setattr(recoveryd, "running_version", lambda: "0.1.0")
  assert recoveryd.update_available() is True


def test_update_available_false_when_up_to_date(recoveryd, monkeypatch,
                                                tmp_path):
  url = _init_upstream(tmp_path / "up", ["v0.1.0", "v0.2.0"])
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  monkeypatch.setattr(recoveryd, "running_version", lambda: "0.2.0")
  # Equal versions is not an update.
  assert recoveryd.update_available() is False


def test_update_available_false_when_running_newer(recoveryd, monkeypatch,
                                                   tmp_path):
  url = _init_upstream(tmp_path / "up", ["v0.1.0", "v0.2.0"])
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  monkeypatch.setattr(recoveryd, "running_version", lambda: "0.9.0")
  assert recoveryd.update_available() is False


def test_update_available_false_when_offline(recoveryd, monkeypatch, tmp_path):
  # No upstream reachable -> latest is None -> no update, never a raise.
  missing = tmp_path / "nonexistent.git"
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", f"file://{missing}")
  monkeypatch.setattr(recoveryd, "running_version", lambda: "0.1.0")
  assert recoveryd.update_available() is False


# -- Task 3.1: pull_latest_recovery ----------------------------------------

def _as_root_with_stubbed_harden(recoveryd, monkeypatch) -> None:
  """Makes pull_latest_recovery runnable from the non-root test process.

  The test user cannot chown to root, so stub the privileged harden step and
  make ownership read as root via the same _uid_and_mode seam the launcher
  tests use. This exercises the real clone -> validate -> atomic-swap
  orchestration with only the two genuinely-privileged operations simulated.
  """
  monkeypatch.setattr(recoveryd.os, "geteuid", lambda: 0)
  monkeypatch.setattr(recoveryd, "_harden_tree", lambda p: True)
  monkeypatch.setattr(recoveryd, "_uid_and_mode", lambda p: (0, 0o644))


def test_pull_latest_recovery_success(recoveryd, monkeypatch, tmp_path):
  url = _init_recovery_upstream(tmp_path / "up", "0.2.0")
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  _as_root_with_stubbed_harden(recoveryd, monkeypatch)
  ok, ver = recoveryd.pull_latest_recovery()
  assert ok is True, ver
  assert ver == "0.2.0"
  live = recoveryd.LIVE_DIR
  assert (live / "recoveryd.py").is_file()
  assert (live / "VERSION").read_text().strip() == "0.2.0"
  # The frozen live copy carries no .git, and the swap leaves no leftovers.
  assert not (live / ".git").exists()
  assert not recoveryd.LIVE_DIR_OLD.exists()
  assert list(recoveryd.DATA_DIR.glob(".recovery-pull-*")) == []


def test_pull_swaps_over_existing_live(recoveryd, monkeypatch, tmp_path):
  live = recoveryd.LIVE_DIR
  live.mkdir(parents=True)
  (live / "recoveryd.py").write_text("# old live\n")
  (live / "VERSION").write_text("0.1.0\n")
  url = _init_recovery_upstream(tmp_path / "up", "0.2.0")
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  _as_root_with_stubbed_harden(recoveryd, monkeypatch)
  ok, ver = recoveryd.pull_latest_recovery()
  assert ok is True, ver
  assert (live / "VERSION").read_text().strip() == "0.2.0"
  assert not recoveryd.LIVE_DIR_OLD.exists()


def test_pull_refuses_when_not_root(recoveryd, monkeypatch, tmp_path):
  url = _init_recovery_upstream(tmp_path / "up", "0.2.0")
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  monkeypatch.setattr(recoveryd.os, "geteuid", lambda: 1000)
  ok, reason = recoveryd.pull_latest_recovery()
  assert ok is False
  assert "root" in reason.lower()
  # Nothing was cloned or swapped when we refused up front.
  assert not recoveryd.LIVE_DIR.exists()


def test_pull_no_upstream_leaves_live_intact(recoveryd, monkeypatch, tmp_path):
  live = recoveryd.LIVE_DIR
  live.mkdir(parents=True)
  (live / "recoveryd.py").write_text("# prior live\n")
  (live / "VERSION").write_text("0.1.0\n")
  missing = tmp_path / "nonexistent.git"
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", f"file://{missing}")
  monkeypatch.setattr(recoveryd.os, "geteuid", lambda: 0)
  ok, reason = recoveryd.pull_latest_recovery()
  assert ok is False
  # A failed pull never touches the existing live copy.
  assert (live / "recoveryd.py").read_text() == "# prior live\n"
  assert (live / "VERSION").read_text() == "0.1.0\n"


def test_pull_rejects_bundle_without_entrypoint(recoveryd, monkeypatch,
                                                tmp_path):
  # Upstream tagged v0.2.0 but its tree has no recoveryd.py -> validation
  # fails and nothing is swapped in.
  url = _init_upstream(tmp_path / "up", ["v0.2.0"])
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  _as_root_with_stubbed_harden(recoveryd, monkeypatch)
  ok, reason = recoveryd.pull_latest_recovery()
  assert ok is False
  assert not recoveryd.LIVE_DIR.exists()
  assert list(recoveryd.DATA_DIR.glob(".recovery-pull-*")) == []


def test_pull_rejects_unparseable_entrypoint(recoveryd, monkeypatch, tmp_path):
  # A recoveryd.py that cannot even be parsed must never become the live
  # copy (a corrupt download that would crash-loop at import).
  url = _init_recovery_upstream(tmp_path / "up", "0.2.0", body="def (:\n")
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  _as_root_with_stubbed_harden(recoveryd, monkeypatch)
  ok, reason = recoveryd.pull_latest_recovery()
  assert ok is False
  assert not recoveryd.LIVE_DIR.exists()


def test_pull_rejects_untrusted_clone(recoveryd, monkeypatch, tmp_path):
  # Even with the harden step stubbed, if the tree still reads as
  # agent-owned the trust check must reject it — nothing is swapped in.
  url = _init_recovery_upstream(tmp_path / "up", "0.2.0")
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  monkeypatch.setattr(recoveryd.os, "geteuid", lambda: 0)
  monkeypatch.setattr(recoveryd, "_harden_tree", lambda p: True)
  monkeypatch.setattr(recoveryd, "_uid_and_mode", lambda p: (1000, 0o644))
  ok, reason = recoveryd.pull_latest_recovery()
  assert ok is False
  assert not recoveryd.LIVE_DIR.exists()


def test_pull_resets_live_attempts(recoveryd, monkeypatch, tmp_path):
  # A fresh successful pull deserves a fresh crash-loop try budget, so it
  # clears any accumulated live-copy attempt count.
  recoveryd._bump_live_attempts()
  recoveryd._bump_live_attempts()
  assert recoveryd._live_attempts() == 2
  url = _init_recovery_upstream(tmp_path / "up", "0.2.0")
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  _as_root_with_stubbed_harden(recoveryd, monkeypatch)
  ok, ver = recoveryd.pull_latest_recovery()
  assert ok is True, ver
  assert recoveryd._live_attempts() == 0


# -- Task 3.3: /recover/update endpoint + Update Recovery button -----------

def _create_owner(recoveryd, username: str, password: str) -> None:
  """Inserts an owner row into recoveryd's isolated DB (mirrors
  test_recoveryd._create_owner) so the session-auth gate has a live owner."""
  import sqlite3

  import bcrypt

  con = sqlite3.connect(str(recoveryd.recovery_db.DB_PATH))
  con.execute(
    "CREATE TABLE IF NOT EXISTS owner "
    "(id INTEGER PRIMARY KEY, username TEXT, hashed_password TEXT)"
  )
  pw = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
  con.execute(
    "INSERT INTO owner (username, hashed_password) VALUES (?, ?)",
    (username, pw),
  )
  con.commit()
  con.close()


def _session_cookie(recoveryd, username: str) -> str:
  token = recoveryd.recovery_auth.create_session_token(username)
  return f"{recoveryd.recovery_auth.COOKIE_NAME}={token}"


def _post(recoveryd, path: str, *, cookie=None, sec_fetch_site=None,
          body: bytes = b""):
  """Drives _Handler.do_POST for `path`, returning captured (code, body,
  content_type) responses.

  Builds a handler via __new__ with fake headers/rfile (the same
  direct-instantiation style test_recoveryd uses) and a stubbed _send that
  records the response, so the REAL routing + cross-site guard + session-auth
  gate + handler run without a live socket. This is the auth-session helper
  Task 3.3 reuses for both /recover/restore and /recover/update.
  """
  h = recoveryd._Handler.__new__(recoveryd._Handler)
  h.command = "POST"
  h.path = path
  h.client_address = ("127.0.0.1", 0)
  h.rfile = io.BytesIO(body)
  h.wfile = io.BytesIO()
  headers = Message()
  headers["Content-Length"] = str(len(body))
  if cookie is not None:
    headers["Cookie"] = cookie
  if sec_fetch_site is not None:
    headers["Sec-Fetch-Site"] = sec_fetch_site
  h.headers = headers
  sent = []
  h._send = (
    lambda code, b, *, content_type="text/html", extra_headers=None:
    sent.append((int(code), b, content_type))
  )
  h.do_POST()
  return sent


def test_dashboard_shows_update_button_when_available(recoveryd):
  pages = recoveryd.recovery_pages
  status = {
    "platform": {"healthy": True},
    "running_version": "0.1.0",
    "available_version": "0.2.0",
    "update_available": True,
  }
  dash = pages.dashboard_html(status)
  assert 'action="/recover/update"' in dash
  assert "Update Recovery" in dash
  assert "0.1.0" in dash and "0.2.0" in dash


def test_dashboard_hides_update_button_when_current(recoveryd):
  pages = recoveryd.recovery_pages
  status = {
    "platform": {"healthy": True},
    "running_version": "0.2.0",
    "available_version": "0.2.0",
    "update_available": False,
  }
  dash = pages.dashboard_html(status)
  assert 'action="/recover/update"' not in dash


def test_dashboard_hides_update_button_when_key_absent(recoveryd):
  # The login/restore render path passes a status with no update fields; the
  # button must not appear then.
  pages = recoveryd.recovery_pages
  dash = pages.dashboard_html({"platform": {"healthy": True}})
  assert 'action="/recover/update"' not in dash


def test_update_requires_session_like_restore(recoveryd):
  _create_owner(recoveryd, "admin", "secret")
  # No session cookie: /recover/update rejects with 401 exactly like
  # /recover/restore.
  r_update = _post(recoveryd, "/recover/update", body=b"")
  r_restore = _post(recoveryd, "/recover/restore", body=b"mode=platform")
  assert r_update[0][0] == 401
  assert r_restore[0][0] == 401


def test_update_rejects_cross_site(recoveryd):
  _create_owner(recoveryd, "admin", "secret")
  sent = _post(recoveryd, "/recover/update", sec_fetch_site="cross-site",
               body=b"")
  assert sent[0][0] == 403


def test_update_with_session_invokes_pull(recoveryd, monkeypatch):
  _create_owner(recoveryd, "admin", "secret")
  monkeypatch.setattr(recoveryd, "_probe_platform_health",
                      lambda *a, **k: None)
  monkeypatch.setattr(recoveryd, "latest_upstream_version",
                      lambda *a, **k: "0.2.0")
  monkeypatch.setattr(recoveryd, "running_version", lambda: "0.2.0")
  called = []
  monkeypatch.setattr(
    recoveryd, "pull_latest_recovery",
    lambda: (called.append(True), (True, "0.2.0"))[1])
  restarts = []
  monkeypatch.setattr(recoveryd, "_restart_recoveryd",
                      lambda: restarts.append(True))
  sent = _post(recoveryd, "/recover/update",
               cookie=_session_cookie(recoveryd, "admin"), body=b"")
  assert called == [True]
  assert sent[0][0] == 200
  assert "Recovery updated to v0.2.0" in sent[0][1]
  # The restart is triggered AFTER the response is rendered.
  assert restarts == [True]


def test_update_failure_reports_reason_no_restart(recoveryd, monkeypatch):
  _create_owner(recoveryd, "admin", "secret")
  monkeypatch.setattr(recoveryd, "_probe_platform_health",
                      lambda *a, **k: None)
  monkeypatch.setattr(recoveryd, "latest_upstream_version",
                      lambda *a, **k: "0.2.0")
  monkeypatch.setattr(recoveryd, "running_version", lambda: "0.1.0")
  monkeypatch.setattr(recoveryd, "pull_latest_recovery",
                      lambda: (False, "no upstream release reachable"))
  restarts = []
  monkeypatch.setattr(recoveryd, "_restart_recoveryd",
                      lambda: restarts.append(True))
  sent = _post(recoveryd, "/recover/update",
               cookie=_session_cookie(recoveryd, "admin"), body=b"")
  assert sent[0][0] == 200
  assert "no upstream release reachable" in sent[0][1]
  # A failed update never restarts.
  assert restarts == []
