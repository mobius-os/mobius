"""Host admission topology and non-dispatch installation publication."""
import copy
import json
import os
import subprocess

import pytest

from tests.test_mobius_rebuild_host import host
from tests.test_admission_host_recovery import incident


def test_prepared_gate_survives_retirement_of_original_ledger_receipt(incident):
    c = incident
    c.host.prepare_admission(c.config, c.tx)
    before = c.gate.observe()
    c.legacy.CUTOVER_RECEIPT_PATH.unlink()
    c.host.prepare_admission(c.config, c.tx)
    assert c.gate.observe() == before
    assert len(c.docker.removes) == 1


@pytest.mark.parametrize('source_status,drain_confirmed,allowed', [
    ('running', False, False), ('running', True, True),
    ('exited', False, True), ('restarting', False, True), ('missing', False, True),
])
def test_missing_initial_receipt_never_removes_undrained_serviceable_source(
        incident, source_status, drain_confirmed, allowed):
    c = incident
    c.legacy.CUTOVER_RECEIPT_PATH.unlink()
    c.tx['drain_confirmed'] = drain_confirmed
    source = c.tx['source_container']
    if source_status == 'missing':
        del c.docker.containers[source]
    else:
        c.docker.containers[source]['State']['Status'] = source_status
    if not allowed:
        with pytest.raises(RuntimeError):
            c.host.prepare_admission(c.config, c.tx)
        assert not c.docker.removes and not c.docker.starts
        return
    c.host.prepare_admission(c.config, c.tx)
    state = c.gate.observe()
    assert state['ledger_degraded'] is True
    assert state['receipt'] is None and state['source_handoff_valid'] is False
    assert source in state['quiesced']


def test_settlement_requires_gate_boot_not_only_a_matching_legacy_pair(incident):
    c = incident
    cid = c.host.prepare_rollback(c.config, c.tx)
    assert c.docker.complete_start(cid)
    ack = json.loads(c.legacy.ACK_PATH.read_text())
    ack['target_boot_id'] = 'unrelated-boot-12345678'
    os.chmod(c.legacy.ACK_PATH, 0o600)
    os.chmod(c.legacy.BOOT_PATH, 0o600)
    c.legacy.ACK_PATH.write_text(json.dumps(ack))
    c.legacy.BOOT_PATH.write_text(ack['target_boot_id'])
    assert c.host.cutover_boot_consumed(c.config, c.tx['operation_id'])
    assert not c.host.finish_verified(c.config, c.tx, cid, c.tx['previous_image'],
                                     state='rolled_back', code='replacement_failed', message='test')
    assert c.host.read_json(c.host.STATUS)['code'] == 'handoff_boot_unconfirmed'
    assert c.host.TRANSACTION.exists()


def test_new_proven_rejection_supersedes_old_unknown_observation(incident, monkeypatch):
    c = incident
    c.tx['phase'] = 'replacement_started'
    c.host.write_transaction(c.tx)
    c.host.prepare_admission(c.config, c.tx)
    cid = c.host.prepare_attempt(c.config, c.tx, 'target')
    assert c.docker.complete_start(cid)
    c.host.write_status(c.config, operation_id=c.tx['operation_id'], state='needs_recovery',
                        code='observation_unconfirmed', message='prior unknown observation')
    def reject(*_):
        raise c.host.ProvenanceRejected('wrong served revision')
    monkeypatch.setattr(c.host, 'verify_served_generation', reject)
    c.host.reconcile()
    status = c.host.read_json(c.host.STATUS)
    assert status['state'] == 'rolled_back'
    assert status['code'] == 'replacement_failed'


