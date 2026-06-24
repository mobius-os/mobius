"""Traversal safety + token validation for the published-site serve route (136).

GET /sites/<token>/<path> serves a published static snapshot from
DATA_DIR/published/<token>/. The token is a hex slug and the resolved file path
must stay confined to the token dir — a traversal path is a 404, never an escape.
"""

import os

import pytest
from fastapi import HTTPException

from app.routes.published import _serve

_DATA_DIR = os.environ.get("DATA_DIR", "/tmp")
_TOKEN = "a" * 32


def _publish(token, rel, body):
  from pathlib import Path
  p = Path(_DATA_DIR) / "published" / token / rel
  p.parent.mkdir(parents=True, exist_ok=True)
  p.write_text(body, encoding="utf-8")


def test_serves_index_and_nested(tmp_path_factory):
  _publish(_TOKEN, "index.html", "<h1>live</h1>")
  _publish(_TOKEN, "css/app.css", "body{}")
  assert _serve(_TOKEN, "").status_code == 200
  assert _serve(_TOKEN, "index.html").status_code == 200
  assert _serve(_TOKEN, "css/app.css").status_code == 200


def test_traversal_is_confined():
  _publish(_TOKEN, "index.html", "<h1>live</h1>")
  from pathlib import Path
  # A secret OUTSIDE the token dir must be unreachable via traversal.
  (Path(_DATA_DIR) / "published" / "secret.txt").write_text("nope", encoding="utf-8")
  # Traversal escapes the token dir → it must NOT serve secret.txt. Either a
  # 404, or the SPA fallback to THIS site's index.html — never the secret.
  try:
    resp = _serve(_TOKEN, "../secret.txt")
  except HTTPException as e:
    assert e.status_code == 404
  else:
    # If it resolved, it must be the confined index.html, not the secret.
    assert "secret.txt" not in str(resp.path)


def test_bad_token_404():
  with pytest.raises(HTTPException) as ei:
    _serve("not-a-hex-token!", "")
  assert ei.value.status_code == 404


def test_unknown_token_404():
  with pytest.raises(HTTPException) as ei:
    _serve("b" * 32, "")
  assert ei.value.status_code == 404
