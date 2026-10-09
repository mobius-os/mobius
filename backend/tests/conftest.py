"""Shared test fixtures."""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Never turn a live application process into a test runner. This check must
# happen before the test overrides below: docker exec inherits production's
# DATA_DIR/DATABASE_URL, while supported disposable runtimes explicitly attest
# that the same production-shaped path belongs to an isolated test volume.
_inherited_data_dir = os.environ.get("DATA_DIR", "").rstrip("/")
_inherited_database_url = os.environ.get("DATABASE_URL", "")
if (
  os.environ.get("MOBIUS_TEST_DATABASE_ISOLATED") != "1"
  and _inherited_data_dir == "/data"
  and "/data/db/" in _inherited_database_url
):
  pytest.exit(
    "Refusing to run pytest against the live Mobius runtime. "
    "Use scripts/test.sh or docker-compose.test.yml.",
    returncode=2,
  )

# Set env vars before importing app modules.
_tmp = tempfile.mkdtemp()
_TEST_SECRET_KEY = "test-secret-key-at-least-32-characters-long"
os.environ["SECRET_KEY"] = _TEST_SECRET_KEY
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["DATA_DIR"] = _tmp
os.environ["DOMAIN"] = "localhost"
os.environ["FRONTEND_ORIGIN"] = "http://localhost:5173"
os.environ["MOBIUS_TEST_RUNTIME"] = "1"
os.environ["MOBIUS_TEST_DATABASE_ISOLATED"] = "1"
# Fail closed when pytest is launched from inside a running production
# container. DATA_DIR isolates Python file writes, but subprocess-facing
# defaults historically still pointed at the live service and /data/apps.
os.environ["MOBIUS_APP_BASE"] = f"{_tmp}/apps"
os.environ["API_BASE_URL"] = "http://127.0.0.1:9"
os.environ["MOEBIUS_SKIP_BOOTSTRAP"] = "1"
# A test started in a managed container must neither adopt its live account
# nor call its root-owned broker. Managed-login tests opt in with local fakes.
os.environ["MOBIUS_SSO_ISSUER"] = ""
os.environ["MOBIUS_SSO_INSTANCE_ID"] = ""
os.environ["MOBIUS_IDENTITY_BROKER_SOCKET"] = f"{_tmp}/no-identity-broker.sock"

# Production entrypoint proves the image-owned filesystem half before FastAPI
# starts. Reproduce that boundary in the host-only runtime so startup can
# combine it with the configured test database migration.
_file_receipt = (
  Path(_tmp) / ".migration-receipts" / "app-identity-files-v1"
)
_file_receipt.parent.mkdir(parents=True, exist_ok=True)
_file_receipt.touch()

# Ensure the baked static dir exists with an index.html carrying the
# __mobius-theme__ slot BEFORE importing app.main — main.py registers the SPA
# fallback route (which GET / hits) only when the static dir is present at
# import. In production this dir is the Vite build baked into the image; in
# dev/test it's absent, so test_index_theme_slot.py (which exercises the
# theme-as-data JSON slot through the real GET / path) needs this stub. Only
# created when missing, so a real build is never clobbered.
from pathlib import Path as _Path

_static = _Path(__file__).resolve().parents[1] / "static"
# The warm publisher validates emitted JavaScript with a checked-in Node
# script. Production uses /data/platform/frontend; host CI must point at the
# checkout under test before importing app.frontend_watcher.
os.environ["MOBIUS_FRONTEND_DIR"] = str(
  _Path(__file__).resolve().parents[2] / "frontend"
)
# main.py resolves its baked static dir to /app/static (the image path) unless
# MOBIUS_BAKED_STATIC_DIR overrides it — off the host that path is absent, so
# point the override at the stub below (created before app.main imports).
os.environ["MOBIUS_BAKED_STATIC_DIR"] = str(_static)
os.environ["MOBIUS_BUILD_INFO_PATH"] = str(_Path(_tmp) / "missing-build-info.json")
os.environ["MOBIUS_SERVING_SOURCE_FILE"] = str(_Path(_tmp) / "serving-source")
os.environ["MOBIUS_SERVING_SHA_FILE"] = str(_Path(_tmp) / "serving-sha")
if not (_static / "index.html").is_file():
  (_static / "assets").mkdir(parents=True, exist_ok=True)
  (_static / "index.html").write_text(
    "<!doctype html><html lang=\"en\"><head>"
    "<meta name=\"theme-color\" content=\"#0d0d0d\" />"
    "<script type=\"application/json\" id=\"__mobius-theme__\"></script>"
    "</head><body style=\"margin:0;background:var(--bg,#0d0d0d)\">"
    "<div id=\"root\"></div></body></html>",
    encoding="utf-8",
  )

