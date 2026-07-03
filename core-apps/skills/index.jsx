import { useState, useEffect, useRef, useMemo } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

// Skills — a read-only browser for the agent's skills (the SKILL-style
// markdown files under /data/shared/skills). Inspired by the Skills screen in
// Hermex. Skills are shared, owner-authored context; a mini-app can READ shared
// storage with its scoped token but not WRITE it, so creating/editing a skill
// is routed to the Möbius agent via a new chat rather than an in-app save.

const CSS = `
/* mobius-ui:Root v1 — keep in sync; library candidate. */
.sk-root { position: relative; display: flex; flex-direction: column; height: 100%;
  overflow: hidden; background: var(--bg); color: var(--text); font-family: var(--font); }
.sk-scroll { flex: 1; min-height: 0; overflow-y: auto; -webkit-overflow-scrolling: touch; }
/* /mobius-ui:Root */

/* mobius-ui:Header v1 — keep in sync; library candidate. */
.sk-header { flex: 0 0 auto; display: flex; align-items: center; gap: 12px; min-height: 48px;
  padding: 12px 16px; background: var(--surface); border-bottom: 1px solid var(--border); }
.sk-brand { display: flex; align-items: center; gap: 11px; min-width: 0; flex: 1; }
.sk-mark { flex: 0 0 auto; width: 30px; height: 30px; border-radius: 9px; display: flex;
  align-items: center; justify-content: center; font-size: 16px;
  background: color-mix(in srgb, var(--accent) 16%, transparent); }
.sk-title { margin: 0; font-size: 18px; font-weight: 700; letter-spacing: -0.015em; }
.sk-subtitle { display: block; margin-top: 1px; font-size: 12px; color: var(--muted); }
.sk-iconbtn { flex: 0 0 auto; width: 40px; height: 40px; display: inline-flex; align-items: center;
  justify-content: center; border-radius: 10px; border: 1px solid var(--border); background: var(--surface);
  color: var(--text); cursor: pointer; transition: background .14s ease, transform .1s ease; }
.sk-iconbtn:active { transform: scale(0.94); }
.sk-iconbtn:disabled { opacity: 0.5; cursor: default; }
.sk-iconbtn svg { width: 18px; height: 18px; }
.sk-iconbtn.is-spinning svg { animation: sk-spin 0.9s linear infinite; }
@keyframes sk-spin { to { transform: rotate(360deg); } }
/* /mobius-ui:Header */

/* search */
.sk-searchwrap { position: sticky; top: 0; z-index: 5; padding: 12px 16px 8px; background: var(--bg); }
.sk-search { position: relative; display: flex; align-items: center; }
.sk-search svg { position: absolute; left: 12px; width: 17px; height: 17px; color: var(--muted); pointer-events: none; }
.sk-input { width: 100%; box-sizing: border-box; min-height: 44px; padding: 11px 14px 11px 38px;
  background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 12px;
  outline: none; font-family: var(--font); font-size: 16px; }
.sk-input:focus { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }

/* list */
.sk-list { display: flex; flex-direction: column; padding: 4px 12px 32px; }
.sk-row { display: flex; align-items: flex-start; gap: 13px; width: 100%; box-sizing: border-box;
  text-align: left; padding: 14px 8px; background: none; border: none; border-bottom: 1px solid var(--border-light, var(--border));
  color: var(--text); font-family: var(--font); cursor: pointer; }
.sk-row:last-child { border-bottom: none; }
.sk-row:active { background: color-mix(in srgb, var(--text) 5%, transparent); }
.sk-rowicon { flex: 0 0 auto; width: 40px; height: 40px; border-radius: 20px; display: flex;
  align-items: center; justify-content: center; font-size: 18px;
  background: color-mix(in srgb, var(--accent) 12%, transparent); }
.sk-rowbody { flex: 1; min-width: 0; }
.sk-rowname { font-size: 16px; font-weight: 650; letter-spacing: -0.01em; word-break: break-word; }
.sk-rowslug { font-size: 12px; color: var(--muted); font-family: var(--mono); margin-top: 1px; }
.sk-rowdesc { margin-top: 4px; font-size: 13.5px; line-height: 1.5; color: var(--muted);
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.sk-chev { flex: 0 0 auto; align-self: center; color: var(--muted); opacity: 0.6; }
.sk-chev svg { width: 18px; height: 18px; }

/* empty / status */
.sk-empty { display: flex; flex-direction: column; align-items: center; text-align: center; gap: 8px;
  margin: auto; padding: 56px 28px; color: var(--muted); }
.sk-empty-mark { width: 64px; height: 64px; margin-bottom: 8px; border-radius: 18px; display: flex;
  align-items: center; justify-content: center; font-size: 30px;
  background: color-mix(in srgb, var(--accent) 14%, transparent); }
.sk-empty-title { font-size: 17px; font-weight: 700; color: var(--text); }
.sk-empty-text { margin: 0; font-size: 14px; line-height: 1.6; max-width: 30ch; }
.sk-spinner { width: 26px; height: 26px; border-radius: 50%; border: 2.5px solid var(--border);
  border-top-color: var(--accent); animation: sk-spin 0.8s linear infinite; }
.sk-retry { margin-top: 6px; min-height: 40px; padding: 9px 18px; border-radius: 10px; border: 1px solid var(--border);
  background: var(--surface); color: var(--text); font-weight: 600; font-size: 14px; cursor: pointer; }

/* detail */
.sk-detail-head { position: sticky; top: 0; z-index: 5; display: flex; align-items: center; gap: 10px;
  padding: 12px 12px; background: var(--surface); border-bottom: 1px solid var(--border); }
.sk-back { flex: 0 0 auto; display: inline-flex; align-items: center; gap: 4px; min-height: 40px; padding: 6px 12px 6px 8px;
  border-radius: 10px; border: none; background: none; color: var(--accent); font-family: var(--font);
  font-size: 15px; font-weight: 600; cursor: pointer; }
.sk-back svg { width: 20px; height: 20px; }
.sk-detail-title { font-size: 16px; font-weight: 700; min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; flex: 1; }
.sk-md { padding: 18px 18px 48px; font-size: 15px; line-height: 1.65; max-width: 720px; margin: 0 auto; }
.sk-md h1 { font-size: 22px; font-weight: 750; letter-spacing: -0.02em; margin: 4px 0 12px; }
.sk-md h2 { font-size: 18px; font-weight: 700; margin: 26px 0 10px; padding-top: 6px; border-top: 1px solid var(--border-light, var(--border)); }
.sk-md h3 { font-size: 15.5px; font-weight: 700; margin: 20px 0 8px; }
.sk-md p { margin: 0 0 12px; }
.sk-md ul, .sk-md ol { margin: 0 0 12px; padding-left: 22px; }
.sk-md li { margin: 4px 0; }
.sk-md a { color: var(--accent); text-decoration: none; }
.sk-md code { font-family: var(--mono); font-size: 0.86em; background: color-mix(in srgb, var(--text) 8%, transparent);
  padding: 1px 5px; border-radius: 5px; word-break: break-word; }
.sk-md pre { background: var(--surface2, var(--surface)); border: 1px solid var(--border); border-radius: 10px;
  padding: 12px 14px; overflow-x: auto; margin: 0 0 14px; }
.sk-md pre code { background: none; padding: 0; font-size: 12.5px; line-height: 1.55; }
.sk-md blockquote { margin: 0 0 12px; padding: 2px 14px; border-left: 3px solid var(--accent);
  color: var(--muted); }
.sk-md table { border-collapse: collapse; width: 100%; margin: 0 0 14px; font-size: 13.5px; display: block; overflow-x: auto; }
.sk-md th, .sk-md td { border: 1px solid var(--border); padding: 7px 10px; text-align: left; }
.sk-md th { background: color-mix(in srgb, var(--text) 5%, transparent); font-weight: 650; }
.sk-md hr { border: none; border-top: 1px solid var(--border); margin: 20px 0; }
.sk-md img { max-width: 100%; }
`