def topology(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    config = {'project': 'mobius', 'data_dir': data, 'control_dir': tmp_path / 'control'}
    config['control_dir'].mkdir()
    tx = {'operation_id': 'a' * 32}
    resolved = {'services': {'app': {'image': 'old', 'volumes': [
        {'type': 'bind', 'source': str(data), 'target': '/data'}],
        'networks': {'edge': {'aliases': ['app', 'mobius']}},
        'environment': {'KEEP': 'value'}, 'restart': 'unless-stopped',
        'build': {'context': '/ignored'}, 'depends_on': {'other': {}}}},
        'networks': {'edge': {'name': 'existing-edge'}}}
    monkeypatch.setattr(host, 'inspect_image', lambda *_: json.dumps({
        'Entrypoint': ['/usr/bin/tini', '--'], 'Cmd': ['/app/start']}))
    monkeypatch.setattr(host, 'docker_command', lambda args, **kw:
                        subprocess.CompletedProcess(args, 0, json.dumps(resolved), ''))
    return config, tx, resolved


def test_attempt_scope_preserves_argv_and_shared_resources_without_reconciliation(tmp_path, monkeypatch):
    config, tx, _ = topology(tmp_path, monkeypatch)
    result = host.wrapped_configuration(config, tx, 'target', 'b' * 32, 'sha256:' + 'c' * 64)
    app = result['services']['app']
    assert result['name'] == app['container_name'] == 'mobius-admission-' + 'b' * 32
    assert app['entrypoint'][-3:] == ['--', '/usr/bin/tini', '--']
    assert app['command'] == ['/app/start']
    assert app['environment'] == {'KEEP': 'value'}
    assert app['networks']['edge']['aliases'] == ['app', 'mobius']
    assert result['networks'] == {'edge': {'name': 'existing-edge', 'external': True}}
    assert 'build' not in app and 'depends_on' not in app
    assert app['labels']['io.mobius.admission.project'] == 'mobius'


@pytest.mark.parametrize('mutation', ['different-data', 'readonly-data', 'anonymous', 'secret', 'namespace'])
def test_preflight_rejects_unpreserved_resource_contract(tmp_path, monkeypatch, mutation):
    config, tx, resolved = topology(tmp_path, monkeypatch)
    app = resolved['services']['app']
    if mutation == 'different-data':
        app['volumes'][0]['source'] = '/different-data'
    elif mutation == 'readonly-data':
        app['volumes'][0]['read_only'] = True
    elif mutation == 'anonymous':
        app['volumes'].append({'type': 'volume', 'target': '/other'})
    elif mutation == 'secret':
        app['secrets'] = ['credential']
    else:
        app['network_mode'] = 'service:other'
    with pytest.raises(RuntimeError):
        host.wrapped_configuration(config, tx, 'target', 'b' * 32, 'sha256:' + 'c' * 64)


def test_manual_roles_use_different_durable_topology_snapshots(tmp_path, monkeypatch):
    config, tx, resolved = topology(tmp_path, monkeypatch)
    tx['source_topology'] = copy.deepcopy(resolved)
    tx['target_topology'] = copy.deepcopy(resolved)
    tx['target_topology']['services']['app']['environment'] = {'NEW': 'target'}
    monkeypatch.setattr(host, 'docker_command', lambda *_a, **_k: pytest.fail('re-read topology instead of frozen input'))
    source = host.wrapped_configuration(config, tx, 'rollback', 'b' * 32, 'sha256:' + 'c' * 64)
    target = host.wrapped_configuration(config, tx, 'target', 'd' * 32, 'sha256:' + 'e' * 64)
    assert source['services']['app']['environment'] == {'KEEP': 'value'}
    assert target['services']['app']['environment'] == {'NEW': 'target'}


def test_capability_publication_never_dispatches_and_preserves_outcome(tmp_path, monkeypatch):
    config, _, _ = topology(tmp_path, monkeypatch)
    monkeypatch.setattr(host, 'config', lambda: config)
    monkeypatch.setattr(host, 'LOCK', tmp_path / 'lock')
    monkeypatch.setattr(host, 'STATUS', tmp_path / 'status')
    monkeypatch.setattr(host, 'TRANSACTION', tmp_path / 'transaction')
    monkeypatch.setattr(host, 'docker_command', lambda *_a, **_k: pytest.fail('installer called Docker'))
    monkeypatch.setenv('MOBIUS_REBUILD_LAUNCHER', '2')
    host.STATUS.write_text(json.dumps({'state': 'rolled_back', 'operation_id': 'old', 'code': 'original'}))
    assert host.publish_capabilities() == 0
    result = host.read_json(host.STATUS)
    assert result['state'] == 'rolled_back' and result['operation_id'] == 'old'
    assert result['code'] == 'original' and result['launcher_revision'] == 2
    before = host.STATUS.read_bytes()
    host.TRANSACTION.write_text('{}')
    assert host.publish_capabilities() == 1
    assert host.STATUS.read_bytes() == before


def test_exact_removal_does_not_guess_from_transport_failure(monkeypatch):
    cid = 'a' * 64
    def response(message):
        monkeypatch.setattr(host, 'docker_command', lambda args, **kw:
                            subprocess.CompletedProcess(args, 1, '', message))
    response('Error response from daemon: No such container: ' + cid)
    host.remove_exact_container(cid)
    for message in ('connection reset', 'No such container', 'Error response from daemon: No such container: other'):
        response(message)
        with pytest.raises(subprocess.CalledProcessError):
            host.remove_exact_container(cid)


@pytest.mark.parametrize("entrypoint,command,expected", [
    (["/custom-start"], None, ["/custom-start"]),
    (["/custom-start"], ["--flag"], ["/custom-start", "--flag"]),
    ([], ["/custom-command"], ["/custom-command"]),
    (None, [], ["/usr/bin/tini", "--"]),
])
def test_wrapper_preserves_compose_entrypoint_command_overrides(tmp_path, monkeypatch, entrypoint, command, expected):
    config, tx, resolved = topology(tmp_path, monkeypatch)
    resolved["services"]["app"].update(entrypoint=entrypoint, command=command)
    result = host.wrapped_configuration(config, tx, "target", "b" * 32, "sha256:" + "c" * 64)
    app = result["services"]["app"]
    original = app["entrypoint"][app["entrypoint"].index("--") + 1:] + app["command"]
    assert original == expected


def test_same_image_cutover_recovery_does_not_mistake_source_for_target(incident):
    c = incident
    c.tx.update(target_image=c.tx['previous_image'], phase='replacement_started')
    c.host.write_transaction(c.tx)
    c.host.recover(c.config, c.tx)
    result = c.host.read_json(c.host.STATUS)
    assert result['state'] == 'rolled_back'
    assert not c.host.TRANSACTION.exists()
    assert c.tx['source_container'] in c.docker.removes
    assert len(c.docker.starts) == 1