from app.database import Base, engine

# Environment overrides cannot retarget an engine or Settings singleton that
# an earlier collected module already imported. Check the actual bind before
# loading the application or allowing either schema-reset fixture to run.
if (
  engine.url.get_backend_name() != "sqlite"
  or not engine.url.database
  or Path(engine.url.database).resolve() != Path(_tmp, "test.db").resolve()
):
  pytest.exit(
    "Refusing schema reset outside the fixture-owned database. "
    "An application engine was imported before test isolation; "
    "use scripts/wt-pytest.sh and import fixtures before app modules.",
    returncode=2,
  )

from app.schema_migrations import _add_transcript_rows, _create_chat_search_tables
from app.main import app
from app.routes import auth as auth_module
from app.routes.auth import _limiter as auth_limiter
from app.routes.notifications import limiter as notifications_limiter

# Disable rate limiters during tests.
app.state.limiter.enabled = False
auth_limiter.enabled = False
notifications_limiter.enabled = False


@pytest.fixture(autouse=True)
def _test_secret_key_in_environment():
  """A test that runs the app lifespan withholds SECRET_KEY from the process
  environment, as production does; later tests that rebuild settings still
  need the fixed test key there."""
  yield
  os.environ["SECRET_KEY"] = _TEST_SECRET_KEY


@pytest.fixture(autouse=True)
def real_end_orphaned_hosts(monkeypatch):
  """Keep the app lifespan's boot sweep from ending processes outside the test.

  ``end_orphaned_hosts`` scans every process on the machine. Tests that enter
  the real lifespan would otherwise end helper hosts that a parallel xdist
  worker's test has just staged as orphans, failing that test intermittently.
  The sweep's own test requests this fixture and calls the real function on a
  scan limited to its processes.
  """
  from app import helper_hosts
  real = helper_hosts.end_orphaned_hosts
  monkeypatch.setattr(helper_hosts, "end_orphaned_hosts", lambda: 0)
  return real


@pytest.fixture(autouse=True)
def _isolate_git_env(monkeypatch, tmp_path, tmp_path_factory):
  """Keep the per-app-git tests' `git` subprocesses hermetic.

  app_git tests run `git init/commit/merge` against a repo in tmp_path via
  `git -C <tmp>`. But git EXPORTS `GIT_DIR` (and friends) into a hook's
  environment, and those env vars OVERRIDE `-C` — so when the suite runs
  inside the pre-push hook, the tests' git ops silently operate on the
  enclosing mobius repo instead, flipping `core.bare` and committing stray
  "Initialize app repo" commits (and failing). Scrub the inherited git env
  and pin global config to a per-test file plus system config to /dev/null,
  with a ceiling so every test git op is fully isolated, whether the suite
  runs from a shell or a git hook. The global config must be a regular
  disposable path: ``git config --global`` uses an atomic lock-and-replace,
  so pointing it at /dev/null can replace the device inside a privileged test
  container and poison every later Git command in that process.
  """
  for var in (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_NAMESPACE",
  ):
    monkeypatch.delenv(var, raising=False)
  # A commit ends with `git maintenance run --auto`, which detaches and briefly
  # holds maintenance.lock after the commit has returned. Tests that assert a
  # read-only operation leaves no lock behind would then flake on that
  # unrelated background writer. Seed the global config with maintenance off.
  # Keep it OUTSIDE tmp_path (a sibling dir from the factory): many tests use
  # their own tmp_path as the subject under test — measuring its size, snapshot
  # status, or agent-rule cleanup — so a config file placed inside tmp_path
  # would leak into those assertions.
  global_config = tmp_path_factory.mktemp("git-global") / "gitconfig"
  global_config.write_text("[maintenance]\n\tauto = false\n")
  monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
  monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
  repo_root = _Path(__file__).resolve().parents[2]
  monkeypatch.setenv(
    "GIT_CEILING_DIRECTORIES",
    os.pathsep.join((tempfile.gettempdir(), str(repo_root))),
  )


