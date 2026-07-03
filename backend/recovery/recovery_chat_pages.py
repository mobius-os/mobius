"""Recovery chat HTML page for the frozen recovery container.

Ported from `backend/app/recover_chat.py`'s `_render_page` — the vanilla
HTML + inline CSS/JS chat surface, served by Python f-string so there is
no Vite/React/build-step dependency. The only substantive change from the
FastAPI original is the runner import name (`recovery_chat_runner`, the
bundle's copy) and the function name (`render_chat_page`, public). All
endpoint paths, the SSE wire parsing, and the OAuth modal are unchanged so
this page drives recoveryd's routes exactly as the in-platform page drove
the FastAPI routes.

FROZEN: baked root-owned + chmod a-w in `/app/recovery/`.
"""

from __future__ import annotations

import json

import recovery_chat_runner


def _escape(s) -> str:
  """Server-side HTML escape for pre-rendered history.

  Coerces to str first: a /data-poisoned jsonl entry can carry a
  non-string `role`/`content` (a number, list, or null), which .replace
  would AttributeError on — a poisoned recovery log must degrade to escaped
  text, never 500 the page you need most.
  """
  s = str(s)
  return (
    s.replace("&", "&amp;")
     .replace("<", "&lt;")
     .replace(">", "&gt;")
     .replace('"', "&quot;")
     .replace("'", "&#39;")
  )


