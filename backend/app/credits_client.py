"""This installation's credits, kept at a paired credits service.

The credits service on mobius.you (``services/credits`` in
mobius-os/mobius.you) is where a Möbius owner's credits and earnings live:
buying credits, spending them on Möbius-hosted models or on another
creator's priced app calls, and keeping or withdrawing what their own apps
earn. This module is this installation's client for it.

The figures are fetched, never stored: the owner of this machine has root
over its database, so a balance kept here could be edited and must not look
like money. Prices live at the service too, under the creator's handle, for
the same reason.

The installation key (``mbc_…``) belongs to the installation, not to an app,
like every account credential Möbius · You holds: it stays in the Möbius
backend, and an app receives only its ordinary app token. It lives in
``<data_dir>/credits-client/credentials.json``, mode 0600, outside every
storage route an app frame can read.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from fastapi import HTTPException

from app.config import get_settings

TIMEOUT_SECONDS = 20.0
KEY_PREFIX = "mbc_"


class CreditsUnavailable(Exception):
  """The credits service refused, or could not be reached."""

  def __init__(self, message: str, *, status: int = 502, code: str = "",
               extra: dict | None = None) -> None:
    super().__init__(message)
    self.message = message
    self.status = status
    self.code = code
    # The service's other refusal fields, e.g. ``price_micro`` on
    # ``price_changed``, passed on so the caller can act on them.
    self.extra = extra or {}


def as_http(error: CreditsUnavailable) -> HTTPException:
  return HTTPException(
    error.status, {**error.extra, "code": error.code, "message": error.message})


# --- The stored key ----------------------------------------------------------


def _credentials_path() -> Path:
  return Path(get_settings().data_dir) / "credits-client" / "credentials.json"


def load_credentials() -> dict | None:
  try:
    stored = json.loads(_credentials_path().read_text())
  except (OSError, ValueError):
    return None
  if not isinstance(stored, dict):
    return None
  url, key = stored.get("operator_url"), stored.get("key")
  if not isinstance(url, str) or not isinstance(key, str) or not url or not key:
    return None
  return {"operator_url": url, "key": key}


def _save_credentials(operator_url: str, key: str) -> None:
  path = _credentials_path()
  path.parent.mkdir(parents=True, exist_ok=True)
  os.chmod(path.parent, 0o700)
  temporary = path.with_suffix(".tmp")
  # Created 0600 rather than chmod-ed after, so the key is never briefly
  # readable by anyone else; replaced atomically so a crash mid-write cannot
  # leave half a credential behind.
  descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
  with os.fdopen(descriptor, "w") as handle:
    handle.write(json.dumps({"operator_url": operator_url, "key": key}))
  os.replace(temporary, path)


def normalise_service_url(value: str) -> str:
  text = (value or "").strip().rstrip("/")
  if not text:
    raise CreditsUnavailable("Enter the address of the credits service.", status=422)
  if "://" not in text:
    text = f"https://{text}"
  parts = urlsplit(text)
  if parts.scheme not in ("http", "https") or not parts.hostname:
    raise CreditsUnavailable("That does not look like a web address.", status=422)
  if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
    # A mistyped scheme would otherwise put a live key on the wire in clear
    # text on every read.
    raise CreditsUnavailable("The credits service must be reached over https.", status=422)
  # The path is kept: mobius.you serves credits under /credits.
  return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def _paired() -> dict:
  credentials = load_credentials()
  if credentials is None:
    raise CreditsUnavailable(
      "This Möbius is not connected to a credits service yet.",
      status=409, code="not_paired")
  return credentials


def _call(method: str, path: str, credentials: dict, payload: dict | None = None) -> dict:
  url = f"{credentials['operator_url']}/api/billing/{path}"
  try:
    response = httpx.request(
      method, url, json=payload, timeout=TIMEOUT_SECONDS,
      headers={"Authorization": f"Bearer {credentials['key']}"},
      follow_redirects=False,
    )
  except httpx.HTTPError as error:
    host = urlsplit(credentials["operator_url"]).hostname
    raise CreditsUnavailable(f"Could not reach {host}.", status=503) from error
  try:
    body = response.json()
  except ValueError:
    body = None
  if response.status_code >= 400:
    # The service refuses with {"detail": {"code", "message", ...}}.
    detail = body.get("detail") if isinstance(body, dict) else None
    fields = detail if isinstance(detail, dict) else {}
    code = str(fields.get("code") or "")
    message = str(fields.get("message") or "")
    extra = {key: value for key, value in fields.items() if key not in ("code", "message")}
    if response.status_code == 401:
      raise CreditsUnavailable(
        "The credits service refused this installation's key. It may have been revoked.",
        status=401, code=code or "unauthorized")
    if response.status_code == 404 and not code:
      raise CreditsUnavailable(
        "That address is not running a credits service.", status=404, code="not_found")
    raise CreditsUnavailable(
      message or f"The credits service answered HTTP {response.status_code}.",
      status=response.status_code, code=code, extra=extra)
  if not isinstance(body, dict):
    raise CreditsUnavailable("The credits service sent something unreadable.")
  return body


# --- Pairing and the one read ---------------------------------------------------


def summary() -> dict:
  """Credits and earnings for this owner, or why they cannot be shown."""
  credentials = load_credentials()
  if credentials is None:
    return {"mode": "unpaired", "service_host": None, "balance": None,
            "earnings": None, "error": None}
  result = {
    "mode": "customer",
    "service_host": urlsplit(credentials["operator_url"]).hostname,
    "balance": None, "earnings": None, "error": None,
  }
  try:
    result["balance"] = _call("GET", "balance", credentials)
    result["earnings"] = _call("GET", "payouts", credentials)
  except CreditsUnavailable as unavailable:
    # A charge-only key may read the balance but not earnings; that is the
    # key's scope, not a failure.
    if unavailable.code != "key_scope":
      result["error"] = {"code": unavailable.code, "message": unavailable.message}
  return result


def pair(operator_url: str, key: str) -> dict:
  """Store a key, but only once the credits service has actually accepted it."""
  key = (key or "").strip()
  if not key.startswith(KEY_PREFIX):
    raise CreditsUnavailable(
      f"A credits key starts with {KEY_PREFIX}. Check you copied all of it.",
      status=422, code="bad_key")
  candidate = {"operator_url": normalise_service_url(operator_url), "key": key}
  _call("GET", "balance", candidate)
  _save_credentials(candidate["operator_url"], key)
  return {"paired": True}


def unpair() -> dict:
  try:
    _credentials_path().unlink()
  except FileNotFoundError:
    pass
  return {"paired": False}


# --- Buying and spending ------------------------------------------------------


def start_topup(amount_cents: int, waiver_accepted: bool,
                return_url: str | None = None) -> dict:
  """A Stripe Checkout page for buying credits, and the purchase's id."""
  started = _call("POST", "topups", _paired(), {
    "amount_cents": int(amount_cents),
    "waiver_accepted": bool(waiver_accepted),
    # Where Stripe sends the buyer back; the service appends the purchase id.
    "return_url": return_url,
  })
  return {"checkout_url": started.get("checkout_url"), "topup_id": started.get("topup_id")}


