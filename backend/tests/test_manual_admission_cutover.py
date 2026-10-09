"""Manual deployment must cross the installed controller, not direct Compose."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHELL = (ROOT / 'scripts/deploy-prod.sh').read_text()
SPEC = importlib.util.spec_from_file_location('manual_cutover', ROOT / 'scripts/mobius-manual-cutover.py')
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)
CID = 'a' * 64
IMAGE = 'sha256:' + 'b' * 64
TARGET = 'sha256:' + 'c' * 64
SHA = 'd' * 40


def topology(*, project='mobius', volume='mobius_app_data', network='mobius_default'):
    return {'name': project,
            'services': {'app': {'volumes': [{'type': 'volume', 'source': 'app_data',
                                             'target': '/data'}]}},
            'volumes': {'app_data': {'name': volume}},
            'networks': {'default': {'name': network}}}


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    state.mkdir()
    for name in ('replace.lock', 'candidate.lock', 'compose.yml', 'config.json'):
        (state / name).write_text('')
    (state / 'compose.yml').write_text('services:\n  app: {}\n')
    desired = state / 'desired.json'
    desired.write_text(json.dumps(topology()))
    desired.chmod(0o600)
    monkeypatch.setattr(adapter, 'STATE', state)
    monkeypatch.setattr(adapter, 'LOCK', state / 'replace.lock')
    monkeypatch.setattr(adapter, 'TRIAL_LOCK', state / 'candidate.lock')
    monkeypatch.setattr(adapter, 'JOURNAL', state / 'transaction.json')
    monkeypatch.setattr(adapter, 'COMPOSE', state / 'compose.yml')
    monkeypatch.setattr(adapter, 'CONFIG', state / 'config.json')
    monkeypatch.setattr(adapter, 'STATUS', state / 'status.json')
    monkeypatch.setattr(adapter, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(adapter, 'TRUSTED_UID', os.geteuid())
    monkeypatch.setattr(adapter, 'TRUSTED_GID', os.getegid())
    calls = []

    class Worker:
        @staticmethod
        def config():
            return {'project': 'mobius'}

        @staticmethod
        def container_health(_):
            return CID, IMAGE if not calls else TARGET, 'healthy'

        @staticmethod
        def inspect_image(image, _):
            return image

        @staticmethod
        def docker_command(command, **_):
            assert command[-3:] == ['config', '--format', 'json']
            return SimpleNamespace(stdout=json.dumps(topology()))

        @staticmethod
        def execute_replacement(_, transaction):
            calls.append(transaction)
            (state / 'status.json').write_text(json.dumps({
                'state': 'succeeded', 'operation_id': transaction['operation_id']}))
            (state / 'status.json').chmod(0o600)
            return 0

    monkeypatch.setattr(adapter, 'active_worker', lambda: Worker)
    args = SimpleNamespace(command='cutover', source_cid=CID, source_image=IMAGE,
                           target_image=TARGET, expected_sha=SHA,
                           resolved_compose=str(desired))
    return state, desired, args, calls


def test_exact_success_returns_verified_new_cid_and_journaling_inputs(fixture):
    _, _, args, calls = fixture
    result = adapter.run(args)
    assert result['state'] == 'succeeded'
    assert result['container_id'] == CID
    assert result['image_id'] == TARGET
    assert len(calls) == 1
    tx = calls[0]
    assert (tx['source_container'], tx['previous_image'], tx['target_image']) == (CID, IMAGE, TARGET)
    assert tx['admission_version'] == 1
    assert tx['source_topology'] == topology() == tx['target_topology']


@pytest.mark.parametrize('change', [
    lambda t: t.update(name='wrong'),
    lambda t: t['volumes']['app_data'].update(name='other_data'),
    lambda t: t['networks']['default'].update(name='other_network'),
])
def test_rejects_project_volume_or_existing_network_retarget(fixture, change):
    _, desired, args, calls = fixture
    value = topology()
    change(value)
    desired.write_text(json.dumps(value))
    desired.chmod(0o600)
    with pytest.raises(ValueError):
        adapter.run(args)
    assert not calls


def test_edge_network_addition_keeps_original_data_and_default_network(fixture):
    _, desired, args, calls = fixture
    value = topology()
    value['networks']['edge-mobius'] = {'name': 'edge-mobius', 'external': True}
    desired.write_text(json.dumps(value))
    desired.chmod(0o600)
    assert adapter.run(args)['state'] == 'succeeded'
    assert calls[0]['target_topology']['networks']['edge-mobius']['external'] is True


def test_bind_data_source_is_supported_but_cannot_change():
    old = topology()
    new = topology()
    for item in (old, new):
        item['services']['app']['volumes'] = [
            {'type': 'bind', 'source': '/srv/mobius-data', 'target': '/data'}]
        item['volumes'] = {}
    adapter.check_topology(old, new, 'mobius')
    new['services']['app']['volumes'][0]['source'] = '/srv/other-data'
    with pytest.raises(ValueError, match='/data volume changed'):
        adapter.check_topology(old, new, 'mobius')


def test_root_input_read_rejects_symlink_and_group_writable_file(fixture):
    _, desired, _, _ = fixture
    link = desired.parent / 'alias.json'
    link.symlink_to(desired)
    with pytest.raises(OSError):
        adapter.read_json(link)
    desired.chmod(0o660)
    with pytest.raises(ValueError, match='untrusted controller input'):
        adapter.read_json(desired)


def test_pending_journal_and_stale_source_refuse_before_worker_execution(fixture):
    state, _, args, calls = fixture
    args.source_cid = 'e' * 64
    with pytest.raises(ValueError, match='identity changed'):
        adapter.run(args)
    args.source_cid = CID
    (state / 'transaction.json').write_text('{}')
    with pytest.raises(ValueError, match='pending replacement'):
        adapter.run(args)
    assert not calls


def test_shell_releases_shared_lock_then_calls_controller_and_switches_identity():
    start = SHELL.index('if [ "$HELPER_MANAGED" = 1 ]; then', SHELL.index('TARGET_IMAGE=$(docker image inspect'))
    end = SHELL.index('else\nif [ "$TARGET" = "prod" ]', start)
    branch = SHELL[start:end]
    assert 'exec 8>&-' in branch
    assert branch.index('exec 8>&-') < branch.index('"$MANUAL_CONTROLLER" cutover')
    assert 'config --format json' in branch
    assert 'CONTAINER=$(printf' in branch
    assert 'docker compose "${COMPOSE_ARGS[@]}" up' not in branch
    assert 'rearm_chat_cutover' not in branch
    assert SHELL.index('fi  # helper-managed production') < SHELL.index('run_deploy_canary\n')


def test_next_deploy_discovers_unique_current_id_before_fixed_name_guard():
    assert SHELL.index('"$MANUAL_CONTROLLER" discover') < SHELL.index('step "[0/4] checking')
    assert 'if ! CONTAINER=$(printf' in SHELL
