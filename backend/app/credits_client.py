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
like every account credential this Möbius holds: it stays in the Möbius
backend, and an app receives only its ordinary app token. It lives in
``<data_dir>/credits-client/credentials.json``, mode 0600, a folder the file
API (``routes/fs.py``) refuses to every caller, backups keep with the other
secrets, and the ``/data`` safety repository ignores. The service address is
sealed together with the key under the installation secret, so whoever can
rewrite the file still cannot point the key at another address: a file that
does not open under this installation's secret reads as unpaired. The
``spend_credits`` gate and an app's
``max_price_micro`` hold for honest app code; an app service runs as an
ordinary process on this machine, so they are not a boundary against a
hostile one, which is why that grant is critical at install.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
import httpx
from fastapi import HTTPException

from app.config import get_settings

TIMEOUT_SECONDS = 20.0
KEY_PREFIX = "mbc_"
# The credits service's account handles, priced-action slugs and UUID ids.
_HANDLE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
_SLUG = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")
_TOPUP_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CHARGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


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


def _fernet() -> Fernet:
  material = f"mobius-credits-sealed-pairing-v1:{get_settings().secret_key}".encode()
  return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material).digest()))


def load_credentials() -> dict | None:
  """The paired service, its key, and an id for this pairing; None if unpaired.

  Only the sealed pairing is trusted. The address is read from inside it, never
  from beside it, so a file whose address was swapped, or that does not open
  under this installation's secret, never sends the key anywhere.
  """
  try:
    stored = json.loads(_credentials_path().read_text())
  except (OSError, ValueError):
    return None
  sealed = stored.get("sealed") if isinstance(stored, dict) else None
  if not isinstance(sealed, str):
    return None
  try:
    pairing = json.loads(_fernet().decrypt(sealed.encode()))
  except (InvalidToken, ValueError):
    # Tampered with, or sealed under another installation secret: connect again.
    return None
  if not isinstance(pairing, dict):
    return None
  url, key = pairing.get("operator_url"), pairing.get("key")
  if not isinstance(url, str) or not url or not isinstance(key, str) \
      or not key.startswith(KEY_PREFIX):
    return None
  return _credentials(url, key)


def _credentials(operator_url: str, key: str) -> dict:
  """A paired service, with ``pairing``: an id that changes when the service
  or its key does, so what was read under one pairing (the amounts it sells,
  its currency) is never used with another. It is a digest, not the key."""
  digest = hashlib.sha256(
    json.dumps(["mobius-credits-pairing-v1", operator_url, key]).encode()).hexdigest()
  return {"operator_url": operator_url, "key": key, "pairing": digest[:16]}


def _save_credentials(operator_url: str, key: str) -> None:
  path = _credentials_path()
  path.parent.mkdir(parents=True, exist_ok=True)
  os.chmod(path.parent, 0o700)
  # mkstemp creates a unique 0600 file, so the key is never briefly readable
  # by anyone else and two pairings at once never write into one file; the
  # replace is atomic, so a crash mid-write cannot leave half a credential.
  descriptor, temporary = tempfile.mkstemp(
    dir=path.parent, prefix=".credentials-", suffix=".tmp")
  try:
    with os.fdopen(descriptor, "w") as handle:
      sealed = _fernet().encrypt(
        json.dumps({"operator_url": operator_url, "key": key}).encode())
      handle.write(json.dumps({"sealed": sealed.decode()}))
    os.replace(temporary, path)
  except BaseException:
    Path(temporary).unlink(missing_ok=True)
    raise


def normalise_service_url(value: str) -> str:
  text = (value or "").strip().rstrip("/")
  if not text:
    raise CreditsUnavailable("Enter the address of the credits service.", status=422)
  if "://" not in text:
    text = f"https://{text}"
  try:
    parts = urlsplit(text)
    _ = parts.port  # urlsplit checks the port only when it is read
  except ValueError:  # a port that is not a number, or an unclosed bracket
    parts = None
  if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
    raise CreditsUnavailable("That does not look like a web address.", status=422)
  if parts.username is not None or parts.password is not None:
    # The key is the only credential this address is used with; a name or
    # password in it would be sent to the service with every read.
    raise CreditsUnavailable(
      "Leave the user name and password out of the credits service address.",
      status=422)
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


def _charge_outcome_unknown() -> CreditsUnavailable:
  # A server/transport failure or unreadable receipt can follow a committed
  # charge. Never present it as a refusal or retry it with a different key.
  return CreditsUnavailable(
    "The charge outcome is unknown. Retry with the same idempotency key.",
    status=503, code="charge_outcome_unknown")


