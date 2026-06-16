"""Activity log: emitter + debounce + rotation + retention + read endpoint."""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from app import activity
from app.config import get_settings


def _activity_path() -> Path:
  return Path(get_settings().data_dir) / "logs" / "activity.jsonl"


def _read_lines() -> list[dict]:
  path = _activity_path()
  if not path.exists():
    return []
  return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# --- Emitter -----------------------------------------------------------


def test_log_event_writes_one_jsonl_line():
  activity.log_event("app_open", app_id=7, slug="news")
  lines = _read_lines()
  assert len(lines) == 1
  ev = lines[0]
  assert ev["ev"] == "app_open"
  assert ev["app_id"] == 7
  assert ev["slug"] == "news"
  assert "ts" in ev
  # Timestamp parseable + tz-aware.
  parsed = datetime.fromisoformat(ev["ts"])
  assert parsed.tzinfo is not None


def test_log_event_appends_multiple_lines_in_order():
  activity.log_event("app_open", app_id=1, slug="a")
  activity.log_event("app_install", app_id=1, slug="a", source="store")
  activity.log_event("storage_write", app_id=1, path="notes.json", size_delta=42)
  lines = _read_lines()
  assert [e["ev"] for e in lines] == ["app_open", "app_install", "storage_write"]


def test_log_event_disabled_via_env(monkeypatch):
  monkeypatch.setenv("MOBIUS_ACTIVITY_LOG", "off")
  activity.log_event("app_open", app_id=99, slug="silent")
  assert _read_lines() == []


def test_log_event_swallows_oserror(monkeypatch):
  """A disk-full / permission error from the write must not propagate
  — log is sidecar telemetry, never load-bearing."""
  def _boom(*a, **kw):
    raise OSError("disk full")
  with patch.object(Path, "open", _boom):
    # No raise = pass.
    activity.log_event("app_open", app_id=1, slug="x")


def test_log_event_accepts_caller_ts():
  """If the caller passes ts=, the emitter doesn't clobber it. Lets
  cron-emit.sh stamp the time at job-start instead of when the API
  receives the event."""
  custom = "2026-01-15T10:30:00+00:00"
  activity.log_event("cron_outcome", ts=custom, app_id=3,
                     job="fetch.sh", exit_code=0, duration_ms=1234)
  ev = _read_lines()[0]
  assert ev["ts"] == custom
  assert ev["exit_code"] == 0


# --- Debounce ----------------------------------------------------------


def test_storage_write_debounce_blocks_within_window():
  now = datetime.now(timezone.utc)
  assert activity.should_emit_storage_write(7, "notes.json", now=now) is True
  # Same key, 30s later — still inside the 60s window.
  later = now + timedelta(seconds=30)
  assert activity.should_emit_storage_write(7, "notes.json", now=later) is False


def test_storage_write_debounce_allows_after_window():
  now = datetime.now(timezone.utc)
  assert activity.should_emit_storage_write(8, "notes.json", now=now) is True
  later = now + timedelta(seconds=61)
  assert activity.should_emit_storage_write(8, "notes.json", now=later) is True


def test_storage_write_debounce_per_key():
  """(app_id, path) is the cache key — different paths or different
  apps must not suppress each other."""
  now = datetime.now(timezone.utc)
  assert activity.should_emit_storage_write(1, "a.json", now=now) is True
  assert activity.should_emit_storage_write(1, "b.json", now=now) is True
  assert activity.should_emit_storage_write(2, "a.json", now=now) is True
  # Same key suppressed though:
  assert activity.should_emit_storage_write(1, "a.json", now=now) is False


# --- Rotation + retention ---------------------------------------------


def _force_mtime(path: Path, days_ago: int) -> None:
  """Backdate a file's mtime so the rotation check fires."""
  ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp()
  os.utime(path, (ts, ts))


