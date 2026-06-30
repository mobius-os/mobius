"""HTML templates for the frozen recovery container (recoveryd).

Vanilla, dependency-free HTML/CSS — no React, no app.theme import. The
styling is lifted from `backend/app/routes/recover_html.py` so the
recovery surface reads as the same shell, but this copy is frozen in the
`/app/recovery/` bundle and renders even when the entire platform is
down.

Tier-1 floor MVP: the dashboard offers the deterministic git-restore
("Restore platform") and a full baked recopy ("Reset to baked floor") —
both CLI-free, agent-free, network-free. The Tier-2 AI rescue chat is
deferred to a follow-on.
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path


def _theme_mode() -> str:
  """Reads the active theme mode ("dark"/"light") from disk with stdlib.

  Mirrors the platform's recover_html._theme_mode so the recovery page
  matches the shell. Falls back to "dark" (the historical default) on any
  read failure — the page must render even when /data/shared is gone.
  """
  data_dir = Path(os.environ.get("DATA_DIR", "/data"))
  path = data_dir / "shared" / "theme-mode"
  try:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
      return "dark"
    try:
      mode = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
      mode = raw
    if mode in ("dark", "light"):
      return mode
  except (OSError, ValueError):
    pass
  return "dark"


def _confirm_attr(text: str) -> str:
  """Returns an escaped HTML attribute value running `return confirm(...)`
  on submit. json.dumps gives a valid JS string literal; html.escape makes
  it safe inside a double-quoted attribute."""
  js = f"return confirm({json.dumps(text)});"
  return html.escape(js, quote=True)


_STYLE = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: var(--font, system-ui, -apple-system, sans-serif);
    background: var(--bg, #0d0d0d); color: var(--text, #ececec);
    min-height: 100vh;
    padding: 32px 16px;
  }
  .card {
    background: var(--surface, #171717);
    border: 1px solid var(--border, #2a2a2a);
    border-radius: 14px; padding: 24px;
    max-width: 480px; width: 100%;
    margin: 0 auto;
  }
  [data-theme="light"] .card {
    box-shadow:
      0 1px 2px rgba(0, 0, 0, 0.04),
      0 1px 3px rgba(0, 0, 0, 0.04);
  }
  h1 {
    font-size: 22px; font-weight: 600;
    letter-spacing: -0.01em; margin-bottom: 4px;
  }
  .sub { color: var(--muted, #9b9b9b); font-size: 14px; margin-bottom: 24px; }
  label {
    display: block; font-size: 13px; font-weight: 500;
    color: var(--muted, #9b9b9b); margin-bottom: 6px;
  }
  input {
    width: 100%; padding: 11px 14px; font-size: 14px;
    background: var(--bg, #0d0d0d);
    border: 1px solid var(--border, #2a2a2a);
    border-radius: 10px; color: var(--text, #ececec); margin-bottom: 14px;
    outline: none;
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  input:focus {
    border-color: var(--accent, #8b6cf7);
    box-shadow: 0 0 0 3px var(--accent-dim, rgba(139, 108, 247, 0.14));
  }
  input:focus-visible { outline: none; }
  .btn {
    display: inline-block; padding: 11px 18px; font-size: 14px;
    font-weight: 500;
    border: none; border-radius: 10px; cursor: pointer;
    color: #fff; text-decoration: none; text-align: center;
    width: 100%;
    transition: background 0.15s, border-color 0.15s, color 0.15s;
    -webkit-tap-highlight-color: transparent;
    user-select: none; -webkit-user-select: none;
    -webkit-touch-callout: none;
  }
  .btn:focus-visible {
    outline: 2px solid var(--accent, #8b6cf7); outline-offset: 2px;
  }
  .btn-primary { background: var(--accent, #8b6cf7); }
  @media (hover: hover) and (pointer: fine) {
    .btn-primary:hover { background: var(--accent-hover, #7c5ce6); }
  }
  .btn-warn {
    background: transparent;
    color: var(--danger, #f87171);
    border: 1px solid var(--danger, #f87171);
  }
  @media (hover: hover) and (pointer: fine) {
    .btn-warn:hover { background: var(--danger, #f87171); color: #fff; }
    .btn-warn:hover:active { background: var(--danger, #f87171); color: #fff; }
  }
  .btn-outline {
    background: var(--surface2, #212121);
    border: 1px solid var(--border-light, #1f1f1f);
    color: var(--text, #ececec);
  }
  @media (hover: hover) and (pointer: fine) {
    .btn-outline:hover {
      background: color-mix(in srgb, var(--accent, #8b6cf7) 14%, var(--surface2, #212121));
      border-color: color-mix(in srgb, var(--accent, #8b6cf7) 60%, var(--border-light, #1f1f1f));
    }
  }
  .error { color: var(--danger, #f87171); font-size: 13px; margin-bottom: 12px; }
  .msg {
    background: color-mix(in srgb, var(--green, #10b981) 12%, transparent);
    border: 1px solid color-mix(in srgb, var(--green, #10b981) 45%, transparent);
    color: var(--green, #10b981);
    font-size: 14px; font-weight: 500;
    border-radius: 10px;
    padding: 12px 14px;
    margin-bottom: 18px;
    display: flex; align-items: flex-start; gap: 10px;
  }
  .msg::before {
    content: "\\2713"; font-size: 16px; font-weight: 700; flex: 0 0 auto;
  }
  .actions { display: flex; flex-direction: column; gap: 10px; }
  .section { margin-bottom: 24px; }
  .section-title {
    font-size: 13px; font-weight: 500;
    color: var(--muted, #9b9b9b);
    margin-bottom: 10px;
  }
  hr { border: none; border-top: 1px solid var(--border, #2a2a2a); margin: 24px 0; }
  .recommended {
    background: color-mix(in srgb, var(--accent, #8b6cf7) 10%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent, #8b6cf7) 55%, transparent);
    border-radius: 12px; padding: 18px; margin-bottom: 24px;
  }
  .recommended .label {
    font-size: 13px; font-weight: 500;
    color: var(--accent, #8b6cf7);
    margin-bottom: 6px;
    display: flex; align-items: center; gap: 6px;
  }
  .recommended .desc {
    font-size: 13px; color: var(--muted, #9b9b9b);
    line-height: 1.5; margin-bottom: 14px;
  }
  .recommended .btn-primary {
    background: var(--surface, #171717);
    color: var(--accent, #8b6cf7);
    border: 1px solid color-mix(in srgb, var(--accent, #8b6cf7) 60%, transparent);
  }
  @media (hover: hover) and (pointer: fine) {
    .recommended .btn-primary:hover {
      background: var(--accent, #8b6cf7); color: #fff;
      border-color: var(--accent, #8b6cf7);
    }
  }
  .desc { font-size: 13px; color: var(--muted, #9b9b9b); line-height: 1.5; }
  .status {
    display: flex; align-items: center; gap: 8px;
    font-size: 13px; margin-bottom: 6px;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto; }
  .dot-ok { background: var(--green, #10b981); }
  .dot-down { background: var(--danger, #f87171); }
  .dot-unknown { background: var(--muted, #9b9b9b); }
  .status-grid {
    background: var(--bg, #0d0d0d);
    border: 1px solid var(--border, #2a2a2a);
    border-radius: 10px; padding: 14px; margin-bottom: 20px;
  }
  .status-meta { font-size: 12px; color: var(--muted, #9b9b9b); margin-top: 8px; }
  code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
  }
"""


