"""HTML/CSS templates for the recovery page.

Separated from recover.py so route logic and markup are easier to
maintain independently.
"""

import html
import json


def _confirm_attr(text: str) -> str:
  """Returns a properly escaped HTML attribute value that runs
  `return confirm(<text>)` on submit. json.dumps gives a valid JS
  string literal (handles quotes, backslashes, control chars); the
  surrounding html.escape makes the value safe inside an HTML
  double-quoted attribute. Without this, editing any _CONFIRM_*
  constant to contain an apostrophe or </script> would break or
  poison the page.
  """
  js = f"return confirm({json.dumps(text)});"
  return html.escape(js, quote=True)

_STYLE = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: var(--font, system-ui, -apple-system, sans-serif);
    /* Fallback colors are kept in sync with backend/app/theme.py
       DEFAULT_THEME so the recovery page reads as the same shell
       even when the React app's theme injection hasn't run (the
       recovery page is meant to load when things are broken). */
    background: var(--bg, #0d0d0d); color: var(--text, #ececec);
    min-height: 100vh;
    padding: 32px 16px;
  }
  .card {
    background: var(--surface, #171717); border: 1px solid var(--border, #2a2a2a);
    border-radius: 14px; padding: 24px;
    max-width: 480px; width: 100%;
    margin: 0 auto;
  }
  body.theme-light .card {
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
    background: var(--bg, #0d0d0d); border: 1px solid var(--border, #2a2a2a);
    border-radius: 10px; color: var(--text, #ececec); margin-bottom: 14px;
    outline: none;
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  input:focus {
    border-color: var(--accent, #8b6cf7);
    box-shadow: 0 0 0 3px var(--accent-dim, rgba(139, 108, 247, 0.14));
  }
  input:focus-visible {
    outline: none;
  }
  .btn {
    display: inline-block; padding: 11px 18px; font-size: 14px; font-weight: 500;
    border: none; border-radius: 10px; cursor: pointer;
    color: #fff; text-decoration: none; text-align: center;
    width: 100%;
    transition: background 0.15s, border-color 0.15s, color 0.15s;
    /* Consistency with the React app: kill the mobile tap-highlight
       overlay and the accidental text-selection on press. */
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
  /* Danger actions read as OUTLINE by default; the fill only
     appears on hover/focus. Three solid-red CTAs in a column
     read alarmist for a recovery surface where the safe actions
     are next to them. The destructive intent is still clear from
     the red text/border + the per-action confirm() dialog. */
  .btn-warn {
    background: transparent;
    color: var(--danger, #f87171);
    border: 1px solid var(--danger, #f87171);
  }
  @media (hover: hover) and (pointer: fine) {
    .btn-warn:hover { background: var(--danger, #f87171); color: #fff; }
  }
  .btn-warn:active { background: var(--danger, #f87171); color: #fff; }
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
    letter-spacing: 0;
    text-transform: none;
    color: var(--accent, #8b6cf7);
    margin-bottom: 6px;
    display: flex; align-items: center; gap: 6px;
  }
  .recommended .desc {
    font-size: 13px; color: var(--muted, #9b9b9b);
    line-height: 1.5; margin-bottom: 14px;
  }
  /* The "primary" CTA inside the recommended panel becomes a
     plain accent-text outline button — solid purple-on-purple
     was hard to read. */
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
  .desc {
    font-size: 13px; color: var(--muted, #9b9b9b);
    line-height: 1.5;
  }
"""


_CLEAR_STORAGE_SCRIPT = """
  <script>
    /* After factory reset, the server-side state is wiped but the
       browser still holds the old JWT in localStorage, the
       TanStack Query IndexedDB cache, and the SetupWizard resume key.
       Clear them here so the next visit to / doesn't try to use stale
       credentials or render stale chat data.

       IndexedDB: the React app persists the query cache via idb-keyval
       under key 'mobius-query-cache' inside idb-keyval's default
       database 'keyval-store'. Deleting the DB drops the only key the
       app keeps there; idb-keyval recreates it lazily on next use. */
    try {
      localStorage.removeItem('token');
      localStorage.removeItem('setup-step');
      localStorage.removeItem('auth_expired');
      sessionStorage.clear();
      if (window.indexedDB) {
        indexedDB.deleteDatabase('keyval-store');
      }
    } catch (e) { /* private mode / quota — best effort */ }
  </script>
"""


def login_html(error: str = "", clear_storage: bool = False) -> str:
  """Returns the recovery login page HTML.

  When clear_storage=True, includes an inline script that wipes the
  React app's localStorage / sessionStorage / IndexedDB cache. Used
  after factory reset so the next visit to the React app doesn't
  pick up a stale token or render cached chat data.
  """
  error_html = (
    f'<p class="error">{html.escape(error)}</p>' if error else ""
  )
  clear_html = _CLEAR_STORAGE_SCRIPT if clear_storage else ""
  return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Möbius Recovery</title>
  <style>{_STYLE}</style>{clear_html}
</head>
<body>
  <div class="card">
    <h1>&#8734; Recovery</h1>
    <p class="sub">Log in to access recovery tools.</p>
    {error_html}
    <form method="POST" action="/recover/auth">
      <label for="username">Username</label>
      <input id="username" name="username" required autofocus>
      <label for="password">Password</label>
      <input id="password" name="password" type="password" required>
      <button class="btn btn-primary" type="submit">Log in</button>
    </form>
  </div>
</body>
</html>"""


_CONFIRM_RESTORE = (
  "Restore the interface from the original image?"
  " Your chats and mini-apps will not be affected."
)
_CONFIRM_APPS = (
  "Delete all mini-apps? Your data files will remain."
)
_CONFIRM_AUTH = (
  "Reset CLI auth? You will need to sign in again."
)
_CONFIRM_FACTORY = (
  "FACTORY RESET: This deletes your account, all apps,"
  " CLI credentials, and theme — but preserves chat history."
  " Are you sure?"
)
_CONFIRM_BACKUP = (
  "Backup includes OAuth credentials for Claude and Codex."
  " Store the file securely. Continue?"
)


def dashboard_html(msg: str = "") -> str:
  """Returns the recovery dashboard page HTML."""
  msg_html = (
    f'<p class="msg">{html.escape(msg)}</p>' if msg else ""
  )
  return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Möbius Recovery</title>
  <style>{_STYLE}</style>
</head>
<body>
  <div class="card">
    <h1>&#8734; Recovery</h1>
    <p class="sub">Restore a working state. Your chats and data are safe.</p>
    {msg_html}

    <div class="recommended">
      <p class="label">&#10003; Talk to the agent</p>
      <p class="desc">Opens a minimal chat with the agent so it can diagnose and fix what's broken. The agent has elevated write access to backend code, scripts, and shell from here.</p>
      <a class="btn btn-primary" href="/recover/chat" style="display:inline-block;text-decoration:none;">
        Open recovery chat
      </a>
    </div>

    <div class="section">
      <p class="section-title">Restore from baked sources</p>
      <p class="desc" style="margin-bottom:8px;">If the agent made things worse, copy the original baked source back over the live copy. Chats, mini-apps, and settings are untouched.</p>
      <div class="actions">
        <form method="POST" action="/recover/action"
              onsubmit="{_confirm_attr(_CONFIRM_RESTORE)}">
          <input type="hidden" name="action" value="restore_shell">
          <button class="btn btn-outline" type="submit">
            Restore shell
          </button>
        </form>
        <form method="POST" action="/recover/action"
              onsubmit="return confirm('Restore /app/app/ from the baked backend copy and restart the server. Continue?');">
          <input type="hidden" name="action" value="restore_backend">
          <button class="btn btn-outline" type="submit">
            Restore backend
          </button>
        </form>
        <form method="POST" action="/recover/action"
              onsubmit="return confirm('Restore /app/scripts/ from the baked scripts copy. Continue?');">
          <input type="hidden" name="action" value="restore_scripts">
          <button class="btn btn-outline" type="submit">
            Restore scripts
          </button>
        </form>
      </div>
    </div>

    <div class="section">
      <p class="section-title">Other safe actions</p>
      <div class="actions">
        <form method="POST" action="/recover/action"
              onsubmit="{_confirm_attr(_CONFIRM_BACKUP)}">
          <input type="hidden" name="action" value="download_backup">
          <button class="btn btn-outline" type="submit">
            Download backup (.zip)
          </button>
          <p style="font-size:12px;color:#94a3b8;margin-top:6px;">Backup includes CLI auth credentials. Store the backup file securely.</p>
        </form>
        <form method="POST" action="/recover/action">
          <input type="hidden" name="action" value="reset_chat">
          <button class="btn btn-outline" type="submit">
            Reset chat log
          </button>
        </form>
      </div>
    </div>

    <hr>

    <div class="section">
      <p class="section-title">Destructive actions</p>
      <div class="actions">
        <form method="POST" action="/recover/action"
              onsubmit="{_confirm_attr(_CONFIRM_APPS)}">
          <input type="hidden" name="action" value="reset_apps">
          <button class="btn btn-warn" type="submit">
            Reset all mini-apps
          </button>
        </form>
        <form method="POST" action="/recover/action"
              onsubmit="{_confirm_attr(_CONFIRM_AUTH)}">
          <input type="hidden" name="action" value="reset_settings">
          <button class="btn btn-warn" type="submit">
            Reset CLI auth
          </button>
        </form>
        <form method="POST" action="/recover/action"
              onsubmit="{_confirm_attr(_CONFIRM_FACTORY)}">
          <input type="hidden" name="action" value="factory_reset">
          <button class="btn btn-warn" type="submit">
            Factory reset
          </button>
        </form>
      </div>
    </div>

    <hr>
    <div class="actions">
      <a href="/" class="btn btn-outline">
        &larr; Back to app
      </a>
      <form method="POST" action="/recover/logout">
        <button class="btn btn-outline" type="submit">
          Log out of recovery
        </button>
      </form>
    </div>
  </div>
</body>
</html>"""
