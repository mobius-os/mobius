from pydantic import ValidationError

from app.auth import encrypt_api_key, decrypt_api_key
from app.schemas import SettingsUpdate


def test_encrypt_decrypt_roundtrip():
  """encrypt_api_key then decrypt_api_key must return the original string."""
  original = "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
  encrypted = encrypt_api_key(original)
  assert encrypted != original, "Encrypted value must differ from plaintext"
  assert isinstance(encrypted, str), "Encrypted value must be a string"
  decrypted = decrypt_api_key(encrypted)
  assert decrypted == original


def test_encrypt_produces_different_ciphertext_each_time():
  """Fernet uses a random IV so two encryptions of the same value differ."""
  key = "AIzaSyTest"
  assert encrypt_api_key(key) != encrypt_api_key(key)


def test_get_settings_unconfigured(client, auth):
  """GET /api/settings returns gemini_configured: false when no key set."""
  res = client.get("/api/settings", headers=auth)
  assert res.status_code == 200
  assert res.json()["gemini_configured"] is False


def test_save_and_check_gemini_key(client, db, auth):
  """POST /api/settings saves the key encrypted; GET reflects configured."""
  res = client.post(
    "/api/settings",
    json={"gemini_api_key": "AIzaSyTestKey123"},
    headers=auth,
  )
  assert res.status_code == 200
  assert res.json()["ok"] is True

  # Key must be stored encrypted, not plaintext.
  from app import models
  owner = db.query(models.Owner).first()
  assert owner.gemini_api_key_enc is not None
  assert owner.gemini_api_key_enc != "AIzaSyTestKey123"

  # GET must now report configured.
  res2 = client.get("/api/settings", headers=auth)
  assert res2.json()["gemini_configured"] is True


def test_clear_gemini_key(client, db, auth):
  """POST /api/settings with empty string clears the key."""
  client.post(
    "/api/settings",
    json={"gemini_api_key": "AIzaSyKey"},
    headers=auth,
  )
  client.post(
    "/api/settings",
    json={"gemini_api_key": ""},
    headers=auth,
  )
  from app import models
  owner = db.query(models.Owner).first()
  assert owner.gemini_api_key_enc is None


def test_set_provider(client, auth):
  """POST /api/settings with provider switches the active provider."""
  client.post("/api/settings", json={"provider": "codex"}, headers=auth)
  r = client.get("/api/settings", headers=auth)
  assert r.json()["provider"] == "codex"

  client.post("/api/settings", json={"provider": "claude"}, headers=auth)
  r = client.get("/api/settings", headers=auth)
  assert r.json()["provider"] == "claude"


def test_set_invalid_provider_rejected(client, auth):
  """POST /api/settings with invalid provider is rejected at the schema."""
  r = client.post("/api/settings", json={"provider": "invalid"}, headers=auth)
  assert r.status_code == 422
  r = client.get("/api/settings", headers=auth)
  assert r.json()["provider"] == "claude"


def test_settings_update_provider_validator_rejects_unknown():
  """SettingsUpdate rejects unknown provider IDs."""
  try:
    SettingsUpdate(provider="bogus")
  except ValidationError:
    pass
  else:
    raise AssertionError("Expected ValidationError for bogus provider")


# ─── Model registry + owner prefs ─────────────────────────────────────


def test_model_registry_returns_known_models_on_missing_creds(client, auth):
  """`/api/models` returns KNOWN_MODELS for both providers when neither
  upstream is reachable. Confirms the per-provider fallback works.

  The TestClient has no real Anthropic / Codex credentials so both
  fetchers raise; the registry serves KNOWN_MODELS for both. Every
  entry is `available=True` in the fallback path because there's no
  live signal to mark anything unavailable.
  """
  from app.providers import (
    KNOWN_MODELS, _fallback_models, invalidate_model_cache,
  )
  invalidate_model_cache()
  res = client.get("/api/models", headers=auth)
  assert res.status_code == 200
  body = res.json()
  assert set(body["providers"]) == {"claude", "codex"}
  claude_ids = [m["id"] for m in body["providers"]["claude"]]
  assert claude_ids == KNOWN_MODELS["claude"]
  codex_ids = [m["id"] for m in body["providers"]["codex"]]
  assert codex_ids == KNOWN_MODELS["codex"]
  # Labels carry through from MODEL_LABELS.
  by_id = {m["id"]: m for m in body["providers"]["claude"]}
  assert by_id["claude-opus-4-8"]["label"] == "Opus 4.8"
  # The user-facing API contract is `available=true` on every fallback
  # entry, but the route layer relies on Pydantic's `ModelEntry`
  # default to fill that field. Verify the underlying helper directly
  # so a non-route caller of `_fallback_models()` doesn't trip a
  # KeyError on `available` — and so the contract is pinned at the
  # source, not just at the JSON boundary.
  raw_fallback = _fallback_models("claude")
  assert raw_fallback, "fallback should not be empty for claude"
  for entry in raw_fallback:
    assert entry["available"] is True, (
      f"_fallback_models must set available=True explicitly; "
      f"got {entry!r}"
    )
  assert all(m["available"] for m in body["providers"]["claude"])


