"""Seeds a Hello World mini-app on first boot (no apps in DB yet).

Called from the FastAPI lifespan after DB init.  Compiles the JSX via
esbuild and inserts directly into the database — no API token needed.
"""

import os
from pathlib import Path

from app.compiler import compile_jsx
from app.database import SessionLocal
from app.models import App

_JSX = r'''
export default function HelloWorld() {
  function askAgent() {
    window.parent.postMessage(
      { type: 'moebius:new-chat', draft: 'What can you do?' },
      '*'
    )
  }

  return (
    <div style={{
      height: '100%', display: 'flex', flexDirection: 'column',
      alignItems: 'center', justifyContent: 'center',
      background: 'var(--bg)', color: 'var(--text)', fontFamily: 'var(--font)',
      padding: '24px', textAlign: 'center',
    }}>
      <div style={{ fontSize: '64px', marginBottom: '16px' }}>&#x1f44b;</div>
      <h1 style={{ fontSize: '28px', fontWeight: 700, margin: '0 0 8px' }}>
        Welcome to M&ouml;bius
      </h1>
      <p style={{ color: 'var(--muted)', maxWidth: '400px', lineHeight: 1.6 }}>
        Your personal AI agent that builds apps, answers questions,
        and customizes this entire interface.
      </p>
      <button
        onClick={askAgent}
        style={{
          marginTop: '24px',
          background: 'var(--accent)', color: '#fff', border: 'none',
          borderRadius: '8px', padding: '12px 24px', fontSize: '15px',
          fontWeight: 600, cursor: 'pointer',
        }}
      >
        Ask the agent what it can do
      </button>
    </div>
  )
}
'''


async def seed():
  """Creates the Hello World app if no apps exist yet."""
  db = SessionLocal()
  try:
    if db.query(App).count() > 0:
      return
    compiled_path = await compile_jsx(1, _JSX)
    app = App(
      name="Hello World",
      description="A simple starter app — ask the agent to modify or replace it.",
      jsx_source=_JSX,
      compiled_path=compiled_path,
      slug="hello-world",
    )
    db.add(app)
    db.commit()
    # Write JSX source to the apps directory too.
    app_dir = Path(os.environ.get("DATA_DIR", "/data")) / "apps" / "hello"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "index.jsx").write_text(_JSX, encoding="utf-8")
    print(f"Seeded Hello World app (id={app.id})")
  finally:
    db.close()
