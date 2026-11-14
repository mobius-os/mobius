"""`network.direct`: an app's screen may reach the exact outside sites it
declares, only after the owner accepted them, and never for scripts."""

import pytest

from app import app_consent, models
from app.app_capabilities import (
  contract_and_digest, diff_contracts, direct_network_origins,
  normalize_runtime_capabilities,
)
from app.response_policy import app_frame_csp
from test_app_fixtures import create_local_app

ORIGIN = "https://app.example.test"
STORE_URL = "https://example.test/store-app/mobius.json"


def _declaration(*origins, reason="Show GIFs."):
  return {"network.direct": {"version": 1, "reason": reason, "origins": list(origins)}}


def _manifest(capabilities=None):
  manifest = {
    "id": "gifs", "name": "Gifs", "version": "1.0.0", "entry": "index.jsx",
    "permissions": {},
  }
  if capabilities is not None:
    manifest["capabilities"] = capabilities
  return manifest


def _directive(policy, name):
  return policy.split(f"{name} ", 1)[1].split(";", 1)[0].split()


def test_declared_sites_are_normalized_exact_https_origins():
  runtime = normalize_runtime_capabilities(_manifest(_declaration(
    "https://Static.Example.com/", "https://api.example.com:443",
  )))
  grant = runtime["network.direct"]
  assert grant["origins"] == ["https://api.example.com", "https://static.example.com"]
  assert grant["kind"] == "frame_policy" and grant["reason"] == "Show GIFs."


@pytest.mark.parametrize("origins", [
  [], None, "https://api.example.com",
  ["http://api.example.com"], ["https://api.example.com/v1"],
  ["https://*.example.com"], ["https://user@api.example.com"],
  ["https://api.example.com?x=1"], ["https://localhost"], ["https://10.0.0.1"],
  ["https://api.example.com", "https://API.example.com"],
  [f"https://s{i}.example.com" for i in range(9)],
])
def test_anything_but_a_short_list_of_exact_public_https_sites_is_refused(origins):
  with pytest.raises(ValueError):
    normalize_runtime_capabilities(_manifest({
      "network.direct": {"version": 1, "origins": origins},
    }))


def test_other_capabilities_cannot_smuggle_in_sites():
  with pytest.raises(ValueError, match="does not take origins"):
    normalize_runtime_capabilities(_manifest({
      "media.speech": {"version": 1, "origins": ["https://api.example.com"]},
    }))


def _contract(*origins):
  contract, _digest = contract_and_digest(
    _manifest(_declaration(*origins) if origins else None),
  )
  return contract


def test_a_new_site_asks_again_and_dropping_one_does_not():
  one = _contract("https://a.example.com")
  two = _contract("https://a.example.com", "https://b.example.com")
  assert diff_contracts(one, two)["widens"] is True
  assert diff_contracts(two, one)["widens"] is False
  assert diff_contracts(_contract(), one)["widens"] is True
  assert diff_contracts(one, _contract())["widens"] is False


def test_the_install_popup_names_every_site_and_binds_them_as_the_answer():
  contract = _contract("https://a.example.com", "https://b.example.com")
  (row,) = [r for r in app_consent.rows_from_contract(contract) if r["id"] == "network.direct"]
  assert row["title"] == "Connect to outside sites"
  assert "Sites: a.example.com, b.example.com." in row["text"]
  assert row["scope"] == ["https://a.example.com", "https://b.example.com"]
  accepted = {"network.direct": {"rank": 1, "scope": {"https://a.example.com"}, "limits": {}}}
  assert not app_consent._covered(row, accepted)


def test_stored_sites_are_revalidated_before_they_reach_a_header():
  contract = _contract("https://a.example.com")
  assert direct_network_origins(contract) == ["https://a.example.com"]
  contract["runtime"]["network.direct"]["origins"] = ["https://a.example.com; script-src *"]
  assert direct_network_origins(contract) == []
  assert direct_network_origins(None) == []


def test_sites_widen_only_images_and_requests_never_code():
  plain = app_frame_csp(ORIGIN)
  policy = app_frame_csp(ORIGIN, direct_origins=("https://media.example.com",))
  assert _directive(policy, "img-src") == [ORIGIN, "https://media.example.com", "data:", "blob:"]
  assert _directive(policy, "connect-src") == [ORIGIN, "https://media.example.com"]
  for name in ("default-src", "script-src", "style-src", "font-src", "frame-src"):
    assert _directive(policy, name) == _directive(plain, name)
  hostile = app_frame_csp(ORIGIN, direct_origins=(
    "http://media.example.com", "https://x.example.com; script-src *",
  ))
  assert hostile == plain


def _frame_app(client, owner_token, db, *, store, accepted):
  app_id = create_local_app(
    client, {"Authorization": f"Bearer {owner_token}"}, name="direct-frame",
    description="test",
  )["id"]
  row = db.get(models.App, app_id)
  row.capability_contract = _contract("https://media.example.com")
  row.manifest_url = STORE_URL if store else None
  db.commit()
  if accepted:
    assert app_consent.accept(row, app_consent.request_digest(app_consent.needed_rows(row)))
    db.commit()
  return app_id


def _frame_policy(client, app_id):
  response = client.get(f"/api/apps/{app_id}/frame")
  assert response.status_code == 200
  return response.headers["content-security-policy"], response.headers.get("etag")


def test_the_frame_reaches_accepted_sites_only(client, owner_token, db):
  waiting = _frame_app(client, owner_token, db, store=True, accepted=False)
  policy, _etag = _frame_policy(client, waiting)
  assert "media.example.com" not in policy

  accepted = _frame_app(client, owner_token, db, store=True, accepted=True)
  policy, _etag = _frame_policy(client, accepted)
  assert "https://media.example.com" in _directive(policy, "img-src")
  assert "https://media.example.com" not in _directive(policy, "script-src")


def test_granting_sites_changes_the_frame_validator(client, owner_token, db):
  app_id = _frame_app(client, owner_token, db, store=False, accepted=False)
  _policy, granted = _frame_policy(client, app_id)
  row = db.get(models.App, app_id)
  row.capability_contract = _contract()
  db.commit()
  policy, plain = _frame_policy(client, app_id)
  assert "media.example.com" not in policy
  assert granted != plain


def test_answering_the_sites_question_reloads_the_running_frame(client, owner_token, db):
  from app.app_compile_contract import app_frame_version
  from app.schemas import AppOut

  app_id = _frame_app(client, owner_token, db, store=True, accepted=False)
  row = db.get(models.App, app_id)
  waiting = app_frame_version(row)
  assert app_consent.accept(row, app_consent.request_digest(app_consent.needed_rows(row)))
  db.commit()
  granted = app_frame_version(row)
  assert granted != waiting
  # The shell reads the key from the API projection; it must agree.
  assert AppOut.model_validate(row, from_attributes=True).frame_version == granted
  app_consent.revoke(row)
  db.commit()
  assert app_frame_version(row) == waiting
