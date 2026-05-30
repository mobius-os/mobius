"""The /api/ai proxy must not let an app-scoped (mini-app) token request
tools. The spawned Claude CLI runs with the OWNER's credentials and no cwd
restriction, so tool access for a sandboxed app would be a privilege
escalation (shell / owner-filesystem reads). Tools are owner-only; app
callers get text-only AI. These assertions all hit the 403/400 gate, which
fires BEFORE the stream opens, so no Claude subprocess is spawned."""


def _app_token(client, owner_token):
  r = client.post(
    "/api/apps/",
    json={
      "name": "ai-scope-app",
      "description": "test",
      "jsx_source": "export default function App() { return <div>hi</div> }",
    },
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  app_id = r.json()["id"]
  r = client.post(
    "/api/auth/app-token",
    json={"app_id": app_id},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  return r.json()["token"]


def test_app_token_cannot_request_all_tools(client, owner_token):
  app_token = _app_token(client, owner_token)
  r = client.post(
    "/api/ai",
    json={"messages": [{"role": "user", "content": "hi"}], "tools": True},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 403


def test_app_token_cannot_request_named_tool(client, owner_token):
  app_token = _app_token(client, owner_token)
  # Even read-only tools are denied — with the owner's creds and no cwd,
  # Read/Glob/Grep would expose the owner's filesystem to a sandboxed app.
  for tool in ("Bash", "Write", "Edit", "Read"):
    r = client.post(
      "/api/ai",
      json={"messages": [{"role": "user", "content": "hi"}], "tools": [tool]},
      headers={"Authorization": f"Bearer {app_token}"},
    )
    assert r.status_code == 403, f"{tool} must be denied for an app-scoped token"


def test_app_token_unknown_tool_is_rejected(client, owner_token):
  # Tool-name validation (_resolve_tools) runs BEFORE the scope gate, so an
  # unknown tool returns 400 specifically (not the 403 scope rejection).
  # Either way no subprocess is spawned.
  app_token = _app_token(client, owner_token)
  r = client.post(
    "/api/ai",
    json={"messages": [{"role": "user", "content": "hi"}], "tools": ["NotATool"]},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 400