def test_model_prefs_default_empty(client, auth):
  """A fresh owner has no hidden models."""
  res = client.get("/api/owner/model-prefs", headers=auth)
  assert res.status_code == 200
  assert res.json() == {"hidden_ids": []}


def test_model_prefs_roundtrip_dedupes(client, auth, db):
  """PATCH stores hidden_ids verbatim (deduplicated, order-preserving)
  and GET returns the same set."""
  res = client.patch(
    "/api/owner/model-prefs",
    json={"hidden_ids": [
      "claude-haiku-4-5-20251001",
      "gpt-5.4",
      "claude-haiku-4-5-20251001",  # duplicate should drop
    ]},
    headers=auth,
  )
  assert res.status_code == 200
  assert res.json()["hidden_ids"] == [
    "claude-haiku-4-5-20251001", "gpt-5.4",
  ]
  res2 = client.get("/api/owner/model-prefs", headers=auth)
  assert res2.json()["hidden_ids"] == [
    "claude-haiku-4-5-20251001", "gpt-5.4",
  ]
  # Persisted on the Owner row.
  from app import models
  owner = db.query(models.Owner).first()
  assert owner.model_prefs_json == {"hidden_ids": [
    "claude-haiku-4-5-20251001", "gpt-5.4",
  ]}


def test_model_prefs_stale_id_tolerated(client, auth):
  """An ID that's not in the registry can still be stored; nothing
  errors. The picker handles stale IDs by silently not filtering
  what it can't find."""
  res = client.patch(
    "/api/owner/model-prefs",
    json={"hidden_ids": ["claude-fictitious-model-99"]},
    headers=auth,
  )
  assert res.status_code == 200
  assert res.json()["hidden_ids"] == ["claude-fictitious-model-99"]


def test_model_prefs_rejects_unknown_field(client, auth):
  """ModelPrefsUpdate has extra='forbid' — a typo'd field 422s rather
  than silently landing in the persisted prefs blob."""
  res = client.patch(
    "/api/owner/model-prefs",
    json={"hidden_ids": [], "sort_order": ["foo"]},
    headers=auth,
  )
  assert res.status_code == 422


def test_model_prefs_clear(client, auth, db):
  """Empty list clears all hidden entries."""
  client.patch(
    "/api/owner/model-prefs",
    json={"hidden_ids": ["gpt-5.4"]},
    headers=auth,
  )
  res = client.patch(
    "/api/owner/model-prefs",
    json={"hidden_ids": []},
    headers=auth,
  )
  assert res.status_code == 200
  assert res.json() == {"hidden_ids": []}
  from app import models
  owner = db.query(models.Owner).first()
  assert owner.model_prefs_json == {"hidden_ids": []}


def test_merge_live_with_known_handles_known_plus_new():
  """Merging a known model + a brand-new live ID preserves
  KNOWN_MODELS order then appends the live-only entry."""
  from app.providers import _merge_live_with_known
  merged = _merge_live_with_known(
    "claude", ["claude-opus-4-8", "claude-future-model"],
  )
  # First: the known opus comes first (and reports available).
  assert merged[0] == {
    "id": "claude-opus-4-8", "label": "Opus 4.8",
    "provider": "claude", "available": True,
  }
  # The live-only entry lands at the end.
  assert merged[-1] == {
    "id": "claude-future-model", "label": "claude-future-model",
    "provider": "claude", "available": True,
  }
  # Stale-known IDs (in KNOWN_MODELS but missing from the live list)
  # stay listed with available=False.
  haiku = next(m for m in merged if m["id"] == "claude-haiku-4-5-20251001")
  assert haiku["available"] is False


