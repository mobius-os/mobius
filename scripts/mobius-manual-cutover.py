#!/usr/bin/env python3
"""Root-pinned adapter between a local build and the installed replacement worker.

The worker, not this adapter, owns the durable cutover and recovery protocol.
Install this file under /usr/local/libexec; never invoke a checkout copy as root.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from pathlib import Path

STATE = Path('/var/lib/mobius-rebuild')
WORKERS = STATE / 'workers'
INDEX = STATE / 'workers.json'
LOCK = STATE / 'replace.lock'
TRIAL_LOCK = STATE / 'candidate.lock'
JOURNAL = STATE / 'transaction.json'
COMPOSE = Path('/etc/mobius-rebuild/compose.yml')
CONFIG = Path('/etc/mobius-rebuild/config.json')
STATUS = STATE / 'status.json'
IMAGE = re.compile(r'sha256:[0-9a-f]{64}\Z')
CID = re.compile(r'[0-9a-f]{64}\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')
TRUSTED_UID = 0
TRUSTED_GID = 0


def trusted(path: Path, *, directory: bool = False, private: bool = True) -> None:
    item = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(item.st_mode) or item.st_uid != 0 or item.st_gid != 0
            or item.st_mode & (0o077 if private else 0o022)
            or (not directory and item.st_nlink != 1)):
        raise ValueError(f'untrusted controller path: {path}')


def read_json(path: Path, *, limit: int = 1024 * 1024) -> dict:
    trusted(path.parent, directory=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        item = os.fstat(fd)
        if (not stat.S_ISREG(item.st_mode) or item.st_uid != TRUSTED_UID or item.st_gid != TRUSTED_GID
                or item.st_mode & 0o077 or item.st_nlink != 1):
            raise ValueError(f'untrusted controller input: {path}')
        raw = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(raw) > limit:
        raise ValueError('oversized controller input')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('controller input must be an object')
    return value


def active_worker():
    trusted(STATE, directory=True)
    trusted(WORKERS, directory=True)
    index = read_json(INDEX, limit=65536)
    if index.get('version') != 1 or index.get('recovery'):
        raise ValueError('controller has an incompatible or recovering worker')
    entry = index.get('active')
    if not isinstance(entry, dict):
        raise ValueError('missing active worker')
    name, digest = entry.get('file'), entry.get('sha256')
    if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9._-]+\.py', name)
            or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        raise ValueError('invalid worker index')
    path = WORKERS / name
    trusted(path)
    source = path.read_bytes()
    if len(source) > 1024 * 1024 or hashlib.sha256(source).hexdigest() != digest:
        raise ValueError('installed worker digest mismatch')
    spec = importlib.util.spec_from_file_location('mobius_installed_rebuild_worker', path)
    if spec is None or spec.loader is None:
        raise ValueError('installed worker unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, 'execute_replacement', None)):
        raise ValueError('installed worker lacks manual cutover support')
    return module


def data_volume(topology: dict) -> tuple[str, str]:
    services = topology.get('services')
    app = services.get('app') if isinstance(services, dict) else None
    if not isinstance(app, dict):
        raise ValueError('resolved topology has no app')
    volumes = app.get('volumes')
    if not isinstance(volumes, list):
        raise ValueError('app has no volumes')
    matched = [v for v in volumes if isinstance(v, dict) and v.get('target') == '/data']
    if len(matched) != 1 or not isinstance(matched[0].get('source'), str):
        raise ValueError('app /data mount is ambiguous')
    mount = matched[0]
    name = mount['source']
    if mount.get('type') == 'bind':
        if not Path(name).is_absolute():
            raise ValueError('app /data bind source is not absolute')
        return 'bind', name
    definition = topology.get('volumes', {}).get(name, {})
    if mount.get('type') != 'volume' or not isinstance(definition, dict):
        raise ValueError('unsupported app /data mount')
    return 'volume', definition.get('name') or name


def check_topology(source: dict, target: dict, project: str) -> None:
    if source.get('name') != project or target.get('name') != project:
        raise ValueError('Compose project changed')
    if data_volume(source) != data_volume(target):
        raise ValueError('persistent /data volume changed')
    # A manual release may add/remove the edge network, but every retained
    # logical network must still refer to the same physical Docker network.
    for name, old in source.get('networks', {}).items():
        new = target.get('networks', {}).get(name)
        if new is not None and old.get('name', name) != new.get('name', name):
            raise ValueError(f'network identity changed: {name}')


def inspect_identity(worker, config_value):
    cid, image, state = worker.container_health(config_value)
    if not CID.fullmatch(cid) or not IMAGE.fullmatch(image) or state not in ('healthy', 'running'):
        raise ValueError('no exact serviceable app identity')
    return cid, image


def source_topology(worker, config_value, source_image: str) -> dict:
    # The installer freezes Compose YAML, not JSON. Ask Compose to resolve
    # that root-owned base under the exact currently running image identity.
    env = {**os.environ, 'MOBIUS_IMAGE': source_image}
    result = worker.docker_command(
        ['docker', 'compose', '-p', config_value['project'], '-f', str(COMPOSE),
         'config', '--format', 'json'],
        cwd=CONFIG.parent, env=env,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError('frozen Compose did not resolve to an object')
    return value


def run(args) -> dict:
    trusted(Path(__file__), private=False)
    trusted(Path(__file__).parent, directory=True, private=False)
    trusted(LOCK)
    trusted(TRIAL_LOCK)
    trusted(CONFIG)
    trusted(COMPOSE)
    # Installer and frozen launcher take candidate before replacement. Keep
    # both while selecting and invoking the indexed worker, or a trial/installer
    # could change the worker between verification and execution.
    with TRIAL_LOCK.open('r+') as trial, LOCK.open('r+') as lock:
        fcntl.flock(trial, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if JOURNAL.exists() or JOURNAL.is_symlink():
            raise ValueError('pending replacement transaction; reconcile first')
        worker = active_worker()
        config_value = worker.config()
        source_cid, source_image = inspect_identity(worker, config_value)
        if args.command == 'discover':
            return {'state': 'active', 'container_id': source_cid, 'image_id': source_image}
        if (source_cid != args.source_cid or source_image != args.source_image
                or not IMAGE.fullmatch(args.target_image) or not SHA.fullmatch(args.expected_sha)):
            raise ValueError('source or target identity changed')
        prior_topology = source_topology(worker, config_value, source_image)
        target_topology = read_json(Path(args.resolved_compose))
        check_topology(prior_topology, target_topology, config_value['project'])
        # Image IDs are immutable; never let a moving tag in the shell input
        # select the runtime image after validation.
        if worker.inspect_image(args.target_image, '{{.Id}}') != args.target_image:
            raise ValueError('target image ID is not present')
        operation = uuid.uuid4().hex
        transaction = {
            'version': 1, 'operation_id': operation,
            'expected_sha': args.expected_sha, 'request_nonce': None,
            'previous_image': source_image, 'target_image': args.target_image,
            'source_container': source_cid, 'admission_version': 1,
            'source_topology': prior_topology, 'target_topology': target_topology,
        }
        result = worker.execute_replacement(config_value, transaction)
        if result != 0:
            raise ValueError('controller did not settle a successful replacement')
        status = read_json(STATUS, limit=65536)
        if status.get('operation_id') != operation or status.get('state') != 'succeeded':
            raise ValueError('replacement outcome is not attributable success')
        cid, image = inspect_identity(worker, config_value)
        if image != args.target_image:
            raise ValueError('verified container does not run the target image')
        return {'state': 'succeeded', 'operation_id': operation,
                'container_id': cid, 'image_id': image}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('discover')
    cutover = sub.add_parser('cutover')
    for name in ('source-cid', 'source-image', 'target-image', 'expected-sha', 'resolved-compose'):
        cutover.add_argument('--' + name, required=True)
    args = parser.parse_args()
    try:
        result = run(args)
    except subprocess.SubprocessError:
        print('manual cutover refused: Docker observation failed', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, ImportError, RuntimeError) as exc:
        print(f'manual cutover refused: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
