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


def test_version_exposes_build_date(client, monkeypatch):
  # The commit date powering the Settings "version · date" line. Always a
  # string ("unknown" when unstamped); reads BUILD_DATE from the env.
  body = client.get("/api/version").json()
  assert isinstance(body.get("build_date"), str) and body["build_date"]
  monkeypatch.setenv("BUILD_DATE", "2026-06-25")
  assert Settings(**_KW).build_date == "2026-06-25"
  monkeypatch.delenv("BUILD_DATE", raising=False)
  assert Settings(**_KW).build_date == "unknown"


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


# ── served-platform identity ────────────────────────────────────────────
# The `sha` field is the IMAGE build sha, which advances on every recreate
# whether or not /data/platform synced. deploy-prod.sh's verify block now
# consumes these four fields to catch the "deployed but never served" false-
# green (a new image still serving the previous deploy's /data/platform Python).
# These pin the field shapes the deploy keys on: string serving_source, the
# .baked-sha passthrough, and platform_sha/platform_dirty only when serving
# from the platform layer.

_SENTINEL = "/tmp/serving-source"


def _baked_sha_path():
  from pathlib import Path

  from app.config import get_settings

  return Path(get_settings().data_dir) / "platform" / ".baked-sha"


def test_version_always_includes_served_platform_keys(client):
  # The deploy reads these unconditionally; a missing key would make the
  # extractor return empty and silently disable the served-platform assertion.
  body = client.get("/api/version").json()
  for key in ("serving_source", "platform_sha", "platform_dirty", "baked_sha"):
    assert key in body


def test_version_includes_served_frontend_identity(client):
  # served_frontend is the identity of the frontend bundle ACTUALLY served
  # (hash of the served index.html), the frontend analogue of served_sha —
  # distinct from the legacy shell_sha which points at the unserved
  # /data/shell/dist under the whole-repo model. frontend_source names the live
  # tree. Both must always be present; served_frontend is a str hash or None
  # (None when the served dir has no index.html, e.g. a bare test env).
  body = client.get("/api/version").json()
  assert "served_frontend" in body and "frontend_source" in body
  assert body["frontend_source"] in ("platform", "baked")
  assert body["served_frontend"] is None or isinstance(
    body["served_frontend"], str
  )


def test_served_platform_degrades_when_unstamped(client):
  # No sentinel + no .baked-sha (a plain instance) ⇒ everything degrades to a
  # serializable default rather than raising or 500-ing the endpoint.
  import os

  baked = _baked_sha_path()
  baked.unlink(missing_ok=True)
  existed = os.path.exists(_SENTINEL)
  prior = None
  if existed:
    with open(_SENTINEL, encoding="utf-8") as fh:
      prior = fh.read()
    os.remove(_SENTINEL)
  try:
    body = client.get("/api/version").json()
    assert body["serving_source"] == "unknown"
    assert body["platform_sha"] is None
    assert body["platform_dirty"] is None
    assert body["baked_sha"] is None
  finally:
    if prior is not None:
      with open(_SENTINEL, "w", encoding="utf-8") as fh:
        fh.write(prior)


def test_served_platform_baked_sha_reflects_file(client):
  # recovery_restore.sh stamps .baked-sha = BUILD_SHA on a platform-baked
  # restore; the deploy compares this to the commit it just built.
  baked = _baked_sha_path()
  baked.parent.mkdir(parents=True, exist_ok=True)
  baked.write_text("abc123def456\n", encoding="utf-8")
  try:
    body = client.get("/api/version").json()
    assert body["baked_sha"] == "abc123def456"
  finally:
    baked.unlink(missing_ok=True)


def test_served_platform_source_reflects_sentinel(client):
  # The entrypoint writes /tmp/serving-source = platform|baked at boot. When it
  # says "baked" (image floor, not /data/platform), the git-derived platform_sha
  # / platform_dirty stay None — they're only meaningful for the platform layer.
  import os

  existed = os.path.exists(_SENTINEL)
  prior = None
  if existed:
    with open(_SENTINEL, encoding="utf-8") as fh:
      prior = fh.read()
  try:
    with open(_SENTINEL, "w", encoding="utf-8") as fh:
      fh.write("baked\n")
    body = client.get("/api/version").json()
    assert body["serving_source"] == "baked"
    assert body["platform_sha"] is None
    assert body["platform_dirty"] is None
  finally:
    if prior is not None:
      with open(_SENTINEL, "w", encoding="utf-8") as fh:
        fh.write(prior)
    else:
      try:
        os.remove(_SENTINEL)
      except OSError:
        pass
