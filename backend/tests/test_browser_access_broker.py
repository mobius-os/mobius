"""The local MCP broker cannot outlive a shared-browser grant."""

from app.chat_writer import create_chat
import asyncio
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from app import access_signal, connectors, models
from app.browser_access import revoke_grant
from app.routes import connectors as routes
from tests.browser_access_fixtures import link_grant


def test_bound_capability_rechecks_owner_and_browser_grant(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'broker.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as db:
    owner = models.Owner(username='owner', hashed_password='unused')
    db.add(owner)
    db.commit()
    grant, _ = link_grant(db, owner, 'recipient')
    cap = connectors.mint_broker_capability(
      7, 'x' * 64, owner_id=owner.id, owner_epoch=owner.token_epoch,
      browser_grant_id=grant.id,
    )
    claims = connectors.verify_broker_capability(cap, 7, 'x' * 64, db=db)
    assert claims['browser_grant_id'] == grant.id
    with pytest.raises(connectors.ConnectorError):
      connectors.verify_broker_capability(cap, 7, 'x' * 64)
    revoke_grant(db, grant.id, owner.id)
    with pytest.raises(connectors.ConnectorError):
      connectors.verify_broker_capability(cap, 7, 'x' * 64, db=db)

    # Pre-upgrade owner capabilities remain accepted for their remaining TTL.
    legacy = connectors.mint_broker_capability(7, 'x' * 64)
    connectors.verify_broker_capability(legacy, 7, 'x' * 64, db=db)


def test_owner_epoch_rotates_new_broker_capabilities(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'owner.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as db:
    owner = models.Owner(username='owner', hashed_password='unused')
    db.add(owner)
    db.commit()
    cap = connectors.mint_broker_capability(
      7, 'x' * 64, owner_id=owner.id, owner_epoch=owner.token_epoch,
    )
    connectors.verify_broker_capability(cap, 7, 'x' * 64, db=db)
    owner.token_epoch += 1
    db.commit()
    with pytest.raises(connectors.ConnectorError):
      connectors.verify_broker_capability(cap, 7, 'x' * 64, db=db)


@pytest.mark.asyncio
async def test_broker_stream_closes_after_grant_revoke_without_remote_io(monkeypatch):
  active = True
  closed = []

  class Resource:
    headers = {}
    status_code = 200

    async def aclose(self):
      closed.append(self)

  client, upstream = Resource(), Resource()
  snapshot = routes._BrokerSnapshot(
    url='https://unused.example/mcp', auth_header='Authorization', secret='unused',
    generation='x' * 64, lineage={'browser_grant_id': 'guest'},
  )
  monkeypatch.setattr(routes, '_require_loopback', lambda request: 'cap')
  monkeypatch.setattr(routes, '_snapshot_broker_row',
                      lambda db, connector_id, capability: snapshot)
  monkeypatch.setattr(routes, '_broker_lineage_active',
                      lambda connector_id, snapshot: active)

  async def fake_open(request, snapshot):
    return client, upstream

  async def chunks(upstream, snapshot):
    yield b'first'
    yield b'second'

  monkeypatch.setattr(routes, '_open_broker_upstream', fake_open)
  monkeypatch.setattr(routes, '_redacted_broker_stream', chunks)
  db = type('Db', (), {'close': lambda self: None})()
  request = Request({'type': 'http', 'method': 'GET', 'path': '/',
                     'headers': [], 'client': ('127.0.0.1', 1234)})
  response = await routes.broker_connector(7, request, db)
  body = response.body_iterator
  assert await anext(body) == b'first'
  active = False
  access_signal.notify_access_changed()  # what the revoking commit does
  with pytest.raises(StopAsyncIteration):
    await anext(body)
  assert client in closed and upstream in closed


def test_turn_plan_issues_bound_broker_token_from_run_session(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'plan.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as db:
    owner = models.Owner(username='owner', hashed_password='unused')
    db.add(owner)
    db.commit()
    grant, _ = link_grant(db, owner, 'recipient')
    connector = models.Connector(
      slug='docs', name='Docs', url='https://docs.example/mcp',
      enabled=True, status='ok', tools_json=[], est_tokens=0,
    )
    db.add(connector)
    db.commit()
    plan = connectors.build_turn_plan(
      db, include_owner_connectors=True,
      owner_id=owner.id, owner_epoch=owner.token_epoch,
      browser_grant_id=grant.id,
    )
    assert plan is not None
    server = next(iter(plan.claude_servers.values()))
    token = server['headers']['Authorization'].removeprefix('Bearer ')
    claims = connectors.verify_broker_capability(
      token, connector.id, connector.capability_id, db=db,
    )
    assert claims['owner_id'] == owner.id
    assert claims['browser_grant_id'] == grant.id
    revoke_grant(db, grant.id, owner.id)
    with pytest.raises(connectors.ConnectorError):
      connectors.verify_broker_capability(
        token, connector.id, connector.capability_id, db=db,
      )


@pytest.mark.asyncio
async def test_broker_upload_stops_forwarding_after_revoke(monkeypatch):
  active = True
  snapshot = routes._BrokerSnapshot(
    url='https://unused.example/mcp', auth_header='Authorization', secret='unused',
    connector_id=7, generation='x' * 64,
    lineage={'browser_grant_id': 'guest'},
  )
  monkeypatch.setattr(routes, '_broker_lineage_active',
                      lambda connector_id, snapshot: active)

  class Upload:
    async def stream(self):
      yield b'first'
      yield b'second'

  iterator = routes._revocable_broker_upload(Upload(), 7, snapshot)
  assert await anext(iterator) == b'first'
  active = False
  access_signal.notify_access_changed()  # what the revoking commit does
  from fastapi import HTTPException
  with pytest.raises(HTTPException) as error:
    await anext(iterator)
  assert error.value.status_code == 401


def test_open_stream_lineage_recheck_sees_fresh_revocation(tmp_path, monkeypatch):
  from sqlalchemy.orm import sessionmaker

  eng = create_engine(f"sqlite:///{tmp_path / 'stream-revoke.db'}")
  models.Base.metadata.create_all(eng)
  factory = sessionmaker(bind=eng)
  monkeypatch.setattr(routes, 'SessionLocal', factory)
  with factory() as db:
    owner = models.Owner(username='owner', hashed_password='unused')
    db.add(owner)
    db.commit()
    grant, _ = link_grant(db, owner, 'recipient')
    connector = models.Connector(
      slug='docs', name='Docs', url='https://docs.example/mcp',
      enabled=True, status='ok', tools_json=[], est_tokens=0,
    )
    db.add(connector)
    db.commit()
    snapshot = routes._BrokerSnapshot(
      url=connector.url, auth_header=None, secret=None,
      connector_id=connector.id, generation=connector.capability_id,
      # Capabilities minted before the grant epoch retired still carry it.
      lineage={'owner_id': owner.id, 'owner_epoch': owner.token_epoch,
               'browser_grant_id': grant.id, 'browser_grant_epoch': 0},
    )
    assert routes._broker_lineage_active(connector.id, snapshot)
    revoke_grant(db, grant.id, owner.id)
    assert not routes._broker_lineage_active(connector.id, snapshot)


def _access_db(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'signal.db'}")
  models.Base.metadata.create_all(eng)
  return eng


def test_only_committed_access_changes_wake_open_broker_streams(tmp_path):
  with Session(_access_db(tmp_path)) as db:
    owner = models.Owner(username='owner', hashed_password='unused')
    connector = models.Connector(
      slug='docs', name='Docs', url='https://docs.example/mcp',
      enabled=True, status='ok', tools_json=[], est_tokens=0,
    )
    db.add_all([owner, connector])
    db.commit()
    grant, _ = link_grant(db, owner, 'recipient')

    def bumps(change) -> bool:
      before = access_signal.current_revision()
      change()
      return access_signal.current_revision() != before

    # New rows cannot revoke an open exchange; unrelated tables are ignored.
    assert not bumps(lambda: (db.add(models.Connector(
      slug='more', name='More', url='https://more.example/mcp',
      enabled=True, status='ok', tools_json=[], est_tokens=0,
    )), db.commit()))
    assert not bumps(lambda: (db.add(create_chat(id='c1', title='x')), db.commit()))

    # An uncommitted revocation must not wake anyone.
    def rolled_back():
      connector.enabled = False
      db.flush()
      db.rollback()
    assert not bumps(rolled_back)

    def disable():
      connector.enabled = False
      db.commit()
    assert bumps(disable)

    def sign_out_everywhere():
      owner.token_epoch += 1
      db.commit()
    assert bumps(sign_out_everywhere)
    # Bulk statements (how grants are revoked) count as well as ORM edits.
    assert bumps(lambda: revoke_grant(db, grant.id, owner.id))
    assert bumps(lambda: (db.delete(connector), db.commit()))

    # Account grants are bound to the owner's mobius.you link, which identity
    # routes can delete on its own (for example after a remote 401).
    link = models.IdentityAccountLink(
      owner_id=owner.id, access_token_encrypted='unused', scopes_json=[],
    )
    db.add(link)
    db.commit()
    assert bumps(lambda: (db.delete(link), db.commit()))

    # A savepoint released inside the revoking transaction must not publish
    # early; the outer commit is what makes the change visible.
    other = models.Connector(
      slug='later', name='Later', url='https://later.example/mcp',
      enabled=True, status='ok', tools_json=[], est_tokens=0,
    )
    db.add(other)
    db.commit()
    before = access_signal.current_revision()
    other.enabled = False
    db.flush()
    with db.begin_nested():
      db.add(create_chat(id='c2', title='y'))
    assert access_signal.current_revision() == before
    db.commit()
    assert access_signal.current_revision() != before


class _LineageProbe:
  """Stands in for the database lineage check and records where it ran."""

  def __init__(self):
    self.active = True
    self.ran_on_main_thread: list[bool] = []

  def __call__(self, connector_id, snapshot):
    self.ran_on_main_thread.append(
      threading.current_thread() is threading.main_thread(),
    )
    return self.active


def _idle_stream(monkeypatch, probe):
  async def chunks():
    yield b'first'
    await asyncio.Event().wait()  # an upstream that goes quiet

  snapshot = routes._BrokerSnapshot(
    url='https://unused.example/mcp', auth_header=None, secret=None,
    connector_id=7, generation='x' * 64,
    access_revision=access_signal.current_revision(),
  )
  monkeypatch.setattr(routes, '_broker_lineage_active', probe)
  return routes._until_broker_revoked(chunks(), 7, snapshot)


@pytest.mark.asyncio
async def test_idle_broker_stream_rechecks_only_after_an_access_change(monkeypatch):
  probe = _LineageProbe()
  stream = _idle_stream(monkeypatch, probe)
  assert await anext(stream) == b'first'
  waiting = asyncio.create_task(anext(stream))
  await asyncio.sleep(1.3)  # longer than the old per-second polling tick
  assert probe.ran_on_main_thread == [] and not waiting.done()

  probe.active = False
  access_signal.notify_access_changed()
  with pytest.raises(routes._BrokerRevoked):
    await asyncio.wait_for(waiting, timeout=2)
  assert probe.ran_on_main_thread == [False], 'one recheck, off the event loop'


@pytest.mark.asyncio
async def test_out_of_process_revocation_is_caught_by_the_safety_recheck(
  monkeypatch,
):
  monkeypatch.setattr(routes, '_BROKER_OUT_OF_PROCESS_RECHECK_SECONDS', 0.05)
  probe = _LineageProbe()
  stream = _idle_stream(monkeypatch, probe)
  assert await anext(stream) == b'first'
  probe.active = False  # e.g. an operator script: no in-process commit signal
  with pytest.raises(routes._BrokerRevoked):
    await asyncio.wait_for(anext(stream), timeout=2)


@pytest.mark.asyncio
async def test_busy_stream_still_runs_the_out_of_process_safety_recheck(
  monkeypatch,
):
  monkeypatch.setattr(routes, '_BROKER_OUT_OF_PROCESS_RECHECK_SECONDS', 0.05)
  probe = _LineageProbe()

  async def chunks():
    while True:
      yield b'tick'
      await asyncio.sleep(0.005)  # far more often than the safety interval

  snapshot = routes._BrokerSnapshot(
    url='https://unused.example/mcp', auth_header=None, secret=None,
    connector_id=7, generation='x' * 64,
    access_revision=access_signal.current_revision(),
  )
  monkeypatch.setattr(routes, '_broker_lineage_active', probe)
  stream = routes._until_broker_revoked(chunks(), 7, snapshot)
  assert await anext(stream) == b'tick'
  probe.active = False  # revoked out of process: no in-process commit signal

  async def drain():
    async for _ in stream:
      pass

  with pytest.raises(routes._BrokerRevoked):
    await asyncio.wait_for(drain(), timeout=2)
