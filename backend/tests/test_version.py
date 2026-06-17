"""GET /api/version — the backend build-identity stamp.

The SHA is baked at `docker build` time via the BUILD_SHA build-arg (Dockerfile
+ deploy-prod.sh) and surfaced here so a deploy can confirm the SERVED backend
matches the intended commit (the backend analogue of the frontend bundle-hash
check). These pin the endpoint contract + the config wiring in both directions.
"""

from app.config import Settings

# A throwaway secret so a fresh Settings() validates without touching the
# process env; `_env_file=None` keeps the repo .env out of the unit so the
# only BUILD_SHA source under test is the (monkeypatched) process env.
_KW = {"secret_key": "x" * 32, "_env_file": None}


def test_version_endpoint_exposes_build_sha(client):
  r = client.get("/api/version")
  assert r.status_code == 200
  body = r.json()
  # Always a non-empty string: "unknown" when unstamped (local/test/CI) or the
  # baked commit in a deployed image. A missing/misnamed field breaks this.
  assert isinstance(body.get("sha"), str) and body["sha"]


def test_build_sha_defaults_to_unknown(monkeypatch):
  monkeypatch.delenv("BUILD_SHA", raising=False)
  assert Settings(**_KW).build_sha == "unknown"


def test_build_sha_reads_env(monkeypatch):
  # Proves the Dockerfile `ENV BUILD_SHA=...` actually flows to the field
  # (pydantic-settings binds it case-insensitively, no prefix).
  monkeypatch.setenv("BUILD_SHA", "abc123def")
  assert Settings(**_KW).build_sha == "abc123def"


def _marker_path():
  """The served-shell build marker entrypoint.sh / deploy-prod.sh stamp.

  Derived from the live settings' data_dir (the test conftest points
  DATA_DIR at a tempdir), so the test reads exactly what the endpoint does.
  """
  from pathlib import Path

  from app.config import get_settings

  return Path(get_settings().data_dir) / "shell" / ".image-build-sha"


def test_version_shell_sha_unknown_without_marker(client):
  # No /data/shell/.image-build-sha (a plain instance or one predating the
  # marker) ⇒ shell_sha falls back to "unknown" rather than erroring.
  marker = _marker_path()
  marker.unlink(missing_ok=True)
  body = client.get("/api/version").json()
  assert body["shell_sha"] == "unknown"


def test_version_shell_sha_reflects_marker(client):
  # The endpoint surfaces the served-shell build identity stamped by the
  # entrypoint's image-update refresh / deploy-prod, so a client can compare
  # it against `sha` to detect a served UI that lags the installed image.
  marker = _marker_path()
  marker.parent.mkdir(parents=True, exist_ok=True)
  marker.write_text("deadbeefcafe\n", encoding="utf-8")
  try:
    body = client.get("/api/version").json()
    assert body["shell_sha"] == "deadbeefcafe"
  finally:
    marker.unlink(missing_ok=True)
