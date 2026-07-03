"""Tests for the frozen recovery container (recoveryd).

These exercise the Tier-1 floor in isolation — the recovery bundle
imports ZERO `app.*`, so the tests load it directly off
`backend/recovery/` rather than through the FastAPI app. The point of
recoveryd is to keep working when the platform is broken, so its tests
must not depend on the platform either.
"""

import importlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import bcrypt
import pytest

# The frozen bundle ships at backend/recovery/. Put it on sys.path so the
# stdlib-only modules import the same way they do inside /app/recovery.
_RECOVERY_DIR = Path(__file__).resolve().parents[1] / "recovery"
if str(_RECOVERY_DIR) not in sys.path:
  sys.path.insert(0, str(_RECOVERY_DIR))


@pytest.fixture()
def recovery_env(monkeypatch, tmp_path):
  """Isolated DATA_DIR + DB for one test, with the recovery modules
  freshly imported against it.

  RECOVERY_LIVE_ROOT points the live copy at a tmp dir that is a SIBLING of
  DATA_DIR (not under it), mirroring the prod split where the live copy lives
  on a recoveryd-only volume separate from shared /data.
  """
  data_dir = tmp_path / "data"
  data_dir.mkdir()
  (data_dir / "db").mkdir()
  db_path = data_dir / "db" / "ultimate.db"
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  monkeypatch.setenv("RECOVERY_LIVE_ROOT", str(tmp_path / "recovery-live"))
  monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
  monkeypatch.setenv("RECOVERY_SKIP_INTEGRITY", "1")
  # Force a clean re-import so module-scope path constants pick up the env.
  for mod in ("recovery_auth", "recovery_db", "recovery_pages", "recoveryd"):
    sys.modules.pop(mod, None)
  recovery_auth = importlib.import_module("recovery_auth")
  recovery_db = importlib.import_module("recovery_db")
  recoveryd = importlib.import_module("recoveryd")
  return {
    "data_dir": data_dir,
    "db_path": db_path,
    "auth": recovery_auth,
    "db": recovery_db,
    "recoveryd": recoveryd,
  }


def _create_owner(db_path: Path, username: str, password: str) -> None:
  con = sqlite3.connect(str(db_path))
  con.execute(
    "CREATE TABLE IF NOT EXISTS owner "
    "(id INTEGER PRIMARY KEY, username TEXT, hashed_password TEXT)"
  )
  pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
  con.execute(
    "INSERT INTO owner (username, hashed_password) VALUES (?, ?)",
    (username, pw_hash),
  )
  con.commit()
  con.close()


# -- auth / cookie ----------------------------------------------------------

def test_session_token_roundtrip(recovery_env):
  auth = recovery_env["auth"]
  tok = auth.create_session_token("admin")
  assert auth.decode_session_token(tok) == "admin"
  assert auth.decode_session_token(tok + "x") is None
  assert auth.decode_session_token(None) is None
  assert auth.decode_session_token("garbage") is None


def test_password_verify(recovery_env):
  auth = recovery_env["auth"]
  h = bcrypt.hashpw(b"hunter2", bcrypt.gensalt()).decode()
  assert auth.verify_password("hunter2", h) is True
  assert auth.verify_password("wrong", h) is False
  assert auth.verify_password("anything", "not-a-hash") is False


def test_owner_db_lookup(recovery_env):
  db = recovery_env["db"]
  assert db.owner_exists() is False
  assert db.owner_password_hash("admin") is None
  _create_owner(recovery_env["db_path"], "admin", "secret")
  assert db.owner_exists() is True
  assert db.owner_password_hash("admin") is not None
  assert db.owner_password_hash("nobody") is None
  assert db.owner_exists_for("admin") is True
  assert db.owner_exists_for("nobody") is False


def test_set_cookie_header_shape(recovery_env):
  """The literal Set-Cookie header MUST carry HttpOnly; SameSite=Strict;
  Secure; Path=/recover (the load-bearing CSRF-resistant cookie)."""
  recoveryd = recovery_env["recoveryd"]
  handler = recoveryd._Handler.__new__(recoveryd._Handler)
  header = handler._set_cookie_header("TOKENVALUE")
  assert header.startswith("moebius_recover=TOKENVALUE; ")
  assert "HttpOnly" in header
  assert "SameSite=Strict" in header
  assert "Secure" in header
  assert "Path=/recover" in header
  # Max-Age present and matches the TTL.
  assert f"Max-Age={recovery_env['auth'].SESSION_TTL_SECONDS}" in header


