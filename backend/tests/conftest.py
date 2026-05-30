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
  bc_mod._broadcasts.clear() if hasattr(bc_mod, "_broadcasts") else None

  yield
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
  """Creates an empty chat row with id 'testchat' and returns the Chat model."""
  from app import models
  c = models.Chat(id="testchat", title="Test chat", messages=[])
  db.add(c)
  db.commit()
  db.refresh(c)
  return c
