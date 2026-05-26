"""Recovery chat: vanilla HTML page + send/stream/reset endpoints.

The page is served by Python f-string (same pattern as
recover_html.py) so there's no Vite/React/build-step dependency.
Inline JavaScript uses fetch + EventSource directly against the
endpoints declared below.

Auth: same recovery session cookie as /recover (issued by
recover_auth.create_session_token, validated by
recover_auth.decode_session_token). The user logs in once at
/recover; the cookie carries over to /recover/chat.

Storage: messages append to /data/recovery_chat.jsonl via
recover_chat_runner. Not the chats DB -- recovery survives a
broken DB schema.

Frozen: this file is on protected-files.txt. The agent cannot
edit it at runtime.
"""

from __future__ import annotations

import os
import signal
import sqlite3
from collections import OrderedDict

from fastapi import APIRouter, Body, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from app import recover_auth, recover_chat_runner

# Recovery is INDEPENDENT of the agent's import chain. We do not
# import app.database / app.models / app.config / app.theme — those
# are on the agent's write surface and can be broken without
# bringing recovery down. The recovery surface uses raw sqlite3
# (stdlib) for the one query it needs (owner-row lookup) and reads
# the DB location straight from the DATABASE_URL env var (no
# app.config dependency). Same env var the rest of the app uses,
# so prod, test, and any future override stay in sync.
_DB_URL = os.environ.get("DATABASE_URL", "sqlite:////data/db/ultimate.db")
# DATABASE_URL is `sqlite:///<path>` (relative) or `sqlite:////<path>`
# (absolute). Strip the `sqlite:///` prefix; the remaining string is
# already the on-disk path in either case.
RECOVERY_DB_PATH = _DB_URL.removeprefix("sqlite:///") if _DB_URL.startswith("sqlite:") else _DB_URL


def _owner_exists(username: str) -> bool:
  """Returns True iff an Owner row with `username` exists.

  Uses raw sqlite3 so a broken app.database / app.models doesn't
  take recovery down. Failures (DB missing, schema mismatch, etc.)
  return False — caller treats that as "no valid session," which
  routes the user back to /recover for re-setup.
  """
  if not username:
    return False
  try:
    with sqlite3.connect(RECOVERY_DB_PATH) as con:
      row = con.execute(
        "SELECT 1 FROM owner WHERE username = ? LIMIT 1",
        (username,),
      ).fetchone()
      return row is not None
  except sqlite3.Error:
    return False

router = APIRouter(tags=["recover"])

# Rate limits defend against a stolen-cookie attacker burning subprocess
# spawns or hammering /recover/restart for DoS amplification. Per-IP
# rather than per-cookie because the cookie itself is the credential
# we're protecting. Limits chosen to be invisible to a single human
# operator but block automation.
_limiter = Limiter(key_func=get_remote_address)


# Bounded set of (chat_id, turn_id) pairs that have already been
# streamed. A second /recover/chat/stream POST for the same pair
# returns 409 instead of spawning a duplicate CLI subprocess. The
# cap keeps memory bounded under any attack pattern; eviction is
# FIFO so the most recent N pairs are always remembered. Key is
# (chat_id, turn_id) — multi-chat means two chats can both have
# turn_id=1 without conflict.
_STREAMED_TURN_IDS_MAX = 256
_streamed_turn_ids: "OrderedDict[tuple, None]" = OrderedDict()


def _mark_turn_id_streamed(chat_id: str, turn_id: int) -> None:
  """Records that (chat_id, turn_id) has been streamed. Evicts the
  oldest entry if the cap is exceeded so the set stays bounded."""
  _streamed_turn_ids[(chat_id, turn_id)] = None
  while len(_streamed_turn_ids) > _STREAMED_TURN_IDS_MAX:
    _streamed_turn_ids.popitem(last=False)


def _require_session(token: str | None) -> str:
  """Validates the recovery cookie AND re-confirms the owner row
  still exists. Returns the username on success; raises 401
  otherwise.

  The owner-existence check is what makes a factory-reset cookie
  invalid even though its HMAC + expiry are still good: factory
  reset deletes the Owner row, so the next request with the stale
  cookie finds no owner and is rejected. Without this lookup, a
  second tab or stolen cookie would retain elevated access on a
  wiped instance for up to the remaining TTL (1h).
  """
  username = recover_auth.decode_session_token(token)
  if not username or not _owner_exists(username):
    raise HTTPException(status_code=401)
  return username


def _extract_chat_id(payload: dict | None) -> str:
  """Validates and returns chat_id from a request payload.

  Centralizes the chat_id parsing for all multi-chat endpoints: send,
  stream, reset, delete. Raises 400 on missing or non-string values
  so the client gets a clean error rather than reaching the runner
  layer's path-traversal validator.
  """
  chat_id = (payload or {}).get("chat_id") if payload else None
  if not isinstance(chat_id, str) or not chat_id:
    raise HTTPException(status_code=400, detail="chat_id required")
  return chat_id


