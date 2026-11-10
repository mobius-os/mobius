"""Every credits charge an app makes waits for the owner's Pay.

An app's service may ask to charge the owner's credits (``routes/credits.py``),
but no charge leaves this Möbius until the owner has seen that exact payment
(who is paid, for what, and what it costs) and pressed Pay in the shell. The
first request is held with ``confirmation_required``; the app retries the same
charge, with the same idempotency key, until the owner answers. Pay lets
exactly that charge through once; Cancel refuses it.

A payment is either from the owner's credits or by card. A card payment's
Pay opens Stripe's page for exactly that price (``credits_client.start_purchase``);
the app's retries then wait for Stripe and receive the same charge receipt.

An app's payments wait in the order it asked, and the shell shows the oldest
first, so two charges at once never push each other out. The same charge
asked again (the same key) is still one question, so the owner is never asked
twice for it.

Questions live in memory: they are minutes long, and a restart only means
the app's next retry asks again. A confirmed card payment is different: the
owner may stay on Stripe's page for much longer, and asking them to pay again
would open a second purchase. So it is kept on disk, through restarts, until
Stripe's checkout can no longer complete (``CARD_PURCHASE_SECONDS``). Only the
owner's human input answers a question (``routes/app_consent.py``), never an
app or an agent.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

from fastapi import HTTPException

from app import credits_client
from app.config import get_settings

# How long an unanswered or answered question waits for the app's retry.
CONFIRM_SECONDS = 10 * 60
# How long a confirmed card payment is kept after Pay. A Stripe Checkout page
# lasts at most 24 hours; the extra hour lets a late payment settle.
CARD_PURCHASE_SECONDS = 25 * 60 * 60
# How many payments one app may have waiting for the owner at once.
MAX_WAITING_PER_APP = 5

WAITING, CONFIRMED, DECLINED, FAILED = "waiting", "confirmed", "declined", "failed"
CREDITS, CARD = "credits", "card"


@dataclass(frozen=True)
class Quote:
  """What the owner is asked to pay, as the seller priced it."""
  description: str
  price_micro: int
  currency: str = ""  # a card payment's currency, as the credits service sells


@dataclass
class Payment:
  id: str
  app_id: int
  app_name: str
  key: str  # the app-scoped idempotency key, one payment's identity
  seller: str
  priced_app: str
  action: str
  max_price_micro: int
  description: str
  price_micro: int = 0
  currency: str = ""
  method: str = CREDITS
  buyer_app: str = ""
  state: str = WAITING
  purchase_id: str = ""
  error: dict | None = None
  paid_at: float = 0.0  # wall-clock time of a card payment's Pay
  at: float = field(default_factory=time.monotonic)

  def view(self) -> dict:
    return {
      "id": self.id, "app_id": self.app_id, "app_name": self.app_name,
      "seller": self.seller, "app": self.priced_app, "action": self.action,
      "description": self.description, "max_price_micro": self.max_price_micro,
      "price_micro": self.price_micro, "currency": self.currency,
      "method": self.method,
      "max_price": credits_text(self.max_price_micro),
      "price": credits_text(self.price_micro),
    }

  def _held_on_disk(self) -> bool:
    return self.method == CARD and self.state == CONFIRMED and bool(self.purchase_id)


def credits_text(micro: int) -> str:
  """Credits as people read them: two decimals, more only when they matter."""
  credits = Decimal(micro) / Decimal(1_000_000)
  shown = credits.quantize(Decimal("0.01"))
  return str(shown if shown == credits else credits.normalize())


_lock = threading.Lock()
_payments: dict[str, Payment] = {}
_cards_loaded = False
_cards_written: str | None = None


# --- Confirmed card payments, kept through restarts ---------------------------


def _cards_path() -> Path:
  return Path(get_settings().data_dir) / "credits-client" / "card-payments.json"


def _load_cards() -> None:
  """Read the confirmed card payments back once, after a restart."""
  global _cards_loaded, _cards_written
  if _cards_loaded:
    return
  _cards_loaded = True
  _cards_written = "[]"  # nothing stored is the same as an empty list
  try:
    stored = json.loads(_cards_path().read_text())
  except (OSError, ValueError):
    return
  for record in stored if isinstance(stored, list) else []:
    if not isinstance(record, dict):
      continue
    try:
      payment = Payment(**{k: v for k, v in record.items() if k not in ("at", "error")})
    except TypeError:
      continue
    if payment._held_on_disk():
      _payments.setdefault(payment.id, payment)
  _cards_written = _cards_snapshot()


def _cards_snapshot() -> str:
  held = sorted((p for p in _payments.values() if p._held_on_disk()), key=lambda p: p.id)
  return json.dumps([{k: v for k, v in asdict(p).items() if k not in ("at", "error")}
                     for p in held])


def _save_cards() -> None:
  """Write the confirmed card payments when they changed (under ``_lock``)."""
  global _cards_written
  snapshot = _cards_snapshot()
  if snapshot == _cards_written:
    return
  path = _cards_path()
  path.parent.mkdir(parents=True, exist_ok=True)
  os.chmod(path.parent, 0o700)  # shared with the sealed credits pairing
  # A unique 0600 file replaced atomically: a crash never leaves half a list.
  descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".card-payments-", suffix=".tmp")
  try:
    with os.fdopen(descriptor, "w") as handle:
      handle.write(snapshot)
    os.replace(temporary, path)
  except BaseException:
    Path(temporary).unlink(missing_ok=True)
    raise
  _cards_written = snapshot


# --- Questions -----------------------------------------------------------------


def _expire() -> None:
  now, wall = time.monotonic(), time.time()
  for pid, p in list(_payments.items()):
    lapsed = (wall - p.paid_at > CARD_PURCHASE_SECONDS) if p._held_on_disk() \
      else (now - p.at > CONFIRM_SECONDS)
    if lapsed:
      del _payments[pid]
  _save_cards()


def _find(app_id: int, key: str) -> Payment | None:
  return next((p for p in _payments.values() if p.app_id == app_id and p.key == key), None)


def _drop(payment: Payment) -> None:
  _payments.pop(payment.id, None)
  _save_cards()


def require(*, app_id: int, app_name: str, key: str, seller: str, priced_app: str,
            action: str, max_price_micro: int, describe, method: str = CREDITS,
            buyer_app: str = "") -> Payment:
  """Return the confirmed payment for this charge, or refuse until it is.

  ``describe()`` returns the ``Quote`` the owner is asked to pay; it runs only
  when a new question is raised. Its refusal (an ``HTTPException``, or the
  credits service unreachable) refuses the charge before the owner is asked:
  a payment whose price could not be read is never put to them.
  """
  with _lock:
    _load_cards()
    _expire()
    payment = _find(app_id, key)
    if payment is not None and (payment.seller, payment.priced_app, payment.action,
                                payment.max_price_micro, payment.method) != (
                                  seller, priced_app, action, max_price_micro, method):
      # The same key for a different payment: the owner never saw this one.
      _drop(payment)
      payment = None
    if payment is not None and payment.state == CONFIRMED:
      return payment
    if payment is not None and payment.state == DECLINED:
      _drop(payment)
      raise HTTPException(403, {"code": "payment_declined", "message": "You cancelled this payment."})
    if payment is not None and payment.state == FAILED:
      _drop(payment)
      raise HTTPException(409, payment.error or {"code": "payment_failed",
                                                 "message": "That payment could not be started."})
    raised = payment is None
  if raised:
    try:
      quote = describe()
    except credits_client.CreditsUnavailable as error:
      raise credits_client.as_http(error) from error
    with _lock:
      payment = _find(app_id, key)
      if payment is None:
        if sum(p.app_id == app_id and p.state == WAITING for p in _payments.values()) \
            >= MAX_WAITING_PER_APP:
          raise HTTPException(429, {
            "code": "too_many_payments_waiting",
            "message": "This app already has payments waiting for you. Answer those first.",
          })
        payment = Payment(id=secrets.token_hex(8), app_id=app_id, app_name=app_name, key=key,
                          seller=seller, priced_app=priced_app, action=action,
                          max_price_micro=max_price_micro,
                          description=quote.description or action,
                          price_micro=quote.price_micro, currency=quote.currency,
                          method=method, buyer_app=buyer_app)
        _payments[payment.id] = payment
    from app.broadcast import get_system_broadcast
    get_system_broadcast().publish({"type": "credits_charge_requested", "appId": str(app_id)})
  raise HTTPException(409, {
    "code": "confirmation_required",
    "message": "Confirm this payment in Möbius.",
    "confirmation_id": payment.id,
  })


def forget(payment: Payment) -> None:
  """The confirmed payment ended without a charge: the next try asks again."""
  with _lock:
    _load_cards()
    _drop(payment)


def waiting(app_id: int) -> list[dict]:
  """The payments of one app still waiting for the owner, oldest first."""
  with _lock:
    _load_cards()
    _expire()
    found = sorted((p for p in _payments.values() if p.app_id == app_id and p.state == WAITING),
                   key=lambda p: p.at)
    return [p.view() for p in found]


def waiting_payment(app_id: int, payment_id: str) -> Payment | None:
  """The payment this question is about, while it still waits for the owner."""
  with _lock:
    payment = _payments.get(payment_id)
    if payment is None or payment.app_id != app_id or payment.state != WAITING:
      return None
    return payment


def answer(app_id: int, payment_id: str, pay: bool, *, purchase_id: str = "",
           error: dict | None = None) -> bool:
  """Record the owner's Pay or Cancel. False if there is no such question.

  A card payment's Pay carries the purchase it opened, or why none could be
  opened (``error``: the app's retry then receives that refusal).
  """
  with _lock:
    _load_cards()
    payment = _payments.get(payment_id)
    if payment is None or payment.app_id != app_id or payment.state != WAITING:
      return False
    payment.state = FAILED if error else (CONFIRMED if pay else DECLINED)
    payment.purchase_id = purchase_id
    payment.error = error
    # A confirmed payment lives ``CONFIRM_SECONDS`` from this answer, not from
    # its first charge: replaying the charge reads the live answer but never
    # pushes the expiry out, so the owner is asked anew once it lapses. A card
    # payment instead lives as long as Stripe's page can still be paid.
    payment.at = time.monotonic()
    payment.paid_at = time.time()
    _save_cards()
  from app.broadcast import get_system_broadcast
  get_system_broadcast().publish({"type": "credits_charge_answered", "appId": str(app_id)})
  return True


def clear() -> None:
  """Forget every question held in memory, as a restart does (tests).

  Confirmed card payments stay on disk and are read back on the next use.
  """
  global _cards_loaded, _cards_written
  with _lock:
    _payments.clear()
    _cards_loaded = False
    _cards_written = None