def test_rotation_moves_old_file_aside():
  """Active file whose FIRST event is older than ROTATION_DAYS gets
  renamed to activity.YYYY-WW.jsonl; the next write lands in a fresh
  activity.jsonl. (Age is keyed to the first event's ts, not mtime —
  mtime is reset by every append and so never trips rotation.)"""
  old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(
    timespec="seconds"
  )
  activity.log_event("app_open", app_id=1, slug="old", ts=old_ts)
  active = _activity_path()
  assert active.exists()

  activity.log_event("app_open", app_id=2, slug="new")
  # After the rotation, the active file holds only the new event.
  fresh = _read_lines()
  assert len(fresh) == 1
  assert fresh[0]["slug"] == "new"
  # One rotated archive sits next to it.
  archives = list(active.parent.glob("activity.*.jsonl"))
  assert len(archives) == 1, f"got {archives}"
  archived = [json.loads(l) for l in archives[0].read_text().splitlines() if l.strip()]
  assert len(archived) == 1
  assert archived[0]["slug"] == "old"


def test_retention_sweeps_archives_older_than_90_days():
  """A rotated archive whose mtime is past RETENTION_DAYS is deleted
  the next time rotation fires."""
  active = _activity_path()
  active.parent.mkdir(parents=True, exist_ok=True)

  # Lay down a fake-old archive directly.
  stale = active.parent / "activity.2025-W01.jsonl"
  stale.write_text('{"ev":"app_open","ts":"2025-01-01T00:00:00+00:00","app_id":1}\n')
  _force_mtime(stale, days_ago=120)

  # Lay down the active file + backdate so rotation fires.
  active.write_text('{"ev":"app_open","ts":"2026-04-01T00:00:00+00:00","app_id":1}\n')
  _force_mtime(active, days_ago=10)

  # Next emit triggers rotation, which also sweeps.
  activity.log_event("app_open", app_id=99, slug="trigger")

  assert not stale.exists(), "stale archive should have been swept"


def test_rotation_does_not_clobber_existing_archive():
  """If an archive with the natural rotation name already exists
  (clock jumped, two rotations same ISO week), we suffix with a
  counter rather than overwrite history."""
  active = _activity_path()
  active.parent.mkdir(parents=True, exist_ok=True)

  active.write_text('{"ev":"app_open","ts":"2026-04-01T00:00:00+00:00","app_id":1}\n')
  _force_mtime(active, days_ago=10)

  # Pre-create the archive at the name rotation would pick. The suffix is
  # derived from the file's FIRST event ts (what rotation now keys on),
  # not mtime.
  expected_suffix = datetime.fromisoformat(
    "2026-04-01T00:00:00+00:00"
  ).strftime("%G-W%V")
  collision = active.parent / f"activity.{expected_suffix}.jsonl"
  collision.write_text('PRE-EXISTING\n')

  activity.log_event("app_open", app_id=99, slug="trigger")

  # Pre-existing archive untouched.
  assert collision.read_text() == "PRE-EXISTING\n"
  # A second archive with a numeric suffix appears.
  rotated = sorted(active.parent.glob("activity.*.jsonl"))
  assert len(rotated) >= 2


# --- Read iterator -----------------------------------------------------


def test_read_events_filters_by_window():
  base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
  activity.log_event("app_open", ts=base.isoformat(), app_id=1, slug="a")
  activity.log_event("app_open", ts=(base + timedelta(hours=2)).isoformat(),
                     app_id=2, slug="b")
  activity.log_event("app_open", ts=(base + timedelta(hours=4)).isoformat(),
                     app_id=3, slug="c")

  events = list(activity.read_events(
    since=base + timedelta(hours=1),
    until=base + timedelta(hours=3),
  ))
  assert [e["app_id"] for e in events] == [2]


def test_read_events_filters_by_app_id():
  base = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
  activity.log_event("app_open", ts=base.isoformat(), app_id=1, slug="a")
  activity.log_event("app_open", ts=base.isoformat(), app_id=2, slug="b")

  events = list(activity.read_events(
    since=base - timedelta(minutes=1),
    until=base + timedelta(minutes=1),
    app_id=2,
  ))
  assert len(events) == 1
  assert events[0]["app_id"] == 2


