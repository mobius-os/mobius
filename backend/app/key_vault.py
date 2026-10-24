"""Server-side owner of the key vault process and its socket.

The vault itself is ``backend/vault/keyvault.py``: a standalone program that
holds keys encrypted. Only this server talks to it, over one mode-0600 Unix
socket, and it states who each call is for (``app:<id>``, ``agent:<chat>``,
``owner`` or ``platform:<purpose>``) after authenticating the caller with its
own platform-issued token. That attestation is the handshake: an app cannot
name itself, and no app ever receives a value.

This module starts and supervises the vault (from
``RuntimeSupervisors.start_process_services``) and gives routes and the
platform a small client for it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path

import httpx

from app.config import get_settings


log = logging.getLogger(__name__)

VAULT_PROGRAM = Path(__file__).resolve().parents[1] / "vault" / "keyvault.py"
SOCKET_NAME = "vault.sock"
_RESTART_BACKOFF_SECS = (1, 2, 5, 10, 30, 60)
# A vault that ran this long before exiting counts as healthy again.
_HEALTHY_RUN_SECS = 120
_STOP_GRACE_SECS = 5
# Matches the vault's own upstream timeout plus room for its answer.
_USE_TIMEOUT_SECS = 75


class VaultUnavailable(RuntimeError):
  """The vault process is not answering on its socket."""


class VaultRefused(RuntimeError):
  """The vault refused; ``reason`` is caller-facing."""

  def __init__(self, status: int, reason: str) -> None:
    super().__init__(reason)
    self.status = status
    self.reason = reason


def vault_dir() -> Path:
  return Path(get_settings().data_dir) / ".vault"


def vault_socket() -> Path:
  return vault_dir() / SOCKET_NAME


def _caller_headers(caller: str | None) -> dict[str, str]:
  return {"X-Vault-Caller": caller} if caller else {}


def _settle(response: httpx.Response) -> dict:
  try:
    body = response.json()
  except ValueError:
    body = {}
  if response.status_code >= 400:
    raise VaultRefused(response.status_code, str(body.get("error") or "The key vault refused."))
  return body


async def vault_call(
  method: str, path: str, payload: dict | None = None, *, caller: str | None = None,
) -> dict:
  """One operation on the vault socket, on behalf of an attested caller."""
  transport = httpx.AsyncHTTPTransport(uds=str(vault_socket()))
  try:
    async with httpx.AsyncClient(
      transport=transport, base_url="http://vault", timeout=10,
    ) as client:
      response = await client.request(
        method, path, json=payload, headers=_caller_headers(caller),
      )
  except (httpx.TransportError, OSError) as exc:
    raise VaultUnavailable("The key vault is not running.") from exc
  return _settle(response)


def vault_call_sync(method: str, path: str, *, caller: str | None = None) -> dict:
  """``vault_call`` for synchronous platform code (model provider launch)."""
  try:
    with httpx.Client(
      transport=httpx.HTTPTransport(uds=str(vault_socket())),
      base_url="http://vault", timeout=10,
    ) as client:
      response = client.request(method, path, headers=_caller_headers(caller))
  except (httpx.TransportError, OSError) as exc:
    raise VaultUnavailable("The key vault is not running.") from exc
  return _settle(response)


async def vault_use(
  method: str, target: str, *, caller: str, headers: list[tuple[str, str]],
  body: bytes | None,
) -> httpx.Response:
  """Relay one use request (``/use/<key>/<host>/<path>?<query>``) as is.

  The vault answers refusals itself (``X-Vault-Denied``), so any response is
  returned to the caller unchanged; only an unreachable vault raises.
  """
  transport = httpx.AsyncHTTPTransport(uds=str(vault_socket()))
  try:
    async with httpx.AsyncClient(
      transport=transport, base_url="http://vault", timeout=_USE_TIMEOUT_SECS,
    ) as client:
      request = client.build_request(
        method, target, headers=[*headers, ("X-Vault-Caller", caller)], content=body,
      )
      # The caller's raw path must reach the vault byte for byte: its path
      # rules refuse encoded slashes and dot segments that normalising here
      # would hide.
      request.url = request.url.copy_with(raw_path=target.encode("ascii"))
      return await client.send(request)
  except (httpx.TransportError, OSError) as exc:
    raise VaultUnavailable("The key vault is not running.") from exc


# -- an app's own key names --------------------------------------------------
#
# Before the vault, each app had its own secret namespace, so two apps could
# both save "api_key", and one app could hold both "api.key" and "api-key".
# Vault names are shared, so each such secret becomes its own key, recorded
# with its origin (the app and the exact original name). That origin, not the
# vault name, is how the platform finds it again, and only a key the app itself
# created that way can be written through the deprecated secret routes: a key
# the owner merely lets the app borrow is never the app's to replace.

def owned_key_in(keys: list[dict], app_id: int, name: str) -> dict | None:
  """The key ``app_id`` created for its own secret ``name`` and still holds.

  An app that gave up a key the owner shares with others no longer owns it,
  and saving that name again creates a fresh key of its own.
  """
  want = {"app": int(app_id), "name": name}
  return next(
    (k for k in keys if k.get("origin") == want and int(app_id) in k["grants"]["apps"]),
    None,
  )


def resolve_key_in(keys: list[dict], app_id: int, name: str) -> str | None:
  """The granted key an app means by ``name``.

  The app's own key for that name comes first; otherwise a key with exactly
  that vault name, as an app asks for through the handshake.
  """
  owned = owned_key_in(keys, app_id, name)
  if owned is not None:
    return owned["name"]
  exact = next((k for k in keys if k["name"] == name), None)
  if exact is not None and app_id in exact["grants"]["apps"]:
    return exact["name"]
  return None


def granted_key_sync(app_id: int, name: str) -> str | None:
  try:
    keys = vault_call_sync("GET", "/keys")["keys"]
  except (VaultUnavailable, VaultRefused):
    return None
  return resolve_key_in(keys, app_id, name)


def app_may_use(app_id: int, name: str) -> bool:
  """Whether the owner granted ``app_id`` the key its ``name`` refers to."""
  return granted_key_sync(app_id, name) is not None


def reveal_for_app_platform_use(app_id: int, name: str) -> str | None:
  """A granted key's value for platform code acting for an app.

  The platform itself (not the app's code) sometimes needs the value, for
  example to launch a model runtime an app declared. The app's grant still
  decides, and the vault logs the read as ``platform:app-<id>``.
  """
  key = granted_key_sync(app_id, name)
  if key is None:
    return None
  try:
    return vault_call_sync(
      "GET", f"/keys/{key}/value", caller=f"platform:app-{app_id}",
    )["value"]
  except (VaultUnavailable, VaultRefused):
    return None


async def store_app_key(app_id: int, name: str, value: str) -> str:
  """Save ``value`` as ``app_id``'s own secret ``name``; return the vault name.

  The vault finds or creates the key by its origin in one locked step
  (``Vault.save_owned``), keeping the owner's choices for an existing key.
  """
  saved = await vault_call("POST", "/owned", {"app": int(app_id), "name": name, "value": value})
  return saved["name"]


async def release_app_key(app_id: int, name: str) -> dict | None:
  """Withdraw ``app_id``'s own secret ``name``; None when it owns no such key.

  The vault checks ownership and deletes the key, or only withdraws this
  app's grant when the owner shares it with other apps, in one locked step
  (``Vault.release_owned``).
  """
  try:
    return await vault_call("POST", "/owned/release", {"app": int(app_id), "name": name})
  except VaultRefused as exc:
    if exc.status == 404:
      return None
    raise


# -- one-time import of the retired per-app secret store ----------------------

_LEGACY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_IMPORT_LEDGER = ".vault-import.json"


def _legacy_fernet():
  from cryptography.fernet import Fernet
  material = f"mobius-app-secret-v1:{get_settings().secret_key}".encode()
  return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material).digest()))


def _write_ledger(path: Path, ledger: dict) -> None:
  fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
  try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
      json.dump(ledger, handle, indent=1, sort_keys=True)
    os.replace(tmp, path)
  except BaseException:
    try:
      os.unlink(tmp)
    except FileNotFoundError:
      pass
    raise


def _legacy_root() -> Path:
  return Path(get_settings().data_dir) / "app-secrets"


def _read_ledger(root: Path) -> dict:
  try:
    return json.loads((root / _IMPORT_LEDGER).read_text())
  except (FileNotFoundError, ValueError):
    return {}


def _pending_legacy_secrets(root: Path, ledger: dict) -> list[tuple[int, Path]]:
  """Saved values not yet moved, as (app id, file); cheap, no vault needed."""
  if not root.is_dir():
    return []
  pending = []
  for app_dir in sorted(root.iterdir()):
    if not app_dir.is_dir() or not app_dir.name.isdigit():
      continue
    for secret in sorted(app_dir.iterdir()):
      if (secret.is_file() and _LEGACY_NAME_RE.fullmatch(secret.name)
          and f"{app_dir.name}/{secret.name}" not in ledger):
        pending.append((int(app_dir.name), secret))
  return pending


async def import_legacy_app_secrets(live_app_ids: set[int]) -> dict:
  """Move ``/data/app-secrets/<app>/<name>`` values into the vault, once each.

  Each value becomes that app's own key for the name (``store_app_key``), so
  model providers and the deprecated secrets adapter keep finding it. A ledger
  next to the old files records what moved, so a key the owner later deletes
  is never re-imported; unreadable values are retried on the next start. The
  old ciphertext stays in place for one release as a fallback copy.
  """
  root = _legacy_root()
  ledger = _read_ledger(root)
  pending = _pending_legacy_secrets(root, ledger)
  if not pending:
    return {}
  fernet = _legacy_fernet()
  moved = {}
  for app_id, secret in pending:
    if app_id not in live_app_ids:
      continue  # a purged app's id may be reused: never grant it onward
    entry = f"{app_id}/{secret.name}"
    try:
      value = fernet.decrypt(secret.read_bytes()).decode()
      ledger[entry] = await store_app_key(app_id, secret.name, value)
    except Exception as exc:  # unreadable or refused: retried on the next start
      log.warning("key vault: could not import secret %s: %s", entry, type(exc).__name__)
      continue
    moved[entry] = ledger[entry]
    _write_ledger(root / _IMPORT_LEDGER, ledger)
  if moved:
    log.info("key vault: imported %d app secret(s) into the vault", len(moved))
  return moved


def _remove_legacy_secrets(app_id: int) -> None:
  """Delete a purged app's not-yet-imported values from the retired store.

  The importer assigns each pending file to the live app with that id, so a
  file left behind would be granted to an unrelated app that reuses the id.
  Raises if the folder survives, so the purge keeps the id claimed.
  """
  import shutil
  folder = _legacy_root() / str(int(app_id))
  if folder.is_symlink() or folder.is_file():
    folder.unlink()
  elif folder.exists():
    shutil.rmtree(folder)
  if folder.exists() or folder.is_symlink():
    raise OSError(f"legacy secrets for app {app_id} survived removal")


async def forget_app(app_id: int) -> None:
  """Drop a purged app from every grant and request; raise if that fails.

  Its values still waiting in the retired per-app store are deleted first.
  An instance whose vault never stored anything has nothing else to forget,
  so a purge does not depend on a vault that was never used.
  """
  await asyncio.to_thread(_remove_legacy_secrets, app_id)
  folder = vault_dir()
  if not (folder / "keys.json").exists() and not (folder / "requests.json").exists():
    return
  await vault_call("DELETE", f"/apps/{int(app_id)}")


async def _import_when_ready() -> None:
  """Run the one-time legacy import once the vault answers.

  Installations with nothing left to move (the usual case) never wait on the
  vault or touch it.
  """
  root = _legacy_root()
  if not _pending_legacy_secrets(root, _read_ledger(root)):
    return
  for _ in range(60):
    try:
      await vault_call("GET", "/health")
      break
    except VaultUnavailable:
      await asyncio.sleep(1)
  else:
    log.warning("key vault did not start; legacy app secrets stay where they are")
    return
  from app import models
  from app.database import SessionLocal
  with SessionLocal() as db:
    live = {row.id for row in db.query(models.App.id).all()}
  try:
    await import_legacy_app_secrets(live)
  except Exception:
    log.exception("key vault: legacy app-secret import failed; it is retried on the next start")


async def supervise_key_vault() -> None:
  """Keep the vault process running for this server's lifetime.

  The vault exits on its own when this server's pid stops being its parent,
  so a crashed server never leaves an orphan holding the socket. Once per
  server start, values from the retired per-app secret store are imported.
  """
  if not VAULT_PROGRAM.is_file():
    log.warning("key vault program missing at %s; Keys stay unavailable", VAULT_PROGRAM)
    return
  importer = asyncio.create_task(_import_when_ready())
  failures = 0
  log_dir = Path(get_settings().data_dir) / "logs"
  log_dir.mkdir(parents=True, exist_ok=True)
  while True:
    started = asyncio.get_running_loop().time()
    with open(log_dir / "key-vault.log", "ab") as output:
      process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(VAULT_PROGRAM),
        "--data", str(vault_dir()),
        "--parent", str(os.getpid()),
        stdin=asyncio.subprocess.DEVNULL, stdout=output, stderr=output,
        # A minimal environment: the vault needs nothing of the server's.
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
      )
    try:
      code = await process.wait()
    except asyncio.CancelledError:
      importer.cancel()
      await _stop(process)
      raise
    if asyncio.get_running_loop().time() - started >= _HEALTHY_RUN_SECS:
      failures = 0
    delay = _RESTART_BACKOFF_SECS[min(failures, len(_RESTART_BACKOFF_SECS) - 1)]
    failures += 1
    log.warning("key vault exited with %s; restarting in %ss", code, delay)
    await asyncio.sleep(delay)


async def _stop(process: asyncio.subprocess.Process) -> None:
  if process.returncode is not None:
    return
  process.terminate()
  try:
    await asyncio.wait_for(process.wait(), _STOP_GRACE_SECS)
  except asyncio.TimeoutError:
    process.kill()
    await process.wait()
