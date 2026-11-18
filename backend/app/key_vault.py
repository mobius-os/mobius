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
import contextlib
import dataclasses
import functools
import hashlib
import importlib.util
import json
import logging
import os
import re
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app import settled
from app.config import get_settings


log = logging.getLogger(__name__)

VAULT_PROGRAM = Path(__file__).resolve().parents[1] / "vault" / "keyvault.py"
SOCKET_NAME = "vault.sock"
_RESTART_BACKOFF_SECS = (1, 2, 5, 10, 30, 60)
# A vault that ran this long before exiting counts as healthy again.
_HEALTHY_RUN_SECS = 120
_STOP_GRACE_SECS = 5
# The vault ends every borrow by its own deadline (BORROW_DEADLINE_SECS, from
# when its handler starts) and answers by then, so the server waits that long
# plus this margin: it never gives up on a borrow the vault is still working
# on. The same margin bounds how long a borrow that may still be with the vault
# keeps its app from being purged.
_BORROW_GRACE_SECS = 30


class VaultUnavailable(RuntimeError):
  """The vault process is not answering on its socket.

  ``may_have_reached_vault`` is False only when the request certainly never
  got to the vault (it could not connect). Otherwise the vault may have
  accepted it and may still act on it.
  """

  def __init__(self, message: str, *, may_have_reached_vault: bool = False) -> None:
    super().__init__(message)
    self.may_have_reached_vault = may_have_reached_vault


class VaultTimeout(RuntimeError):
  """The vault took the request but its answer took too long (usually the site).

  The vault may still be acting on it, like a ``VaultUnavailable`` that may
  have reached the vault.
  """


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
  except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
    raise VaultUnavailable("The key vault is not running.") from exc
  except httpx.TimeoutException as exc:
    raise VaultTimeout("The key vault did not answer in time.") from exc
  except (httpx.TransportError, OSError) as exc:
    # It took the call but closed without an answer: running, but failing.
    raise VaultUnavailable("The key vault did not answer.") from exc
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
) -> tuple[httpx.Response, httpx.AsyncClient]:
  """Relay one use request (``/use/<key>/<host>/<path>?<query>``) as is.

  Returns the vault's answer with its body still open, so it streams on as
  the site sends it; the caller closes both the response and the client
  (``close_use``). The vault answers refusals itself (``X-Vault-Denied``), so
  any response is returned unchanged; only an unreachable or silent vault
  raises.

  Once sending has begun, every failure is ``VaultTimeout`` or
  ``VaultUnavailable`` (``may_have_reached_vault`` unless it never
  connected); anything else was raised before a byte left, such as a header
  value httpx cannot encode. ``borrow`` relies on this.
  """
  client = httpx.AsyncClient(
    transport=httpx.AsyncHTTPTransport(uds=str(vault_socket())),
    base_url="http://vault", timeout=httpx.Timeout(_borrow_wait_secs(), connect=10),
  )
  try:
    request = client.build_request(
      method, target, headers=[*headers, ("X-Vault-Caller", caller)], content=body,
    )
    # The caller's raw path must reach the vault byte for byte: its path
    # rules refuse encoded slashes and dot segments that normalising here
    # would hide.
    request.url = request.url.copy_with(raw_path=target.encode("ascii"))
  except BaseException:
    await client.aclose()
    raise
  try:
    return await client.send(request, stream=True), client
  except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
    await client.aclose()
    raise VaultUnavailable("The key vault is not running.") from exc
  except httpx.TimeoutException as exc:
    await client.aclose()
    # The vault itself answers at once; a timeout here is the site being slow.
    raise VaultTimeout("The site took too long to answer through the key vault.") from exc
  except Exception as exc:
    await client.aclose()
    # The request may already be with the vault, which acts on it whether or
    # not anyone is still waiting for the answer.
    raise VaultUnavailable(
      "The key vault did not answer.", may_have_reached_vault=True,
    ) from exc
  except BaseException:
    await client.aclose()
    raise


