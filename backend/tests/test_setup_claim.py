"""Tests for the first-boot claim gate (card 261) — app.setup_claim + the
POST /api/auth/setup gating, fail-closed lifecycle, atomic publication,
env-preset precedence/validation, uniform-403 behavior, no-leak status, and
the /api/fs denial of the claim file.
"""

import os
import stat
import threading

import pytest
from fastapi.testclient import TestClient

from app import setup_claim
from app.config import get_settings
from app.main import app
from app import models
from tests.conftest import SETUP_CLAIM


def _data_dir():
  return get_settings().data_dir


def _owner_count(db):
  return db.query(models.Owner).count()


# ---------------------------------------------------------------------------
# Route: uniform 403, success consumes the claim, no-leak status
# ---------------------------------------------------------------------------

def test_setup_status_advertises_claim_required_without_leaking_token(client):
  """setup/status reports claim_required while unconfigured and NEVER echoes
  the published token."""
  published = setup_claim._read_claim_file(_data_dir())
  assert published  # the fixture ensured a claim exists

  res = client.get("/api/auth/setup/status")
  assert res.status_code == 200
  body = res.json()
  assert body["configured"] is False
  assert body["claim_required"] is True
  # The one-time token must never travel to the client via this endpoint.
  assert published not in res.text


def test_missing_empty_malformed_wrong_claim_all_uniform_403(client):
  """Missing / empty / malformed / wrong claim all take the SAME 403 path with
  the SAME detail — no oracle for why the claim was rejected."""
  base = {"username": "admin", "password": "securepassword123"}
  attempts = [
    {**base},                              # missing claim field entirely
    {**base, "claim": ""},                 # empty
    {**base, "claim": "!!!not-base64!!!"}, # malformed charset
    {**base, "claim": "wrongclaimvalue0000000000"},  # valid shape, wrong value
  ]
  details = set()
  for payload in attempts:
    r = client.post("/api/auth/setup", json=payload)
    assert r.status_code == 403, (payload, r.text)
    details.add(r.json()["detail"])
  # One uniform message across every rejection reason.
  assert details == {"Invalid setup claim."}

  # None of the rejected attempts created an owner; a valid claim still works.
  ok = client.post("/api/auth/setup", json={**base, "claim": SETUP_CLAIM})
  assert ok.status_code == 200
  assert "access_token" in ok.json()


def test_valid_setup_consumes_claim_and_second_setup_400(client):
  """A successful setup writes the durable marker, deletes the claim file, and
  a second setup 400s (owner exists)."""
  data_dir = _data_dir()
  assert setup_claim._read_claim_file(data_dir)
  assert not setup_claim.is_consumed(data_dir)

  r = client.post("/api/auth/setup", json={
    "username": "admin", "password": "securepassword123", "claim": SETUP_CLAIM,
  })
  assert r.status_code == 200
  # Claim file gone, marker written.
  assert setup_claim._read_claim_file(data_dir) is None
  assert setup_claim.is_consumed(data_dir) is True

  again = client.post("/api/auth/setup", json={
    "username": "admin2", "password": "otherpassword", "claim": SETUP_CLAIM,
  })
  assert again.status_code == 400


# ---------------------------------------------------------------------------
# Barrier: concurrent setup yields exactly one owner
# ---------------------------------------------------------------------------

def test_concurrent_setup_yields_one_owner(db):
  """N simultaneous first-boot setups (distinct usernames, valid claim) produce
  exactly one 200 and one Owner row; the rest 400. Distinct usernames make a
  broken lock fail loudly — two owners would both commit without an
  IntegrityError."""
  n = 8
  barrier = threading.Barrier(n)
  results: list[int] = []
  lock = threading.Lock()

  def attempt(i):
    c = TestClient(app)
    barrier.wait()  # align all threads at the POST
    r = c.post("/api/auth/setup", json={
      "username": f"user{i}", "password": "securepassword123",
      "claim": SETUP_CLAIM,
    })
    with lock:
      results.append(r.status_code)

  threads = [threading.Thread(target=attempt, args=(i,)) for i in range(n)]
  for t in threads:
    t.start()
  for t in threads:
    t.join()

  assert results.count(200) == 1, results
  assert all(s == 400 for s in results if s != 200), results
  assert _owner_count(db) == 1


