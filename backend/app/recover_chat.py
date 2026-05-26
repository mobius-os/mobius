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


# Bounded set of turn_ids that have already been streamed. A second
# /recover/chat/stream POST for the same id returns 409 instead of
# spawning a duplicate CLI subprocess. The cap keeps memory bounded
# under any attack pattern; the eviction order is FIFO so the most
# recent N ids are always remembered. OrderedDict gives O(1) insert,
# membership check, and popitem(last=False) for FIFO eviction —
# simpler than a separate deque + set pair.
_STREAMED_TURN_IDS_MAX = 256
_streamed_turn_ids: "OrderedDict[int, None]" = OrderedDict()


def _mark_turn_id_streamed(turn_id: int) -> None:
  """Records that `turn_id` has been streamed. Evicts the oldest id
  if the cap is exceeded so the set stays bounded under load."""
  _streamed_turn_ids[turn_id] = None
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


@router.get("/recover/chat", response_class=HTMLResponse)
def recover_chat_page(
  moebius_recover: str | None = Cookie(default=None),
):
  """Serves the recovery chat HTML. Requires the recovery cookie
  AND a live owner row — a factory-reset instance redirects stale
  cookies back to /recover for re-setup, same as no cookie at all."""
  username = recover_auth.decode_session_token(moebius_recover)
  if not username or not _owner_exists(username):
    return HTMLResponse(
      '<meta http-equiv="refresh" content="0; url=/recover">',
      status_code=302,
    )
  history = recover_chat_runner.load_log()
  return HTMLResponse(_render_page(history))


@router.post("/recover/chat/send")
@_limiter.limit("30/minute")
async def recover_chat_send(
  request: Request,
  payload: dict = Body(...),
  moebius_recover: str | None = Cookie(default=None),
):
  """Accepts a user message, persists it, returns a turn_id the
  client uses to pair with the subsequent /stream POST.

  The turn_id is the message's index in the log (counted from 0).
  Pairing via id rather than 'latest user message' closes a race
  where two browser tabs (or two rapid sends) cause one stream to
  respond to the wrong message.
  """
  _require_session(moebius_recover)
  text = (payload.get("message") or "").strip()
  if not text:
    raise HTTPException(status_code=400, detail="message required")
  turn_id = recover_chat_runner.append_log("user", text)
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
  """SSE stream of the agent's response to a specific persisted
  message. POST (not GET) so the message body never appears in
  uvicorn access logs, Caddy access logs, or browser history.

  Pairing: the client passes the `turn_id` returned by /send. The
  runner reads THAT specific message (by index in the jsonl log)
  rather than 'latest user message' — closes the multi-tab race
  where two sends + two streams could mis-pair.

  Replay guard: a given `turn_id` can only be streamed once. A
  duplicate POST (double-click, network retry, second tab, malicious
  replay) returns 409 instead of spawning a second CLI subprocess
  that would re-bill the user and append a duplicate assistant
  entry to the log. The dedup happens at the route boundary so the
  underlying runner's stream lock isn't relied upon for this
  semantic.

  For backward compatibility, if turn_id is omitted the runner
  still falls back to latest_user_message — but the client always
  sends the id, and only id-tagged streams participate in dedup."""
  _require_session(moebius_recover)
  turn_id = (payload or {}).get("turn_id") if payload else None
  # Provider picker — client passes "claude" or "codex"; anything else
  # falls back to the default (first configured, claude-preferred).
  provider = (payload or {}).get("provider") if payload else None
  if provider not in recover_chat_runner.SUPPORTED_PROVIDERS:
    provider = None  # runner picks the default
  if isinstance(turn_id, int):
    if turn_id in _streamed_turn_ids:
      raise HTTPException(
        status_code=409,
        detail="turn_id already streamed",
      )
    message = recover_chat_runner.user_message_by_id(turn_id)
  else:
    message = recover_chat_runner.latest_user_message()
  if not message:
    raise HTTPException(status_code=400, detail="no message in log")

  # Mark BEFORE starting the stream so a duplicate POST that arrives
  # while the first is still streaming also gets the 409 — not just
  # duplicates that arrive after completion.
  if isinstance(turn_id, int):
    _mark_turn_id_streamed(turn_id)

  async def gen():
    async for chunk in recover_chat_runner.stream_turn(message, provider):
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
  moebius_recover: str | None = Cookie(default=None),
):
  """Wipes /data/recovery_chat.jsonl."""
  _require_session(moebius_recover)
  recover_chat_runner.reset_log()
  # Reset re-numbers turn_ids from 0. Clear the replay-dedup set so
  # the next /stream POST doesn't 409 against ids from the previous
  # log generation (which the reset just invalidated).
  _streamed_turn_ids.clear()
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