def read_topup(topup_id: str) -> dict:
  """A purchase's state; reading it confirms a paid checkout with Stripe."""
  return _call("GET", f"topups/{quote(topup_id, safe='')}", _paired())


def transactions(limit: int = 50) -> dict:
  return _call("GET", f"transactions?limit={max(1, min(int(limit), 200))}", _paired())


def fund_models(amount_micro: int, idempotency_key: str) -> dict:
  """Move credits into this owner's Möbius-hosted model balance."""
  return _call("POST", "model-funding", _paired(), {
    "amount_micro": int(amount_micro), "idempotency_key": idempotency_key})


def prices(seller: str, app: str | None = None) -> dict:
  path = f"prices/{quote(seller, safe='')}"
  if app:
    path += f"?app_slug={quote(app, safe='')}"
  return _call("GET", path, _paired())


def publish_price(app: str, action: str, price_micro: int,
                  description: str | None, enabled: bool) -> dict:
  """Set what one action of the owner's app costs other people."""
  path = f"priced-actions/{quote(app, safe='')}/{quote(action, safe='')}"
  return _call("PUT", path, _paired(), {
    "price_micro": int(price_micro), "description": description, "enabled": enabled})


def charge(seller: str, app: str, action: str, idempotency_key: str,
           max_price_micro: int) -> dict:
  """Pay for one call to ``seller``'s priced action from this owner's credits.

  ``max_price_micro`` is the price the caller showed; a higher current price
  refuses with ``price_changed`` instead of charging more.
  """
  return _call("POST", "charges", _paired(), {
    "seller": seller, "app": app, "action": action,
    "idempotency_key": idempotency_key, "max_price_micro": int(max_price_micro)})


# --- Earnings -----------------------------------------------------------------


def start_connect(return_url: str | None = None) -> dict:
  """Stripe's page for connecting the account that receives withdrawals."""
  started = _call("POST", "payouts/connect", _paired(), {"return_url": return_url})
  return {"connect_url": started.get("connect_url")}


def payout_action(action: str) -> dict:
  """``convert`` cleared earnings to credits, ``withdraw`` them, or ``disconnect``."""
  return _call("POST", f"payouts/{action}", _paired(), {})
