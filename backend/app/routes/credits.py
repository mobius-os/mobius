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
- ``get_credits_receipt_reader``: one charge's receipt, which a marketplace
  checks before it unlocks what a buyer says they paid for, under its
  read-only ``check_credits_charges`` grant.

This is money, so ``credits_manage`` is its own critical grant: the
``identity_manage`` grant the identity routes accept covers the sign-in link
only and never reaches these routes. The paths stay under ``/api/identity``,
where the app contract documents them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import credits_client, credits_confirmations, models
from app.deps import (
  app_manages_credits,
  credits_charge_apps,
  get_credits_controller,
  get_credits_price_reader,
  get_credits_reader,
  get_credits_receipt_reader,
  get_credits_spender,
  reject_cross_site,
  spend_credits_allows,
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
  # Only "credits" for now; any other method is refused rather than ignored,
  # so an app asking for a card payment is never charged credits instead.
  method: str = Field(default="credits", max_length=20)


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
  It pays only the ``"<seller>/<app>"`` (or ``"*/<app>"``) entries the owner
  allowed.
  """
  if body.method != "credits":
    raise HTTPException(422, {
      "code": "payment_method_unsupported",
      "message": "Only payments from Möbius credits are supported.",
    })
  if not spend_credits_allows(app, body.seller, body.app):
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
  # Nothing is charged until the owner has seen this payment and pressed Pay.
  payment = credits_confirmations.require(
    app_id=app.id, app_name=app.name,
    key=credits_client.app_scoped_key(payer, body.idempotency_key),
    seller=body.seller, priced_app=body.app, action=body.action,
    max_price_micro=body.max_price_micro,
    describe=lambda: _quote(body.seller, body.app, body.action, body.max_price_micro))
  # A payer may read its own charge; only an app that also manages the owner's
  # credits may read the owner's balance the service returns alongside it.
  manages = app_manages_credits(app)
  try:
    charged = credits_client.charge(
      body.seller, body.app, body.action, body.idempotency_key, body.max_price_micro,
      payer=payer, buyer_app=app.package_id or app.slug)
  except credits_client.CreditsUnavailable as error:
    # A definite refusal charged nothing, so forget the confirmation and ask the
    # owner again next time. An unknown outcome may have charged: its retry must
    # replay the same confirmed payment, so leave the confirmation in place.
    if error.code != credits_client.CHARGE_OUTCOME_UNKNOWN:
      credits_confirmations.forget(payment)
    raise credits_client.as_http(error if manages else credits_client.payer_refusal_view(error)) from error
  return charged if manages else credits_client.payer_charge_view(charged)


def _quote(seller: str, priced_app: str, action: str,
           max_price_micro: int) -> credits_confirmations.Quote:
  """What the owner is asked to pay: the seller's own description and price.

  Anything the credits service would refuse is refused here, before the owner
  is asked to pay for it: an action the seller never priced (or switched off),
  or a price above what the app agreed to pay.
  """
  listed = credits_client.prices(seller, priced_app).get("prices") or []
  match = next((p for p in listed if isinstance(p, dict) and p.get("action") == action), None)
  if match is None or match.get("enabled") is False:
    raise HTTPException(404, {
      "code": "price_not_found",
      "message": "The creator hasn't set this price up at mobius.you yet.",
    })
  price_micro = match.get("price_micro")
  if not isinstance(price_micro, int) or isinstance(price_micro, bool) or price_micro < 0:
    price_micro = max_price_micro  # the service did not say: the most it can cost
  if price_micro > max_price_micro:
    raise HTTPException(409, {
      "code": "price_changed",
      "message": "The creator's price is higher than this app agreed to pay.",
      "price_micro": price_micro, "max_price_micro": max_price_micro,
    })
  return credits_confirmations.Quote(
    description=str(match.get("description") or ""), price_micro=price_micro)


@router.get("/charges/{charge_id}")
def read_credits_charge_receipt(
  charge_id: str,
  app: models.App = Depends(get_credits_receipt_reader),
):
  """Who paid this owner, for which call, and how much: a claimed sale, checked.

  A marketplace unlocks an item for a buyer on another Möbius only after its
  service confirms the charge id that buyer's Möbius reports. Either lane
  may ask; reading a receipt moves no money. Only a charge that paid this
  owner for one of the apps the grant names is shown; any other is not found.
  """
  return _credits(credits_client.own_sale_receipt, charge_id, credits_charge_apps(app))


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