def _assert_legacy_mirrors_rows(connection) -> None:
  """Suite-wide guard: a converted chat's legacy column equals its rows.

  The previous release reads only ``chats.messages``; every committed state
  this suite produces must leave it the decoded value of the rows.
  """
  import json as _json

  def canonical(value):
    return _json.dumps(value, sort_keys=True)

  for chat_id, legacy in connection.exec_driver_sql(
    "SELECT c.id, c.messages FROM chats c JOIN chat_transcript_state s ON s.chat_id = c.id"
  ).fetchall():
    rows = [_json.loads(body) for (body,) in connection.exec_driver_sql(
      "SELECT body FROM chat_messages WHERE chat_id = ? ORDER BY seq", (chat_id,),
    ).fetchall()]
    assert canonical(_json.loads(legacy)) == canonical(rows), (
      f"chat {chat_id}: chats.messages does not mirror its rows"
    )


_ALL_UNCONVERTED_PAUSED = [False]


def pytest_configure(config):
  config.addinivalue_line(
    "markers", "converted_chats: keep chats converted in all-unconverted mode",
  )


@pytest.fixture(autouse=True)
def _converted_chats_marker(request):
  _ALL_UNCONVERTED_PAUSED[0] = request.node.get_closest_marker("converted_chats") is not None
  yield
  _ALL_UNCONVERTED_PAUSED[0] = False


