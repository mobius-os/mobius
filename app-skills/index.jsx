// Skills — browse, install, and manage agent skills.
//
// Three zones: a source browser over public GitHub skill catalogs (fetched
// through /api/proxy, SKILL.md frontmatter parsed client-side), the installed
// list from GET /api/skills, and an embedded agent chat that can search the
// ecosystem and install on the owner's go (permissions.manage_skills gates the
// install/uninstall calls; the platform's finding-skills seed skill is the
// agent's playbook here).
//
// Catalog sources are app data (sources.json) — agent-editable like everything
// else, so "add the awesome-claude-skills list as a source" is a chat request,
// not a code change.

import { useEffect, useMemo, useRef, useState } from 'react'

const CSS = `
.skills-app { display: flex; flex-direction: column; height: 100%; font-family: var(--font); color: var(--text); background: var(--bg); }
.skills-app * { box-sizing: border-box; }
.sk-head { display: flex; align-items: center; gap: 8px; padding: 10px 14px; border-bottom: 1px solid var(--border); }
.sk-head h1 { font-size: 17px; margin: 0; flex: 1; }
.sk-tabs { display: flex; gap: 4px; }
.sk-tab { border: 1px solid var(--border); background: var(--surface); color: var(--text); border-radius: 8px; padding: 5px 12px; font-size: 13px; cursor: pointer; }
.sk-tab.active { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
.sk-body { flex: 1; overflow-y: auto; padding: 12px 14px; min-height: 0; }
.sk-crumbs { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; font-size: 12.5px; color: var(--muted); margin-bottom: 10px; }
.sk-crumbs button { border: none; background: none; color: var(--accent); cursor: pointer; padding: 2px 2px; font-size: 12.5px; }
.sk-row { display: flex; align-items: center; gap: 10px; width: 100%; text-align: left; padding: 9px 10px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface); margin-bottom: 8px; cursor: pointer; color: var(--text); font-size: 14px; }
.sk-row:hover { border-color: var(--accent); }
.sk-row .ico { flex: 0 0 auto; opacity: 0.75; }
.sk-card { border: 1px solid var(--border); border-radius: 12px; background: var(--surface); padding: 12px; margin-bottom: 10px; }
.sk-card h3 { margin: 0 0 4px 0; font-size: 15px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.sk-card p { margin: 0 0 8px 0; font-size: 13px; color: var(--muted); line-height: 1.45; }
.sk-chip { font-size: 10.5px; padding: 2px 7px; border-radius: 9px; border: 1px solid var(--border); color: var(--muted); font-weight: 500; white-space: nowrap; }
.sk-chip.seed { color: var(--accent); border-color: var(--accent); }
.sk-chip.installed { color: var(--green, #1e7a46); border-color: var(--green, #1e7a46); }
.sk-btn { border: 1px solid var(--accent); background: var(--accent); color: var(--accent-fg); border-radius: 8px; padding: 6px 14px; font-size: 13px; cursor: pointer; }
.sk-btn[disabled] { opacity: 0.5; cursor: default; }
.sk-btn.ghost { background: none; color: var(--accent); }
.sk-btn.danger { background: none; color: var(--danger); border-color: var(--danger); }
.sk-note { font-size: 12.5px; color: var(--muted); margin: 6px 0 12px; }
.sk-err { font-size: 13px; color: var(--danger); background: var(--surface); border: 1px solid var(--danger); border-radius: 10px; padding: 8px 10px; margin-bottom: 10px; white-space: pre-wrap; }
.sk-empty { text-align: center; color: var(--muted); padding: 28px 10px; font-size: 13.5px; }
.sk-chat { flex: 0 0 300px; border-top: 1px solid var(--border); min-height: 0; display: flex; flex-direction: column; }
.sk-chat.closed { flex-basis: 40px; }
.sk-chat-bar { display: flex; align-items: center; gap: 8px; padding: 8px 14px; font-size: 13px; color: var(--muted); cursor: pointer; user-select: none; }
.sk-chat-bar b { color: var(--text); font-weight: 600; }
.sk-chat-mount { flex: 1; min-height: 0; display: flex; }
.sk-chat-mount > * { flex: 1; }
.sk-uses { font-size: 11px; color: var(--muted); margin-left: auto; white-space: nowrap; }
`

const DEFAULT_SOURCES = [
  { label: 'Anthropic Skills', repo: 'anthropics/skills', path: '', ref: 'main' },
  { label: 'Hermes bundled', repo: 'NousResearch/hermes-agent', path: 'skills', ref: 'main' },
  { label: 'Hermes optional', repo: 'NousResearch/hermes-agent', path: 'optional-skills', ref: 'main' },
]

