# backend/tests/test_generate.py
import base64
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch


FAKE_PNG = base64.b64encode(b"fake-png-bytes").decode()

GEMINI_RESPONSE = {
  "candidates": [{
    "content": {
      "parts": [
        {"text": "Here is your image."},
        {"inlineData": {"mimeType": "image/png", "data": FAKE_PNG}},
      ]
    }
  }]
}


def _set_gemini_key(client, auth):
  client.post(
    "/api/settings",
    json={"gemini_api_key": "AIzaFakeKey"},
    headers=auth,
  )


def test_generate_image_no_key(client, db, auth, chat):
  """Without a Gemini key, generate-image must return 503."""
  res = client.post(
    f"/api/chats/{chat.id}/generate-image",
    json={"prompt": "a cat"},
    headers=auth,
  )
  assert res.status_code == 503


def test_generate_image_rejects_cross_site_request(client, auth, chat):
  cross = client.post(
    f"/api/chats/{chat.id}/generate-image",
    json={"prompt": "a cat"},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_generate_image_returns_url(client, db, auth, chat):
  """With a valid key and mocked Gemini response, must return an image URL."""
  _set_gemini_key(client, auth)

  mock_response = MagicMock()
  mock_response.status_code = 200
  mock_response.json.return_value = GEMINI_RESPONSE

  with patch("app.routes.generate.httpx.AsyncClient") as MockClient:
    instance = AsyncMock()
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    instance.post = AsyncMock(return_value=mock_response)
    MockClient.return_value = instance

    res = client.post(
      f"/api/chats/{chat.id}/generate-image",
      json={"prompt": "a sunset", "aspect_ratio": "16:9"},
      headers=auth,
    )

  assert res.status_code == 200
  body = res.json()
  assert "url" in body
  assert f"/api/chats/{chat.id}/generated/" in body["url"]


def test_serve_generated_image(client, db, auth, chat):
  """GET /api/chats/{id}/generated/{filename} serves the saved file.

  Uses a media token on ?token= (owner JWTs are rejected on that path).
  """
  _set_gemini_key(client, auth)

  mock_response = MagicMock()
  mock_response.status_code = 200
  mock_response.json.return_value = GEMINI_RESPONSE

  with patch("app.routes.generate.httpx.AsyncClient") as MockClient:
    instance = AsyncMock()
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    instance.post = AsyncMock(return_value=mock_response)
    MockClient.return_value = instance

    gen_res = client.post(
      f"/api/chats/{chat.id}/generate-image",
      json={"prompt": "a mountain"},
      headers=auth,
    )

  url = gen_res.json()["url"]
  filename = url.split("/")[-1].split("?")[0]

  media_token_r = client.post(
    f"/api/chats/{chat.id}/media-token",
    headers=auth,
  )
  media_token = media_token_r.json()["token"]
  serve_res = client.get(
    f"/api/chats/{chat.id}/generated/{filename}",
    params={"token": media_token},
  )
  assert serve_res.status_code == 200
  assert serve_res.content == b"fake-png-bytes"


def test_generate_rejects_non_uuid_chat_id(client, auth):
  """POST /api/chats/{id}/generate-image with a non-UUID4 chat_id must 400 (Task 2)."""
  res = client.post(
    "/api/chats/not-a-uuid/generate-image",
    json={"prompt": "a cat"},
    headers=auth,
  )
  assert res.status_code == 400


def test_serve_generated_rejects_non_uuid_chat_id(client, auth):
  """GET /api/chats/{id}/generated/{file} with a non-UUID4 chat_id must 400 (Task 2)."""
  from app.auth import create_access_token
  token = create_access_token({"sub": "test"})
  res = client.get(
    "/api/chats/not-a-uuid/generated/some.png",
    params={"token": token},
  )
  assert res.status_code == 400


def test_generate_image_dir_cap_enforced(client, db, auth, chat, monkeypatch):
  """generate-image must return 413 when the per-chat generated dir is full (Task 8)."""
  import sys
  _set_gemini_key(client, auth)

  mock_response = MagicMock()
  mock_response.status_code = 200
  mock_response.json.return_value = GEMINI_RESPONSE

  # Patch the cap to a tiny value so the test doesn't write a real 100 MB.
  for mod in list(sys.modules.values()):
    if getattr(mod, "__name__", "") == "app.routes.generate":
      monkeypatch.setattr(mod, "_MAX_CHAT_GENERATED_BYTES", 1, raising=False)
  ep = next(
    (r.endpoint for r in client.app.routes
     if getattr(r, "path", None) == "/api/chats/{chat_id}/generate-image"),
    None,
  )
  if ep is not None:
    monkeypatch.setitem(ep.__globals__, "_MAX_CHAT_GENERATED_BYTES", 1)

  with patch("app.routes.generate.httpx.AsyncClient") as MockClient:
    instance = AsyncMock()
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    instance.post = AsyncMock(return_value=mock_response)
    MockClient.return_value = instance

    res = client.post(
      f"/api/chats/{chat.id}/generate-image",
      json={"prompt": "overflow"},
      headers=auth,
    )

  assert res.status_code == 413
  assert "full" in res.json()["detail"].lower() or "limit" in res.json()["detail"].lower()
