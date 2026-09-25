"""Every pinned first-boot app installs on an empty instance.

First boot only logs a failed default-app install, so a platform contract change
that a pinned package no longer satisfies would otherwise ship silently. This
fetches the pinned packages from GitHub, so the hermetic suite skips it; CI runs
it as its own step with MOBIUS_LIVE_BOOTSTRAP=1.
"""

import asyncio
import logging
import os

import pytest

from app import models
from app.bootstrap import _CORE_BOOTSTRAP_APPS, ensure_bootstrap_apps_installed

pytestmark = pytest.mark.skipif(
  os.environ.get("MOBIUS_LIVE_BOOTSTRAP") != "1",
  reason="fetches the pinned first-boot apps from GitHub",
)


def test_empty_instance_installs_every_pinned_bootstrap_app(
  db, monkeypatch, caplog,
):
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  caplog.set_level(logging.ERROR, logger="mobius.bootstrap")

  asyncio.run(ensure_bootstrap_apps_installed(db))

  assert [record.getMessage() for record in caplog.records] == []
  installed = {
    row.slug
    for row in db.query(models.App).filter(models.App.deleted_at.is_(None))
  }
  assert {app.manifest_id for app in _CORE_BOOTSTRAP_APPS} <= installed