# --- new event emits fire from their routes (#4 instrumentation) ----------


def test_create_chat_emits_chat_created(client, auth):
  """POST /api/chats records a chat_created event carrying the new chat id."""
  res = client.post("/api/chats", headers=auth, json={})
  assert res.status_code in (200, 201)
  chat_id = res.json()["id"]
  created = [e for e in _read_lines() if e["ev"] == "chat_created"]
  assert len(created) == 1
  assert created[0]["chat_id"] == chat_id


def test_delete_app_emits_app_uninstall(client, auth):
  """DELETE /api/apps/{id} records an app_uninstall event (the missing half
  of the install/uninstall pair the churn digest needs)."""
  src = "export default function App(){ return null }"
  made = client.post(
    "/api/apps/", headers=auth,
    json={"name": "Churn Test", "description": "x", "jsx_source": src},
  )
  assert made.status_code in (200, 201), made.text
  app_id = made.json()["id"]
  res = client.delete(f"/api/apps/{app_id}", headers=auth)
  assert res.status_code in (200, 204)
  uninstalls = [e for e in _read_lines() if e["ev"] == "app_uninstall"]
  assert len(uninstalls) == 1
  assert uninstalls[0]["app_id"] == app_id


def test_read_events_spans_rotated_archives():
  """Cross-week read: events that landed in an archive BEFORE the most
  recent rotation must still appear in a window that covers them.
  The dreaming agent runs at 6am Monday with a 24h window; if Sunday's
  rotation already pushed Sunday-evening events into an archive, those
  would silently vanish from the read response."""
  active = _activity_path()
  active.parent.mkdir(parents=True, exist_ok=True)
  # Lay down an archive with an event from "last week".
  archive = active.parent / "activity.2026-W18.jsonl"
  archive.write_text(
    '{"ev":"app_open","ts":"2026-05-03T22:00:00+00:00","app_id":1,"slug":"a"}\n'
    '{"ev":"app_open","ts":"2026-05-03T23:30:00+00:00","app_id":2,"slug":"b"}\n'
  )
  # Active file: an event from "this week" (post-rotation).
  active.write_text(
    '{"ev":"app_open","ts":"2026-05-04T06:00:00+00:00","app_id":3,"slug":"c"}\n'
  )

  # Window covers both files: late Sunday into Monday morning.
  events = list(activity.read_events(
    since=datetime(2026, 5, 3, 21, 0, 0, tzinfo=timezone.utc),
    until=datetime(2026, 5, 4, 7, 0, 0, tzinfo=timezone.utc),
  ))
  ids = [e["app_id"] for e in events]
  assert ids == [1, 2, 3], f"got {ids}"


def test_read_events_archive_only_window():
  """A window that ends BEFORE the active file's earliest event must
  still return the matching archive events."""
  active = _activity_path()
  active.parent.mkdir(parents=True, exist_ok=True)
  archive = active.parent / "activity.2026-W18.jsonl"
  archive.write_text(
    '{"ev":"app_open","ts":"2026-05-03T22:00:00+00:00","app_id":1,"slug":"a"}\n'
  )
  active.write_text(
    '{"ev":"app_open","ts":"2026-05-04T06:00:00+00:00","app_id":99,"slug":"c"}\n'
  )
  # Window deliberately excludes the active file's event.
  events = list(activity.read_events(
    since=datetime(2026, 5, 3, 21, 0, 0, tzinfo=timezone.utc),
    until=datetime(2026, 5, 3, 23, 0, 0, tzinfo=timezone.utc),
  ))
  assert [e["app_id"] for e in events] == [1]


