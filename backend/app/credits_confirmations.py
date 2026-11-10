"""Every credits charge an app makes waits for the owner's Pay.

An app's service may ask to charge the owner's credits (``routes/credits.py``),
but no charge leaves this Möbius until the owner has seen that exact payment
(who is paid, for what, and the most it can cost) and pressed Pay in the
shell. The first request is held with ``confirmation_required``; the app
retries the same charge, with the same idempotency key, until the owner
answers. Pay lets exactly that charge through once; Cancel refuses it.

A payment is either from the owner's credits or by card. A card payment's
Pay opens Stripe's page for exactly that price (``credits_client.start_purchase``);
the app's retries then wait for Stripe and receive the same charge receipt.

Only the payment an app asked for last waits: a new one from the same app
replaces any older unanswered one, so the owner is never asked twice.

Questions live in memory: they are minutes long, and a restart only means
the app's next retry asks again. Only the owner's human input answers one
(``routes/app_consent.py``), never an app or an agent.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal

from fastapi import HTTPException

# How long an unanswered or answered question waits for the app's retry.
CONFIRM_SECONDS = 10 * 60

WAITING, CONFIRMED, DECLINED, FAILED = "waiting", "confirmed", "declined", "failed"
CREDITS, CARD = "credits", "card"


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
  method: str = CREDITS
  buyer_app: str = ""
  state: str = WAITING
  purchase_id: str = ""
  error: dict | None = None
  at: float = field(default_factory=time.monotonic)

  def view(self) -> dict:
    return {
      "id": self.id, "app_id": self.app_id, "app_name": self.app_name,
      "seller": self.seller, "app": self.priced_app, "action": self.action,
      "description": self.description, "max_price_micro": self.max_price_micro,
      "method": self.method,
      "max_price": credits_text(self.max_price_micro),
    }


def credits_text(micro: int) -> str:
  """Credits as people read them: two decimals, more only when they matter."""
  credits = Decimal(micro) / Decimal(1_000_000)
  shown = credits.quantize(Decimal("0.01"))
  return str(shown if shown == credits else credits.normalize())


_lock = threading.Lock()
_payments: dict[str, Payment] = {}


def _expire(now: float) -> None:
  for pid in [pid for pid, p in _payments.items() if now - p.at > CONFIRM_SECONDS]:
    del _payments[pid]


def _find(app_id: int, key: str) -> Payment | None:
  return next((p for p in _payments.values() if p.app_id == app_id and p.key == key), None)


def require(*, app_id: int, app_name: str, key: str, seller: str, priced_app: str,
            action: str, max_price_micro: int, describe, method: str = CREDITS,
            buyer_app: str = "") -> Payment:
  """Return the confirmed payment for this charge, or refuse until it is.

  ``describe()`` names what is being bought for the question; it runs only
  when a new question is raised, and a failure falls back to the action.
  """
  with _lock:
    _expire(time.monotonic())
    payment = _find(app_id, key)
    if payment is not None and (payment.seller, payment.priced_app, payment.action,
                                payment.max_price_micro, payment.method) != (
                                  seller, priced_app, action, max_price_micro, method):
      # The same key for a different payment: the owner never saw this one.
      del _payments[payment.id]
      payment = None
    if payment is not None and payment.state == CONFIRMED:
      return payment
    if payment is not None and payment.state == DECLINED:
      del _payments[payment.id]
      raise HTTPException(403, {"code": "payment_declined", "message": "You cancelled this payment."})
    if payment is not None and payment.state == FAILED:
      del _payments[payment.id]
      raise HTTPException(409, payment.error or {"code": "payment_failed",
                                                 "message": "That payment could not be started."})
    raised = payment is None
  if raised:
    try:
      description = str(describe() or "") or action
    except HTTPException:
      raise  # e.g. nothing priced: refuse rather than ask
    except Exception:
      description = action
    with _lock:
      payment = _find(app_id, key)
      if payment is None:
        # Only the payment the app asked for last waits for the owner.
        for older in [p for p in _payments.values() if p.app_id == app_id and p.state == WAITING]:
          del _payments[older.id]
        payment = Payment(id=secrets.token_hex(8), app_id=app_id, app_name=app_name, key=key,
                          seller=seller, priced_app=priced_app, action=action,
                          max_price_micro=max_price_micro, description=description,
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
    _payments.pop(payment.id, None)


def waiting(app_id: int) -> list[dict]:
  """The payments of one app still waiting for the owner, oldest first."""
  with _lock:
    _expire(time.monotonic())
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
    payment = _payments.get(payment_id)
    if payment is None or payment.app_id != app_id or payment.state != WAITING:
      return False
    payment.state = FAILED if error else (CONFIRMED if pay else DECLINED)
    payment.purchase_id = purchase_id
    payment.error = error
    # A confirmed payment lives ``CONFIRM_SECONDS`` from this answer, not from
    # its first charge: replaying the charge reads the live answer but never
    # pushes the expiry out, so the owner is asked anew once it lapses.
    payment.at = time.monotonic()
  from app.broadcast import get_system_broadcast
  get_system_broadcast().publish({"type": "credits_charge_answered", "appId": str(app_id)})
  return True


def clear() -> None:
  """Forget every question (tests)."""
  with _lock:
    _payments.clear()