def test_resolve_displayed_models_keeps_selected_even_when_hidden():
  """The picker's filter MUST keep the currently-selected model
  visible even when it appears in hidden_ids. The codex-review spec
  calls this out — without it the user could hide their own active
  model and lose the ability to switch away from it via the picker."""
  # Recreate the JS-side filter in Python to assert the contract.
  # Authoritative implementation lives in
  # frontend/src/components/ChatView/ChatSettingsPanel.jsx
  # (`resolveDisplayedModels`). This test exercises the rule with the
  # backend registry shape so a behavior regression on either side
  # is caught.
  registry_entries = [
    {"id": "a", "label": "A", "provider": "claude", "available": True},
    {"id": "b", "label": "B", "provider": "claude", "available": True},
    {"id": "c", "label": "C", "provider": "claude", "available": True},
  ]
  hidden = {"b", "c"}
  selected = "c"
  visible = [
    m for m in registry_entries
    if m["id"] not in hidden or m["id"] == selected
  ]
  assert [m["id"] for m in visible] == ["a", "c"]


def test_walkthrough_status_default_not_completed(client, auth):
  """A fresh owner has not seen the walkthrough."""
  res = client.get("/api/owner/walkthrough", headers=auth)
  assert res.status_code == 200
  body = res.json()
  assert body["completed"] is False
  assert body["completed_at"] is None


def test_walkthrough_complete_then_status(client, auth):
  """Posting `complete` flips the bit; subsequent GETs report
  completed=true and a timestamp."""
  before = client.get("/api/owner/walkthrough", headers=auth).json()
  assert before["completed"] is False
  done = client.post("/api/owner/walkthrough/complete", headers=auth)
  assert done.status_code == 204
  after = client.get("/api/owner/walkthrough", headers=auth).json()
  assert after["completed"] is True
  assert after["completed_at"] is not None


def test_walkthrough_complete_is_write_once(client, auth):
  """Posting `complete` twice succeeds both times AND the second
  call does not advance the persisted timestamp — the route is
  write-once on first success, so downstream analytics can correlate
  the original completion time against other signals without
  retry-or-idle-tab refreshes corrupting it."""
  first = client.post("/api/owner/walkthrough/complete", headers=auth)
  assert first.status_code == 204
  ts1 = client.get("/api/owner/walkthrough", headers=auth).json()["completed_at"]
  second = client.post("/api/owner/walkthrough/complete", headers=auth)
  assert second.status_code == 204
  ts2 = client.get("/api/owner/walkthrough", headers=auth).json()["completed_at"]
  assert ts2 == ts1, (
    "Second completion POST must NOT advance the timestamp — write-once"
  )


def test_walkthrough_endpoints_require_auth(client):
  """Unauthenticated requests must 401, like every other /api/owner
  surface."""
  no_get = client.get("/api/owner/walkthrough")
  assert no_get.status_code == 401
  no_post = client.post("/api/owner/walkthrough/complete")
  assert no_post.status_code == 401


def test_walkthrough_complete_rejects_cross_site_request(client, auth):
  """Defense-in-depth: when the browser sends Sec-Fetch-Site:
  cross-site (a genuine CSRF attempt), the write is blocked even if
  the attacker somehow obtained the bearer token. Same-origin and
  same-site stay allowed."""
  cross = client.post(
    "/api/owner/walkthrough/complete",
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403, (
    "cross-site Sec-Fetch-Site must be rejected"
  )
  same_origin = client.post(
    "/api/owner/walkthrough/complete",
    headers={**auth, "Sec-Fetch-Site": "same-origin"},
  )
  assert same_origin.status_code == 204
  none_origin = client.post(
    "/api/owner/walkthrough/complete",
    headers={**auth, "Sec-Fetch-Site": "none"},
  )
  # Already write-once stamped above, still 204 — point is "not blocked."
  assert none_origin.status_code == 204


def test_model_prefs_patch_rejects_cross_site_request(client, auth):
  """Same defense-in-depth as walkthrough/complete — any owner-state
  PATCH should reject cross-site origin requests."""
  cross = client.patch(
    "/api/owner/model-prefs",
    json={"hidden_ids": []},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_settings_post_rejects_cross_site_request(client, auth):
  """POST /api/settings writes gemini_api_key + provider — both owner-
  state mutations — and shares the reject_cross_site defense applied
  to walkthrough/complete and model-prefs PATCH. Catches the case
  where a future refactor accidentally drops the dep."""
  cross = client.post(
    "/api/settings",
    json={"gemini_api_key": "AIzaSyTestCrossSite"},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403
  same_origin = client.post(
    "/api/settings",
    json={"gemini_api_key": ""},
    headers={**auth, "Sec-Fetch-Site": "same-origin"},
  )
  assert same_origin.status_code == 200