# ---------------------------------------------------------------------------
# Fail-closed lifecycle
# ---------------------------------------------------------------------------

def test_fail_closed_when_marker_present_and_no_owner(client, db):
  """A consumed marker with no owner (DB wiped without factory reset) makes
  setup refuse with 409 — even with a valid claim — and creates no owner."""
  data_dir = _data_dir()
  setup_claim._write_marker(data_dir)
  assert setup_claim.is_fail_closed(data_dir)

  r = client.post("/api/auth/setup", json={
    "username": "admin", "password": "securepassword123", "claim": SETUP_CLAIM,
  })
  assert r.status_code == 409
  assert _owner_count(db) == 0


def test_fail_closed_when_recovery_seed_present_and_no_owner(client, db):
  """A recovery seed with no owner is likewise fail-closed: the instance had an
  owner and must be recovered, not re-claimed."""
  data_dir = _data_dir()
  seed = os.path.join(data_dir, ".recovery-owner.json")
  with open(seed, "w", encoding="utf-8") as fh:
    fh.write('{"username":"x","hashed_password":"y"}')
  assert setup_claim.is_fail_closed(data_dir)

  r = client.post("/api/auth/setup", json={
    "username": "admin", "password": "securepassword123", "claim": SETUP_CLAIM,
  })
  assert r.status_code == 409
  assert _owner_count(db) == 0


# ---------------------------------------------------------------------------
# ensure_claim / atomic publication / permissions (isolated tmp data dir)
# ---------------------------------------------------------------------------

def test_ensure_claim_publishes_regular_0600_file(tmp_path, monkeypatch):
  monkeypatch.delenv("MOBIUS_SETUP_CLAIM", raising=False)
  token = setup_claim.ensure_claim(str(tmp_path), owner_exists=False)
  assert token
  path = tmp_path / ".setup-claim"
  st = path.lstat()
  assert stat.S_ISREG(st.st_mode)
  assert stat.S_IMODE(st.st_mode) == 0o600
  assert path.read_text(encoding="ascii").strip() == token
  # No leftover temp files from the publish.
  assert [p.name for p in tmp_path.iterdir()] == [".setup-claim"]


def test_ensure_claim_idempotent_never_regenerates(tmp_path, monkeypatch):
  monkeypatch.delenv("MOBIUS_SETUP_CLAIM", raising=False)
  first = setup_claim.ensure_claim(str(tmp_path), owner_exists=False)
  second = setup_claim.ensure_claim(str(tmp_path), owner_exists=False)
  assert first == second
  assert setup_claim.verify(str(tmp_path), first) is True


def test_owner_present_purges_stale_claim(tmp_path, monkeypatch):
  monkeypatch.delenv("MOBIUS_SETUP_CLAIM", raising=False)
  setup_claim.ensure_claim(str(tmp_path), owner_exists=False)
  assert (tmp_path / ".setup-claim").exists()
  out = setup_claim.ensure_claim(str(tmp_path), owner_exists=True)
  assert out is None
  assert not (tmp_path / ".setup-claim").exists()


def test_read_rejects_symlink_badmode_and_empty(tmp_path):
  claim = tmp_path / ".setup-claim"

  # symlink is never trusted
  target = tmp_path / "real-token"
  target.write_text("sneaky", encoding="ascii")
  claim.symlink_to(target)
  assert setup_claim._read_claim_file(str(tmp_path)) is None
  claim.unlink()

  # group/other bits set -> not the 0600 we publish
  claim.write_text("token12345", encoding="ascii")
  os.chmod(claim, 0o644)
  assert setup_claim._read_claim_file(str(tmp_path)) is None

  # empty file -> no claim
  os.chmod(claim, 0o600)
  claim.write_text("", encoding="ascii")
  assert setup_claim._read_claim_file(str(tmp_path)) is None

  # a well-formed 0600 file reads back
  claim.write_text("goodtoken", encoding="ascii")
  os.chmod(claim, 0o600)
  assert setup_claim._read_claim_file(str(tmp_path)) == "goodtoken"


