"""Every credits charge an app makes waits for the owner's Pay.

An app's service may ask to charge the owner's credits (``routes/credits.py``),
but no charge leaves this Möbius until the owner has seen that exact payment
(who is paid, for what, and what it costs) and pressed Pay in the shell. The
first request is held with ``confirmation_required``; the app retries the same
charge, with the same idempotency key, until the owner answers. Pay lets
exactly that charge through; Cancel refuses it, and keeps refusing every retry
with that key until the answer lapses, so an app that keeps retrying never puts
the same payment to the owner again. Asking again takes a new key.

An app's payments wait in the order it asked, and the shell shows the oldest
first, so two charges at once never push each other out. The same charge
asked again (the same key) is still one question, so the owner is never asked
twice for it.

Questions live in memory: they are minutes long, and a restart only means
the app's next retry asks again. Only the owner's human input answers a
question (``routes/app_consent.py``), never an app or an agent.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal

from fastapi import HTTPException

from app import credits_client

# How long a question waits for the owner, and how long their answer then
# holds for the app's retries.
CONFIRM_SECONDS = 10 * 60
# How many payments one app may have waiting for the owner at once.
MAX_WAITING_PER_APP = 5

WAITING, CONFIRMED, DECLINED = "waiting", "confirmed", "declined"


@dataclass(frozen=True)
class Quote:
  """What the owner is asked to pay, as the seller priced it."""
  description: str
  price_micro: int


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
  state: str = WAITING
  at: float = field(default_factory=time.monotonic)

  def view(self) -> dict:
    return {
      "id": self.id, "app_id": self.app_id, "app_name": self.app_name,
      "seller": self.seller, "app": self.priced_app, "action": self.action,
      "description": self.description, "max_price_micro": self.max_price_micro,
      "price_micro": self.price_micro,
      "max_price": credits_text(self.max_price_micro),
      "price": credits_text(self.price_micro),
    }


def credits_text(micro: int) -> str:
  """Credits as people read them: two decimals, more only when they matter."""
  credits = Decimal(micro) / Decimal(1_000_000)
  shown = credits.quantize(Decimal("0.01"))
  return str(shown if shown == credits else credits.normalize())


_lock = threading.Lock()
_payments: dict[str, Payment] = {}


def _expire() -> None:
  now = time.monotonic()
  for pid, p in list(_payments.items()):
    if now - p.at > CONFIRM_SECONDS:
      del _payments[pid]


def _find(app_id: int, key: str) -> Payment | None:
  return next((p for p in _payments.values() if p.app_id == app_id and p.key == key), None)


def require(*, app_id: int, app_name: str, key: str, seller: str, priced_app: str,
            action: str, max_price_micro: int, describe) -> Payment:
  """Return the confirmed payment for this charge, or refuse until it is.

  ``describe()`` returns the ``Quote`` the owner is asked to pay; it runs only
  when a new question is raised. Its refusal (an ``HTTPException``, or the
  credits service unreachable) refuses the charge before the owner is asked:
  a payment whose price could not be read is never put to them.
  """
  with _lock:
    _expire()
    payment = _find(app_id, key)
    if payment is not None and payment.state == DECLINED:
      # Checked before anything else about the charge: changing the price or
      # the seller under a cancelled key never asks the owner again either.
      raise HTTPException(403, {"code": "payment_declined", "message": "You cancelled this payment."})
    if payment is not None and (payment.seller, payment.priced_app, payment.action,
                                payment.max_price_micro) != (
                                  seller, priced_app, action, max_price_micro):
      # The same key for a different payment: the owner never saw this one.
      _payments.pop(payment.id, None)
      payment = None
    if payment is not None and payment.state == CONFIRMED:
      return payment
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
                          price_micro=quote.price_micro)
        _payments[payment.id] = payment
    from app.broadcast import get_system_broadcast
    get_system_broadcast().publish({"type": "credits_charge_requested", "appId": str(app_id)})
  raise HTTPException(409, {
    "code": "confirmation_required",
    "message": "Confirm this payment in Möbius.",
  })


def forget(payment: Payment) -> None:
  """The confirmed payment ended without a charge: the next try asks again."""
  with _lock:
    _payments.pop(payment.id, None)


def waiting(app_id: int) -> list[dict]:
  """The payments of one app still waiting for the owner, oldest first."""
  with _lock:
    _expire()
    found = sorted((p for p in _payments.values() if p.app_id == app_id and p.state == WAITING),
                   key=lambda p: p.at)
    return [p.view() for p in found]


def answer(app_id: int, payment_id: str, pay: bool) -> bool:
  """Record the owner's Pay or Cancel. False if there is no such question,
  including one left unanswered past its window."""
  with _lock:
    _expire()
    payment = _payments.get(payment_id)
    if payment is None or payment.app_id != app_id or payment.state != WAITING:
      return False
    payment.state = CONFIRMED if pay else DECLINED
    # An answer lives ``CONFIRM_SECONDS`` from now, not from the first charge:
    # replaying the charge reads the live answer but never pushes the expiry
    # out, so a confirmation lapses and the owner is asked anew, and a Cancel
    # keeps refusing that key for the same window.
    payment.at = time.monotonic()
  from app.broadcast import get_system_broadcast
  get_system_broadcast().publish({"type": "credits_charge_answered", "appId": str(app_id)})
  return True


def clear() -> None:
  """Forget every question, as a restart does (tests)."""
  with _lock:
    _payments.clear()