@router.get("/recover/chat", response_class=HTMLResponse)
def recover_chat_page(
  id: str | None = None,
  moebius_recover: str | None = Cookie(default=None),
):
  """Serves the recovery chat HTML. Requires the recovery cookie
  AND a live owner row — a factory-reset instance redirects stale
  cookies back to /recover for re-setup.

  When `?id=<chat_id>` is set, the page renders the chat surface
  for that specific chat. Otherwise it renders the chat-picker
  view (list of prior chats + "new chat" form).
  """
  username = recover_auth.decode_session_token(moebius_recover)
  if not username or not _owner_exists(username):
    return HTMLResponse(
      '<meta http-equiv="refresh" content="0; url=/recover">',
      status_code=302,
    )
  chats = recover_chat_runner.list_chats()
  active_chat_id: str | None = None
  history: list[dict] = []
  active_provider: str | None = None
  if id:
    # Validate the chat exists; otherwise drop the id and render the
    # picker so the user can choose another or create one.
    try:
      provider = recover_chat_runner.get_chat_provider(id)
    except ValueError:
      provider = None
    if provider:
      active_chat_id = id
      active_provider = provider
      history = recover_chat_runner.load_log(id)
  return HTMLResponse(_render_page(
    chats=chats,
    active_chat_id=active_chat_id,
    history=history,
    active_provider=active_provider,
  ))


@router.post("/recover/chat/new")
@_limiter.limit("30/minute")
def recover_chat_new(
  request: Request,
  payload: dict = Body(...),
  moebius_recover: str | None = Cookie(default=None),
):
  """Creates a new chat with the chosen provider; returns its chat_id.

  Client redirects to /recover/chat?id=<chat_id> after this returns.
  The chat is created with a `_meta` line carrying the provider so
  later opens render the picker with the original provider preset.
  """
  _require_session(moebius_recover)
  provider = (payload or {}).get("provider")
  if provider not in recover_chat_runner.SUPPORTED_PROVIDERS:
    raise HTTPException(status_code=400, detail="invalid provider")
  try:
    chat_id = recover_chat_runner.create_chat(provider)
  except (ValueError, RuntimeError) as exc:
    raise HTTPException(status_code=400, detail=str(exc))
  return JSONResponse({"chat_id": chat_id, "provider": provider})


@router.post("/recover/chat/delete")
@_limiter.limit("30/minute")
def recover_chat_delete(
  request: Request,
  payload: dict = Body(...),
  moebius_recover: str | None = Cookie(default=None),
):
  """Deletes a chat permanently. 404 if it didn't exist."""
  _require_session(moebius_recover)
  chat_id = _extract_chat_id(payload)
  try:
    existed = recover_chat_runner.delete_chat(chat_id)
  except ValueError as exc:
    raise HTTPException(status_code=400, detail=str(exc))
  if not existed:
    raise HTTPException(status_code=404, detail="chat not found")
  return JSONResponse({"status": "deleted"})


@router.post("/recover/chat/send")
@_limiter.limit("30/minute")
async def recover_chat_send(
  request: Request,
  payload: dict = Body(...),
  moebius_recover: str | None = Cookie(default=None),
):
  """Appends a user message to a chat; returns the turn_id used
  by the subsequent /stream POST to pair on.

  Body: {chat_id: str, message: str}.
  """
  _require_session(moebius_recover)
  chat_id = _extract_chat_id(payload)
  text = (payload.get("message") or "").strip()
  if not text:
    raise HTTPException(status_code=400, detail="message required")
  try:
    turn_id = recover_chat_runner.append_log(chat_id, "user", text)
  except ValueError as exc:
    raise HTTPException(status_code=404, detail=str(exc))
  # append_log returns -1 on disk error (full disk, perm denied, etc.).
  # Surface as 500 so the client sees a real failure rather than a
  # "queued" status whose turn_id can't be streamed later. Codex
  # caught this swallowed-error path in review.
  if turn_id < 0:
    raise HTTPException(status_code=500, detail="failed to persist message")
  return JSONResponse(
    {"status": "queued", "message": text, "turn_id": turn_id}
  )


@router.post("/recover/chat/stream")
@_limiter.limit("30/minute")
async def recover_chat_stream(
  request: Request,
  payload: dict | None = Body(default=None),
  moebius_recover: str | None = Cookie(default=None),
):
  """SSE stream of the agent's response to a chat's most recent
  user message. POST (not GET) so the message body never appears in
  uvicorn access logs, Caddy access logs, or browser history.

  Body: {chat_id: str, turn_id: int, provider?: str}.

  Provider defaults to the chat's stored `_meta.provider`, but the
  client can override (e.g. a chat was created with Codex but the
  user wants to ask Claude for a follow-up via a quick override).
  Unknown provider names normalize to None so the runner picks the
  chat's default.

  Replay guard: each (chat_id, turn_id) can only be streamed once.
  A duplicate POST gets 409 instead of spawning a second CLI.
  """
  _require_session(moebius_recover)
  chat_id = _extract_chat_id(payload)
  turn_id = (payload or {}).get("turn_id") if payload else None
  override_provider = (payload or {}).get("provider") if payload else None
  if override_provider not in recover_chat_runner.SUPPORTED_PROVIDERS:
    override_provider = None
  # Provider resolution order: explicit override > chat's stored
  # provider > runner's default.
  effective_provider = (
    override_provider
    or recover_chat_runner.get_chat_provider(chat_id)
    or None
  )
  if isinstance(turn_id, int):
    if (chat_id, turn_id) in _streamed_turn_ids:
      raise HTTPException(
        status_code=409,
        detail="turn_id already streamed",
      )
    message = recover_chat_runner.user_message_by_id(chat_id, turn_id)
  else:
    message = recover_chat_runner.latest_user_message(chat_id)
  if not message:
    raise HTTPException(status_code=400, detail="no message in log")

  # Mark BEFORE starting the stream so a duplicate POST that arrives
  # while the first is still streaming also gets the 409.
  if isinstance(turn_id, int):
    _mark_turn_id_streamed(chat_id, turn_id)

  async def gen():
    async for chunk in recover_chat_runner.stream_turn(
      message, effective_provider, chat_id=chat_id,
    ):
      yield chunk

  return StreamingResponse(
    gen(),
    media_type="text/event-stream",
    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
  )


