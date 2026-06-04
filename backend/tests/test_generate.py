# backend/tests/test_generate.py
import base64
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
  """GET /api/chats/{id}/generated/{filename} serves the saved file."""
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

  from app.auth import create_access_token
  token = create_access_token({"sub": "test"})
  serve_res = client.get(
    f"/api/chats/{chat.id}/generated/{filename}",
    params={"token": token},
  )
  assert serve_res.status_code == 200
  assert serve_res.content == b"fake-png-bytes"
