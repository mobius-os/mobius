"""The owner's credits at the paired credits service (``app.credits_client``).

Every route here is gated by exactly one dependency that both identifies the
caller and applies the whole rule for what it may do:

- ``get_credits_reader``: the balance, earnings, history and purchase terms.
  The owner, or an app holding the reviewed ``credits_manage`` grant; never
  a shared-browser guest.
- ``get_credits_controller``: pairing, buying, model funding, publishing a
  price and moving earnings. As the reader, and never a delegated helper.
- ``get_credits_spender``: an app's own service paying for one priced call,
  under its ``spend_credits`` grant.
- ``get_credits_price_reader``: a seller's public prices, which a paying app
  shows before it charges.

This is money, so ``credits_manage`` is its own critical grant: the
``identity_manage`` grant the identity routes accept covers the sign-in link
only and never reaches these routes. The paths stay under ``/api/identity``,
where the app contract documents them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import credits_client, models
from app.deps import (
  get_credits_controller,
  get_credits_price_reader,
  get_credits_reader,
  get_credits_spender,
  reject_cross_site,
  spend_credits_targets,
)

router = APIRouter(
  prefix="/api/identity/credits",
  tags=["credits"],
  dependencies=[Depends(reject_cross_site)],
)


class _PairRequest(BaseModel):
  operator_url: str = Field(max_length=2000)
  key: str = Field(max_length=500)


class _TopupRequest(BaseModel):
  amount_cents: int = Field(gt=0)
  waiver_accepted: bool = False
  # The ``pairing`` the purchase terms were read under; a re-paired service
  # refuses rather than sell on terms the owner never saw.
  pairing: str | None = Field(default=None, max_length=64)


class _FundingRequest(BaseModel):
  amount_micro: int = Field(gt=0)
  idempotency_key: str = Field(min_length=8, max_length=200)


class _PriceRequest(BaseModel):
  price_micro: int = Field(ge=0)
  description: str | None = Field(default=None, max_length=300)
  enabled: bool = True


class _ChargeRequest(BaseModel):
  seller: str = Field(min_length=1, max_length=40)
  app: str = Field(min_length=1, max_length=80)
  action: str = Field(min_length=1, max_length=80)
  idempotency_key: str = Field(min_length=8, max_length=200)
  # The price the app showed the owner; a higher current price refuses.
  max_price_micro: int = Field(ge=0)


def _credits(action, *args):
  try:
    return action(*args)
  except credits_client.CreditsUnavailable as error:
    raise credits_client.as_http(error) from error


@router.get("")
def read_credits(_owner: models.Owner = Depends(get_credits_reader)):
  """Balance and earnings from the paired credits service, never stored here."""
  return credits_client.summary()


@router.post("/pair")
def pair_credits(
  body: _PairRequest,
  _owner: models.Owner = Depends(get_credits_controller),
):
  return _credits(credits_client.pair, body.operator_url, body.key)


@router.post("/pair/account")
def pair_credits_with_account(_owner: models.Owner = Depends(get_credits_controller)):
  """Connect a Möbius launched from mobius.you to its owner's wallet in one step."""
  return _credits(credits_client.pair_with_account)


@router.post("/unpair")
def unpair_credits(_owner: models.Owner = Depends(get_credits_controller)):
  return credits_client.unpair()


@router.post("/topup")
def start_credits_topup(
  body: _TopupRequest,
  _owner: models.Owner = Depends(get_credits_controller),
):
  return _credits(
    credits_client.start_topup,
    body.amount_cents, body.waiver_accepted, body.pairing)


@router.get("/purchase-terms")
def read_credits_purchase_terms(_owner: models.Owner = Depends(get_credits_reader)):
  return _credits(credits_client.purchase_terms)


@router.get("/topups/{topup_id}")
def read_credits_topup(
  topup_id: str,
  _owner: models.Owner = Depends(get_credits_reader),
):
  """A purchase's state. Reading it after Stripe's page confirms payment."""
  return _credits(credits_client.read_topup, topup_id)


@router.get("/transactions")
def read_credits_transactions(
  limit: int = 50,
  _owner: models.Owner = Depends(get_credits_reader),
):
  return _credits(credits_client.transactions, limit)


@router.post("/model-funding")
def fund_model_balance(
  body: _FundingRequest,
  _owner: models.Owner = Depends(get_credits_controller),
):
  """Spend credits on Möbius-hosted model usage."""
  return _credits(credits_client.fund_models, body.amount_micro, body.idempotency_key)


@router.get("/prices/{seller}")
def read_credits_prices(
  seller: str,
  app_slug: str | None = None,
  _owner: models.Owner = Depends(get_credits_price_reader),
):
  """A seller's published prices, which a paying app shows before it charges."""
  return _credits(credits_client.prices, seller, app_slug)


@router.put("/prices/{app_slug}/{action}")
def publish_credits_price(
  app_slug: str,
  action: str,
  body: _PriceRequest,
  _owner: models.Owner = Depends(get_credits_controller),
):
  """Set what one action of the owner's app costs other people."""
  return _credits(
    credits_client.publish_price,
    app_slug, action, body.price_micro, body.description, body.enabled)


@router.post("/charges")
def charge_credits_for_call(
  body: _ChargeRequest,
  app: models.App = Depends(get_credits_spender),
):
  """An app's own service pays for one priced call from the owner's credits.

  The price is the seller's, read at the credits service, so the app cannot
  choose what a call costs; it can only refuse to pay more than it showed.
  It pays only the ``"<seller>/<app>"`` entries the owner allowed at install.
  """
  if f"{body.seller}/{body.app}" not in spend_credits_targets(app):
    raise HTTPException(403, {
      "code": "seller_not_allowed",
      "message": (
        f"This app may not pay {body.seller}/{body.app}: its spend_credits "
        "permission does not list it."
      ),
    })
  # The permanent package id outlives reinstalls and slug changes; an
  # owner-built app has none, so its row id stands in.
  payer = f"package:{app.package_id}" if app.package_id else f"app:{app.id}"
  return _credits(
    lambda: credits_client.charge(
      body.seller, body.app, body.action, body.idempotency_key, body.max_price_micro,
      payer=payer, buyer_app=app.package_id or app.slug))


@router.post("/connect")
def start_credits_connect(
  _owner: models.Owner = Depends(get_credits_controller),
):
  """Stripe's page for connecting the account that receives withdrawals."""
  return _credits(credits_client.start_connect)


@router.post("/convert")
def convert_credits_earnings(_owner: models.Owner = Depends(get_credits_controller)):
  """Keep cleared earnings as credits."""
  return _credits(credits_client.payout_action, "convert")


@router.post("/withdraw")
def withdraw_credits_earnings(_owner: models.Owner = Depends(get_credits_controller)):
  """Withdraw cleared earnings to the connected Stripe account."""
  return _credits(credits_client.payout_action, "withdraw")


@router.post("/disconnect")
def disconnect_credits_payouts(_owner: models.Owner = Depends(get_credits_controller)):
  """Unlink the Stripe account that receives withdrawals."""
  return _credits(credits_client.payout_action, "disconnect")
