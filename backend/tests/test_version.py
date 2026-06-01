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