def _render_page(history: list[dict]) -> str:
  """Returns the recovery chat HTML with prior log baked in."""
  history_html_parts = []
  for msg in history:
    # role MUST be escaped — an attacker (or compromised agent in a
    # normal chat) with /data/ write access could plant a poisoned
    # jsonl entry like {"role":"<script>...</script>"}. _escape on
    # `content` alone was incomplete.
    role = _escape(msg.get("role", "?"))
    content = _escape(msg.get("content") or "")
    # cls is derived from the RAW role, not the escaped form, so
    # the comparison still works even if an attacker plants junk in
    # role (which becomes a visible-but-inert string).
    cls = "rc-user" if msg.get("role") == "user" else "rc-asst"
    history_html_parts.append(
      f'<div class="rc-msg {cls}"><div class="rc-role">{role}</div>'
      f'<div class="rc-text">{content}</div></div>'
    )
  history_html = "\n".join(history_html_parts)

  # Provider picker. One radio per supported provider; the default
  # checked radio is the runner's preferred-default. Status badge
  # tells the user which providers have credentials configured.
  prov_status = recover_chat_runner.provider_status()
  prov_default = recover_chat_runner.default_provider()
  prov_radio_html_parts = []
  for name in recover_chat_runner.SUPPORTED_PROVIDERS:
    configured = bool(prov_status.get(name))
    checked = "checked" if name == prov_default else ""
    badge = (
      '<span class="rc-prov-ok" title="configured">●</span>'
      if configured
      else '<span class="rc-prov-missing" title="not connected">○</span>'
    )
    label_title = "" if configured else (
      ' title="No credentials in /data/cli-auth/' + name + '. '
      'Connect from Settings in the main app, '
      'or use the Reconnect link (coming soon)."'
    )
    prov_radio_html_parts.append(
      f'<label class="rc-prov"{label_title}>'
      f'<input type="radio" name="rc-prov" value="{name}" {checked}>'
      f'{badge} {name}'
      f'</label>'
    )
  provider_picker_html = "\n".join(prov_radio_html_parts)

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
<div class="rc-actions">
  <button id="rc-restart-btn">Restart server</button>
  <button id="rc-reset-btn">Reset recovery chat</button>
</div>
<div class="rc-prov-row">
  <span class="rc-prov-label">Rescue agent:</span>
  {provider_picker_html}
</div>
<div id="rc-log" class="rc-log">{history_html}</div>
<form class="rc-form" id="rc-form">
  <textarea id="rc-input" placeholder="Tell the agent what is broken..." required></textarea>
  <button type="submit" id="rc-send">Send</button>
</form>
<script>
const logEl = document.getElementById('rc-log');
const inputEl = document.getElementById('rc-input');
const sendBtn = document.getElementById('rc-send');
const formEl = document.getElementById('rc-form');
const restartBtn = document.getElementById('rc-restart-btn');
const resetBtn = document.getElementById('rc-reset-btn');

function scrollToBottom() {{
  logEl.scrollTop = logEl.scrollHeight;
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
      body: JSON.stringify({{message: text}}),
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
    const selectedProv = document.querySelector('input[name="rc-prov"]:checked');
    const provName = selectedProv ? selectedProv.value : undefined;
    const resp = await fetch('/recover/chat/stream', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{turn_id: turnId, provider: provName}}),
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
  if (!confirm('Wipe the recovery chat log? This cannot be undone.')) return;
  try {{
    const resp = await fetch('/recover/chat/reset', {{method: 'POST'}});
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

formEl.addEventListener('submit', handleSend);
restartBtn.addEventListener('click', handleRestart);
resetBtn.addEventListener('click', handleReset);

scrollToBottom();
inputEl.focus();
</script>
</body>
</html>"""