async def close_use(answer: tuple[httpx.Response, httpx.AsyncClient]) -> None:
  response, client = answer
  try:
    await response.aclose()
  finally:
    await client.aclose()


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
    (k for k in keys if k.get("origin") == want and _grant_of(k, app_id) is not None),
    None,
  )


def _grant_of(key: dict, app_id: int) -> dict | None:
  """``app_id``'s own grant on a listed key: its id and the sites it may reach."""
  return next((g for g in key["grants"]["apps"] if g["id"] == int(app_id)), None)


def resolve_key_in(keys: list[dict], app_id: int, name: str) -> str | None:
  """The granted key an app means by ``name``.

  The app's own key for that name comes first; otherwise a key with exactly
  that vault name, as an app asks for through the handshake.
  """
  owned = owned_key_in(keys, app_id, name)
  if owned is not None:
    return owned["name"]
  exact = next((k for k in keys if k["name"] == name), None)
  if exact is not None and _grant_of(exact, app_id) is not None:
    return exact["name"]
  return None


@functools.cache
def _vault_rules():
  """The vault program's own destination rules (it imports only the stdlib)."""
  spec = importlib.util.spec_from_file_location("mobius_vault_rules", VAULT_PROGRAM)
  module = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = module  # its dataclasses resolve their own module
  spec.loader.exec_module(module)
  return module


@dataclasses.dataclass(frozen=True)
class GrantWidening:
  """What a save's grants allow that the key's current grants do not."""

  apps: tuple[int, ...] = ()  # apps that may newly use the key, reach more or are unpaused
  agents: bool = False        # the Agents switch turned on
  sites: bool = False         # the owner's and agents' own sites reach more


def sites_outside(sites: list[dict], requested: list[dict]) -> list[dict]:
  """The part of ``requested`` that ``sites`` do not allow, by the vault's own rules."""
  return _vault_rules().sites_outside(sites, requested)


def stored_slot(inject):
  """A slot as the vault stores it, so one written differently but stored the
  same (spacing around its name, an absent prefix) compares equal; one the
  vault would refuse stays as given, for the vault to refuse on save."""
  rules = _vault_rules()
  try:
    return rules.validate_inject(inject)
  except rules.VaultError:
    return inject


def grant_widening(before: dict | None, grants) -> GrantWidening:
  """Compare a save's ``grants`` with ``before``, the key's current grants
  (None for a new key), by what their sites allow under the vault's own
  rules, so removing, narrowing, reordering or re-casing sites never counts.

  ``grants`` None is an update that keeps the current grants: no change.
  Malformed grants count as no change too; the vault refuses them. A new key
  widens by the apps it names and the sites it starts with: an agent sets up
  a key without sites, and asks for them (the ``request_key`` control).
  """
  rules = _vault_rules()
  if grants is None:
    return GrantWidening()
  try:
    after = rules.validate_grants(grants)
  except rules.VaultError:
    return GrantWidening()
  return GrantWidening(
    apps=tuple(rules.widened_apps(before, after)),
    agents=before is not None and after["agents"] and not before.get("agents"),
    sites=bool(rules.sites_outside(before["sites"] if before else [], after["sites"])),
  )


# Where the model relay (``routes/model_relay.py``, ``_PROTOCOLS``) sends an
# app's provider key, under the provider's declared ``base_url``, for each
# wire protocol; the first is the protocol's main endpoint.
PROVIDER_ENDPOINTS: dict[str, tuple[str, ...]] = {
  "responses": ("/responses",),
  "anthropic_messages": ("/v1/messages", "/v1/messages/count_tokens"),
}