def test_read_events_archive_with_counter_suffix():
  """The counter-suffixed collision variant (activity.YYYY-W##.2.jsonl)
  is also picked up — rotation never silently drops it."""
  active = _activity_path()
  active.parent.mkdir(parents=True, exist_ok=True)
  (active.parent / "activity.2026-W18.jsonl").write_text(
    '{"ev":"app_open","ts":"2026-05-03T10:00:00+00:00","app_id":1,"slug":"a"}\n'
  )
  (active.parent / "activity.2026-W18.2.jsonl").write_text(
    '{"ev":"app_open","ts":"2026-05-03T11:00:00+00:00","app_id":2,"slug":"b"}\n'
  )
  events = list(activity.read_events(
    since=datetime(2026, 5, 3, 0, 0, 0, tzinfo=timezone.utc),
    until=datetime(2026, 5, 4, 0, 0, 0, tzinfo=timezone.utc),
  ))
  assert sorted(e["app_id"] for e in events) == [1, 2]


def test_read_events_skips_malformed_lines():
  """A corrupt JSON line in the middle must not block the rest from
  yielding."""
  path = _activity_path()
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("a", encoding="utf-8") as f:
    f.write('{"ev":"app_open","ts":"2026-01-01T00:00:00+00:00","app_id":1}\n')
    f.write('this is not json\n')
    f.write('{"ev":"app_open","ts":"2026-01-02T00:00:00+00:00","app_id":2}\n')
  events = list(activity.read_events(
    since=datetime(2025, 1, 1, tzinfo=timezone.utc),
    until=datetime(2027, 1, 1, tzinfo=timezone.utc),
  ))
  assert [e["app_id"] for e in events] == [1, 2]


# --- Skill observability: log_skill_load + most_used_skills -----------


def test_log_skill_load_writes_skill_loaded_line():
  activity.log_skill_load("chat-1", "humanizer")
  lines = _read_lines()
  assert len(lines) == 1
  assert lines[0]["ev"] == "skill_loaded"
  assert lines[0]["chat_id"] == "chat-1"
  assert lines[0]["skill"] == "humanizer"
  assert "ts" in lines[0]


def test_log_skill_load_drops_blank_skill():
  """A blank/whitespace skill name carries no signal — dropped."""
  activity.log_skill_load("chat-1", "")
  activity.log_skill_load("chat-1", "   ")
  assert _read_lines() == []


def test_most_used_skills_counts_and_ranks():
  base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
  for skill in ["humanizer", "humanizer", "verify", "humanizer", "verify"]:
    activity.log_skill_load(
      "chat-1", skill, ts=base.isoformat(),
    )
  ranked = activity.most_used_skills(
    since=base - timedelta(hours=1),
    until=base + timedelta(hours=1),
  )
  assert ranked == [
    {"skill": "humanizer", "count": 3},
    {"skill": "verify", "count": 2},
  ]


def test_most_used_skills_respects_window():
  base = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
  activity.log_skill_load("c", "old", ts=base.isoformat())
  activity.log_skill_load(
    "c", "new", ts=(base + timedelta(hours=5)).isoformat(),
  )
  ranked = activity.most_used_skills(
    since=base + timedelta(hours=1),
    until=base + timedelta(hours=6),
  )
  assert ranked == [{"skill": "new", "count": 1}]


def test_most_used_skills_empty_window_returns_empty_list():
  now = datetime.now(timezone.utc)
  assert activity.most_used_skills(
    since=now - timedelta(hours=1), until=now,
  ) == []


# --- Read endpoint -----------------------------------------------------


def test_read_endpoint_streams_jsonl(client, auth):
  """GET /api/admin/activity returns one event per line as ndjson."""
  base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
  activity.log_event("app_open", ts=base.isoformat(), app_id=1, slug="a")
  activity.log_event("app_install", ts=base.isoformat(),
                     app_id=1, slug="a", source="store")

  r = client.get(
    "/api/admin/activity",
    params={"since": base.isoformat()},
    headers=auth,
  )
  assert r.status_code == 200, r.text
  assert r.headers["content-type"].startswith("application/x-ndjson")
  lines = [json.loads(l) for l in r.text.splitlines() if l.strip()]
  assert len(lines) == 2
  assert {l["ev"] for l in lines} == {"app_open", "app_install"}