const CHAT_SYSTEM_PROMPT = `You are the embedded agent of the Skills app. The partner is browsing,
installing, and managing skills. Read shared/skills/finding-skills.md before acting — it holds the
catalog sources, the evaluation criteria, the trust ritual (read a third-party SKILL.md fully,
summarize what it instructs, install only on the partner's go), and the exact
POST /api/skills/install and DELETE /api/skills/{name} calls. When asked for "a skill for X",
search the sources, offer the best matches with one-line summaries and provenance, and install the
chosen one. Keep answers short; this is a side panel.`

function parseFrontmatter(text) {
  // Minimal SKILL.md frontmatter reader: leading --- block, flat scalars only.
  if (!text || !text.startsWith('---')) return { body: text || '' }
  const end = text.indexOf('\n---', 3)
  if (end === -1) return { body: text }
  const meta = {}
  for (const line of text.slice(3, end).split('\n')) {
    const i = line.indexOf(':')
    if (i > 0) {
      const key = line.slice(0, i).trim()
      const value = line.slice(i + 1).trim().replace(/^["']|["']$/g, '')
      if (key && value) meta[key] = value
    }
  }
  return { ...meta, body: text.slice(end + 4) }
}

function firstParagraph(body) {
  for (const raw of (body || '').split('\n')) {
    const line = raw.trim()
    if (line && !line.startsWith('#') && !line.startsWith('---')) return line
  }
  return ''
}

export default function SkillsApp({ appId, token }) {
  const [tab, setTab] = useState('browse')
  const [sources, setSources] = useState(DEFAULT_SOURCES)
  const [nav, setNav] = useState(null) // { source, path } | null = source list
  const [listing, setListing] = useState(null)
  const [skillMeta, setSkillMeta] = useState(null) // current dir's SKILL.md info
  const [installed, setInstalled] = useState([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [chatOpen, setChatOpen] = useState(true)
  const chatMountRef = useRef(null)

  const authed = useMemo(() => ({ Authorization: `Bearer ${token}` }), [token])

  const proxyJson = async (url) => {
    const res = await fetch(`/api/proxy?url=${encodeURIComponent(url)}`, { headers: authed })
    if (!res.ok) throw new Error(`fetch failed (${res.status}) for ${url}`)
    return JSON.parse(await res.text())
  }
  const proxyText = async (url) => {
    const res = await fetch(`/api/proxy?url=${encodeURIComponent(url)}`, { headers: authed })
    if (!res.ok) throw new Error(`fetch failed (${res.status}) for ${url}`)
    return res.text()
  }

  const refreshInstalled = async () => {
    try {
      const res = await fetch('/api/skills', { headers: authed })
      if (res.ok) setInstalled((await res.json()).skills || [])
    } catch { /* keep the last list; the API being down shows via installs */ }
  }

  useEffect(() => {
    refreshInstalled()
    window.mobius.storage.get('sources.json').then((saved) => {
      if (Array.isArray(saved) && saved.length) setSources(saved)
    })
  }, [])

  // Embedded agent chat — finding-skills.md is its playbook.
  useEffect(() => {
    if (!chatMountRef.current) return undefined
    let disposed = false
    let handle = null
    window.mobius.chat({
      mount: chatMountRef.current,
      persist: 'chat_id.json',
      systemPrompt: CHAT_SYSTEM_PROMPT,
      picker: false,
      onTurnDone: () => refreshInstalled(),
    }).then((h) => { if (disposed) h.destroy(); else handle = h })
    return () => { disposed = true; handle?.destroy() }
  }, [])

  const openPath = async (source, path) => {
    setBusy(true); setError(null); setNotice(null); setSkillMeta(null); setListing(null)
    setNav({ source, path })
    try {
      const clean = path.replace(/^\/+|\/+$/g, '')
      const url = `https://api.github.com/repos/${source.repo}/contents/${clean}?ref=${source.ref || 'main'}`
      const entries = await proxyJson(url)
      if (!Array.isArray(entries)) throw new Error('unexpected GitHub response (not a directory)')
      const dirs = entries.filter((e) => e.type === 'dir' && !e.name.startsWith('.'))
      const skillFile = entries.find((e) => e.type === 'file' && e.name.toUpperCase() === 'SKILL.MD')
      setListing(dirs)
      if (skillFile?.download_url) {
        const meta = parseFrontmatter(await proxyText(skillFile.download_url))
        setSkillMeta({
          name: meta.name || clean.split('/').pop(),
          description: meta.description || firstParagraph(meta.body),
          license: meta.license || null,
        })
      }
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  const installCurrent = async () => {
    if (!nav) return
    setBusy(true); setError(null); setNotice(null)
    try {
      const res = await fetch('/api/skills/install', {
        method: 'POST',
        headers: { ...authed, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          repo: nav.source.repo,
          path: nav.path.replace(/^\/+|\/+$/g, ''),
          ref: nav.source.ref || 'main',
        }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || `install failed (${res.status})`)
      setNotice(`Installed "${data.name}" — it's in your skills index now.`)
      refreshInstalled()
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  const uninstall = async (id) => {
    setBusy(true); setError(null); setNotice(null)
    try {
      const res = await fetch(`/api/skills/${encodeURIComponent(id)}`, {
        method: 'DELETE', headers: authed,
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || `uninstall failed (${res.status})`)
      setNotice(`Removed "${id}" (bytes snapshotted to git first).`)
      refreshInstalled()
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  const installedIds = useMemo(() => new Set(installed.map((s) => s.id)), [installed])
  const crumbs = nav ? nav.path.split('/').filter(Boolean) : []

  return (
    <div className="skills-app">
      <style>{CSS}</style>
      <div className="sk-head">
        <h1>Skills</h1>
        <div className="sk-tabs">
          <button className={`sk-tab ${tab === 'browse' ? 'active' : ''}`} onClick={() => setTab('browse')}>Browse</button>
          <button className={`sk-tab ${tab === 'installed' ? 'active' : ''}`} onClick={() => setTab('installed')}>
            Installed ({installed.length})
          </button>
        </div>
      </div>

      <div className="sk-body">
        {error && <div className="sk-err">{error}</div>}
        {notice && <div className="sk-note">{notice}</div>}

        {tab === 'browse' && !nav && (
          <>
            <div className="sk-note">
              Public skill catalogs. Open a source, drill into a folder — when it holds a
              SKILL.md you can install it. Ask the agent below to search across all of them.
            </div>
            {sources.map((s) => (
              <button key={`${s.repo}/${s.path}`} className="sk-row" onClick={() => openPath(s, s.path)}>
                <span className="ico">📚</span>
                <span><b>{s.label}</b> · {s.repo}{s.path ? `/${s.path}` : ''}</span>
              </button>
            ))}
          </>
        )}

        {tab === 'browse' && nav && (
          <>
            <div className="sk-crumbs">
              <button onClick={() => { setNav(null); setListing(null); setSkillMeta(null); setError(null) }}>Sources</button>
              <span>/</span>
              <button onClick={() => openPath(nav.source, nav.source.path)}>{nav.source.label}</button>
              {crumbs.slice(nav.source.path.split('/').filter(Boolean).length).map((seg, i, rel) => (
                <span key={i}>
                  <span> / </span>
                  <button onClick={() => openPath(nav.source, [
                    nav.source.path, ...rel.slice(0, i + 1),
                  ].filter(Boolean).join('/'))}>{seg}</button>
                </span>
              ))}
            </div>
            {busy && !listing && <div className="sk-empty">Loading…</div>}
            {skillMeta && (
              <div className="sk-card">
                <h3>
                  {skillMeta.name}
                  {skillMeta.license && <span className="sk-chip">{skillMeta.license}</span>}
                  {installedIds.has(nav.path.split('/').filter(Boolean).pop()) && (
                    <span className="sk-chip installed">installed</span>
                  )}
                </h3>
                <p>{skillMeta.description || 'No description in frontmatter.'}</p>
                <button className="sk-btn" disabled={busy} onClick={installCurrent}>Install skill</button>
              </div>
            )}
            {listing && listing.map((d) => (
              <button key={d.path} className="sk-row" onClick={() => openPath(nav.source, d.path)}>
                <span className="ico">📁</span>
                <span>{d.name}</span>
              </button>
            ))}
            {listing && !listing.length && !skillMeta && (
              <div className="sk-empty">Nothing recognizable here.</div>
            )}
          </>
        )}

        {tab === 'installed' && (
          <>
            {!installed.length && <div className="sk-empty">No skills yet — browse a catalog or ask the agent.</div>}
            {installed.map((s) => (
              <div key={s.id} className="sk-card">
                <h3>
                  {s.name}
                  <span className={`sk-chip ${s.provenance === 'seed' ? 'seed' : s.provenance.startsWith('installed') ? 'installed' : ''}`}>
                    {s.provenance}
                  </span>
                  {s.uses_30d > 0 && <span className="sk-uses">{s.uses_30d} uses / 30d</span>}
                </h3>
                <p>{s.description || '—'}</p>
                {s.provenance.startsWith('installed') && (
                  <button className="sk-btn danger" disabled={busy} onClick={() => uninstall(s.id)}>Remove</button>
                )}
              </div>
            ))}
          </>
        )}
      </div>

      <div className={`sk-chat ${chatOpen ? '' : 'closed'}`}>
        <div className="sk-chat-bar" onClick={() => setChatOpen(!chatOpen)}>
          <b>Skill agent</b>
          <span>— “find me a skill for…”</span>
          <span style={{ marginLeft: 'auto' }}>{chatOpen ? '▾' : '▴'}</span>
        </div>
        <div className="sk-chat-mount" ref={chatMountRef} style={{ display: chatOpen ? 'flex' : 'none' }} />
      </div>
    </div>
  )
}