def provider_site(base_url: str, protocol: str = "responses") -> dict | None:
  """The site an app's model provider sends its key to, or None.

  It allows ``POST`` on each endpoint the relay calls for ``protocol``
  (``PROVIDER_ENDPOINTS``); a base URL that is not plain HTTPS on port 443
  cannot be a vault site.
  """
  endpoints = PROVIDER_ENDPOINTS.get(protocol)
  url = urllib.parse.urlsplit(str(base_url or ""))
  if (
    not endpoints
    or url.scheme != "https" or url.port not in (None, 443) or not url.hostname
    # Credentials, a query or a fragment in the address are never part of a site.
    or url.username is not None or url.password is not None or url.query or url.fragment
    # A literal address, never a site pattern: the manifest is the app's word.
    or "*" in url.hostname or any(ch in url.path for ch in "*?[]")
  ):
    return None
  paths = [url.path.rstrip("/") + endpoint for endpoint in endpoints]
  rules = _vault_rules()
  try:
    # The same path rules a use meets: no dot segments or encoded slashes that
    # would match a narrow site rule but reach another endpoint.
    for path in paths:
      rules.normalized_request_path(path)
  except rules.VaultError:
    return None
  return {"host": url.hostname, "methods": ["POST"], "paths": paths}


def model_provider_key_in(
  keys: list[dict], app_id: int, name: str, base_url: str,
  protocol: str = "responses", endpoint: str | None = None,
) -> str | None:
  """The key an app's declared model provider may be sent with to ``endpoint``.

  The platform sends the value to the provider's ``base_url`` plus the
  endpoint the relay calls (the protocol's main one by default), for the
  app, so the app's own grant on the key, whether the app created it or the
  owner lets it borrow one, must allow that destination by the same site rule
  a use through the vault meets. An app's own key starts with the protocol's
  site (``store_app_key``). A key with line breaks, or one the owner paused
  for this app, is never sent.
  """
  endpoints = PROVIDER_ENDPOINTS.get(protocol, ())
  endpoint = endpoint if endpoint is not None else next(iter(endpoints), None)
  key = resolve_key_in(keys, app_id, name)
  site = provider_site(base_url, protocol)
  if key is None or site is None or endpoint not in endpoints:
    return None
  record = next(k for k in keys if k["name"] == key)
  grant = _grant_of(record, app_id)
  if record.get("multiline") or grant.get("paused"):
    return None
  allowed = _vault_rules().site_allows(
    grant["sites"], site["host"], "POST",
    site["paths"][endpoints.index(endpoint)],
  ) is None
  return key if allowed else None


def model_provider_key_sync(
  app_id: int, name: str, base_url: str,
  protocol: str = "responses", endpoint: str | None = None,
) -> str | None:
  try:
    keys = vault_call_sync("GET", "/keys")["keys"]
  except (VaultUnavailable, VaultRefused):
    return None
  return model_provider_key_in(keys, app_id, name, base_url, protocol, endpoint)


def reveal_model_provider_key(
  app_id: int, name: str, base_url: str,
  protocol: str = "responses", endpoint: str | None = None,
) -> str | None:
  """The value an app's declared model runtime sends to ``endpoint`` under ``base_url``.

  The platform itself (not the app's code) reads it; the vault logs the read
  as ``platform:app-<id>``.
  """
  key = model_provider_key_sync(app_id, name, base_url, protocol, endpoint)
  if key is None:
    return None
  try:
    return vault_call_sync(
      "GET", f"/keys/{key}/value", caller=f"platform:app-{app_id}",
    )["value"]
  except (VaultUnavailable, VaultRefused):
    return None


class AppGone(LookupError):
  """The app was removed or reinstalled while a vault change was pending."""


@contextlib.asynccontextmanager
async def for_live_app(app_id: int, expected_nonce: str | None = None):
  """Hold ``app_id``'s storage lock and confirm the app is still installed.

  Anything that grants a key to an app, or queues a request an approval would
  turn into a grant, runs inside this block. App removal holds the same lock
  while ``forget_app`` withdraws the app from the vault, so a change that was
  authorized before the removal can never land after it and be inherited by
  an unrelated app that reuses the id. ``expected_nonce`` additionally
  refuses a reinstall that happened since the caller was authorized.
  """
  from app import fs_locks, models
  from app.database import SessionLocal
  async with fs_locks.app_storage_lock(int(app_id)):
    with SessionLocal() as db:
      app = db.query(models.App).filter(
        models.App.id == int(app_id), models.App.deleted_at.is_(None),
      ).first()
      live = app is not None and (expected_nonce is None or app.token_nonce == expected_nonce)
    if not live:
      raise AppGone(app_id)
    yield


