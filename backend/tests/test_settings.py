from app.auth import encrypt_api_key, decrypt_api_key


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


def test_set_invalid_provider_ignored(client, auth):
  """POST /api/settings with invalid provider is silently ignored."""
  client.post("/api/settings", json={"provider": "invalid"}, headers=auth)
  r = client.get("/api/settings", headers=auth)
  assert r.json()["provider"] == "claude"
