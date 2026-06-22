"""POST /api/client-error -> app_error activity events (app-attributed).

Closes the gap where uncaught client/app JS errors were captured only in the
browser (errorLog.js ring buffer) and never reached the activity log, so the
nightly Reflection digest's last_5_errors stayed empty for ~every app.
"""
import json
from pathlib import Path

from app.config import get_settings


def _activity_lines() -> list[dict]:
    p = Path(get_settings().data_dir) / "logs" / "activity.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _make_app(client, owner_token) -> int:
    r = client.post(
        "/api/apps/",
        json={
            "name": "erroring-app",
            "description": "x",
            "jsx_source": "export default function App(){ return <div/> }",
        },
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _app_token(client, owner_token, app_id) -> str:
    r = client.post(
        "/api/auth/app-token",
        json={"app_id": app_id},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_app_token_client_error_records_app_error_attributed_to_the_app(client, owner_token):
    app_id = _make_app(client, owner_token)
    token = _app_token(client, owner_token, app_id)

    r = client.post(
        "/api/client-error",
        json={"message": "TypeError: x is undefined", "where": "window.onerror"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 204, r.text

    errs = [e for e in _activity_lines() if e.get("ev") == "app_error"]
    assert len(errs) == 1, errs
    assert errs[0]["app_id"] == app_id
    assert errs[0]["message"] == "TypeError: x is undefined"
    assert errs[0]["where"] == "window.onerror"


def test_repeated_identical_app_error_is_debounced(client, owner_token):
    app_id = _make_app(client, owner_token)
    token = _app_token(client, owner_token, app_id)
    for _ in range(5):
        r = client.post(
            "/api/client-error",
            json={"message": "Boom: render loop", "where": "render"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 204, r.text
    errs = [e for e in _activity_lines() if e.get("ev") == "app_error"]
    assert len(errs) == 1, f"expected debounce to collapse 5 identical errors to 1, got {len(errs)}"


def test_oversized_message_and_stack_are_truncated(client, owner_token):
    app_id = _make_app(client, owner_token)
    token = _app_token(client, owner_token, app_id)
    r = client.post(
        "/api/client-error",
        json={"message": "X" * 10000, "stack": "S" * 50000, "where": "boundary"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 204, r.text
    errs = [e for e in _activity_lines() if e.get("ev") == "app_error"]
    assert len(errs) == 1
    assert len(errs[0]["message"]) <= 2000
    assert len(errs[0].get("stack", "")) <= 8000


def test_owner_shell_error_records_no_app_id(client, owner_token):
    # An error reported with the owner JWT (the shell, not an app iframe)
    # must NOT carry an app_id, or it would pollute a real app's last_5_errors.
    r = client.post(
        "/api/client-error",
        json={"message": "shell boom", "where": "window.onerror"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert r.status_code == 204, r.text
    errs = [e for e in _activity_lines() if e.get("ev") == "app_error"]
    assert len(errs) == 1
    assert "app_id" not in errs[0]