# Borrows each app has on their way through the vault, by app id.
_borrows_in_flight: dict[int, int] = {}


def _borrow_wait_secs() -> int:
  """How long the vault can still act on a borrow it has taken, and then some."""
  return _vault_rules().BORROW_DEADLINE_SECS + _BORROW_GRACE_SECS


def _release_borrow(app_id: int) -> None:
  left = _borrows_in_flight.pop(app_id, 0) - 1
  if left > 0:
    _borrows_in_flight[app_id] = left


def _may_still_be_with_vault(error: BaseException) -> bool:
  """Whether a relay that failed with ``error`` may have left its request
  with the vault: only a failure after sending began (``vault_use``) does."""
  if isinstance(error, VaultTimeout):
    return True
  return isinstance(error, VaultUnavailable) and error.may_have_reached_vault


async def borrow(
  app_id: int, installation: str | None, method: str, target: str, *,
  caller: str, headers: list[tuple[str, str]], body: bytes | None,
) -> tuple[httpx.Response, httpx.AsyncClient]:
  """Relay an app's use request, keeping the app from being forgotten until
  the vault can no longer act on it.

  The check runs under ``app_id``'s lock: ``installation`` (the token nonce
  the app's token names) must still be the app. The vault knows an app only
  by its reusable id, so the purge must not free the id while the vault could
  still authorize this request, or it could be sent with the grant of an
  unrelated app that reuses the id. The lock is held only for the check, never
  across the relay, so borrows run side by side and storage writes do not
  wait on a third party; ``forget_app`` refuses while any borrow is out.

  A borrow is out until the vault has answered. A cancelled caller does not
  end it: the relay is settled first. When the relay fails after the request
  may have reached the vault, the vault could still act on it, so the borrow
  stays out until the vault's own deadline for it has surely passed
  (``_borrow_wait_secs`` from now): the vault authorizes a borrow only when
  its handler starts and ends every borrow by that deadline. The vault process
  also ends with the server, so a restart ends any such borrow sooner. Any
  failure before the request was sent releases the borrow at once.
  """
  if not installation:
    raise AppGone(app_id)
  app_id = int(app_id)
  async with for_live_app(app_id, installation):
    _borrows_in_flight[app_id] = _borrows_in_flight.get(app_id, 0) + 1
  # Stays out only when the relay was itself cancelled or may still be with
  # the vault.
  released = False

  def after_cancel(task: asyncio.Future) -> None:
    nonlocal released
    error = None if task.cancelled() else task.exception()
    released = not task.cancelled() and not (error is not None and _may_still_be_with_vault(error))
    if error is not None:
      settled.log_abandoned_failure(task, "Key borrow")
    elif not task.cancelled():
      # Nobody will read the answer: close its stream.
      asyncio.ensure_future(close_use(task.result()))

  try:
    answer = await settled.settle(
      vault_use(method, target, caller=caller, headers=headers, body=body),
      what="Key borrow", abandoned=after_cancel,
    )
  except asyncio.CancelledError:
    # ``after_cancel`` has judged the relay that ran on without its caller.
    raise
  except BaseException as exc:
    released = not _may_still_be_with_vault(exc)
    raise
  else:
    # The vault has authorized and answered; what remains is the site's body
    # streaming through, which the caller reads and closes (``close_use``).
    released = True
    return answer
  finally:
    if released:
      _release_borrow(app_id)
    else:
      wait = _borrow_wait_secs()
      log.warning(
        "app %s: a key borrow may still be with the vault; "
        "its purge waits %s seconds for the vault's deadline", app_id, wait,
      )
      asyncio.get_running_loop().call_later(wait, _release_borrow, app_id)


class BorrowInFlight(RuntimeError):
  """A purge must wait: a key borrow the app started has no answer yet.

  A RuntimeError, so the TTL sweep leaves the tombstone for its next pass;
  the owner's permanent delete answers 409 and asks them to try again.
  """