def _install_all_unconverted_mode():
  """All-unconverted mode (MOBIUS_TEST_ALL_UNCONVERTED=1).

  At every commit every chat loses its conversion marker, as if the previous
  release had just written all of them: the state a first boot after an
  update serves, at every moment of every test. Readers must then serve each
  chat exactly from its legacy value, and each write converts its chat
  inline again. Two guards hold throughout: no conversion runs on the event
  loop's thread, and the event loop never blocks waiting for the writer.
  Off by default. A test marked ``converted_chats`` pins the converted state
  itself (conversion mechanics, converted-only search prose, row-path query
  shapes) and runs with conversion left as it is.
  """
  if os.environ.get("MOBIUS_TEST_ALL_UNCONVERTED") != "1":
    return
  import asyncio
  import functools

  from sqlalchemy import event as sa_event
  from sqlalchemy import text as sa_text
  from sqlalchemy.orm import Session as SASession

  from app import chat_writer as mode_chat_writer
  from app import transcript_rows as mode_transcript_rows

  def on_event_loop() -> bool:
    try:
      asyncio.get_running_loop()
    except RuntimeError:
      return False
    return True

  # Registered after transcript_rows' mirror listener, so it runs after it.
  @sa_event.listens_for(SASession, "before_commit")
  def unconvert_every_chat(session):
    if _ALL_UNCONVERTED_PAUSED[0] or session.in_nested_transaction():
      return
    if session.execute(sa_text("SELECT 1 FROM chat_transcript_state LIMIT 1")).first():
      session.execute(sa_text("DELETE FROM chat_transcript_state"))

  # The process fact would otherwise end every per-read marker check.
  mode_transcript_rows.mark_all_converted = lambda _db: None

  import sys

  def called_by_test_code() -> bool:
    # A test seeding its own fixture state directly is not a production path.
    frame = sys._getframe(2)
    while frame is not None and frame.f_code.co_filename.endswith("transcript_rows.py"):
      frame = frame.f_back
    return frame is not None and "/tests/" in frame.f_code.co_filename

  def off_the_loop(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
      assert not on_event_loop() or called_by_test_code(), (
        f"{function.__name__} ran on the event loop")
      return function(*args, **kwargs)
    return wrapper

  mode_transcript_rows.convert = off_the_loop(mode_transcript_rows.convert)
  mode_chat_writer.wait_ack = off_the_loop(mode_chat_writer.wait_ack)


_install_all_unconverted_mode()


@pytest.fixture(scope="session", autouse=True)
def _test_schema():
  """Create model and migration-owned schemas once for the test process."""
  Base.metadata.drop_all(bind=engine)
  Base.metadata.create_all(bind=engine)
  # Tests do not enter FastAPI's production lifespan, which normally runs
  # numbered migrations after create_all. Search tables deliberately have no
  # ORM model, so install their migration-owned schema explicitly here.
  _create_chat_search_tables(engine)
  _add_transcript_rows(engine)
  yield
  with engine.begin() as connection:
    connection.exec_driver_sql("DROP TABLE IF EXISTS chat_search_entries_fts")
    connection.exec_driver_sql("DROP TABLE IF EXISTS chat_search_entries")
    connection.exec_driver_sql("DROP TABLE IF EXISTS chat_search_fts")
    connection.exec_driver_sql("DROP TABLE IF EXISTS chat_search_docs")
    connection.exec_driver_sql("DROP TABLE IF EXISTS chat_search_state")
  Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def fresh_db():
  """Clears durable and in-memory state so tests don't leak into one another."""
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
  from app import secure_inputs as secure_inputs_mod
  from app import restart_util as restart_util_mod
  from app.runner_registry import registry
  # ticket 033: pending-question registry lives in app.questions;
  # queue locks live in app.chat_queue. Reset both canonical homes.
  questions_mod._pending.clear()
  questions_mod._cancelled.clear()
  secure_inputs_mod._requests.clear()
  registry.reset_for_tests()
  # Reset the per-chat queue-lock registry so a lock held by a leaked
  # task from a prior test can't be returned to the next test's caller.
  chat_queue_mod.reset_for_tests()
  # Drop any cached skill text loaded by a prior test; the next caller
  # will re-read from disk. Using setattr in case the attribute is
  # declared lazily below the read-site.
  setattr(chat_mod, "_SKILL_TEXT_CACHE", None)
  chat_mod.draining = False
  restart_util_mod._RESTART_ADMITTED = False
  chat_mod._clear_after_terminal_generation.clear()
  chat_mod._clear_after_terminal_status.clear()
  chat_mod._restart_draining_chats.clear()
  chat_mod._next_limit_auto_resume_at = 0.0
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
  from app.routes import client_signal as client_signal_mod
  client_signal_mod._reset_for_tests()
  # The single-writer chat-persistence actor is a process singleton the
  # FastAPI lifespan starts in production. TestClient(app) (no `with`)
  # doesn't run lifespan, and the C2 live write paths now route through
  # `get_writer()`, so start a fresh actor per test bound to the
  # recreated test DB. Restarting each test gives the actor a fresh
  # session that sees the just-created tables — its long-lived session
  # would otherwise hold a stale identity map across the drop/create.
  from app import chat_writer as chat_writer_mod
  chat_writer_mod.stop_writer(timeout=5)
  # Per-engine schema and "all converted" facts describe one database; the
  # suite reuses one engine across tests that rebuild its state.
  from app import transcript_rows as transcript_rows_mod
  transcript_rows_mod.reset_conversion_facts()
  chat_writer_mod.transcript_conversion_status.update(state="idle", error=None, failed={})
  from app.database import SessionLocal as _WriterSession
  chat_writer_mod.start_writer(_WriterSession)
  # start_writer intentionally publishes before its worker opens and probes
  # the lazy SQLAlchemy session: production readiness must expose that window.
  # Tests, however, use this fixture as their documented healthy baseline and
  # some exercise consumer-only recovery seams directly. Wait for the boot
  # probe so those calls cannot race the writer thread over the same session.
  _writer = chat_writer_mod.get_writer()
  assert _writer._session_ready.wait(timeout=5), chat_writer_mod.writer_readiness()
  assert chat_writer_mod.writer_readiness() == (True, None)
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
  # file trees so the filesystem matches the freshly-recreated DB. Provider
  # credentials are test fixtures too: several chat-run tests seed Claude auth,
  # and without clearing cli-auth a later provider-default test can inherit it.
  # Serial collection happened to hide that dependency; xdist correctly makes
  # function order nondeterministic within each worker.
  import shutil as _shutil
  _data_dir = _os.environ.get("DATA_DIR", "/tmp")
  # Content-addressed app bundles no longer overwrite app-<id>.js between
  # tests. Clear compiled too, otherwise the per-test id reset leaves the next
  # test seeing an earlier test's immutable artifact for the same numeric id.
  for _sub in ("apps", "app-secrets", "app-runtime", "app-envs", "shared", "compiled", "cli-auth"):
    _shutil.rmtree(_os.path.join(_data_dir, _sub), ignore_errors=True)

  # Installed apps' model-provider declarations are projected into the
  # process-global provider registry, and that projection is read-throttled
  # for a second. The previous test's App rows are gone, so re-project from
  # the empty tables now and reopen the throttle; otherwise a test that runs
  # within a second of one that installed the identity app inherits its
  # Möbius provider as available.
  from app import providers as providers_mod
  from app.config import get_settings as _get_settings
  providers_mod.sync_app_model_providers(_get_settings().data_dir, force=True)
  providers_mod._app_provider_sync_at = 0.0

  yield
  from app import chat_writer as _cw
  _cw.stop_writer(timeout=5)
  # The model schema is immutable during the suite. Delete rows in dependency
  # order instead of dropping and rebuilding every table thousands of times.
  # The writer is already stopped, so no background transaction can race this
  # cleanup; the next test still gets a fresh actor and SQLAlchemy session.
  with engine.begin() as connection:
    _assert_legacy_mirrors_rows(connection)
    # These disposable tables are migration-owned rather than ORM-owned, so
    # Base.metadata cannot include them in the generic deletion pass. Deleting
    # search rows first also drives the SQLite external-content FTS triggers.
    for name in ("chat_search_docs", "chat_search_state", "chat_search_entries"):
      connection.exec_driver_sql(f'DELETE FROM "{name}"')
    for table in reversed(Base.metadata.sorted_tables):
      connection.execute(table.delete())


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
  assert r.status_code == 200, r.text
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
  """Create a send-ready empty chat with an explicit model selection.

  UUID4 is the production format (str(uuid.uuid4())); using it here
  keeps upload/generate endpoint tests valid after the chat_id format
  check landed in those routes. Production new chats deliberately start with
  no per-chat model; tests for that admission state clear this field.
  """
  import uuid
  from app import chat_writer
  c = chat_writer.create_chat(
    id=str(uuid.uuid4()),
    title="Test chat",
    messages=[],
    agent_settings_json={"model": "claude-opus-4-8"},
  )
  db.add(c)
  db.commit()
  db.refresh(c)
  return c


# ── Parallel shards ──────────────────────────────────────────────────────
# MOBIUS_TEST_SHARD=k/n runs one of n slices of the suite; CI runs the slices
# as parallel jobs and combines their coverage. Whole files are assigned,
# heaviest first, to the least-loaded slice by their recorded duration in
# shard_weights.json; a file without a record weighs the median. A full run
# with MOBIUS_TEST_RECORD_WEIGHTS=1 rewrites the record.
import collections as _collections
import json as _json
import statistics as _statistics

_SHARD_WEIGHTS = _Path(__file__).with_name("shard_weights.json")
_recorded_weights: _collections.Counter = _collections.Counter()


def _test_file(nodeid):
  return nodeid.split("::", 1)[0]


def _requested_shard():
  spec = os.environ.get("MOBIUS_TEST_SHARD", "").strip()
  if not spec:
    return None
  try:
    index, total = (int(part) for part in spec.split("/"))
  except ValueError:
    index = total = 0
  if not 1 <= index <= total:
    raise pytest.UsageError(f"MOBIUS_TEST_SHARD must be k/n with 1 <= k <= n, not {spec!r}")
  return index, total


def pytest_collection_modifyitems(config, items):
  shard = _requested_shard()
  if shard is None:
    return
  index, total = shard
  weights = _json.loads(_SHARD_WEIGHTS.read_text()) if _SHARD_WEIGHTS.exists() else {}
  default = _statistics.median(weights.values()) if weights else 1.0
  files = {_test_file(item.nodeid) for item in items}
  loads = {slot: 0.0 for slot in range(1, total + 1)}
  owner = {}
  for path in sorted(files, key=lambda f: (-weights.get(f, default), f)):
    slot = min(loads, key=lambda s: (loads[s], s))
    owner[path] = slot
    loads[slot] += weights.get(path, default)
  keep = [item for item in items if owner[_test_file(item.nodeid)] == index]
  if len(keep) != len(items):
    config.hook.pytest_deselected(
      items=[item for item in items if owner[_test_file(item.nodeid)] != index])
    items[:] = keep


def pytest_runtest_logreport(report):
  if os.environ.get("MOBIUS_TEST_RECORD_WEIGHTS") == "1":
    _recorded_weights[_test_file(report.nodeid)] += report.duration


def pytest_sessionfinish(session):
  # Only the controller sees every worker's reports.
  if os.environ.get("MOBIUS_TEST_RECORD_WEIGHTS") != "1" or hasattr(session.config, "workerinput"):
    return
  record = {path: round(seconds, 1) for path, seconds in sorted(_recorded_weights.items())}
  _SHARD_WEIGHTS.write_text(_json.dumps(record, indent=2) + "\n")