def test_clear_cookie_header_shape(recovery_env):
  recoveryd = recovery_env["recoveryd"]
  handler = recoveryd._Handler.__new__(recoveryd._Handler)
  header = handler._clear_cookie_header()
  assert header.startswith("moebius_recover=; ")
  assert "Max-Age=0" in header
  assert "Path=/recover" in header
  assert "Secure" in header


# -- restore scheduling -----------------------------------------------------

def test_schedule_restore_writes_flags(recovery_env):
  recoveryd = recovery_env["recoveryd"]
  data_dir = recovery_env["data_dir"]
  ok, detail = recoveryd.schedule_restore("platform")
  assert ok is True, detail
  assert (data_dir / ".recover-pending").read_text() == "platform"
  assert (data_dir / ".platform-restart-requested").exists()


def test_schedule_restore_baked_mode(recovery_env):
  recoveryd = recovery_env["recoveryd"]
  data_dir = recovery_env["data_dir"]
  ok, _ = recoveryd.schedule_restore("platform-baked")
  assert ok is True
  assert (data_dir / ".recover-pending").read_text() == "platform-baked"


def test_schedule_restore_rejects_bad_mode(recovery_env):
  recoveryd = recovery_env["recoveryd"]
  data_dir = recovery_env["data_dir"]
  ok, _ = recoveryd.schedule_restore("rm -rf /")
  assert ok is False
  assert not (data_dir / ".recover-pending").exists()
  assert not (data_dir / ".platform-restart-requested").exists()


def test_restore_modes_match_entrypoint_handler():
  """The modes recoveryd can schedule MUST be a subset of what the
  entrypoint's .recover-pending handler accepts, or a scheduled restore
  is silently dropped on next boot."""
  sys.modules.pop("recoveryd", None)
  os.environ["RECOVERY_SKIP_INTEGRITY"] = "1"
  recoveryd = importlib.import_module("recoveryd")
  entrypoint = (
    Path(__file__).resolve().parents[1] / "scripts" / "entrypoint.sh"
  ).read_text()
  # The handler's `case "$mode" in backend|scripts|...|platform|platform-baked)`
  # must list every mode recoveryd may write.
  for mode in recoveryd._RESTORE_MODES:
    assert mode in entrypoint, (
      f"recoveryd mode {mode!r} is not handled by entrypoint.sh"
    )


# -- status -----------------------------------------------------------------

def test_build_status_shape(recovery_env, monkeypatch):
  recoveryd = recovery_env["recoveryd"]
  # Avoid a real network probe in the unit test.
  monkeypatch.setattr(recoveryd, "_probe_platform_health", lambda *a, **k: None)
  status = recoveryd.build_status()
  assert "platform" in status
  assert "last_successful_boot" in status
  assert "cli_creds_present" in status
  assert status["owner_configured"] is False
  _create_owner(recovery_env["db_path"], "admin", "secret")
  assert recoveryd.build_status()["owner_configured"] is True


# -- import isolation -------------------------------------------------------

def test_recovery_modules_have_no_app_imports():
  """CI guard: the frozen bundle must never `import app` / `from app`."""
  for name in ("recoveryd.py", "recovery_auth.py", "recovery_db.py",
               "recovery_pages.py"):
    src = (_RECOVERY_DIR / name).read_text()
    for line in src.splitlines():
      stripped = line.strip()
      assert not stripped.startswith("import app"), f"{name}: {stripped!r}"
      assert not stripped.startswith("from app "), f"{name}: {stripped!r}"
      assert not stripped.startswith("from app."), f"{name}: {stripped!r}"


def test_pages_render(recovery_env):
  """The HTML templates render without raising and carry the recovery
  markers."""
  sys.modules.pop("recovery_pages", None)
  recovery_pages = importlib.import_module("recovery_pages")
  assert "Recovery" in recovery_pages.login_html()
  assert "not set up yet" in recovery_pages.not_configured_html()
  status = {
    "platform": {"healthy": False},
    "last_successful_boot": "2026-06-30T00:00:00Z",
    "cli_creds_present": True,
  }
  dash = recovery_pages.dashboard_html(status)
  assert "Restore platform" in dash
  assert 'value="platform"' in dash
  assert 'value="platform-baked"' in dash
  assert "DOWN" in dash  # platform health reflected
