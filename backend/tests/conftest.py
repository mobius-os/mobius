"""Shared test fixtures."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

# Set env vars before importing app modules.
_tmp = tempfile.mkdtemp()
os.environ["SECRET_KEY"] = "test-secret-key-at-least-32-characters-long"
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["DATA_DIR"] = _tmp
os.environ["FRONTEND_ORIGIN"] = "http://localhost:5173"

from app.database import Base, engine
from app.main import app
from app.routes import auth as auth_module
from app.routes.auth import _limiter as auth_limiter

# Disable rate limiters during tests.
app.state.limiter.enabled = False
auth_limiter.enabled = False


@pytest.fixture(autouse=True)
def _isolate_git_env(monkeypatch):
  """Keep the per-app-git tests' `git` subprocesses hermetic.

  app_git tests run `git init/commit/merge` against a repo in tmp_path via
  `git -C <tmp>`. But git EXPORTS `GIT_DIR` (and friends) into a hook's
  environment, and those env vars OVERRIDE `-C` — so when the suite runs
  inside the pre-push hook, the tests' git ops silently operate on the
  enclosing mobius repo instead, flipping `core.bare` and committing stray
  "Initialize app repo" commits (and failing). Scrub the inherited git env
  and pin global/system config to /dev/null + a ceiling so every test git
  op is fully isolated, whether the suite runs from a shell or a git hook.
  """
  for var in (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_NAMESPACE",
  ):
    monkeypatch.delenv(var, raising=False)
  monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
  monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
  monkeypatch.setenv("GIT_CEILING_DIRECTORIES", tempfile.gettempdir())


@pytest.fixture(autouse=True)
def fresh_db():
  """Recreates all tables before each test and clears shared in-memory
  state (broadcasts, starting guards, active procs) so tests don't
  leak state into one another."""
  Base.metadata.drop_all(bind=engine)
  Base.metadata.create_all(bind=engine)
  # Reset to empty dicts — the module declares both as `dict[str, ...]`
  # and the routes do per-username lookups. Setting them to scalar 0
  # (the prior reset, written before auth was rate-limited per-user)
  # made `_ensure_login_tracking_maps` paper over the type mismatch on
  # every login. Cleaner to reset to the right type from the start.
  auth_module._login_failures = {}
  auth_module._login_cooldown_until = {}

  # Clear chat runtime state across tests. Includes the SDK
  # registries even though no current test populates them — once SDK
  # unit tests are added (see _003-tech-debt-and-test-gaps.md TG-2),
  # leaving these uncleared would cross-contaminate.
  from app import chat as chat_mod
  from app import broadcast as bc_mod
  from app import chat_queue as chat_queue_mod
  from app import questions as questions_mod
  from app.runner_registry import registry
  # ticket 033: pending-question registry lives in app.questions;
  # queue locks live in app.chat_queue. Reset both canonical homes.
  questions_mod._pending.clear()
  registry.reset_for_tests()
  # Reset the per-chat queue-lock registry so a lock held by a leaked
  # task from a prior test can't be returned to the next test's caller.
  chat_queue_mod.reset_for_tests()
  # Drop any cached skill text loaded by a prior test; the next caller
  # will re-read from disk. Using setattr in case the attribute is
  # declared lazily below the read-site.
  setattr(chat_mod, "_SKILL_TEXT_CACHE", None)
  chat_mod._clear_after_terminal_generation.clear()
  bc_mod._broadcasts.clear() if hasattr(bc_mod, "_broadcasts") else None
  # Activity log: clear the per-process debounce cache and delete any
  # /data/logs/activity*.jsonl files written by an earlier test so a
  # later assertion on "the log contains exactly N lines" doesn't
  # inherit cruft. Tests that DON'T want activity-log noise can set
  # MOBIUS_ACTIVITY_LOG=off; we leave it on by default so the wiring
  # is exercised on every test that touches a write site. We sweep
  # both the active file and rotated archives — the cross-week read
  # tests write archive files directly, and a leftover archive would
  # show up in any later test's read_events() merged stream.
  from app import activity as activity_mod
  activity_mod._reset_for_tests()
  # The single-writer chat-persistence actor is a process singleton the
  # FastAPI lifespan starts in production. TestClient(app) (no `with`)
  # doesn't run lifespan, and the C2 live write paths now route through
  # `get_writer()`, so start a fresh actor per test bound to the
  # recreated test DB. Restarting each test gives the actor a fresh
  # session that sees the just-created tables — its long-lived session
  # would otherwise hold a stale identity map across the drop/create.
  from app import chat_writer as chat_writer_mod
  chat_writer_mod.stop_writer(timeout=5)
  from app.database import SessionLocal as _WriterSession
  chat_writer_mod.start_writer(_WriterSession)
  import glob as _glob
  import os as _os
  _logs_dir = _os.path.join(
    _os.environ.get("DATA_DIR", "/tmp"), "logs",
  )
  for _stale in _glob.glob(_os.path.join(_logs_dir, "activity*.jsonl")):
    try:
      _os.unlink(_stale)
    except OSError:
      pass

  # The DB is recreated per test, so app_id autoincrement restarts at 1
  # every test — but DATA_DIR is a single module-level tempdir that
  # persists across the whole run. Without wiping the storage trees, the
  # directory for app N accumulates files from every earlier test that
  # also got app_id N, so order-dependent listing assertions see a
  # sibling test's files (this is what made test_list_pagination pass in
  # isolation but fail in the full suite). Clear the per-app and shared
  # file trees so the filesystem matches the freshly-recreated DB.
  import shutil as _shutil
  _data_dir = _os.environ.get("DATA_DIR", "/tmp")
  for _sub in ("apps", "shared"):
    _shutil.rmtree(_os.path.join(_data_dir, _sub), ignore_errors=True)

  yield
  from app import chat_writer as _cw
  _cw.stop_writer(timeout=5)
  Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
  """Returns a FastAPI TestClient."""
  return TestClient(app)


@pytest.fixture
def owner_token(client):
  """Creates an owner account (username 'test') and returns the JWT.

  Several tests create their own tokens via `auth.create_access_token`
  with `sub='test'`; keeping the username aligned avoids 401s on
  download endpoints that look up the owner by sub.
  """
  r = client.post("/api/auth/setup", json={
    "username": "test",
    "password": "testpassword123",
  })
  return r.json()["access_token"]


@pytest.fixture
def auth(owner_token):
  """Authorization header for an owner-authenticated request."""
  return {"Authorization": f"Bearer {owner_token}"}


@pytest.fixture
def db():
  """A short-lived SQLAlchemy session for direct DB manipulation in tests.

  Uses the same engine as the app so writes here are visible to the
  TestClient and vice versa.
  """
  from app.database import SessionLocal
  s = SessionLocal()
  try:
    yield s
  finally:
    s.close()


@pytest.fixture
def chat(db, owner_token):
  """Creates an empty chat row with a UUID4 id and returns the Chat model.

  UUID4 is the production format (str(uuid.uuid4())); using it here
  keeps upload/generate endpoint tests valid after the chat_id format
  check landed in those routes.
  """
  import uuid
  from app import models
  c = models.Chat(id=str(uuid.uuid4()), title="Test chat", messages=[])
  db.add(c)
  db.commit()
  db.refresh(c)
  return c