def _page(body: str, mode: str) -> str:
  return f"""<!DOCTYPE html>
<html lang="en" data-theme="{mode}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>M&#246;bius Recovery</title>
  <style>{_STYLE}</style>
</head>
<body>
  <div class="card">
    <h1>&#8734; Recovery</h1>
{body}
  </div>
</body>
</html>"""


def login_html(error: str = "") -> str:
  """Recovery login page. Posts to /recover/auth."""
  error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
  body = f"""    <p class="sub">Frozen recovery system. Log in to continue.</p>
    {error_html}
    <form method="POST" action="/recover/auth">
      <label for="username">Username</label>
      <input id="username" name="username" required autofocus
             autocomplete="username">
      <label for="password">Password</label>
      <input id="password" name="password" type="password" required
             autocomplete="current-password">
      <button class="btn btn-primary" type="submit">Log in</button>
    </form>"""
  return _page(body, _theme_mode())


def not_configured_html() -> str:
  """Read-only page shown when no owner row exists yet.

  Closes the first-boot-takeover: until the owner is created (at the
  main app's setup wizard), the recovery surface authenticates no one
  and every destructive route refuses.
  """
  body = """    <p class="sub">This instance is not set up yet.</p>
    <p class="desc">
      Recovery is read-only until an owner account exists. Finish setup at
      the main app (<code>/</code>), then return here if you ever need to
      restore a broken platform.
    </p>
    <hr>
    <div class="actions">
      <a href="/" class="btn btn-outline">&larr; Go to setup</a>
    </div>"""
  return _page(body, _theme_mode())