def test_verify_uses_constant_time_compare(tmp_path, monkeypatch):
  monkeypatch.delenv("MOBIUS_SETUP_CLAIM", raising=False)
  token = setup_claim.ensure_claim(str(tmp_path), owner_exists=False)
  calls = {"n": 0}
  real = setup_claim.hmac.compare_digest

  def spy(a, b):
    calls["n"] += 1
    return real(a, b)

  monkeypatch.setattr(setup_claim.hmac, "compare_digest", spy)
  assert setup_claim.verify(str(tmp_path), token) is True
  assert setup_claim.verify(str(tmp_path), "definitely-wrong") is False
  assert calls["n"] >= 2  # the compare ran on both the right and wrong path
  # An oversized candidate is rejected before any compare (no crash, no work).
  assert setup_claim.verify(str(tmp_path), "x" * 10_000) is False


# ---------------------------------------------------------------------------
# Env-preset precedence + validation
# ---------------------------------------------------------------------------

def test_env_preset_is_authoritative_and_overwrites_generated(
  tmp_path, monkeypatch,
):
  # A generated claim exists first...
  monkeypatch.delenv("MOBIUS_SETUP_CLAIM", raising=False)
  generated = setup_claim.ensure_claim(str(tmp_path), owner_exists=False)

  # ...then a preset appears: it is authoritative and supersedes the generated.
  preset = "Preset-Claim-Value-0123456789"
  monkeypatch.setenv("MOBIUS_SETUP_CLAIM", preset)
  out = setup_claim.ensure_claim(str(tmp_path), owner_exists=False)
  assert out == preset
  assert setup_claim._read_claim_file(str(tmp_path)) == preset
  assert setup_claim.verify(str(tmp_path), preset) is True
  assert setup_claim.verify(str(tmp_path), generated) is False

  # A second ensure with the same preset is a stable no-op.
  assert setup_claim.ensure_claim(str(tmp_path), owner_exists=False) == preset


def test_preset_validation_rejects_weak_or_malformed_outside_test_runtime(
  tmp_path, monkeypatch,
):
  monkeypatch.delenv("MOBIUS_TEST_RUNTIME", raising=False)

  # Too short for production strength.
  monkeypatch.setenv("MOBIUS_SETUP_CLAIM", "short")
  with pytest.raises(ValueError):
    setup_claim.ensure_claim(str(tmp_path), owner_exists=False)
  assert not (tmp_path / ".setup-claim").exists()  # fail-closed, nothing written

  # Non-base64url chars are rejected regardless of length.
  monkeypatch.setenv("MOBIUS_SETUP_CLAIM", "has spaces and !!! xxxxxxxxxxxxxx")
  with pytest.raises(ValueError):
    setup_claim.ensure_claim(str(tmp_path), owner_exists=False)


def test_preset_allows_fixed_short_value_under_test_runtime(
  tmp_path, monkeypatch,
):
  monkeypatch.setenv("MOBIUS_TEST_RUNTIME", "1")
  monkeypatch.setenv("MOBIUS_SETUP_CLAIM", "short-fixed")
  out = setup_claim.ensure_claim(str(tmp_path), owner_exists=False)
  assert out == "short-fixed"


# ---------------------------------------------------------------------------
# Filesystem API must never expose the claim
# ---------------------------------------------------------------------------

def test_fs_denies_setup_claim(client, auth):
  """/api/fs must 403 a read of the claim file and omit it (+ the marker) from
  listings, reporting them as redacted."""
  data_dir = _data_dir()
  # The `auth` fixture consumed the boot claim; re-materialize one so there is a
  # real file to prove the deny path (deny fires even if absent, but this also
  # exercises the tree redaction).
  with open(os.path.join(data_dir, ".setup-claim"), "w", encoding="ascii") as fh:
    fh.write("tokenvalue123")
  os.chmod(os.path.join(data_dir, ".setup-claim"), 0o600)

  r = client.get(
    "/api/fs/read", params={"path": ".setup-claim"}, headers=auth,
  )
  assert r.status_code == 403

  tree = client.get("/api/fs/tree", headers=auth).json()
  names = [e["name"] for e in tree["entries"]]
  assert ".setup-claim" not in names
  assert ".setup-claim" in tree["redacted"]
  # The consumed marker written during the fixture's setup is denied too.
  assert ".setup-consumed" not in names