def render_chat_page(
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
  endpoints in recovery_oauth.py.
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

  # Connection status per provider (badge + inline Connect for any that
  # isn't authed). This is ONLY auth/status now — it no longer doubles as
  # the chat's provider selector. The single model dropdown below picks
  # both provider and model (the model implies its provider), so the owner
  # makes one choice, not two.
  prov_status = recovery_chat_runner.provider_status()
  prov_default = active_provider or recovery_chat_runner.default_provider()
  prov_status_html_parts = []
  for name in recovery_chat_runner.SUPPORTED_PROVIDERS:
    configured = bool(prov_status.get(name))
    badge = (
      '<span class="rc-prov-ok" title="configured">●</span>'
      if configured
      else '<span class="rc-prov-missing" title="not connected">○</span>'
    )
    connect_html = (
      f'<button type="button" class="rc-connect-btn"'
      f' data-provider="{name}">Connect</button>'
      if not configured else ""
    )
    prov_status_html_parts.append(
      f'<span class="rc-prov">{badge} {name}</span>{connect_html}'
    )
  provider_picker_html = "\n".join(prov_status_html_parts)

  # Single unified model dropdown. Every option encodes BOTH the provider
  # and the model as `provider:model` (model empty = that provider's CLI
  # default), so selecting one option sets both — no separate provider
  # control. Grouped under per-provider <optgroup>s with a "CLI default"
  # row at the top of each group. Class (not id) selector so the same
  # markup appears in both the picker and chat-override views; the JS
  # scopes its query to the visible view. The backend still validates the
  # model against the derived provider.
  model_group_parts = []
  for prov_name in recovery_chat_runner.SUPPORTED_PROVIDERS:
    opts = [
      f'<option value="{_escape(prov_name)}:"'
      f'{" selected" if prov_name == prov_default else ""}>'
      f'{_escape(prov_name)} — CLI default</option>'
    ]
    for mid in recovery_chat_runner.RECOVERY_MODELS.get(prov_name, ()):  # noqa: E501
      opts.append(
        f'<option value="{_escape(prov_name)}:{_escape(mid)}">{_escape(mid)}</option>'
      )
    model_group_parts.append(
      f'<optgroup label="{_escape(prov_name)}">{"".join(opts)}</optgroup>'
    )
  model_select_html = (
    '<label class="rc-pick">Model: '
    f'<select class="rc-select rc-model-sel">{"".join(model_group_parts)}</select>'
    '</label>'
  )

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
      f'<a class="rc-chat-link" href="/recover/chat?chat={cid}">'
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
  # chat_id flows into a JS string literal below. HTML-escape isn't
  # the right escape there (a backslash in chat_id would escape the
  # closing quote). json.dumps yields a valid JS string literal
  # including its surrounding quotes, so substitute it without
  # adding our own.
  active_chat_id_js = json.dumps(active_chat_id) if active_chat_id else '""'
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
  .rc-pick {{
    display: inline-flex; align-items: center; gap: 4px;
    color: #888; font-size: 13px;
  }}
  .rc-select {{
    background: #222; color: #ddd; border: 1px solid #444;
    border-radius: 3px; padding: 3px 6px; font-size: 13px;
  }}
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
  After backend edits, click <strong>Restart</strong> to reload the platform.
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
      {model_select_html}
      <button type="button" id="rc-start-btn" class="rc-start-btn">Start chat</button>
    </div>
  </div>
</div>

<!-- Chat view: shown when ?chat=<chat_id> is set. The chat surface
     hosts the live conversation; the actions row provides
     Restart / Reset / back-to-list. -->
<div id="rc-chat-view" style="display: {show_chat_view}">
  <div class="rc-actions">
    <a href="/recover/chat" style="color: #cde; text-decoration: none; padding: 4px 10px; border: 1px solid #555; border-radius: 3px;">&larr; All chats</a>
    <button id="rc-restart-btn">Restart server</button>
    <button id="rc-reset-btn">Reset chat</button>
  </div>
  <div class="rc-prov-row">
    <span class="rc-prov-label">Provider (override):</span>
    {provider_picker_html}
    {model_select_html}
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
const CHAT_ID = {active_chat_id_js};

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
      body: JSON.stringify({{chat_id: CHAT_ID, text: text}}),
    }});
    if (r.status === 401) {{
      // Recovery session expired — show a clear, actionable message
      // with a link rather than a generic "send failed: 401" string.
      asstMsg.role.textContent = 'error';
      asstMsg.text.innerHTML =
        'Your recovery session has expired. ' +
        '<a href="/recover" style="color:#8b6cf7">Log in again at /recover</a>.';
      sendBtn.disabled = false;
      return false;
    }}
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
    // Per-turn override: the model dropdown encodes "provider:model"
    // (empty model = that provider's CLI default), so one choice sets both.
    const modelSel = document.querySelector('#rc-chat-view .rc-model-sel');
    const ovrParts = (modelSel ? modelSel.value : ':').split(':');
    const ovrProvider = ovrParts.shift() || undefined;
    const ovrModel = ovrParts.join(':') || undefined;
    const resp = await fetch('/recover/chat/stream', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        chat_id: CHAT_ID, turn_id: turnId,
        provider: ovrProvider,
        model: ovrModel,
      }}),
    }});
    if (resp.status === 401) {{
      // Session expired between /send and /stream — same actionable
      // message as the /send 401 path above.
      asstMsg.role.textContent = 'error';
      asstMsg.text.innerHTML =
        'Your recovery session has expired. ' +
        '<a href="/recover" style="color:#8b6cf7">Log in again at /recover</a>.';
      sendBtn.disabled = false;
      return false;
    }}
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
      if (Date.now() - start > 30000) {{
        // Health never came back within 30 s. If the container has no
        // restart policy, the server will not come back on its own —
        // a location.reload() into a dead server would just show a
        // browser error page with no context. Tell the user what to do.
        const sys = makeMsg('system');
        sys.text.innerHTML =
          'Server did not restart automatically. ' +
          'If Möbius runs without a restart policy, restart the container manually, ' +
          'then <a href="/recover/chat" style="color:#8b6cf7">reload this page</a>.';
        return;
      }}
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
  // The model dropdown encodes provider+model as "provider:model"
  // (empty model = that provider's CLI default), so one choice sets both.
  const modelSel = document.querySelector('#rc-picker-view .rc-model-sel');
  const pickParts = (modelSel ? modelSel.value : 'claude:').split(':');
  const pickProvider = pickParts.shift();
  const pickModel = pickParts.join(':') || undefined;
  if (!pickProvider) {{ alert('Pick a model first.'); return; }}
  startBtn.disabled = true;
  try {{
    const r = await fetch('/recover/chat/new', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        provider: pickProvider,
        model: pickModel,
      }}),
    }});
    if (!r.ok) {{ throw new Error(await r.text() || ('status ' + r.status)); }}
    const body = await r.json();
    window.location.href = '/recover/chat?chat=' + encodeURIComponent(body.chat_id);
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
  // Defense in depth: only allow http(s) schemes on the href.
  // The OAuth URLs returned by the server are always https://, but
  // a compromised/MITM'd response could plant a `javascript:` URL
  // that would execute on click. Showing the URL as text is fine
  // either way; the difference is whether clicking does anything.
  if (/^https?:\\/\\//i.test(url)) {{
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener';
  }} else {{
    a.href = '#';
    a.title = 'URL blocked: unexpected scheme';
  }}
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