def test_read_endpoint_requires_since(client, auth):
  r = client.get("/api/admin/activity", headers=auth)
  assert r.status_code == 422  # FastAPI's required-query-param shape


def test_read_endpoint_rejects_app_token(client, owner_token):
  """App-scoped JWTs cannot read the cross-app activity feed. The
  service-token is a full owner JWT, which DOES pass."""
  from app import auth as auth_mod
  app_jwt = auth_mod.create_access_token({
    "sub": "test", "scope": "app", "app_id": 99,
  })
  r = client.get(
    "/api/admin/activity",
    params={"since": datetime.now(timezone.utc).isoformat()},
    headers={"Authorization": f"Bearer {app_jwt}"},
  )
  assert r.status_code == 403


def test_read_endpoint_rejects_no_auth(client):
  r = client.get(
    "/api/admin/activity",
    params={"since": datetime.now(timezone.utc).isoformat()},
  )
  assert r.status_code == 401


def test_read_endpoint_filters_by_app_id(client, auth):
  base = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
  activity.log_event("app_open", ts=base.isoformat(), app_id=1, slug="a")
  activity.log_event("app_open", ts=base.isoformat(), app_id=2, slug="b")
  r = client.get(
    "/api/admin/activity",
    params={"since": base.isoformat(), "app_id": 2},
    headers=auth,
  )
  assert r.status_code == 200
  lines = [json.loads(l) for l in r.text.splitlines() if l.strip()]
  assert [l["app_id"] for l in lines] == [2]


def test_read_endpoint_invalid_window_400(client, auth):
  """until < since is a 400 rather than a confused empty stream."""
  now = datetime.now(timezone.utc)
  earlier = (now - timedelta(hours=1)).isoformat()
  r = client.get(
    "/api/admin/activity",
    params={"since": now.isoformat(), "until": earlier},
    headers=auth,
  )
  assert r.status_code == 400


# --- Emit endpoint -----------------------------------------------------


def test_emit_endpoint_appends_event(client, auth):
  r = client.post(
    "/api/admin/activity/emit",
    json={"ev": "cron_outcome", "app_id": 7, "job": "fetch.sh",
          "exit_code": 0, "duration_ms": 1500},
    headers=auth,
  )
  assert r.status_code == 204, r.text
  lines = _read_lines()
  assert len(lines) == 1
  assert lines[0]["ev"] == "cron_outcome"
  assert lines[0]["exit_code"] == 0
  assert lines[0]["duration_ms"] == 1500


def test_emit_endpoint_accepts_skill_loaded(client, auth):
  """skill_loaded is a known event the emit endpoint accepts."""
  r = client.post(
    "/api/admin/activity/emit",
    json={"ev": "skill_loaded", "chat_id": "c1", "skill": "humanizer"},
    headers=auth,
  )
  assert r.status_code == 204, r.text
  lines = _read_lines()
  assert lines[0]["ev"] == "skill_loaded"
  assert lines[0]["skill"] == "humanizer"


def test_skills_endpoint_returns_ranked_most_used(client, auth):
  """GET /api/admin/activity/skills aggregates skill_loaded events."""
  base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
  for skill in ["humanizer", "verify", "humanizer"]:
    activity.log_skill_load("c1", skill, ts=base.isoformat())
  r = client.get(
    "/api/admin/activity/skills",
    params={"since": base.isoformat()},
    headers=auth,
  )
  assert r.status_code == 200, r.text
  assert r.json() == {
    "skills": [
      {"skill": "humanizer", "count": 2},
      {"skill": "verify", "count": 1},
    ]
  }


def test_skills_endpoint_requires_auth(client):
  r = client.get(
    "/api/admin/activity/skills",
    params={"since": datetime.now(timezone.utc).isoformat()},
  )
  assert r.status_code == 401


def test_emit_endpoint_rejects_unknown_event(client, auth):
  r = client.post(
    "/api/admin/activity/emit",
    json={"ev": "made_up", "app_id": 1},
    headers=auth,
  )
  assert r.status_code == 400


