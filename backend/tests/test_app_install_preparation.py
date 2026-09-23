"""Download concurrency must not weaken ordered app lifecycle publication."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app import fs_locks, install, schemas
from app.routes import apps as apps_routes
from app.routes.apps import install_app


@pytest.fixture(autouse=True)
def lifecycle_lock_for_test_loop(monkeypatch):
  # Production has one event loop; pytest deliberately creates one per test.
  monkeypatch.setattr(fs_locks, '_lifecycle_lock', asyncio.Lock())
  monkeypatch.setattr(
    apps_routes, '_APP_INSTALL_PREPARATION_SLOTS', asyncio.Semaphore(3),
  )


def _app(*, app_id=7, updated_at=None, deleted_at=None, package_id='pkg'):
  now = updated_at or datetime.now(UTC)
  return SimpleNamespace(
    id=app_id,
    created_at=now,
    updated_at=now,
    deleted_at=deleted_at,
    manifest_url='https://example.test/app/mobius.json#manifest-id=app',
    package_id=package_id,
    source_identity='source:app',
    slug='app',
    source_dir='/data/apps/app',
    upstream_commit='upstream-1',
    source_commit='source-1',
  )


def _candidate(version):
  return SimpleNamespace(
    manifest={"id": "app", "package_id": "pkg", "version": version},
    raw_base='https://example.test/app/',
  )


def _target(app):
  return SimpleNamespace(existing=app)


def _db_with_apps(*apps):
  db = MagicMock()
  db.query.return_value.all.return_value = list(apps)
  return db


@pytest.mark.asyncio
async def test_independent_install_downloads_overlap_but_publication_does_not(monkeypatch):
  prepared = []
  publishing = []
  all_downloading = asyncio.Event()
  events = []

  async def prepare(**kwargs):
    identity = kwargs['manifest_url']
    prepared.append(identity)
    if len(prepared) == 2:
      all_downloading.set()
    await asyncio.wait_for(all_downloading.wait(), timeout=2)
    events.append(('prepared', identity))
    return identity

  async def publish(db, *, candidate, **kwargs):
    assert not publishing, 'source publication overlapped another lifecycle mutation'
    publishing.append(candidate)
    events.append(('publish', candidate))
    await asyncio.sleep(0)
    publishing.remove(candidate)
    # Exercise the failure path without creating production app rows.
    raise HTTPException(409, 'test candidate rejected without mutation')

  monkeypatch.setattr(install, 'prepare_install_candidate', prepare)
  monkeypatch.setattr(install, 'install_candidate', publish)
  dbs = [MagicMock(), MagicMock()]
  outcomes = await asyncio.gather(*[
    install_app(schemas.AppInstall(manifest_url=f'https://example.test/{i}/mobius.json'), db=db, _=None)
    for i, db in enumerate(dbs)
  ], return_exceptions=True)
  assert all(isinstance(outcome, HTTPException) and outcome.status_code == 409 for outcome in outcomes)
  assert len([event for event in events if event[0] == 'publish']) == 2
  for db in dbs:
    db.close.assert_called_once()
  assert not fs_locks.install_uninstall_lock().locked()


@pytest.mark.asyncio
async def test_download_can_finish_during_uninstall_but_cannot_publish(monkeypatch):
  downloaded = asyncio.Event()
  published = asyncio.Event()

  async def prepare(**kwargs):
    downloaded.set()
    return object()

  async def publish(*args, **kwargs):
    published.set()
    raise HTTPException(409, 'app identity changed while download was pending')

  monkeypatch.setattr(install, 'prepare_install_candidate', prepare)
  monkeypatch.setattr(install, 'install_candidate', publish)
  async with fs_locks.install_uninstall_lock():
    task = asyncio.create_task(install_app(
      schemas.AppInstall(manifest_url='https://example.test/mobius.json'),
      db=MagicMock(), _=None,
    ))
    await asyncio.wait_for(downloaded.wait(), timeout=2)
    assert not published.is_set()
  with pytest.raises(HTTPException, match='app identity changed'):
    await task
  assert published.is_set()


@pytest.mark.asyncio
async def test_reversed_same_app_completion_rejects_stale_publication(monkeypatch):
  current = _app()
  dbs = [_db_with_apps(current), _db_with_apps(current)]
  both_preparing = asyncio.Event()
  v3_published = asyncio.Event()

  async def prepare(**kwargs):
    version = kwargs['manifest'].get('version')
    if version == 'v2':
      both_preparing.set()
      await v3_published.wait()
    else:
      await both_preparing.wait()
    return _candidate(version)

  async def publish(db, *, candidate, preparation_baseline, **kwargs):
    install.assert_install_preparation_fresh(
      preparation_baseline,
      target=_target(current),
      candidate=candidate,
      manifest_url=None,
    )
    if candidate.manifest['version'] == 'v3':
      current.updated_at = current.updated_at + timedelta(seconds=1)
      v3_published.set()
    raise HTTPException(409, 'published')

  monkeypatch.setattr(install, 'prepare_install_candidate', prepare)
  monkeypatch.setattr(install, 'install_candidate', publish)
  outcomes = await asyncio.gather(*[
    install_app(
      schemas.AppInstall(
        manifest={
          'id': 'app', 'name': 'App', 'entry': 'index.jsx',
          'version': version,
        },
        raw_base='https://example.test/app/',
      ),
      db=db,
      _=None,
    )
    for version, db in (('v2', dbs[0]), ('v3', dbs[1]))
  ], return_exceptions=True)
  assert all(isinstance(outcome, HTTPException) for outcome in outcomes)
  assert any(
    isinstance(outcome.detail, dict)
    and outcome.detail.get('code') == 'install_target_changed'
    for outcome in outcomes
  )


@pytest.mark.asyncio
async def test_delete_during_download_rejects_target_after_lock(monkeypatch):
  current = _app()
  db = _db_with_apps(current)
  downloaded = asyncio.Event()
  continue_download = asyncio.Event()

  async def prepare(**kwargs):
    downloaded.set()
    await continue_download.wait()
    return _candidate('v2')

  async def publish(db, *, candidate, preparation_baseline, **kwargs):
    install.assert_install_preparation_fresh(
      preparation_baseline,
      target=_target(current),
      candidate=candidate,
      manifest_url=None,
    )
    raise AssertionError('deleted target was published')

  monkeypatch.setattr(install, 'prepare_install_candidate', prepare)
  monkeypatch.setattr(install, 'install_candidate', publish)
  task = asyncio.create_task(install_app(
    schemas.AppInstall(
      manifest={
        'id': 'app', 'name': 'App', 'entry': 'index.jsx', 'version': 'v2',
      },
      raw_base='https://example.test/app/',
    ),
    db=db,
    _=None,
  ))
  await asyncio.wait_for(downloaded.wait(), timeout=2)
  current.deleted_at = datetime.now(UTC)
  current.updated_at = current.updated_at + timedelta(seconds=1)
  continue_download.set()
  with pytest.raises(HTTPException) as exc:
    await task
  assert exc.value.detail['code'] == 'install_target_changed'


def test_unrelated_app_change_does_not_invalidate_candidate():
  target = _app()
  unrelated = _app(app_id=8, package_id='other')
  baseline = install.capture_install_preparation_baseline(
    _db_with_apps(target, unrelated),
  )
  unrelated.updated_at = unrelated.updated_at + timedelta(seconds=1)
  install.assert_install_preparation_fresh(
    baseline,
    target=_target(target),
    candidate=_candidate('v2'),
    manifest_url=None,
  )


@pytest.mark.asyncio
async def test_install_preparation_is_capped_at_three_active_requests(monkeypatch):
  active = 0
  peak = 0
  started = asyncio.Event()
  release = asyncio.Event()

  async def prepare(**kwargs):
    nonlocal active, peak
    active += 1
    peak = max(peak, active)
    if active == 3:
      started.set()
    try:
      await release.wait()
      return object()
    finally:
      active -= 1

  async def publish(*args, **kwargs):
    raise HTTPException(409, 'test candidate rejected without mutation')

  monkeypatch.setattr(install, 'prepare_install_candidate', prepare)
  monkeypatch.setattr(install, 'install_candidate', publish)
  tasks = [
    asyncio.create_task(install_app(
      schemas.AppInstall(manifest_url=f'https://example.test/{i}/mobius.json'),
      db=MagicMock(),
      _=None,
    ))
    for i in range(4)
  ]
  await asyncio.wait_for(started.wait(), timeout=2)
  await asyncio.sleep(0)
  assert peak == 3
  assert active == 3
  assert not tasks[3].done()
  release.set()
  outcomes = await asyncio.gather(*tasks, return_exceptions=True)
  assert all(isinstance(outcome, HTTPException) for outcome in outcomes)
