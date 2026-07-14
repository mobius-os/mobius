"""Tests for the model registry (providers.list_models) — the picker's
source of truth for which models each provider offers.

Two gaps this guards:

- The Claude /v1/models fetch must refresh an EXPIRED OAuth access token
  instead of 401ing. Before the fix, `_fetch_claude_models` read
  `accessToken` verbatim and never checked `expiresAt`, so once the CLI's
  token expired every call 401'd and the picker silently fell back to the
  static KNOWN_MODELS list forever. We now refresh via the refresh-token
  grant and write the result back.

- The KNOWN_MODELS fallback (served whenever the live fetch fails) must
  list the current model ids so a fresh container with no working live
  fetch still offers today's models, not a stale snapshot.
"""

import json
import stat
import time

import httpx
import pytest

from app import providers


# --- KNOWN_MODELS fallback is current ---------------------------------


def test_known_models_fallback_lists_current_claude_and_codex():
  """The static fallback (used on any live-fetch failure) must include each
  current canonical model id, so a container that can't reach the live
  endpoint still offers today's models — not a stale snapshot.

  These assert PRESENCE of the known-current ids rather than freezing an
  exact list: the registry is meant to grow (new dated aliases get appended
  as Anthropic ships them), and an exact-match would force a test edit on
  every additive bump. But a missing or renamed CANONICAL id (e.g. a typo'd
  Opus suffix or a dropped default) is a real regression the prefix-only
  checks would have waved through, so each current id is named explicitly."""
  claude = providers.KNOWN_MODELS["claude"]
  codex = providers.KNOWN_MODELS["codex"]
  # Current Claude family — each canonical id must be present by name. The
  # dated Haiku/Sonnet/Opus ids are the literal entries in KNOWN_MODELS;
  # asserting them by name catches a wrong date suffix that a startswith
  # check would miss.
  for model_id in (
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-sonnet-4-7-20251215",
    "claude-sonnet-4-5-20251001",
    "claude-haiku-4-5-20251001",
  ):
    assert model_id in claude, f"{model_id} missing from KNOWN_MODELS[claude]"
  assert claude[0] == "claude-opus-4-8", "Opus 4.8 must be the default"
  # Current Codex family — each canonical id present by name.
  for model_id in (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
  ):
    assert model_id in codex, f"{model_id} missing from KNOWN_MODELS[codex]"
  assert codex[0] == "gpt-5.6-sol", "gpt-5.6-sol must be the Codex default"


def test_fallback_models_shape_matches_registry_entries():
  """`_fallback_models` returns the same {id,label,provider,available}
  shape the live path produces, so the picker renders identically whether
  the data came live or from the fallback."""
  entries = providers._fallback_models("claude")
  assert entries, "fallback must be non-empty for a known provider"
  for e in entries:
    assert set(e) == {"id", "label", "provider", "available"}
    assert e["provider"] == "claude"
    assert e["available"] is True
  ids = [e["id"] for e in entries]
  assert ids == providers.KNOWN_MODELS["claude"], "order preserved"


def test_model_specific_effort_levels_are_registry_metadata(monkeypatch):
  """A future model can declare a different effort scale in one backend map;
  pickers receive it without model-id conditionals in the frontend."""
  monkeypatch.setitem(
    providers.MODEL_EFFORT_LEVELS,
    "claude-opus-4-8",
    ["low", "medium", "high", "max"],
  )
  entry = next(
    row for row in providers._fallback_models("claude")
    if row["id"] == "claude-opus-4-8"
  )
  assert entry["effort_levels"] == ["low", "medium", "high", "max"]


# --- Expired-token refresh (the 401 root cause) -----------------------


def _write_creds(tmp_path, *, access, refresh, expires_at):
  cli = tmp_path / "cli-auth" / "claude"
  cli.mkdir(parents=True, exist_ok=True)
  (cli / ".credentials.json").write_text(json.dumps({
    "claudeAiOauth": {
      "accessToken": access,
      "refreshToken": refresh,
      "expiresAt": expires_at,
      "scopes": ["user:inference"],
      "email": "owner@example.test",
    }
  }))
  return cli / ".credentials.json"


def _install_mock_transport(monkeypatch, handler):
  """Patch providers' httpx.AsyncClient so every request in this module is
  served by `handler` (an httpx.MockTransport route) — no network, no
  respx dependency."""
  real_async_client = httpx.AsyncClient

  def factory(*args, **kwargs):
    kwargs["transport"] = httpx.MockTransport(handler)
    return real_async_client(*args, **kwargs)

  monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_access_token_returned_verbatim_when_fresh(tmp_path):
  """A token with comfortable life left is used as-is — no refresh call."""
  future = int(time.time() * 1000) + 60 * 60 * 1000  # +1h
  _write_creds(tmp_path, access="fresh-tok", refresh="r", expires_at=future)
  token = await providers._claude_access_token(str(tmp_path))
  assert token == "fresh-tok"