def _status_block(status: dict) -> str:
  """Renders the platform-health status grid from a status dict (see
  recoveryd.build_status)."""
  platform = status.get("platform", {})
  healthy = platform.get("healthy")
  if healthy is True:
    dot, txt = "dot-ok", "Platform is responding"
  elif healthy is False:
    dot, txt = "dot-down", "Platform is DOWN"
  else:
    dot, txt = "dot-unknown", "Platform status unknown"
  last_boot = status.get("last_successful_boot") or "never recorded"
  creds = status.get("cli_creds_present")
  creds_txt = "present" if creds else "missing"
  return f"""    <div class="status-grid">
      <div class="status"><span class="dot {dot}"></span><span>{html.escape(txt)}</span></div>
      <div class="status-meta">Last successful boot: <code>{html.escape(str(last_boot))}</code></div>
      <div class="status-meta">Agent CLI credentials: {html.escape(creds_txt)}</div>
    </div>"""


_CONFIRM_PLATFORM = (
  "Restore the platform by reverting uncommitted edits"
  " (git reset --hard). The agent's saved commits are kept."
  " The server will restart. Continue?"
)
_CONFIRM_BAKED = (
  "Reset the platform to the baked image floor. This wipes ALL"
  " uncommitted platform edits and recopies the shipped code, then"
  " restarts. Use only if the git restore wasn't enough. Continue?"
)


def dashboard_html(status: dict, msg: str = "") -> str:
  """Recovery dashboard: platform health + the Tier-1 restore actions."""
  msg_html = f'<p class="msg">{html.escape(msg)}</p>' if msg else ""
  body = f"""    <p class="sub">Restore a working platform. Chats and data are untouched.</p>
    {msg_html}
{_status_block(status)}

    <div class="recommended">
      <p class="label">&#10003; Restore platform (recommended)</p>
      <p class="desc">
        Reverts uncommitted edits to the platform code (<code>git reset
        --hard</code>) and restarts the server. This is the deterministic,
        offline floor — no agent, no network, no AI needed. Fixes the common
        case where a bad edit broke the platform.
      </p>
      <form method="POST" action="/recover/restore"
            onsubmit="{_confirm_attr(_CONFIRM_PLATFORM)}">
        <input type="hidden" name="mode" value="platform">
        <button class="btn btn-primary" type="submit">Restore platform</button>
      </form>
    </div>

    <hr>

    <div class="section">
      <p class="section-title">Last resort</p>
      <p class="desc" style="margin-bottom:8px;">
        Reset the platform code to the baked image floor — wipes every
        uncommitted edit and recopies the shipped code, then restarts. Use
        only if the git restore above wasn't enough.
      </p>
      <div class="actions">
        <form method="POST" action="/recover/restore"
              onsubmit="{_confirm_attr(_CONFIRM_BAKED)}">
          <input type="hidden" name="mode" value="platform-baked">
          <button class="btn btn-warn" type="submit">Reset to baked floor</button>
        </form>
      </div>
    </div>

    <hr>
    <div class="actions">
      <a href="/" class="btn btn-outline">&larr; Back to app</a>
      <form method="POST" action="/recover/logout">
        <button class="btn btn-outline" type="submit">Log out of recovery</button>
      </form>
    </div>"""
  return _page(body, _theme_mode())