def refuse_purge_while_borrowing(app_id: int) -> None:
  """Raise while a borrow ``app_id`` started has not had its answer yet.

  The purge holds the app's lock, so no new borrow can start; it checks this
  before removing anything, and ``forget_app`` checks it again.
  """
  if _borrows_in_flight.get(int(app_id)):
    raise BorrowInFlight(f"app {app_id} has a key borrow in flight; purge it later")


@contextlib.asynccontextmanager
async def for_granted_apps(key_name: str, app_ids: list[int], installations: dict):
  """Hold the lock of every app a save of ``key_name`` grants, and confirm
  each app it newly grants is the installation the owner chose.

  An app id alone cannot name an app: one removed after the owner's page
  loaded can be reused by an unrelated app, and the key's revision does not
  change when the removed app held no grant on it. So a new grant names its
  app's installation (``installations``: id to the app's non-secret
  ``storage_generation``). A grant the key already holds stays valid: app
  removal withdraws it under the same lock before the id is freed.
  """
  from app import fs_locks, models
  from app.app_compile_contract import app_storage_generation
  from app.database import SessionLocal
  async with contextlib.AsyncExitStack() as locks:
    wanted = sorted(set(app_ids))
    for app_id in wanted:
      await locks.enter_async_context(fs_locks.app_storage_lock(app_id))
    if wanted:
      key = next((k for k in (await vault_call("GET", "/keys"))["keys"] if k["name"] == key_name), None)
      added = set(wanted) - {g["id"] for g in (key["grants"]["apps"] if key else [])}
      with SessionLocal() as db:
        live = {
          app.id: app_storage_generation(app.token_nonce)
          for app in db.query(models.App).filter(
            models.App.id.in_(added), models.App.deleted_at.is_(None),
          )
        }
      for app_id in sorted(added):
        if app_id not in live or installations.get(str(app_id)) != live[app_id]:
          raise AppGone(app_id)
    yield


def _provider_declaration(app_id: int) -> dict | None:
  """The installed app's accepted model-provider declaration, when it names a key."""
  from app import models
  from app.database import SessionLocal
  with SessionLocal() as db:
    contract = db.query(models.App.capability_contract).filter(
      models.App.id == int(app_id), models.App.deleted_at.is_(None),
    ).scalar()
  provider = contract.get("model_provider") if isinstance(contract, dict) else None
  if isinstance(provider, dict) and isinstance(provider.get("secret_name"), str):
    return provider
  return None


def _own_provider_sites(provider: dict | None, name: str) -> list[dict]:
  """The sites a new own key ``name`` starts with: its provider's, if any."""
  if provider is None or provider.get("secret_name") != name:
    return []
  site = provider_site(provider.get("base_url"), provider.get("protocol", "responses"))
  return [site] if site else []


async def store_app_key(app_id: int, name: str, value: str) -> str:
  """Save ``value`` as ``app_id``'s own secret ``name``; return the vault name.

  The vault finds or creates the key by its origin in one locked step
  (``Vault.save_owned``), keeping the owner's choices for an existing key. A
  new key for the app's model provider starts allowed on the provider's site
  only, which the owner sees and can change in Settings › Keys.
  """
  provider = await asyncio.to_thread(_provider_declaration, app_id)
  saved = await vault_call("POST", "/owned", {
    "app": int(app_id), "name": name, "value": value,
    "sites": _own_provider_sites(provider, name),
  })
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


def _record_in_ledger(root: Path, entry: str, outcome) -> bool:
  """Settle one legacy file for good unless it already is; whether this did.

  ``outcome`` is the vault name it moved to, ``{"superseded_by": <name>}``
  when the app had saved the secret again before the import reached it, or
  ``{"deleted_by_app": true}``.

  The importer and the deprecated DELETE route both add entries, on the event
  loop: this re-read and write has no await in between, so neither writes
  back a copy missing the other's. Which of them settles a file first is
  decided under that app's lock (``for_live_app``), where both re-check it.
  """
  ledger = _read_ledger(root)
  if entry in ledger:
    return False
  ledger[entry] = outcome
  _write_ledger(root / _IMPORT_LEDGER, ledger)
  return True