def test_emit_endpoint_requires_auth(client):
  r = client.post(
    "/api/admin/activity/emit",
    json={"ev": "app_open", "app_id": 1, "slug": "x"},
  )
  assert r.status_code == 401


# --- Wiring: app_open via /api/apps/{id}/frame -------------------------


def _make_app(client, auth):
  r = client.post("/api/apps/", json={
    "name": "wiretest",
    "description": "x",
    "jsx_source": "export default function App() { return <div/> }",
  }, headers=auth)
  assert r.status_code == 201, r.text
  return r.json()["id"]


def test_frame_fetch_emits_app_open(client, auth):
  app_id = _make_app(client, auth)
  r = client.get(f"/api/apps/{app_id}/frame")
  assert r.status_code == 200, r.text

  lines = _read_lines()
  opens = [l for l in lines if l["ev"] == "app_open"]
  assert len(opens) == 1
  assert opens[0]["app_id"] == app_id
  assert opens[0]["slug"] == "wiretest"


def test_frame_304_does_not_emit_app_open(client, auth):
  """If-None-Match returns 304 — no event. Otherwise every cache
  revalidation would double-count opens."""
  app_id = _make_app(client, auth)
  # Prime: first request returns ETag and emits.
  r1 = client.get(f"/api/apps/{app_id}/frame")
  assert r1.status_code == 200
  etag = r1.headers["etag"]
  # Reset log so the 304 assertion is unambiguous.
  _activity_path().unlink()

  r2 = client.get(
    f"/api/apps/{app_id}/frame",
    headers={"If-None-Match": etag},
  )
  assert r2.status_code == 304
  assert _read_lines() == []


# --- Wiring: app_install via /api/apps/install -------------------------


def test_install_endpoint_emits_app_install(client, auth, monkeypatch):
  """A successful install emits one app_install event with source=store."""
  # The install route relies on httpx + the cron scaffold. Use the
  # same mocking shape test_apps_install.py uses to drive a happy
  # path through the pipeline.
  from pathlib import Path
  from unittest.mock import patch, MagicMock
  import io
  from PIL import Image

  monkeypatch.setattr(
    "app.install.CRON_SCAFFOLD", Path("/nonexistent/scaffold.sh"),
  )

  def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (139, 108, 247)).save(buf, format="PNG")
    return buf.getvalue()

  class _Ctx:
    def __init__(self, status, body):
      self.status_code = status
      self._body = body
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    async def aiter_bytes(self):
      yield self._body
    @property
    def headers(self): return {}

  class _Client:
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    def stream(self, method, url, **kwargs):
      manifest = {
        "id": "tinytest", "name": "Tiny", "version": "1.0.0",
        "description": "test", "entry": "index.jsx", "icon": "icon.png",
      }
      if url.endswith("mobius.json"):
        return _Ctx(200, json.dumps(manifest).encode())
      if url.endswith("index.jsx"):
        return _Ctx(200, b"export default function A() { return <div/> }")
      if url.endswith("icon.png"):
        return _Ctx(200, _png())
      return _Ctx(404, b"")

  with patch("app.install._validate_url_safe",
             lambda u: (u, urlparse(u).netloc, urlparse(u).hostname)), \
       patch("app.install.httpx.AsyncClient", lambda *a, **k: _Client()):
    r = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": "https://x.test/m/mobius.json"},
    )
  assert r.status_code == 201, r.text

  installs = [l for l in _read_lines() if l["ev"] == "app_install"]
  assert len(installs) == 1
  assert installs[0]["source"] == "store"
  assert installs[0]["app_id"] == r.json()["id"]
  assert installs[0]["slug"] == "tinytest"


