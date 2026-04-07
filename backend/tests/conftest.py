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
  """Recreates all tables before each test."""
  Base.metadata.drop_all(bind=engine)
  Base.metadata.create_all(bind=engine)
  auth_module._login_failures = 0
  auth_module._login_cooldown_until = 0.0
  yield
  Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
  """Returns a FastAPI TestClient."""
  return TestClient(app)


@pytest.fixture
def owner_token(client):
  """Creates an owner account and returns the JWT."""
  r = client.post("/api/auth/setup", json={
    "username": "testowner",
    "password": "testpassword123",
  })
  return r.json()["access_token"]