def withdraw_legacy_secret(app_id: int, name: str) -> bool:
  """Keep an app's not-yet-imported secret ``name``, which it just deleted,
  from ever being imported; whether there was one.

  The caller holds the app's lock, as the importer does for each file.
  """
  if not _LEGACY_NAME_RE.fullmatch(name):
    return False
  root = _legacy_root()
  if not (root / str(int(app_id)) / name).is_file():
    return False
  return _record_in_ledger(root, f"{int(app_id)}/{name}", {"deleted_by_app": True})


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

  The app may use the deprecated routes before the import reaches its file:
  a value it saved since then is newer, so the old one is recorded as
  superseded rather than written over it, and one it deleted
  (``withdraw_legacy_secret``) is never brought back.
  """
  root = _legacy_root()
  pending = _pending_legacy_secrets(root, _read_ledger(root))
  if not pending:
    return {}
  fernet = _legacy_fernet()
  moved = {}
  for app_id, secret in pending:
    entry = f"{app_id}/{secret.name}"
    if app_id not in live_app_ids:
      # No app row at all: the app was purged (or a restore dropped it), and
      # its id may be reused by an unrelated app, which must never inherit
      # this value. A removed app still awaiting recovery keeps its row and
      # is retried. Recorded so a later app with this id never imports it.
      _record_in_ledger(root, entry, {"skipped": "no app holds this id"})
      continue
    try:
      # A purge removes the file under the same lock, so a file still present
      # here belongs to the app that is live now. The deprecated routes save
      # and delete under it too.
      async with for_live_app(app_id):
        if not secret.is_file() or entry in _read_ledger(root):
          continue
        newer = owned_key_in((await vault_call("GET", "/keys"))["keys"], app_id, secret.name)
        if newer is not None:
          _record_in_ledger(root, entry, {"superseded_by": newer["name"]})
          continue
        value = fernet.decrypt(secret.read_bytes()).decode()
        name = await store_app_key(app_id, secret.name, value)
        _record_in_ledger(root, entry, name)
    except AppGone:
      continue
    except Exception as exc:  # unreadable or refused: retried on the next start
      log.warning("key vault: could not import secret %s: %s", entry, type(exc).__name__)
      continue
    moved[entry] = name
  if moved:
    log.info("key vault: imported %d app secret(s) into the vault", len(moved))
  return moved


def _remove_legacy_secrets(app_id: int) -> None:
  """Delete an app's files from the retired store, imported or not.

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


def _vault_ever_used() -> bool:
  folder = vault_dir()
  return (folder / "keys.json").exists() or (folder / "requests.json").exists()


async def erase_app_keys(app_id: int) -> None:
  """Erase an app's own secrets when its data is wiped; raise if that fails.

  Its files in the retired per-app store go, and each vault key the app
  created for itself is deleted (or kept only for other apps the owner shared
  it with). Keys it borrows stay granted: the owner's choices are not app data.
  """
  await asyncio.to_thread(_remove_legacy_secrets, app_id)
  if _vault_ever_used():
    await vault_call("DELETE", f"/apps/{int(app_id)}/owned")


async def forget_app(app_id: int) -> None:
  """Erase a purged app's own keys and drop it from every grant and request.

  Raises if that fails. Its values still waiting in the retired per-app store
  are deleted first. An instance whose vault never stored anything has
  nothing else to forget, so a purge does not depend on a vault that was
  never used.

  The caller holds the app's lock. A borrow the app started before then
  (``borrow``) may not have reached the vault yet; the purge is
  refused until it has its answer, and the next sweep retries it.
  """
  refuse_purge_while_borrowing(app_id)
  await asyncio.to_thread(_remove_legacy_secrets, app_id)
  if _vault_ever_used():
    await vault_call("DELETE", f"/apps/{int(app_id)}", caller="platform:app-purge")


