"""Opt-in real Chromium failure tests; isolated profiles and no owner credentials.

MOBIUS_BROWSER_LIVE_TESTS=1 scripts/wt-pytest.sh tests/test_browser_live_lifecycle.py
"""
import asyncio
import os
import re
from pathlib import Path
import subprocess
import time
import uuid

import pytest

from app import browser_processes as bp, chat

pytestmark = pytest.mark.skipif(
  os.environ.get('MOBIUS_BROWSER_LIVE_TESTS') != '1',
  reason='explicit real-browser test opt-in required',
)


@pytest.fixture
def browsers(tmp_path):
  owners = []

  def launch():
    owner = 'browser-test-' + uuid.uuid4().hex
    profile = str(tmp_path / owner)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('AGENT_BROWSER_') and k != 'AGENT_TOKEN'}
    env.update(CHAT_ID=owner, AGENT_BROWSER_SESSION=owner,
               AGENT_BROWSER_PROFILE=profile, AGENT_BROWSER_CONFIG='/app/agent-browser-config.json',
               AGENT_BROWSER_IDLE_TIMEOUT_MS='600000')
    owners.append((owner, profile, env))
    run(env, 'open', 'about:blank')
    return owner, profile, env

  yield launch
  for owner, profile, _ in owners:
    bp.reset_browser_processes(chat_id=owner, profile=profile)


def run(env, *args):
  return subprocess.run(['agent-browser', *args], env=env, check=True,
                        capture_output=True, text=True, timeout=15)


def test_real_graceful_close_releases_all_owned_processes(browsers):
  owner, profile, env = browsers()
  before = bp.scan_browser_processes(chat_id=owner, profile=profile)
  assert before.complete and before.targets and before.processes
  asyncio.run(chat._close_browser_session(owner))
  assert bp.scan_browser_processes(chat_id=owner, profile=profile).idle


def test_real_hung_command_cleanup_preserves_other_chat(browsers, monkeypatch):
  owner, profile, env = browsers()
  other, other_profile, other_env = browsers()
  client = subprocess.Popen(['agent-browser', 'eval', 'new Promise(() => {})'],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  try:
    time.sleep(0.5)  # fault injection: leave an in-flight CDP evaluation
    client.terminate()
    client.wait(timeout=3)
    monkeypatch.setattr(chat, '_BROWSER_CLOSE_WAIT_TIMEOUT', 0.25)
    started = time.monotonic()
    asyncio.run(chat._close_browser_session(owner))
    assert time.monotonic() - started < 8
    assert bp.scan_browser_processes(chat_id=owner, profile=profile).idle
    assert not bp.scan_browser_processes(chat_id=other, profile=other_profile).idle
    assert 'about:blank' in run(other_env, 'get', 'url').stdout
  finally:
    if client.poll() is None:
      client.kill()
      client.wait(timeout=3)


def test_real_orphan_browser_is_discovered_and_released(browsers):
  owner, profile, env = browsers()
  scan = bp.scan_browser_processes(chat_id=owner, profile=profile)
  daemons = tuple(p for p in scan.processes if Path('/proc', str(p.pid), 'cmdline')
                  .read_bytes().split(b'\0')[0].split(b'/')[-1].decode() in bp.DAEMONS)
  assert len(daemons) == 1
  bp.terminate_processes(daemons)
  orphaned = bp.scan_browser_processes(chat_id=owner, profile=profile)
  assert not orphaned.targets and orphaned.processes
  asyncio.run(chat._close_browser_session(owner))
  assert bp.scan_browser_processes(chat_id=owner, profile=profile).idle


def test_real_upgrade_snapshot_and_conditional_capture_features(browsers, tmp_path):
  owner, profile, env = browsers()
  run(env, 'eval', 'document.body.innerHTML = `<button>Keep</button>`')
  first = run(env, 'snapshot', '-i').stdout
  ref = re.search(r'button "Keep".*\[ref=(e\d+)\]', first)
  assert ref, first
  run(env, 'snapshot', '--delta')
  run(env, 'eval', 'document.body.insertAdjacentHTML("beforeend", "<p>Added</p>")')
  delta = run(env, 'snapshot', '--delta').stdout
  assert 'Added' in delta
  after = run(env, 'snapshot', '-i').stdout
  assert f'[ref={ref.group(1)}]' in after
  first_image, unchanged_image = tmp_path / 'first.png', tmp_path / 'unchanged.png'
  run(env, 'screenshot', '--if-changed', str(first_image))
  assert first_image.stat().st_size > 0
  run(env, 'screenshot', '--if-changed', str(unchanged_image))
  assert not unchanged_image.exists()