def _call(method: str, path: str, credentials: dict, payload: dict | None = None) -> dict:
  url = f"{credentials['operator_url']}/api/billing/{path}"
  try:
    response = httpx.request(
      method, url, json=payload, timeout=TIMEOUT_SECONDS,
      headers={"Authorization": f"Bearer {credentials['key']}"},
      follow_redirects=False,
    )
  except httpx.InvalidURL as error:
    raise CreditsUnavailable("That does not look like a web address.", status=422) from error
  except httpx.HTTPError as error:
    if path == "charges":
      raise _charge_outcome_unknown() from error
    host = urlsplit(credentials["operator_url"]).hostname
    raise CreditsUnavailable(f"Could not reach {host}.", status=503) from error
  try:
    body = response.json()
  except ValueError:
    body = None
  if path == "charges" and response.status_code >= 500:
    raise _charge_outcome_unknown()
  if response.status_code >= 400:
    # The service refuses with {"detail": {"code", "message", ...}}.
    detail = body.get("detail") if isinstance(body, dict) else None
    fields = detail if isinstance(detail, dict) else {}
    code = str(fields.get("code") or "")
    message = str(fields.get("message") or "")
    extra = {key: value for key, value in fields.items() if key not in ("code", "message")}
    # Neither answer is about this Möbius's own request, so neither keeps its
    # status: a 401 here would read as the app's own sign-in expiring.
    if response.status_code == 401:
      raise CreditsUnavailable(
        "The credits service refused this installation's key. It may have been revoked.",
        status=409, code=code or "key_refused")
    if response.status_code == 404 and not code:
      raise CreditsUnavailable(
        "That address is not running a credits service.", status=502, code="not_found")
    raise CreditsUnavailable(
      message or f"The credits service answered HTTP {response.status_code}.",
      status=response.status_code, code=code, extra=extra)
  if not isinstance(body, dict):
    if path == "charges":
      raise _charge_outcome_unknown()
    raise CreditsUnavailable("The credits service sent something unreadable.")
  return body


# --- Pairing and the one read ---------------------------------------------------


def summary() -> dict:
  """Credits and earnings for this owner, or why they cannot be shown."""
  credentials = load_credentials()
  if credentials is None:
    return {"mode": "unpaired", "service_host": None, "pairing": None,
            "balance": None, "earnings": None, "error": None}
  result = {
    "mode": "customer",
    "service_host": urlsplit(credentials["operator_url"]).hostname,
    "pairing": credentials["pairing"],
    "balance": None, "earnings": None, "error": None,
  }
  # Both reads at once, so a slow service costs one timeout, not two.
  with ThreadPoolExecutor(max_workers=2) as pool:
    reads = {name: pool.submit(_call, "GET", name, credentials)
             for name in ("balance", "payouts")}
  for name, field in (("balance", "balance"), ("payouts", "earnings")):
    try:
      result[field] = reads[name].result()
    except CreditsUnavailable as unavailable:
      # A charge-only key may read the balance but not earnings; that is the
      # key's scope, not a failure.
      if unavailable.code != "key_scope" and result["error"] is None:
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


def _stripe_page(value: object, host: str) -> str:
  """A page the service sends the owner to, passed on only if it is Stripe's.

  The browser opens it in a window that starts on this Möbius's own origin, so
  a ``javascript:`` or look-alike address from a compromised service would act
  as the owner. Stripe Checkout and Connect onboarding are plain https pages on
  their own hosts; anything else is refused rather than opened. The address is
  rebuilt from what was checked, so the browser gets exactly that.
  """
  try:
    parts = urlsplit(value if isinstance(value, str) else "")
    stripes = (parts.scheme == "https" and parts.hostname == host
               and parts.username is None and parts.password is None and parts.port is None)
  except ValueError:  # an unreadable port or bracketed host
    stripes = False
  if not stripes:
    raise CreditsUnavailable(
      "The credits service sent a payment page that is not Stripe's.",
      code="unexpected_page")
  return parts.geturl()


def start_topup(amount_cents: int, waiver_accepted: bool,
                pairing: str | None = None) -> dict:
  """A Stripe Checkout page for buying credits, and the purchase's id.

  ``pairing`` is the pairing the caller read the purchase terms under. When it
  is given and this Möbius has since been paired again, nothing is bought:
  the amount and waiver the owner saw were another service's terms.
  """
  credentials = _paired()
  if pairing is not None and pairing != credentials["pairing"]:
    raise CreditsUnavailable(
      "This Möbius was connected to a different credits service after these "
      "amounts were shown. Check them and try again.",
      status=409, code="pairing_changed")
  started = _call("POST", "topups", credentials, {
    "amount_cents": int(amount_cents),
    "waiver_accepted": bool(waiver_accepted),
  })
  return {"checkout_url": _stripe_page(started.get("checkout_url"), "checkout.stripe.com"),
          "topup_id": started.get("topup_id")}