@router.post("/recover/chat/reset")
@_limiter.limit("10/minute")
def recover_chat_reset(
  request: Request,
  payload: dict = Body(...),
  moebius_recover: str | None = Cookie(default=None),
):
  """Truncates a chat to its _meta line (keeps the chat slot + provider).

  Body: {chat_id: str}. To delete the chat entirely, use
  /recover/chat/delete instead.
  """
  _require_session(moebius_recover)
  chat_id = _extract_chat_id(payload)
  try:
    recover_chat_runner.reset_log(chat_id)
  except ValueError as exc:
    raise HTTPException(status_code=400, detail=str(exc))
  # Reset re-numbers turn_ids from 0 (after the _meta line). Clear
  # this chat's entries from the replay-dedup set so the next /stream
  # POST doesn't 409 against ids from the previous log generation.
  for key in list(_streamed_turn_ids.keys()):
    if key[0] == chat_id:
      _streamed_turn_ids.pop(key, None)
  return JSONResponse({"status": "ok"})


@router.post("/recover/restart")
@_limiter.limit("5/minute")
def recover_restart(
  request: Request,
  moebius_recover: str | None = Cookie(default=None),
):
  """Soft restart: SIGTERM the uvicorn process so the container
  supervisor (docker restart policy: unless-stopped on prod, "no"
  on test) restarts it. After the agent edits backend code, the
  user clicks Restart in the recovery chat to load new code.

  We SIGTERM os.getpid() (uvicorn itself), NOT os.getppid().
  Reason: entrypoint.sh ends with `exec su -s /bin/sh mobius -c
  "exec uvicorn ..."`. Under this chain `getppid()` is the `su`
  process which is effectively the container init — killing it
  kills the whole container including any deferred state. Killing
  uvicorn directly lets docker restart it cleanly. Verified on
  mobius-test."""
  _require_session(moebius_recover)
  # Schedule the SIGTERM after the HTTP response has been flushed
  # so the client actually sees the {"status": "restarting"} body.
  # Without the threading.Timer, uvicorn dies before flushing.
  import threading
  threading.Timer(0.2, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
  return JSONResponse({"status": "restarting"})


def _escape(s: str) -> str:
  """Server-side HTML escape for pre-rendered history."""
  return (
    s.replace("&", "&amp;")
     .replace("<", "&lt;")
     .replace(">", "&gt;")
     .replace('"', "&quot;")
     .replace("'", "&#39;")
  )


def _render_page(
  chats: list[dict],
  active_chat_id: str | None,
  history: list[dict],
  active_provider: str | None,
) -> str:
  """Returns the recovery chat HTML.

  Two view shapes share the page:
   - **Picker view** (active_chat_id is None): list of prior chats
     + new-chat form with provider radios + Connect buttons for
     unconfigured providers.
   - **Chat view** (active_chat_id is set): the picker is collapsed
     into a header dropdown, and the chat surface renders below
     with the loaded history.

  All providers (configured or not) are listed in the picker. The
  Connect buttons trigger OAuth via /recover/provider/<name>/start
  endpoints in recover_oauth.py.
  """
  history_html_parts = []
  for msg in history:
    # role MUST be escaped — an attacker (or compromised agent in a
    # normal chat) with /data/ write access could plant a poisoned
    # jsonl entry like {"role":"<script>...</script>"}. _escape on
    # `content` alone was incomplete.
    role = _escape(msg.get("role", "?"))
    content = _escape(msg.get("content") or "")
    cls = "rc-user" if msg.get("role") == "user" else "rc-asst"
    history_html_parts.append(
      f'<div class="rc-msg {cls}"><div class="rc-role">{role}</div>'
      f'<div class="rc-text">{content}</div></div>'
    )
  history_html = "\n".join(history_html_parts)

  # Provider picker. One radio per supported provider; the badge
  # tells the user whether credentials are configured. Disconnected
  # providers get a Connect button right inline.
  prov_status = recover_chat_runner.provider_status()
  prov_default = active_provider or recover_chat_runner.default_provider()
  prov_radio_html_parts = []
  for name in recover_chat_runner.SUPPORTED_PROVIDERS:
    configured = bool(prov_status.get(name))
    checked = "checked" if name == prov_default else ""
    badge = (
      '<span class="rc-prov-ok" title="configured">●</span>'
      if configured
      else '<span class="rc-prov-missing" title="not connected">○</span>'
    )
    disabled_attr = "" if configured else " disabled"
    connect_html = (
      f'<button type="button" class="rc-connect-btn"'
      f' data-provider="{name}">Connect</button>'
      if not configured else ""
    )
    prov_radio_html_parts.append(
      f'<label class="rc-prov">'
      f'<input type="radio" name="rc-prov" value="{name}"'
      f' {checked}{disabled_attr}>'
      f'{badge} {name}'
      f'</label>{connect_html}'
    )
  provider_picker_html = "\n".join(prov_radio_html_parts)

  # Chat list — newest first, each row shows provider + relative
  # mtime + open + delete buttons. Used by both picker view and
  # chat view (in the latter it's collapsed/scrollable).
  chat_rows = []
  for c in chats:
    cid = _escape(c.get("chat_id") or "")
    prov = _escape(str(c.get("provider") or "?"))
    badge_legacy = (
      ' <span class="rc-chat-legacy" title="migrated from pre-multi-chat">legacy</span>'
      if c.get("migrated_from_legacy") else ""
    )
    is_active = " rc-chat-active" if cid == (active_chat_id or "") else ""
    chat_rows.append(
      f'<div class="rc-chat-row{is_active}" data-chat-id="{cid}">'
      f'<a class="rc-chat-link" href="/recover/chat?id={cid}">'
      f'<span class="rc-chat-id">{cid}</span>'
      f' <span class="rc-chat-prov">{prov}</span>{badge_legacy}'
      f'</a>'
      f'<button type="button" class="rc-chat-delete"'
      f' data-chat-id="{cid}" title="delete">×</button>'
      f'</div>'
    )
  chat_list_html = "\n".join(chat_rows) if chat_rows else (
    '<div class="rc-chat-empty">No chats yet — create one below.</div>'
  )

  # When in chat view, render the chat surface; otherwise the
  # picker view is the primary content. JS hides/shows the
  # appropriate sections.
  active_chat_id_js = _escape(active_chat_id) if active_chat_id else ""
  show_chat_view = "block" if active_chat_id else "none"
  show_picker_view = "none" if active_chat_id else "block"

  return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mobius recovery chat</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: system-ui, -apple-system, sans-serif;
    background: #1a1a1a;
    color: #ddd;
    display: flex;
    flex-direction: column;
    height: 100vh;
  }}
  .rc-banner {{
    background: #4a2a2a;
    border-bottom: 1px solid #6a3a3a;
    padding: 8px 12px;
    font-size: 13px;
    line-height: 1.4;
  }}
  .rc-banner a {{ color: #ffaaaa; }}
  .rc-actions {{
    padding: 6px 12px;
    border-bottom: 1px solid #333;
    display: flex;
    gap: 8px;
    font-size: 13px;
  }}
  .rc-actions button {{
    background: #333;
    color: #ddd;
    border: 1px solid #555;
    padding: 4px 10px;
    border-radius: 3px;
    cursor: pointer;
    font-size: 13px;
  }}
  .rc-actions button:hover {{ background: #444; }}
  .rc-prov-row {{
    padding: 6px 12px;
    border-bottom: 1px solid #333;
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: 13px;
    color: #aaa;
    flex-wrap: wrap;
  }}
  .rc-prov-label {{ color: #888; }}
  .rc-prov {{
    display: inline-flex;
    align-items: center;
    gap: 4px;
    cursor: pointer;
    user-select: none;
  }}
  .rc-prov input[type="radio"] {{ margin: 0 4px 0 0; }}
  .rc-prov-ok {{ color: #2a5; }}
  .rc-prov-missing {{ color: #c66; }}
  .rc-connect-btn {{
    background: #2a4; color: white; border: 1px solid #3a5;
    padding: 2px 8px; border-radius: 3px; cursor: pointer;
    font-size: 12px;
  }}
  .rc-connect-btn:hover {{ background: #3b5; }}
  .rc-chats {{
    padding: 8px 12px; border-bottom: 1px solid #333;
    font-size: 13px;
  }}
  .rc-chats h3 {{
    margin: 0 0 6px 0; font-size: 12px; color: #888;
    text-transform: uppercase; letter-spacing: 0.5px;
  }}
  .rc-chat-row {{
    display: flex; align-items: center; gap: 8px;
    padding: 4px 6px; border-radius: 3px;
  }}
  .rc-chat-row:hover {{ background: #2a2a2a; }}
  .rc-chat-active {{ background: #2a3a4a; }}
  .rc-chat-link {{
    flex: 1; color: #cde; text-decoration: none;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }}
  .rc-chat-id {{ font-family: ui-monospace, monospace; color: #aac; }}
  .rc-chat-prov {{ color: #999; font-size: 12px; }}
  .rc-chat-legacy {{
    font-size: 10px; color: #aa8; border: 1px solid #553;
    border-radius: 2px; padding: 1px 4px;
  }}
  .rc-chat-empty {{ color: #888; font-style: italic; }}
  .rc-chat-delete {{
    background: transparent; color: #c66; border: none;
    cursor: pointer; font-size: 16px; padding: 0 6px;
  }}
  .rc-chat-delete:hover {{ color: #f88; }}
  .rc-newchat {{
    padding: 12px; border-bottom: 1px solid #333;
  }}
  .rc-newchat h3 {{
    margin: 0 0 6px 0; font-size: 12px; color: #888;
    text-transform: uppercase; letter-spacing: 0.5px;
  }}
  .rc-newchat-row {{
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    font-size: 13px;
  }}
  .rc-start-btn {{
    background: #2a5; color: white; border: none;
    padding: 6px 14px; border-radius: 3px; cursor: pointer;
    font-size: 13px; margin-left: auto;
  }}
  .rc-start-btn:disabled {{ background: #444; cursor: not-allowed; }}
  .rc-oauth-modal {{
    position: fixed; inset: 0; background: rgba(0,0,0,0.7);
    display: none; align-items: center; justify-content: center;
    z-index: 100; padding: 20px;
  }}
  .rc-oauth-modal.open {{ display: flex; }}
  .rc-oauth-box {{
    background: #222; border: 1px solid #555; border-radius: 6px;
    padding: 20px; max-width: 540px; width: 100%;
    color: #ddd; font-size: 14px; line-height: 1.5;
  }}
  .rc-oauth-box h2 {{ margin: 0 0 12px 0; font-size: 18px; }}
  .rc-oauth-box a {{ color: #8af; word-break: break-all; }}
  .rc-oauth-box code {{
    background: #111; padding: 2px 6px; border-radius: 3px;
    font-size: 13px;
  }}
  .rc-oauth-box input[type="text"] {{
    width: 100%; padding: 8px; background: #111; color: #ddd;
    border: 1px solid #444; border-radius: 3px; margin: 8px 0;
    font-family: ui-monospace, monospace;
  }}
  .rc-oauth-box button {{
    background: #2a5; color: white; border: none;
    padding: 6px 14px; border-radius: 3px; cursor: pointer;
    margin-right: 8px;
  }}
  .rc-oauth-box .rc-oauth-close {{ background: #555; }}
  .rc-oauth-status {{ margin-top: 8px; color: #aaa; font-size: 13px; }}
  .rc-log {{
    flex: 1;
    overflow-y: auto;
    padding: 12px;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 13px;
    line-height: 1.5;
  }}
  .rc-msg {{ margin-bottom: 16px; }}
  .rc-role {{
    font-size: 11px;
    text-transform: uppercase;
    color: #888;
    margin-bottom: 2px;
  }}
  .rc-user .rc-role {{ color: #88aaff; }}
  .rc-asst .rc-role {{ color: #88ff88; }}
  .rc-text {{
    white-space: pre-wrap;
    word-wrap: break-word;
  }}
  .rc-tool {{
    color: #ccaa66;
    font-style: italic;
    font-size: 12px;
    padding: 2px 8px;
    border-left: 2px solid #665522;
    margin: 4px 0;
  }}
  .rc-err {{
    color: #ff8888;
    background: #3a1a1a;
    padding: 6px 8px;
    border-left: 2px solid #aa3333;
    margin: 4px 0;
    white-space: pre-wrap;
  }}
  .rc-form {{
    border-top: 1px solid #333;
    padding: 8px;
    display: flex;
    gap: 8px;
  }}
  .rc-form textarea {{
    flex: 1;
    min-height: 60px;
    max-height: 200px;
    padding: 8px;
    background: #222;
    color: #ddd;
    border: 1px solid #444;
    border-radius: 3px;
    font-family: inherit;
    font-size: 14px;
    resize: vertical;
  }}
  .rc-form button {{
    align-self: flex-end;
    padding: 8px 16px;
    background: #2a5;
    color: white;
    border: none;
    border-radius: 3px;
    cursor: pointer;
    font-size: 14px;
  }}
  .rc-form button:disabled {{ background: #444; cursor: wait; }}
</style>
</head>
<body>
<div class="rc-banner">
  Recovery mode &mdash; minimal interface. The agent has elevated write access here
  (<code>/app/app/</code>, <code>/app/scripts/</code>, <code>/data/shell/</code>).
  After backend edits, click <strong>Restart</strong> to reload uvicorn.
  <a href="/recover">&larr; Main recovery page</a>
</div>

<!-- Picker view: shown when no chat is active. Lists prior chats
     + provides a new-chat form with Connect buttons for unconfigured
     providers. -->
<div id="rc-picker-view" style="display: {show_picker_view}">
  <div class="rc-chats">
    <h3>Recovery chats</h3>
    {chat_list_html}
  </div>
  <div class="rc-newchat">
    <h3>Start a new chat</h3>
    <div class="rc-newchat-row">
      <span class="rc-prov-label">Provider:</span>
      {provider_picker_html}
      <button type="button" id="rc-start-btn" class="rc-start-btn">Start chat</button>
    </div>
  </div>
</div>

<!-- Chat view: shown when ?id=<chat_id> is set. The chat surface
     hosts the live conversation; the actions row provides
     Restart / Reset / back-to-list. -->
<div id="rc-chat-view" style="display: {show_chat_view}">
  <div class="rc-actions">
    <a href="/recover/chat" style="color: #cde; text-decoration: none; padding: 4px 10px; border: 1px solid #555; border-radius: 3px;">&larr; All chats</a>
    <button id="rc-restart-btn">Restart server</button>
    <button id="rc-reset-btn">Reset chat</button>
  </div>
  <div class="rc-prov-row">
    <span class="rc-prov-label">Rescue agent (override):</span>
    {provider_picker_html}
  </div>
  <div id="rc-log" class="rc-log">{history_html}</div>
  <form class="rc-form" id="rc-form">
    <textarea id="rc-input" placeholder="Tell the agent what is broken..." required></textarea>
    <button type="submit" id="rc-send">Send</button>
  </form>
</div>

<!-- OAuth modal: opened by Connect buttons. The same modal is
     used for both Claude (paste-code flow) and Codex (device-auth
     URL+code flow); JS swaps the body based on which provider. -->
<div id="rc-oauth-modal" class="rc-oauth-modal">
  <div class="rc-oauth-box" id="rc-oauth-body">
    <!-- populated by JS -->
  </div>
</div>
<script>
// chat_id for this view, baked in by the server. Empty string in
// picker view (no chat selected).
const CHAT_ID = "{active_chat_id_js}";

const logEl = document.getElementById('rc-log');
const inputEl = document.getElementById('rc-input');
const sendBtn = document.getElementById('rc-send');
const formEl = document.getElementById('rc-form');
const restartBtn = document.getElementById('rc-restart-btn');
const resetBtn = document.getElementById('rc-reset-btn');
const startBtn = document.getElementById('rc-start-btn');
const oauthModal = document.getElementById('rc-oauth-modal');
const oauthBody = document.getElementById('rc-oauth-body');

function scrollToBottom() {{
  if (logEl) logEl.scrollTop = logEl.scrollHeight;
}}

function makeMsg(role) {{
  const wrap = document.createElement('div');
  wrap.className = 'rc-msg ' + (role === 'user' ? 'rc-user' : 'rc-asst');
  const roleEl = document.createElement('div');
  roleEl.className = 'rc-role';
  roleEl.textContent = role;
  const textEl = document.createElement('div');
  textEl.className = 'rc-text';
  wrap.appendChild(roleEl);
  wrap.appendChild(textEl);
  logEl.appendChild(wrap);
  scrollToBottom();
  return {{ wrap: wrap, text: textEl, role: roleEl }};
}}

function appendInline(parentWrap, className, text) {{
  const div = document.createElement('div');
  div.className = className;
  div.textContent = text;
  parentWrap.appendChild(div);
  scrollToBottom();
}}

async function handleSend(e) {{
  if (e) e.preventDefault();
  const text = inputEl.value.trim();
  if (!text) return false;
  inputEl.value = '';
  sendBtn.disabled = true;
  const userMsg = makeMsg('user');
  userMsg.text.textContent = text;
  const asstMsg = makeMsg('assistant');

  // turn_id from /send pairs the subsequent /stream with this
  // specific message, so a second send from another tab can't make
  // /stream answer the wrong message.
  let turnId = null;
  try {{
    const r = await fetch('/recover/chat/send', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{chat_id: CHAT_ID, message: text}}),
    }});
    if (!r.ok) throw new Error('send failed: ' + r.status);
    const sendBody = await r.json();
    turnId = sendBody.turn_id;
  }} catch (err) {{
    asstMsg.role.textContent = 'error';
    asstMsg.text.textContent = String(err);
    sendBtn.disabled = false;
    return false;
  }}

  // Use fetch+ReadableStream rather than EventSource so the stream
  // endpoint can be POST (no message in the URL). The wire format is
  // still SSE (data: <json>\\n\\n) so we parse it line-by-line.
  let streamOk = true;
  try {{
    // Per-chat override: if a different provider radio is selected
    // than the chat's stored one, send it as `provider` so the
    // backend overrides for this turn. Skip when picker is empty
    // (chat-view radios are inside #rc-chat-view).
    const selectedProv = document.querySelector(
      '#rc-chat-view input[name="rc-prov"]:checked'
    );
    const provName = selectedProv ? selectedProv.value : undefined;
    const resp = await fetch('/recover/chat/stream', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        chat_id: CHAT_ID, turn_id: turnId, provider: provName,
      }}),
    }});
    if (!resp.ok || !resp.body) {{
      throw new Error('stream failed: ' + resp.status);
    }}
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {{
      const {{ value, done }} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {{ stream: true }});
      // SSE frames are separated by a blank line. Process whole
      // frames; leave partial frame in `buf`.
      let idx;
      while ((idx = buf.indexOf('\\n\\n')) !== -1) {{
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (!frame.startsWith('data:')) continue;
        try {{
          const data = JSON.parse(frame.slice(5).trim());
          if (data.type === 'text') {{
            asstMsg.text.textContent += data.content;
            scrollToBottom();
          }} else if (data.type === 'tool') {{
            appendInline(asstMsg.wrap, 'rc-tool', '▸ Tool: ' + data.name);
          }} else if (data.type === 'error') {{
            appendInline(asstMsg.wrap, 'rc-err', data.message);
          }} else if (data.type === 'done') {{
            // Drain the rest of the response then exit the loop.
          }}
        }} catch (e) {{ /* malformed frame - ignore */ }}
      }}
    }}
  }} catch (err) {{
    streamOk = false;
    appendInline(asstMsg.wrap, 'rc-err', 'Stream interrupted: ' + err);
  }}
  sendBtn.disabled = false;
  inputEl.focus();
  return false;
}}

async function handleRestart() {{
  if (!confirm('Restart the server? Active chats will disconnect.')) return;
  try {{
    await fetch('/recover/restart', {{method: 'POST'}});
    const sys = makeMsg('system');
    sys.text.textContent = 'Restart signal sent. Reconnecting in ~10 seconds...';
    // Poll health every 1.5s; reload the page once the backend is
    // back up (or hard-reload after 30s if health never comes back).
    // Without this auto-reload the user would refresh nervously,
    // which several reviews flagged as poor UX.
    const start = Date.now();
    const tick = async () => {{
      if (Date.now() - start > 30000) {{ location.reload(); return; }}
      try {{
        const r = await fetch('/api/health', {{cache: 'no-store'}});
        if (r.ok) {{ location.reload(); return; }}
      }} catch (_) {{ /* ignore until backend is up */ }}
      setTimeout(tick, 1500);
    }};
    setTimeout(tick, 3000);
  }} catch (err) {{
    const sys = makeMsg('system');
    sys.text.textContent = 'Restart request failed: ' + err;
  }}
}}

async function handleReset() {{
  if (!confirm('Wipe this chat? This cannot be undone.')) return;
  try {{
    const resp = await fetch('/recover/chat/reset', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{chat_id: CHAT_ID}}),
    }});
    if (!resp.ok) {{
      // Server-side delete failed (disk error, perm issue). Do NOT
      // wipe the DOM — a successful-looking UI would mask the real
      // state: page reload would re-render the un-deleted log and
      // the user would be confused about whether the reset worked.
      throw new Error('reset returned ' + resp.status);
    }}
    while (logEl.firstChild) logEl.removeChild(logEl.firstChild);
  }} catch (err) {{
    const sys = makeMsg('system');
    sys.text.textContent = 'Reset failed: ' + err;
  }}
}}

// --- Multi-chat helpers -------------------------------------------

async function handleStartChat() {{
  // Pick the provider radio inside the picker view ONLY (chat-view
  // radios are per-turn overrides, not for new-chat creation).
  const sel = document.querySelector(
    '#rc-picker-view input[name="rc-prov"]:checked'
  );
  if (!sel) {{ alert('Pick a provider first.'); return; }}
  startBtn.disabled = true;
  try {{
    const r = await fetch('/recover/chat/new', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{provider: sel.value}}),
    }});
    if (!r.ok) {{ throw new Error(await r.text() || ('status ' + r.status)); }}
    const body = await r.json();
    window.location.href = '/recover/chat?id=' + encodeURIComponent(body.chat_id);
  }} catch (err) {{
    alert('Could not create chat: ' + err);
    startBtn.disabled = false;
  }}
}}

async function handleDeleteChat(chatId) {{
  if (!confirm('Delete chat ' + chatId + '? This cannot be undone.')) return;
  try {{
    const r = await fetch('/recover/chat/delete', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{chat_id: chatId}}),
    }});
    if (!r.ok && r.status !== 404) {{ throw new Error('status ' + r.status); }}
    location.reload();
  }} catch (err) {{
    alert('Could not delete chat: ' + err);
  }}
}}

// --- OAuth (Connect provider) -------------------------------------
// The modal is built via createElement (not innerHTML) so a stray
// XSS-flavoured token in an error message can't escape — the server
// returns text/plain error bodies that we surface as textContent.

function clearChildren(el) {{
  while (el.firstChild) el.removeChild(el.firstChild);
}}

function openOauthModal() {{
  oauthModal.classList.add('open');
}}

function closeOauthModal() {{
  oauthModal.classList.remove('open');
  clearChildren(oauthBody);
}}

function makeBtn(label, onClick, cls) {{
  const b = document.createElement('button');
  b.type = 'button';
  if (cls) b.className = cls;
  b.textContent = label;
  if (onClick) b.addEventListener('click', onClick);
  return b;
}}

function makeLine(text) {{
  const p = document.createElement('p');
  p.textContent = text;
  return p;
}}

function makeLink(url) {{
  const p = document.createElement('p');
  const a = document.createElement('a');
  a.href = url;
  a.target = '_blank';
  a.rel = 'noopener';
  a.textContent = url;
  p.appendChild(a);
  return p;
}}

function makeHeading(text) {{
  const h = document.createElement('h2');
  h.textContent = text;
  return h;
}}

async function handleConnectClaude() {{
  clearChildren(oauthBody);
  oauthBody.appendChild(makeHeading('Connect Claude'));
  oauthBody.appendChild(makeLine('Generating auth URL…'));
  openOauthModal();
  try {{
    const r = await fetch('/recover/provider/claude/start', {{method: 'POST'}});
    if (!r.ok) throw new Error('status ' + r.status);
    const body = await r.json();
    clearChildren(oauthBody);
    oauthBody.appendChild(makeHeading('Connect Claude'));
    oauthBody.appendChild(makeLine('1. Open this URL in a new tab and authorize:'));
    oauthBody.appendChild(makeLink(body.auth_url));
    oauthBody.appendChild(makeLine('2. Paste the code from the callback URL here:'));
    const codeInput = document.createElement('input');
    codeInput.type = 'text';
    codeInput.placeholder = 'code=...';
    oauthBody.appendChild(codeInput);
    const status = document.createElement('div');
    status.className = 'rc-oauth-status';
    const submit = makeBtn('Submit code', async () => {{
      status.textContent = 'Exchanging code…';
      try {{
        const rr = await fetch('/recover/provider/claude/code', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{code: codeInput.value.trim()}}),
        }});
        if (!rr.ok) {{ throw new Error(await rr.text()); }}
        status.textContent = 'Connected! Reloading…';
        setTimeout(() => location.reload(), 800);
      }} catch (err) {{
        status.textContent = 'Failed: ' + err;
      }}
    }});
    oauthBody.appendChild(submit);
    oauthBody.appendChild(makeBtn('Cancel', closeOauthModal, 'rc-oauth-close'));
    oauthBody.appendChild(status);
  }} catch (err) {{
    clearChildren(oauthBody);
    oauthBody.appendChild(makeHeading('Connect Claude'));
    const errLine = makeLine('Could not start auth flow: ' + err);
    errLine.style.color = '#c66';
    oauthBody.appendChild(errLine);
    oauthBody.appendChild(makeBtn('Close', closeOauthModal, 'rc-oauth-close'));
  }}
}}

async function handleConnectCodex() {{
  clearChildren(oauthBody);
  oauthBody.appendChild(makeHeading('Connect Codex'));
  oauthBody.appendChild(makeLine('Starting device-auth…'));
  openOauthModal();
  try {{
    const r = await fetch('/recover/provider/codex/start', {{method: 'POST'}});
    if (!r.ok) throw new Error('status ' + r.status);
    const body = await r.json();
    clearChildren(oauthBody);
    oauthBody.appendChild(makeHeading('Connect Codex'));
    oauthBody.appendChild(makeLine('1. Open this URL:'));
    oauthBody.appendChild(makeLink(body.url));
    const codePara = document.createElement('p');
    codePara.appendChild(document.createTextNode('2. Enter this code: '));
    const codeEl = document.createElement('code');
    codeEl.textContent = body.code;
    codePara.appendChild(codeEl);
    oauthBody.appendChild(codePara);
    oauthBody.appendChild(makeLine('The page reloads once authentication completes.'));
    const status = document.createElement('div');
    status.className = 'rc-oauth-status';
    status.textContent = 'Waiting…';
    oauthBody.appendChild(makeBtn('Cancel', closeOauthModal, 'rc-oauth-close'));
    oauthBody.appendChild(status);
    // Poll status every 2s until non-in_progress or modal closed.
    const poll = async () => {{
      if (!oauthModal.classList.contains('open')) return;
      try {{
        const rr = await fetch('/recover/provider/codex/status');
        if (rr.ok) {{
          const j = await rr.json();
          if (j.state === 'complete') {{
            status.textContent = 'Connected! Reloading…';
            setTimeout(() => location.reload(), 800);
            return;
          }} else if (j.state === 'failed') {{
            status.textContent = 'Authentication failed.';
            return;
          }}
        }}
      }} catch (_) {{ /* keep polling */ }}
      setTimeout(poll, 2000);
    }};
    setTimeout(poll, 2000);
  }} catch (err) {{
    clearChildren(oauthBody);
    oauthBody.appendChild(makeHeading('Connect Codex'));
    const errLine = makeLine('Could not start: ' + err);
    errLine.style.color = '#c66';
    oauthBody.appendChild(errLine);
    oauthBody.appendChild(makeBtn('Close', closeOauthModal, 'rc-oauth-close'));
  }}
}}

// --- Event wiring (defensive: elements may not exist in some views) ---

if (formEl) formEl.addEventListener('submit', handleSend);
if (restartBtn) restartBtn.addEventListener('click', handleRestart);
if (resetBtn) resetBtn.addEventListener('click', handleReset);
if (startBtn) startBtn.addEventListener('click', handleStartChat);

document.querySelectorAll('.rc-chat-delete').forEach(btn => {{
  btn.addEventListener('click', (e) => {{
    e.preventDefault();
    handleDeleteChat(btn.dataset.chatId);
  }});
}});

document.querySelectorAll('.rc-connect-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const prov = btn.dataset.provider;
    if (prov === 'claude') handleConnectClaude();
    else if (prov === 'codex') handleConnectCodex();
  }});
}});

scrollToBottom();
if (inputEl) inputEl.focus();
</script>
</body>
</html>"""