const HAMMER = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="m15 12-8.5 8.5a2.12 2.12 0 1 1-3-3L12 9"/><path d="M17.64 15 22 10.64"/><path d="m20.91 11.7-1.25-1.25c-.6-.6-.93-1.4-.93-2.25v-.86L16.01 4.6a5.56 5.56 0 0 0-3.94-1.64H9l.92.82A6.18 6.18 0 0 1 12 8.4v1.56l2 2h.86c.85 0 1.65.34 2.25.93l1.25 1.25"/></svg>
const REFRESH = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>
const SEARCH = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
const CHEV = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m9 18 6-6-6-6"/></svg>
const BACK = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m15 18-6-6 6-6"/></svg>
const PLUS = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14"/><path d="M12 5v14"/></svg>

// Parse a skill's markdown into a display title + one-line description.
// Skill files are "# Title\n\n<description paragraph>..." with no frontmatter.
function parseSkill(name, content) {
  const slug = name.replace(/\.md$/, '')
  const text = content || ''
  const lines = text.split('\n')
  let title = ''
  let descStart = 0
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(/^#\s+(.+?)\s*$/)
    if (m) { title = m[1].trim(); descStart = i + 1; break }
  }
  if (!title) title = slug.replace(/[-_]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
  let description = ''
  for (let i = descStart; i < lines.length; i++) {
    const l = lines[i].trim()
    if (!l) { if (description) break; else continue }
    if (/^#{1,6}\s/.test(l) || l === '---') { if (description) break; else continue }
    description += (description ? ' ' : '') + l
    if (description.length > 240) break
  }
  return { slug, name, title, description: description.trim(), content: text }
}

export default function SkillsApp({ appId, token }) {
  const [skills, setSkills] = useState(null) // null = loading, [] = loaded-empty
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(null) // slug of open skill
  const navRef = useRef(null)

  const authHeaders = useMemo(() => ({ Authorization: `Bearer ${token}` }), [token])

  async function load() {
    setError(null)
    try {
      const res = await fetch('/api/storage/shared-list/skills/', { headers: authHeaders })
      if (!res.ok) throw new Error(`list ${res.status}`)
      const { entries } = await res.json()
      const files = (entries || []).filter((e) => e.type === 'file' && e.name.endsWith('.md') && !e.name.startsWith('.'))
      const parsed = await Promise.all(files.map(async (e) => {
        try {
          const r = await fetch(`/api/storage/shared/skills/${encodeURIComponent(e.name)}`, { headers: authHeaders })
          const md = r.ok ? await r.text() : ''
          return parseSkill(e.name, md)
        } catch { return parseSkill(e.name, '') }
      }))
      parsed.sort((a, b) => a.title.toLowerCase().localeCompare(b.title.toLowerCase()))
      setSkills(parsed)
      window.mobius?.signal?.('app_ready', { item_count: parsed.length })
    } catch (err) {
      setError(err.message || 'Could not load skills')
      setSkills([])
      window.mobius?.signal?.('error', { message: String(err.message || err), source: 'load' })
    }
  }

  useEffect(() => { load() }, []) // shared storage has no subscribe(); refresh is explicit

  async function refresh() {
    setRefreshing(true)
    await load()
    setRefreshing(false)
  }

  // Android/browser back for the detail drill-down.
  function openSkill(slug) {
    if (window.mobius?.nav?.open) {
      try {
        const handle = window.mobius.nav.open('skill-detail', () => { navRef.current = null; setSelected(null) })
        navRef.current = handle
      } catch { /* nav is best-effort */ }
    }
    setSelected(slug)
    window.mobius?.signal?.('item_opened', { type: 'skill' })
  }
  function closeSkill() {
    try { navRef.current?.close?.() } catch {}
    navRef.current = null
    setSelected(null)
  }

  function askAgent(draft) {
    window.parent.postMessage({ type: 'moebius:new-chat', draft }, window.location.origin)
  }

  const filtered = useMemo(() => {
    if (!skills) return []
    const q = query.trim().toLowerCase()
    if (!q) return skills
    return skills.filter((s) =>
      s.title.toLowerCase().includes(q) || s.slug.toLowerCase().includes(q) || s.description.toLowerCase().includes(q))
  }, [skills, query])

  const current = selected && skills ? skills.find((s) => s.slug === selected) : null
  const detailHtml = useMemo(() => {
    if (!current) return ''
    try { return DOMPurify.sanitize(marked.parse(current.content || '')) } catch { return '' }
  }, [current])

  // ---- Detail view ----
  if (current) {
    return (
      <div className="sk-root">
        <style>{CSS}</style>
        <div className="sk-detail-head">
          <button className="sk-back" onClick={closeSkill} aria-label="Back to skills">{BACK}<span>Skills</span></button>
          <div className="sk-detail-title">{current.title}</div>
          <button className="sk-iconbtn" onClick={() => askAgent(`Help me edit the "${current.slug}" skill. Here's what I want to change: `)} aria-label="Edit skill with the agent">{PLUS}</button>
        </div>
        <div className="sk-scroll">
          <div className="sk-md" dangerouslySetInnerHTML={{ __html: detailHtml }} />
        </div>
      </div>
    )
  }

  // ---- List view ----
  const loading = skills === null
  return (
    <div className="sk-root">
      <style>{CSS}</style>
      <header className="sk-header">
        <div className="sk-brand">
          <span className="sk-mark" aria-hidden="true">{HAMMER}</span>
          <div>
            <h1 className="sk-title">Skills</h1>
            <span className="sk-subtitle">{skills ? `${skills.length} agent ${skills.length === 1 ? 'skill' : 'skills'}` : 'Your agent’s abilities'}</span>
          </div>
        </div>
        <button className={`sk-iconbtn${refreshing ? ' is-spinning' : ''}`} onClick={refresh} disabled={refreshing} aria-label="Refresh skills">{REFRESH}</button>
      </header>

      <div className="sk-scroll">
        {!loading && !error && (
          <div className="sk-searchwrap">
            <div className="sk-search">
              {SEARCH}
              <input className="sk-input" type="search" value={query} placeholder="Search skills…"
                onChange={(e) => setQuery(e.target.value)} aria-label="Search skills" />
            </div>
          </div>
        )}

        {loading && (
          <div className="sk-empty"><div className="sk-spinner" /><div className="sk-empty-title">Loading skills…</div></div>
        )}

        {!loading && error && (
          <div className="sk-empty">
            <div className="sk-empty-mark" aria-hidden="true">⚠️</div>
            <div className="sk-empty-title">Couldn’t load skills</div>
            <p className="sk-empty-text">{error}</p>
            <button className="sk-retry" onClick={refresh}>Try again</button>
          </div>
        )}

        {!loading && !error && filtered.length === 0 && skills.length === 0 && (
          <div className="sk-empty">
            <div className="sk-empty-mark" aria-hidden="true">{HAMMER}</div>
            <div className="sk-empty-title">No skills yet</div>
            <p className="sk-empty-text">Skills extend what your agent can do. Ask the agent to create one and it’ll appear here.</p>
            <button className="sk-retry" onClick={() => askAgent('Create a new skill for me. It should: ')}>Ask the agent</button>
          </div>
        )}

        {!loading && !error && filtered.length === 0 && skills.length > 0 && (
          <div className="sk-empty">
            <div className="sk-empty-mark" aria-hidden="true">{SEARCH}</div>
            <div className="sk-empty-title">No matches</div>
            <p className="sk-empty-text">No skills match “{query}”.</p>
          </div>
        )}

        {!loading && !error && filtered.length > 0 && (
          <div className="sk-list">
            {filtered.map((s) => (
              <button key={s.slug} className="sk-row" onClick={() => openSkill(s.slug)}>
                <span className="sk-rowicon" aria-hidden="true">{HAMMER}</span>
                <span className="sk-rowbody">
                  <div className="sk-rowname">{s.title}</div>
                  <div className="sk-rowslug">{s.slug}</div>
                  {s.description && <div className="sk-rowdesc">{s.description}</div>}
                </span>
                <span className="sk-chev" aria-hidden="true">{CHEV}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