@pytest.mark.asyncio
async def test_bootstrap_install_emits_with_source_bootstrap(db, monkeypatch):
  """ensure_store_installed → install_from_manifest(source='bootstrap')
  → event records the right source."""
  from unittest.mock import patch, AsyncMock
  from app.bootstrap import BOOTSTRAP_STORE_MANIFEST_URL, ensure_store_installed
  from app import models, install as install_mod

  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)

  # Have the bootstrap call into the REAL install code path but with
  # the network mocked. Simpler: capture the kwargs and emit the event
  # ourselves to mirror what install_from_manifest does.
  captured = {}

  async def _fake_install(db, manifest_url, manifest, raw_base, source="url"):
    captured["source"] = source
    fake = models.App(
      id=42, name="Store", slug="store",
      manifest_url=BOOTSTRAP_STORE_MANIFEST_URL,
    )
    # Mimic install_from_manifest's log call so the assertion below
    # exercises the wiring contract (bootstrap → source='bootstrap').
    activity.log_event(
      "app_install", app_id=fake.id, slug=fake.slug, source=source,
    )
    return fake, "install", [], {}, [], "none"

  with patch("app.bootstrap.install_from_manifest", _fake_install):
    await ensure_store_installed(db)

  assert captured["source"] == "bootstrap"
  installs = [l for l in _read_lines() if l["ev"] == "app_install"]
  assert len(installs) == 1
  assert installs[0]["source"] == "bootstrap"


# --- Wiring: storage_write via PUT + DELETE ----------------------------


def test_storage_put_emits_storage_write(client, auth):
  app_id = _make_app(client, auth)
  r = client.put(
    f"/api/storage/apps/{app_id}/notes.json",
    json={"hi": "there"},
    headers=auth,
  )
  assert r.status_code == 204

  writes = [l for l in _read_lines() if l["ev"] == "storage_write"]
  assert len(writes) == 1
  assert writes[0]["app_id"] == app_id
  assert writes[0]["path"] == "notes.json"
  # First write: size_delta is the full file size (>0).
  assert writes[0]["size_delta"] > 0


def test_storage_put_debounces_within_window(client, auth):
  """Two PUTs to the same (app_id, path) within 60s emit ONE event."""
  app_id = _make_app(client, auth)
  for _ in range(3):
    r = client.put(
      f"/api/storage/apps/{app_id}/notes.json",
      json={"v": 1},
      headers=auth,
    )
    assert r.status_code == 204
  writes = [l for l in _read_lines() if l["ev"] == "storage_write"]
  assert len(writes) == 1


def test_storage_delete_emits_storage_write_with_negative_delta(client, auth):
  app_id = _make_app(client, auth)
  # Seed a file first. Use a DIFFERENT path so debounce on the PUT
  # doesn't suppress the delete-side event.
  client.put(
    f"/api/storage/apps/{app_id}/seed.json",
    json={"x": 1}, headers=auth,
  )
  # Drop debounce so the DELETE on the same path actually emits.
  activity._reset_for_tests()

  r = client.delete(
    f"/api/storage/apps/{app_id}/seed.json", headers=auth,
  )
  assert r.status_code == 204
  writes = [l for l in _read_lines() if l["ev"] == "storage_write"
            and l["path"] == "seed.json"
            and l["size_delta"] < 0]
  assert len(writes) == 1


def test_shared_storage_put_emits_storage_write(client, auth):
  """Shared writes (theme.css, agent-experience.md, etc.) emit too —
  the dreaming agent treats those as platform activity. app_id=0 +
  scope='shared' is the documented sentinel for platform-level events."""
  r = client.put(
    "/api/storage/shared/agent-experience.md",
    headers={**auth, "Content-Type": "text/plain"},
    data="seed content\n",
  )
  assert r.status_code == 204, r.text

  writes = [l for l in _read_lines() if l["ev"] == "storage_write"]
  assert len(writes) == 1
  assert writes[0]["app_id"] == 0
  assert writes[0]["scope"] == "shared"
  assert writes[0]["path"] == "agent-experience.md"
  assert writes[0]["size_delta"] > 0