def _path_piece(value: str, pattern: re.Pattern[str]) -> str:
  # Quoting alone leaves . and .. intact; httpx collapses those path segments,
  # letting a public price read reach another endpoint with the owner's key.
  if not pattern.fullmatch(value):
    raise CreditsUnavailable(
      "That credits handle, slug or id is not valid.", status=422, code="invalid_name")
  return value


def read_topup(topup_id: str) -> dict:
  """A purchase's state; reading it confirms a paid checkout with Stripe."""
  return _call("GET", f"topups/{_path_piece(topup_id, _TOPUP_ID)}", _paired())


def purchase_terms() -> dict:
  """The amounts the credits service sells, its currency, and the waiver rule,
  with the ``pairing`` they were read under."""
  credentials = _paired()
  return {**_call("GET", "purchase-terms", credentials), "pairing": credentials["pairing"]}


def transactions(limit: int = 50) -> dict:
  return _call("GET", f"transactions?limit={max(1, min(int(limit), 200))}", _paired())


def fund_models(amount_micro: int, idempotency_key: str) -> dict:
  """Move credits into this owner's Möbius-hosted model balance."""
  return _call("POST", "model-funding", _paired(), {
    "amount_micro": int(amount_micro), "idempotency_key": idempotency_key})


def prices(seller: str, app: str | None = None) -> dict:
  path = f"prices/{_path_piece(seller, _HANDLE)}"
  if app is not None:
    path += f"?app_slug={_path_piece(app, _SLUG)}"
  return _call("GET", path, _paired())


def publish_price(app: str, action: str, price_micro: int,
                  description: str | None, enabled: bool) -> dict:
  """Set what one action of the owner's app costs other people."""
  path = f"priced-actions/{_path_piece(app, _SLUG)}/{_path_piece(action, _SLUG)}"
  return _call("PUT", path, _paired(), {
    "price_micro": int(price_micro), "description": description, "enabled": enabled})


def charge(seller: str, app: str, action: str, idempotency_key: str,
           max_price_micro: int, *, payer: str, buyer_app: str) -> dict:
  """Pay for one call to ``seller``'s priced action from this owner's credits.

  ``max_price_micro`` is the price the caller showed; a higher current price
  refuses with ``price_changed`` instead of charging more. ``payer`` is the
  paying app's stable identity, which scopes its idempotency key. ``buyer_app``
  names that app in the owner's history, so it can say which one spent.
  """
  return _call("POST", "charges", _paired(), {
    "seller": seller, "app": app, "action": action,
    "idempotency_key": app_scoped_key(payer, idempotency_key),
    "max_price_micro": int(max_price_micro), "buyer_app": buyer_app})


def charge_receipt(charge_id: str) -> dict:
  """Who paid whom, for which call, and how much: a claimed charge, checked.

  A marketplace app unlocks an item for a buyer on another Möbius only after
  this confirms the charge id that buyer handed over. Reading a receipt moves
  no money, so its failures keep the service's own refusal.
  """
  return _call("GET", f"charges/{_path_piece(charge_id, _CHARGE_ID)}", _paired())


def app_scoped_key(payer: str, idempotency_key: str) -> str:
  """The idempotency key the credits service sees for one app's charge.

  The service replays a charge by buyer account and key, and every app here
  pays from the same account. So an app's own key is scoped to that app before
  it leaves: two apps that pick the same key make two charges (or a conflict)
  of their own, never a replay of each other's, while one app's retry still
  replays its own. A digest keeps any key an app may send within the service's
  8 to 200 characters.
  """
  digest = hashlib.sha256(json.dumps([payer, idempotency_key]).encode()).hexdigest()
  return f"app-{digest}"


# --- Earnings -----------------------------------------------------------------


def start_connect() -> dict:
  """Stripe's page for connecting the account that receives withdrawals."""
  started = _call("POST", "payouts/connect", _paired(), {})
  return {"connect_url": _stripe_page(started.get("connect_url"), "connect.stripe.com")}


def payout_action(action: str) -> dict:
  """``convert`` cleared earnings to credits, ``withdraw`` them, or ``disconnect``."""
  return _call("POST", f"payouts/{action}", _paired(), {})
