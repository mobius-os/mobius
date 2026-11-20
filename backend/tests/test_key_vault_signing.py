"""Signing recipes: the vault signs requests (AWS) and mints tokens from private
keys (JWT) itself, so neither the secret nor what it makes reaches the caller.

``aws_sigv4`` is checked against AWS's published Signature Version 4 test
vectors. ``jwt_bearer`` is checked by verifying the signed token with the
public half of a throwaway key, and its Google and GitHub exchanges against a
local stand-in for their token endpoints.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import importlib.util
import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature


VAULT_PATH = Path(__file__).parents[1] / "vault" / "keyvault.py"
SPEC = importlib.util.spec_from_file_location("mobius_key_vault_signing", VAULT_PATH)
kv = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = kv
SPEC.loader.exec_module(kv)

SITE = {"host": "echo.example.com", "methods": ["GET", "POST", "PUT"], "paths": ["/*"]}
GRANTS = {"apps": [], "agents": True, "sites": [SITE]}
AGENT = "agent:c1"

# AWS's published Signature Version 4 test suite credentials and time.
AWS_ID, AWS_SECRET = "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
AWS_DATE = "20150830T123600Z"
AWS_NOW = 1440938160  # 2015-08-30T12:36:00Z


def _pem(key) -> str:
  return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()


RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())


def _b64(text: str) -> bytes:
  return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _verified(token: str, public) -> tuple[dict, dict]:
  """The header and claims of ``token`` once its signature checks out."""
  head, body, signature = token.split(".")
  signed, raw = f"{head}.{body}".encode(), _b64(signature)
  if isinstance(public, rsa.RSAPublicKey):
    public.verify(raw, signed, padding.PKCS1v15(), hashes.SHA256())
  else:
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public.verify(der, signed, ec.ECDSA(hashes.SHA256()))
  return json.loads(_b64(head)), json.loads(_b64(body))


# -- AWS Signature Version 4 ---------------------------------------------------

@pytest.mark.parametrize("method, raw_path, raw_query, extra, body, signature", [
  ("GET", "/", "", [], b"", "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"),
  ("GET", "/", "Param2=value2&Param1=value1", [], b"",
   "b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500"),
  ("POST", "/", "", [("Content-Type", "application/x-www-form-urlencoded")], b"Param1=value1",
   "ff11897932ad3f4e8b18135d722051e5ac45fc38421b1da7b9d196a0fe09473a"),
], ids=["get-vanilla", "get-vanilla-query-order-key-case", "post-x-www-form-urlencoded"])
def test_aws_signing_matches_the_published_test_vectors(method, raw_path, raw_query, extra, body, signature):
  loan = kv.Loan({"type": "aws_sigv4", "access_key_id": "id", "secret_access_key": "secret",
                  "region": "us-east-1", "service": "service"},
                 {"id": AWS_ID, "secret": AWS_SECRET}, frozenset({"secret"}))
  request, sent = kv.build_upstream_request(
    method=method, host="example.amazonaws.com", raw_path=raw_path, raw_query=raw_query,
    caller_headers=extra, loan=loan, body=body or None, now=AWS_NOW,
  )
  headers = {k.lower(): v for k, v in request.headers}
  assert headers["x-amz-date"] == AWS_DATE
  assert headers["authorization"].endswith(f"Signature={signature}")
  assert (sent or b"") == body


def test_aws_path_is_encoded_twice_except_for_s3():
  assert kv.aws_canonical_target("/a%20b/c", "x=1 2", "s3")[:2] == ("/a%20b/c", "/a%20b/c")
  assert kv.aws_canonical_target("/a%20b/c", "", "execute-api")[:2] == ("/a%20b/c", "/a%2520b/c")
  # The query is sorted and sent exactly as signed, spaces as %20.
  assert kv.aws_canonical_target("/", "b=2&a=x%20y&a=1", "s3")[2:] == ("a=1&a=x%20y&b=2",) * 2


def test_s3_signs_its_payload_hash_and_session_token_and_drops_the_callers_signature():
  loan = kv.Loan({"type": "aws_sigv4", "access_key_id": "id", "secret_access_key": "secret",
                  "session_token": "token", "region": "auto", "service": "s3"},
                 {"id": AWS_ID, "secret": AWS_SECRET, "token": "fake-session-token"}, frozenset({"secret", "token"}))
  request, _ = kv.build_upstream_request(
    method="PUT", host="echo.example.com", raw_path="/bucket/key", raw_query="",
    caller_headers=[("Authorization", "AWS4-HMAC-SHA256 forged"), ("X-Amz-Date", "20000101T000000Z"),
                    ("X-Amz-Security-Token", "forged"), ("X-Amz-Content-Sha256", "UNSIGNED-PAYLOAD"),
                    ("X-Amz-Meta-Colour", "blue")],
    loan=loan, body=b"hello", now=AWS_NOW,
  )
  headers = [(k.lower(), v) for k, v in request.headers]
  assert [v for k, v in headers if k == "authorization"][0].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
  assert [v for k, v in headers if k == "x-amz-date"] == [AWS_DATE]
  assert [v for k, v in headers if k == "x-amz-security-token"] == ["fake-session-token"]
  assert [v for k, v in headers if k == "x-amz-content-sha256"] == [hashlib.sha256(b"hello").hexdigest()]
  assert "forged" not in json.dumps(headers)
  signed = [v for k, v in headers if k == "authorization"][0].split("SignedHeaders=")[1].split(",")[0]
  assert signed == "host;x-amz-content-sha256;x-amz-date;x-amz-meta-colour;x-amz-security-token"


@pytest.mark.parametrize("recipe, message", [
  ({"access_key_id": "id", "secret_access_key": "id", "region": "us-east-1", "service": "s3"}, "different parts"),
  ({"access_key_id": "id", "secret_access_key": "secret", "region": "US East", "service": "s3"}, "region"),
  ({"access_key_id": "id", "secret_access_key": "secret", "region": "us-east-1"}, "service"),
])
def test_invalid_aws_recipes_are_refused(recipe, message):
  with pytest.raises(kv.VaultError, match=message):
    kv.validate_inject({"type": "aws_sigv4", **recipe})


# -- tokens from private keys ----------------------------------------------------

def _jwt_loan(recipe: dict, private: str) -> kv.Loan:
  inject = kv.validate_inject({"type": "jwt_bearer", "private_key": "key", **recipe})
  return kv.Loan(inject, {"key": private}, frozenset({"key"}))


def test_a_direct_rs256_token_carries_exactly_the_recipes_claims():
  loan = _jwt_loan({"claims": {"iss": "123456", "aud": "appstoreconnect-v1"}, "key_id": "KEY1",
                    "lifetime_secs": 1200}, _pem(RSA_KEY))
  token, expires = kv.sign_jwt(loan.inject, loan.values["key"], 1_800_000_000)
  header, claims = _verified(token, RSA_KEY.public_key())
  assert header == {"alg": "RS256", "typ": "JWT", "kid": "KEY1"}
  assert claims == {"iss": "123456", "aud": "appstoreconnect-v1", "iat": 1_800_000_000, "exp": 1_800_001_200}
  assert expires == 1_800_001_200


def test_an_es256_token_is_signed_with_a_raw_p256_signature():
  loan = _jwt_loan({"alg": "ES256", "claims": {"iss": "issuer", "aud": "appstoreconnect-v1"}}, _pem(EC_KEY))
  token, _ = kv.sign_jwt(loan.inject, loan.values["key"], time.time())
  assert len(_b64(token.split(".")[2])) == 64
  assert _verified(token, EC_KEY.public_key())[0]["alg"] == "ES256"


def test_a_key_of_the_wrong_kind_is_refused():
  loan = _jwt_loan({"alg": "ES256", "claims": {"iss": "x"}}, _pem(RSA_KEY))
  with pytest.raises(kv.VaultError, match="P-256"):
    kv.sign_jwt(loan.inject, loan.values["key"], time.time())


def test_a_service_account_file_supplies_its_key_and_issuer():
  account = json.dumps({"type": "service_account", "client_email": "bot@example.iam.gserviceaccount.com",
                        "private_key": _pem(RSA_KEY), "token_uri": "https://evil.example/token"})
  loan = _jwt_loan({"claims": {"scope": "https://www.googleapis.com/auth/drive.readonly"},
                    "exchange": {"kind": "google"}}, account)
  token, _ = kv.sign_jwt(loan.inject, loan.values["key"], time.time())
  _, claims = _verified(token, RSA_KEY.public_key())
  assert claims["iss"] == "bot@example.iam.gserviceaccount.com"
  assert claims["aud"] == kv.GOOGLE_AUDIENCE


@pytest.mark.parametrize("recipe, message", [
  ({"claims": {"aud": "x"}}, "issuer"),
  ({"claims": {"iss": "x", "nbf": "1"}}, "claims are only"),
  ({"claims": {"iss": "x\nInjected: y"}}, "one line"),
  ({"claims": {"iss": "x"}, "alg": "HS256"}, "RS256 or ES256"),
  ({"claims": {"iss": "x"}, "lifetime_secs": 7200}, "between 60"),
  ({"claims": {}, "exchange": {"kind": "google"}}, "scope"),
  ({"claims": {"scope": "s", "aud": "https://evil.example/token"}, "exchange": {"kind": "google"}}, "audience"),
  ({"claims": {"iss": "1"}, "exchange": {"kind": "github", "installation_id": "../x"}}, "installation"),
  ({"claims": {"iss": "1"}, "exchange": {"kind": "elsewhere"}}, "google or github"),
])
def test_invalid_token_recipes_are_refused(recipe, message):
  with pytest.raises(kv.VaultError, match=message):
    kv.validate_inject({"type": "jwt_bearer", "private_key": "key", **recipe})


def test_a_github_app_token_lives_at_most_ten_minutes_and_starts_a_minute_early():
  loan = _jwt_loan({"claims": {"iss": "Iv1.client"}, "lifetime_secs": 3600,
                    "exchange": {"kind": "github", "installation_id": "42"}}, _pem(RSA_KEY))
  token, _ = kv.sign_jwt(loan.inject, loan.values["key"], 1_800_000_000)
  _, claims = _verified(token, RSA_KEY.public_key())
  assert claims["iat"] == 1_800_000_000 - 60 and claims["exp"] == 1_800_000_000 + 600


# -- through the vault socket ----------------------------------------------------

class _Sites(BaseHTTPRequestHandler):
  """Google's and GitHub's token endpoints and an echoing site, by path."""

  token_requests: list = []
  refuse = False

  def log_message(self, *args):
    pass

  def _json(self, status, payload):
    data = json.dumps(payload).encode()
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)

  def _answer(self):
    size = int(self.headers.get("Content-Length") or 0)
    body = self.rfile.read(size).decode() if size else ""
    if self.path == "/token":
      type(self).token_requests.append(("google", dict(self.headers), body))
      if type(self).refuse:
        return self._json(400, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."})
      return self._json(200, {"access_token": "ya29.fake-google-access-token", "expires_in": 3599,
                              "token_type": "Bearer"})
    if self.path.startswith("/app/installations/"):
      type(self).token_requests.append(("github", dict(self.headers), self.path))
      return self._json(201, {"token": "ghs_fakeInstallationToken0123", "expires_at": "2099-01-01T00:00:00Z"})
    self._json(200, {"path": self.path, "headers": dict(self.headers), "body": body})

  do_GET = do_POST = do_PUT = _answer


@pytest.fixture
def vault(tmp_path):
  return kv.Vault(tmp_path / "vault")


@pytest.fixture
def client(vault):
  _Sites.token_requests, _Sites.refuse = [], False
  upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Sites)
  threading.Thread(target=upstream.serve_forever, daemon=True).start()
  port = upstream.server_address[1]
  reached = []

  def connect(host, timeout):
    reached.append(host)
    return http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
  short = Path(tempfile.mkdtemp(prefix="kvs-"))
  server = kv.VaultServer(str(short / kv.SOCKET_NAME),
                          type("SigningVault", (kv.VaultHandler,), {"vault": vault, "connect": staticmethod(connect)}))
  threading.Thread(target=server.serve_forever, daemon=True).start()
  try:
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(short / kv.SOCKET_NAME)), base_url="http://vault") as session:
      session.reached = reached
      yield session
  finally:
    server.shutdown()
    server.server_close()
    upstream.shutdown()
    shutil.rmtree(short, ignore_errors=True)


def _use(client, key, method="GET", **kwargs):
  headers = {"X-Vault-Caller": AGENT, **kwargs.pop("headers", {})}
  return client.request(method, f"/use/{key}/echo.example.com/v1/x", headers=headers, **kwargs)


def test_a_google_token_is_minted_once_sent_and_never_shown(vault, client):
  account = json.dumps({"client_email": "bot@example.iam.gserviceaccount.com", "private_key": _pem(RSA_KEY),
                        "token_uri": "https://evil.example/token"})
  vault.put_key("drive", {"parts": [{"name": "account", "value": account, "label": "Service account file"}],
                          "inject": {"type": "jwt_bearer", "private_key": "account",
                                     "claims": {"scope": "https://www.googleapis.com/auth/drive.readonly"},
                                     "exchange": {"kind": "google"}},
                          "grants": GRANTS})
  first = _use(client, "drive", headers={"Authorization": "Bearer forged"})
  second = _use(client, "drive")
  assert first.status_code == second.status_code == 200, first.text
  assert first.json()["headers"]["Authorization"] == "Bearer [redacted by key vault]"
  assert "ya29" not in first.text and "forged" not in first.text
  # One exchange, at Google's fixed endpoint (never the file's token_uri).
  assert [kind for kind, *_ in _Sites.token_requests] == ["google"]
  assert client.reached.count("oauth2.googleapis.com") == 1 and "evil.example" not in client.reached
  _, headers, form = _Sites.token_requests[0]
  fields = dict(urllib.parse.parse_qsl(form))
  assert fields["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
  assert _verified(fields["assertion"], RSA_KEY.public_key())[1]["iss"] == "bot@example.iam.gserviceaccount.com"


def test_a_changed_key_mints_a_new_token(vault, client):
  recipe = {"type": "jwt_bearer", "private_key": "value", "claims": {"scope": "s"}, "exchange": {"kind": "google"}}
  account = {"client_email": "bot@example.iam.gserviceaccount.com", "private_key": _pem(RSA_KEY)}
  vault.put_key("drive", {"value": json.dumps(account), "inject": recipe, "grants": GRANTS})
  assert _use(client, "drive").status_code == 200
  other = {**account, "private_key": _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))}
  vault.put_key("drive", {"value": json.dumps(other)})
  assert _use(client, "drive").status_code == 200
  assert len(_Sites.token_requests) == 2


def test_a_refused_exchange_is_reported_without_the_assertion(vault, client):
  _Sites.refuse = True
  vault.put_key("drive", {"value": _pem(RSA_KEY), "grants": GRANTS, "inject": {
    "type": "jwt_bearer", "private_key": "value", "claims": {"iss": "bot@x", "scope": "s"},
    "exchange": {"kind": "google"}}})
  answer = _use(client, "drive")
  assert answer.status_code == 502
  assert answer.json()["reason"] == "oauth2.googleapis.com refused the token (400): Invalid JWT Signature."
  assert vault.usage(key="drive")[0]["outcome"] == "failed"


def test_a_github_installation_token_is_exchanged_with_the_app_jwt(vault, client):
  vault.put_key("gh", {"value": _pem(RSA_KEY), "grants": GRANTS, "inject": {
    "type": "jwt_bearer", "private_key": "value", "claims": {"iss": "Iv1.client"},
    "exchange": {"kind": "github", "installation_id": "42"}}})
  answer = _use(client, "gh")
  assert answer.status_code == 200, answer.text
  assert answer.json()["headers"]["Authorization"] == "Bearer [redacted by key vault]"
  [(kind, headers, path)] = _Sites.token_requests
  assert (kind, path) == ("github", "/app/installations/42/access_tokens")
  assert _verified(headers["Authorization"].removeprefix("Bearer "), RSA_KEY.public_key())[1]["iss"] == "Iv1.client"
  assert "api.github.com" in client.reached


def test_a_direct_token_is_sent_and_redacted(vault, client):
  vault.put_key("asc", {"value": _pem(EC_KEY), "grants": GRANTS, "inject": {
    "type": "jwt_bearer", "private_key": "value", "alg": "ES256", "key_id": "KEY1",
    "claims": {"iss": "issuer", "aud": "appstoreconnect-v1"}}})
  answer = _use(client, "asc")
  assert answer.status_code == 200
  assert answer.json()["headers"]["Authorization"] == "Bearer [redacted by key vault]"
  assert _Sites.token_requests == []


def test_an_aws_signed_request_reaches_the_site_with_the_secret_redacted(vault, client):
  vault.put_key("bucket", {"grants": GRANTS, "parts": [
    {"name": "id", "value": AWS_ID, "secret": False}, {"name": "secret", "value": AWS_SECRET}],
    "inject": {"type": "aws_sigv4", "access_key_id": "id", "secret_access_key": "secret",
               "region": "auto", "service": "s3"}})
  answer = _use(client, "bucket", method="PUT", content=b"file body")
  assert answer.status_code == 200, answer.text
  echoed = answer.json()["headers"]
  assert echoed["Authorization"].startswith(f"AWS4-HMAC-SHA256 Credential={AWS_ID}/")
  assert echoed["X-Amz-Content-Sha256"] == hashlib.sha256(b"file body").hexdigest()
  assert AWS_SECRET not in answer.text


# -- asking and saving -------------------------------------------------------------

def test_an_agent_asks_for_a_service_account_key_and_the_card_gets_a_file_box(vault):
  vault.request_for_agents({
    "key": "drive", "reason": "Read your Drive", "sites": [SITE],
    "inject": {"type": "jwt_bearer", "private_key": "account", "claims": {"scope": "drive.readonly"},
               "exchange": {"kind": "google"}},
    "parts": [{"name": "account", "label": "Service account file", "multiline": True}],
  }, actor=AGENT)
  [request] = vault.list_requests()
  assert request["parts"] == [{"name": "account", "label": "Service account file", "secret": True, "multiline": True}]
  account = json.dumps({"client_email": "bot@example.iam.gserviceaccount.com", "private_key": _pem(RSA_KEY)})
  vault.approve_request(request["id"], {"ticket": request["ticket"], "sites": [SITE], "parts": {"account": account}})
  [key] = vault.list_keys()
  assert key["inject"]["exchange"] == {"kind": "google"}
  # The JSON file is one line (its key's line breaks are escaped), so the stored part isn't multiline.
  assert key["parts"] == [{"name": "account", "label": "Service account file", "secret": True, "multiline": False}]


def test_a_plain_part_cannot_be_a_key_file(vault):
  with pytest.raises(kv.VaultError, match="Only a secret part"):
    vault.request_for_agents({"key": "x", "reason": "r", "sites": [SITE],
                              "inject": {"type": "jwt_bearer", "private_key": "pem", "claims": {"iss": "i"}},
                              "parts": [{"name": "pem", "secret": False, "multiline": True}]}, actor=AGENT)


def test_store_key_takes_a_whole_recipe(monkeypatch, vault):
  import io
  spec = importlib.util.spec_from_file_location("mobius_store_key_signing", Path(__file__).parents[1] / "scripts" / "store-key.py")
  store_key = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(store_key)
  short = Path(tempfile.mkdtemp(prefix="kvk-"))
  server = kv.VaultServer(str(short / kv.SOCKET_NAME), type("StoreKeyVault", (kv.VaultHandler,), {"vault": vault}))
  threading.Thread(target=server.serve_forever, daemon=True).start()
  recipe = {"type": "aws_sigv4", "access_key_id": "id", "secret_access_key": "secret", "region": "auto", "service": "s3"}
  try:
    monkeypatch.setattr(store_key, "VAULT_SOCKET", short / kv.SOCKET_NAME)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"id": AWS_ID, "secret": AWS_SECRET})))
    assert store_key.main(["r2", "--part", "id:plain:Access key id", "--part", "secret::Secret access key",
                           "--recipe", json.dumps(recipe)]) == 0
  finally:
    server.shutdown()
    server.server_close()
    shutil.rmtree(short, ignore_errors=True)
  [key] = vault.list_keys()
  assert key["inject"] == recipe
  assert [(p["name"], p["secret"]) for p in key["parts"]] == [("id", False), ("secret", True)]