@pytest.mark.asyncio
async def test_expired_token_is_refreshed_and_persisted(tmp_path, monkeypatch):
  """An expired access token triggers a refresh-token grant; the new token
  is returned AND written back so the chat path / next call reuse it."""
  past = int(time.time() * 1000) - 1000  # already expired
  creds_path = _write_creds(
    tmp_path, access="stale-tok", refresh="refresh-tok-A", expires_at=past,
  )

  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["url"] = str(request.url)
    captured["body"] = json.loads(request.content)
    return httpx.Response(200, json={
      "access_token": "new-access-tok",
      "refresh_token": "refresh-tok-B",  # endpoint rotates it
      "expires_in": 3600,
      "scope": "user:inference user:profile",
    })

  _install_mock_transport(monkeypatch, handler)

  token = await providers._claude_access_token(str(tmp_path))
  assert token == "new-access-tok"
  # Correct refresh-grant shape sent upstream.
  assert captured["url"] == providers._CLAUDE_OAUTH_TOKEN_URL
  assert captured["body"]["grant_type"] == "refresh_token"
  assert captured["body"]["refresh_token"] == "refresh-tok-A"
  assert captured["body"]["client_id"] == providers._CLAUDE_OAUTH_CLIENT_ID
  # Persisted back in CLI shape, including the rotated refresh token.
  saved = json.loads(creds_path.read_text())["claudeAiOauth"]
  assert saved["accessToken"] == "new-access-tok"
  assert saved["refreshToken"] == "refresh-tok-B"
  assert saved["expiresAt"] > int(time.time() * 1000)


@pytest.mark.asyncio
async def test_refresh_preserves_sibling_credential_keys(tmp_path, monkeypatch):
  """A registry-path refresh must NOT drop other top-level keys in the
  credentials file. The host CLI's .credentials.json — shipped into the
  container via `docker cp ~/.claude/.credentials.json` — carries mcpOAuth
  (MCP server OAuth) and organizationUuid alongside claudeAiOauth. The write
  is read-modify-write, replacing only claudeAiOauth, so those survive."""
  past = int(time.time() * 1000) - 1000
  cli = tmp_path / "cli-auth" / "claude"
  cli.mkdir(parents=True, exist_ok=True)
  creds_path = cli / ".credentials.json"
  creds_path.write_text(json.dumps({
    "claudeAiOauth": {
      "accessToken": "stale-tok",
      "refreshToken": "refresh-tok-A",
      "expiresAt": past,
      "scopes": ["user:inference"],
    },
    "mcpOAuth": {"some-server": {"accessToken": "mcp-tok", "expiresAt": 123}},
    "organizationUuid": "org-1234-abcd",
  }))
  creds_path.chmod(0o600)

  def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={
      "access_token": "new-access-tok",
      "refresh_token": "refresh-tok-B",
      "expires_in": 3600,
    })

  _install_mock_transport(monkeypatch, handler)

  token = await providers._claude_access_token(str(tmp_path))
  assert token == "new-access-tok"

  saved = json.loads(creds_path.read_text())
  # The refreshed block landed.
  assert saved["claudeAiOauth"]["accessToken"] == "new-access-tok"
  assert saved["claudeAiOauth"]["refreshToken"] == "refresh-tok-B"
  # The sibling keys survived the refresh+persist round-trip.
  assert saved["mcpOAuth"] == {
    "some-server": {"accessToken": "mcp-tok", "expiresAt": 123}
  }
  assert saved["organizationUuid"] == "org-1234-abcd"
  # The 0600 file mode is preserved across the atomic rewrite.
  assert stat.S_IMODE(creds_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_fetch_claude_models_uses_refreshed_token(tmp_path, monkeypatch):
  """End-to-end: an expired token does NOT 401 the models fetch — it
  refreshes first, then the /v1/models GET carries the new token and the
  live id list comes back (not the static fallback)."""
  past = int(time.time() * 1000) - 1000
  _write_creds(
    tmp_path, access="stale", refresh="refresh-tok", expires_at=past,
  )

  def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/oauth/token"):
      return httpx.Response(200, json={
        "access_token": "live-tok",
        "refresh_token": "refresh-tok-2",
        "expires_in": 3600,
      })
    # /v1/models — must carry the refreshed token, else 401.
    assert request.headers["authorization"] == "Bearer live-tok"
    return httpx.Response(200, json={"data": [
      {"id": "claude-opus-4-8"},
      {"id": "claude-some-future-model"},
    ]})

  _install_mock_transport(monkeypatch, handler)

  ids = await providers._fetch_claude_models(str(tmp_path))
  assert "claude-opus-4-8" in ids
  assert "claude-some-future-model" in ids


@pytest.mark.asyncio
async def test_fetch_claude_models_raises_when_refresh_fails(
  tmp_path, monkeypatch
):
  """When the refresh itself fails (e.g. revoked refresh token → 400), the
  fetch raises so list_models serves the KNOWN_MODELS fallback rather than
  propagating the error — the picker stays usable."""
  past = int(time.time() * 1000) - 1000
  _write_creds(tmp_path, access="stale", refresh="dead", expires_at=past)

  def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(400, json={"error": "invalid_grant"})

  _install_mock_transport(monkeypatch, handler)

  with pytest.raises(httpx.HTTPStatusError):
    await providers._fetch_claude_models(str(tmp_path))

  # The fallback path still yields the current KNOWN_MODELS list.
  fallback = providers._fallback_models("claude")
  assert any(e["id"] == "claude-opus-4-8" for e in fallback)


def test_codex_model_slugs_from_raw_cli_catalog():
  """The CLI debug fallback parses raw catalog JSON without caring about
  newer metadata fields such as max/ultra reasoning levels."""
  payload = {
    "models": [
      {
        "slug": "gpt-5.6-sol",
        "supported_reasoning_levels": [
          {"effort": "medium"},
          {"effort": "max"},
          {"effort": "ultra"},
        ],
      },
      {"id": "gpt-future"},
      {"slug": "codex-auto-review", "visibility": "hide"},
      "gpt-string-shape",
    ],
  }
  assert providers._codex_model_slugs_from_payload(payload) == [
    "gpt-5.6-sol",
    "gpt-future",
    "gpt-string-shape",
  ]