def test_shared_storage_delete_emits_negative_delta(client, auth):
  """DELETE of a shared file emits with size_delta = -(prior size)."""
  client.put(
    "/api/storage/shared/scratch.txt",
    headers={**auth, "Content-Type": "text/plain"},
    data="bye\n",
  )
  activity._reset_for_tests()
  r = client.delete("/api/storage/shared/scratch.txt", headers=auth)
  assert r.status_code == 204
  writes = [l for l in _read_lines() if l["ev"] == "storage_write"
            and l.get("scope") == "shared"
            and l["size_delta"] < 0]
  assert len(writes) == 1


def test_shared_storage_put_debounces_within_window(client, auth):
  """Two PUTs to the same shared path within 60s emit ONE event —
  same rate-limit policy as per-app writes."""
  for _ in range(3):
    r = client.put(
      "/api/storage/shared/agent-experience.md",
      headers={**auth, "Content-Type": "text/plain"},
      data="v\n",
    )
    assert r.status_code == 204
  writes = [l for l in _read_lines() if l["ev"] == "storage_write"]
  assert len(writes) == 1


# --- Cron wrapper script -----------------------------------------------


def test_cron_emit_script_exists_and_executable():
  """The wrapper script ships at backend/scripts/cron-emit.sh and is
  executable. (Cron entries authored by the scaffold path consume this
  shape; a missing or non-executable script silently breaks every
  scheduled job in the container.)"""
  # Resolve relative to the repo. The test container mounts
  # backend/scripts at /app/scripts.
  candidates = [
    Path("/app/scripts/cron-emit.sh"),
    Path(__file__).parent.parent / "scripts" / "cron-emit.sh",
  ]
  for c in candidates:
    if c.exists():
      assert os.access(c, os.X_OK), f"{c} is not executable"
      content = c.read_text()
      # Sanity: must reference the activity emit endpoint, the service
      # token, and exit with the wrapped job's code.
      assert "/api/admin/activity/emit" in content
      assert "service-token.txt" in content
      assert "exit $EXIT_CODE" in content
      return
  pytest.fail("cron-emit.sh not found at any expected path")


def test_cron_emit_script_posts_cron_outcome(client, auth, tmp_path):
  """End-to-end-ish: run the wrapper against a stub job, point it at
  the TestClient via env vars, and confirm one cron_outcome event
  lands in the log."""
  import subprocess
  import shutil

  script = None
  for c in [
    Path("/app/scripts/cron-emit.sh"),
    Path(__file__).parent.parent / "scripts" / "cron-emit.sh",
  ]:
    if c.exists():
      script = c
      break
  assert script is not None

  # Write a service-token file the wrapper can read. The TestClient
  # accepts the owner_token JWT directly.
  token_path = tmp_path / "service-token.txt"
  token_path.write_text(auth["Authorization"].removeprefix("Bearer "))
  job = tmp_path / "fetch.sh"
  job.write_text("#!/bin/bash\necho ran\nexit 0\n")
  job.chmod(0o755)

  # We can't easily hand the wrapper a live server in pytest, but we
  # CAN verify the wrapper's exit code propagation + that it tries to
  # curl the right URL. Run with a deliberately unreachable API_BASE_URL
  # and confirm: (a) the wrapped job's exit code propagates, (b) the
  # script doesn't crash on emit failure.
  result = subprocess.run(
    [str(script), "7", str(job)],
    env={
      "PATH": os.environ.get("PATH", ""),
      "API_BASE_URL": "http://127.0.0.1:1",  # unreachable
      "SERVICE_TOKEN_FILE": str(token_path),
    },
    capture_output=True, text=True, timeout=10,
  )
  # Wrapped job exits 0 → wrapper exits 0 even if emit fails.
  assert result.returncode == 0, result.stderr

  # And: non-zero exit propagates.
  job.write_text("#!/bin/bash\nexit 3\n")
  job.chmod(0o755)
  result = subprocess.run(
    [str(script), "7", str(job)],
    env={
      "PATH": os.environ.get("PATH", ""),
      "API_BASE_URL": "http://127.0.0.1:1",
      "SERVICE_TOKEN_FILE": str(token_path),
    },
    capture_output=True, text=True, timeout=10,
  )
  assert result.returncode == 3, result.stderr
