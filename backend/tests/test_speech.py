"""Speech stays app-scoped and streams the isolated official runtime."""

from pathlib import Path

from app import speech_runtime
from app.routes import speech as speech_module
from test_app_fixtures import create_local_app


def _app_and_token(client, owner_token, name):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, owner_auth, name=name)["id"]
  token = client.post(
    "/api/auth/app-token",
    json={"app_id": app_id},
    headers=owner_auth,
  ).json()["token"]
  return app_id, token


class _Upstream:
  status_code = 200

  async def aiter_raw(self):
    yield b"RIFF"
    yield b"speech"

  async def aclose(self):
    return None


class _Client:
  def __init__(self, *args, **kwargs):
    pass

  def build_request(self, method, url, data):
    return {"method": method, "url": url, "data": data}

  async def send(self, request, stream):
    assert request["url"] == "http://127.0.0.1:8791/tts"
    assert request["data"]["voice_url"] == "alba"
    assert stream is True
    return _Upstream()

  async def aclose(self):
    return None


def test_app_can_stream_its_own_speech(client, owner_token, monkeypatch):
  app_id, token = _app_and_token(client, owner_token, "news-speech")
  monkeypatch.setattr(
    speech_module, "ensure_runtime", lambda language: "http://127.0.0.1:8791",
  )
  monkeypatch.setattr(speech_module.httpx, "AsyncClient", _Client)

  response = client.post(
    f"/api/apps/{app_id}/speech",
    json={"text": "Today in the news.", "language": "english"},
    headers={"Authorization": f"Bearer {token}"},
  )

  assert response.status_code == 200, response.text
  assert response.content == b"RIFFspeech"
  assert response.headers["content-type"].startswith("audio/wav")
  assert response.headers["cache-control"] == "no-store"


def test_app_token_cannot_spend_speech_cpu_for_another_app(
  client, owner_token, monkeypatch,
):
  first_id, first_token = _app_and_token(client, owner_token, "first-speech")
  second_id, _ = _app_and_token(client, owner_token, "second-speech")
  called = []
  monkeypatch.setattr(
    speech_module, "ensure_runtime", lambda language: called.append(language),
  )

  response = client.post(
    f"/api/apps/{second_id}/speech",
    json={"text": "No.", "language": "english"},
    headers={"Authorization": f"Bearer {first_token}"},
  )

  assert first_id != second_id
  assert response.status_code == 403
  assert called == []


def test_unsupported_language_is_rejected_before_runtime_start(
  client, owner_token, monkeypatch,
):
  app_id, token = _app_and_token(client, owner_token, "language-speech")
  called = []
  monkeypatch.setattr(
    speech_module, "ensure_runtime", lambda language: called.append(language),
  )

  response = client.post(
    f"/api/apps/{app_id}/speech",
    json={"text": "Hello.", "language": "klingon"},
    headers={"Authorization": f"Bearer {token}"},
  )

  assert response.status_code == 400
  assert called == []


def test_first_use_installs_a_pinned_runtime_atomically(tmp_path, monkeypatch):
  root = tmp_path / "pocket-tts"
  commands = []

  def fake_run(command, **kwargs):
    commands.append(command)
    if command[1:3] == ["-m", "venv"]:
      python = Path(command[-1]) / "bin" / "python"
      python.parent.mkdir(parents=True)
      python.write_text("", encoding="utf-8")

  monkeypatch.setattr(speech_runtime, "_root", lambda: root)
  monkeypatch.setattr(speech_runtime.subprocess, "run", fake_run)

  speech_runtime._ensure_installed()

  assert (root / "venv" / "bin" / "python").is_file()
  assert commands[1][-1] == "pocket-tts==2.1.0"
  assert not (root / "venv.installing").exists()
