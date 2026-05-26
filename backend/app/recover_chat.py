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

from fastapi import APIRouter, Body, Cookie, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from app import recover_auth, recover_chat_runner

router = APIRouter(tags=["recover"])


def _require_session(token: str | None) -> str:
  """Returns username if cookie is valid; raises 401 otherwise."""
  username = recover_auth.decode_session_token(token)
  if not username:
    raise HTTPException(status_code=401)
  return username


@router.get("/recover/chat", response_class=HTMLResponse)
def recover_chat_page(
  moebius_recover: str | None = Cookie(default=None),
):
  """Serves the recovery chat HTML. Requires the recovery cookie."""
  if not recover_auth.decode_session_token(moebius_recover):
    return HTMLResponse(
      '<meta http-equiv="refresh" content="0; url=/recover">',
      status_code=302,
    )
  history = recover_chat_runner.load_log()
  return HTMLResponse(_render_page(history))


@router.post("/recover/chat/send")
async def recover_chat_send(
  payload: dict = Body(...),
  moebius_recover: str | None = Cookie(default=None),
):
  """Accepts a user message, persists it, returns 202.

  Streaming happens via GET /recover/chat/stream where the client
  picks up the response. Split send + stream so the EventSource
  setup is idempotent on reconnect.
  """
  _require_session(moebius_recover)
  text = (payload.get("message") or "").strip()
  if not text:
    raise HTTPException(status_code=400, detail="message required")
  recover_chat_runner.append_log("user", text)
  return JSONResponse({"status": "queued", "message": text})


@router.post("/recover/chat/stream")
async def recover_chat_stream(
  moebius_recover: str | None = Cookie(default=None),
):
  """SSE stream of the agent's response to the latest user message
  in the log. POST (not GET) so the message body never appears in
  uvicorn access logs, Caddy access logs, or browser history — the
  recovery chat handles sensitive repair conversations.

  Send + stream are split: the client POSTs /recover/chat/send first
  (which persists the message to /data/recovery_chat.jsonl), then
  POSTs to this endpoint to consume the response. The runner reads
  the latest user-role line from the log, so there is nothing to
  pass in the URL or body."""
  _require_session(moebius_recover)
  message = recover_chat_runner.latest_user_message()
  if not message:
    raise HTTPException(status_code=400, detail="no message in log")

  async def gen():
    async for chunk in recover_chat_runner.stream_turn(message):
      yield chunk

  return StreamingResponse(
    gen(),
    media_type="text/event-stream",
    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
  )


@router.post("/recover/chat/reset")
def recover_chat_reset(
  moebius_recover: str | None = Cookie(default=None),
):
  """Wipes /data/recovery_chat.jsonl."""
  _require_session(moebius_recover)
  recover_chat_runner.reset_log()
  return JSONResponse({"status": "ok"})


@router.post("/recover/restart")
def recover_restart(
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
    role = msg.get("role", "?")
    content = _escape(msg.get("content") or "")
    cls = "rc-user" if role == "user" else "rc-asst"
    history_html_parts.append(
      f'<div class="rc-msg {cls}"><div class="rc-role">{role}</div>'
      f'<div class="rc-text">{content}</div></div>'
    )
  history_html = "\n".join(history_html_parts)

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

  try {{
    const r = await fetch('/recover/chat/send', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{message: text}}),
    }});
    if (!r.ok) throw new Error('send failed: ' + r.status);
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
    const resp = await fetch('/recover/chat/stream', {{ method: 'POST' }});
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
    sys.text.textContent = 'Restart signal sent. Wait a few seconds and reload.';
  }} catch (err) {{
    const sys = makeMsg('system');
    sys.text.textContent = 'Restart request failed: ' + err;
  }}
}}

async function handleReset() {{
  if (!confirm('Wipe the recovery chat log? This cannot be undone.')) return;
  try {{
    await fetch('/recover/chat/reset', {{method: 'POST'}});
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