async def forget_chat(chat_id: str, deleted_at: datetime) -> None:
  """Withdraw the waiting key requests a deleted chat's agent made up to its
  deletion at ``deleted_at`` (naive UTC, as the database stores it).

  Deleting the chat settled its card, so nothing could answer them there. A
  request the chat asks for after it is recovered is newer than the deletion
  and stays, however late this reaches the vault, so recovery needs no call.
  Best effort, and it waits at most one vault timeout: a request it leaves
  behind still shows in Settings › Keys, where the owner answers or removes it.
  """
  if not _vault_ever_used():
    return
  before = deleted_at.replace(tzinfo=timezone.utc).isoformat()
  path = f"/chats/{urllib.parse.quote(chat_id, safe='')}/requests?before={urllib.parse.quote(before, safe='')}"
  try:
    await vault_call("DELETE", path, caller="platform:chat-delete")
  except (VaultUnavailable, VaultTimeout, VaultRefused):
    log.warning("Could not withdraw the key requests of deleted chat %s", chat_id, exc_info=True)


async def forget_chats(chat_ids, deleted_at: datetime) -> None:
  """``forget_chat`` for chats deleted together, at once rather than in turn."""
  await asyncio.gather(*(forget_chat(str(chat_id), deleted_at) for chat_id in chat_ids))


async def withdraw_request(request_id: str, ticket: str | None) -> None:
  """Withdraw one agent request whose card could not be saved, while it is
  still the exact ask (``ticket``) that was made. Best effort: if the vault
  can't be reached, the request shows in Settings › Keys instead."""
  path = f"/requests/{urllib.parse.quote(request_id, safe='')}"
  if ticket:
    path += f"?ticket={urllib.parse.quote(ticket, safe='')}"
  try:
    await vault_call("DELETE", path, caller="platform:key-request")
  except (VaultUnavailable, VaultTimeout, VaultRefused):
    log.warning("Could not withdraw key request %s whose card was not saved", request_id, exc_info=True)


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
    moved = await import_legacy_app_secrets(live)
  except Exception:
    log.exception("key vault: legacy app-secret import failed; it is retried on the next start")
    return
  try:
    await _tell_owner_which_imported_keys_need_sites(moved)
  except Exception:
    log.exception("key vault: could not tell the owner which imported keys need sites")


async def _tell_owner_which_imported_keys_need_sites(moved: dict) -> None:
  """Name the apps whose keys this import (``moved``) left without sites.

  An imported key starts with no sites, except an app's model-provider key
  on its provider's, so its app cannot use it until the owner adds them in
  Settings › Keys. The ledger settles each old value once, so a later start
  never imports, or names, the same key again.
  """
  if not moved:
    return
  keys = {k["name"]: k for k in (await vault_call("GET", "/keys"))["keys"]}
  app_ids = set()
  for entry, name in moved.items():
    app_id = int(entry.split("/", 1)[0])
    grant = _grant_of(keys[name], app_id) if name in keys else None
    if grant is not None and not grant["sites"]:
      app_ids.add(app_id)
  if not app_ids:
    return
  from app import models, push
  from app.database import SessionLocal
  with SessionLocal() as db:
    owner = db.query(models.Owner).first()
    rows = db.query(models.App.id, models.App.name).filter(models.App.id.in_(app_ids)).all()
    counts = {}
    for _, name in rows:
      counts[name] = counts.get(name, 0) + 1
    # Two apps may share a name; the owner must still tell them apart.
    names = sorted(name if counts[name] == 1 else f"{name} (#{app_id})" for app_id, name in rows)
    if owner is None or not names:
      return
    shown = names if len(names) <= 5 else [*names[:4], f"{len(names) - 4} more apps"]
    listed = shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} and {shown[-1]}"
    await push.notify_owner_async(
      db, owner.id,
      title="Saved app keys need sites",
      body=(f"Keys saved by {listed} moved into Settings › Keys without sites, "
            "so those apps cannot use them until you add the sites each key is for."),
      source_type="system",
      target="/shell/?settings=keys",
      tag="key-import-needs-sites",
    )


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
        # A minimal environment: only the trust store settings TLS needs.
        env={
          "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
          **{k: os.environ[k] for k in ("SSL_CERT_FILE", "SSL_CERT_DIR") if k in os.environ},
        },
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
