import { useEffect, useMemo, useRef, useState } from 'react'
import { apiFetch } from '../../api/client.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
// Same marked + DOMPurify markdown pipeline used by MsgContent.
import { StandardMarkdown } from './markdown/BlockRenderer.jsx'


const CSS = `
.aci__overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: grid;
  place-items: center;
  padding: 18px;
  box-sizing: border-box;
  background: rgba(0, 0, 0, 0.45);
}

[data-theme="light"] .aci__overlay {
  background: rgba(15, 18, 25, 0.32);
}

.aci {
  width: min(680px, 100%);
  max-height: calc(100vh - 36px);
  max-height: calc(100dvh - 36px);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  gap: 0;
  box-sizing: border-box;
  padding: 0;
  border-radius: 18px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  box-shadow: 0 12px 28px rgba(0, 0, 0, 0.24);
}

[data-theme="light"] .aci {
  box-shadow:
    0 4px 12px rgba(0, 0, 0, 0.08),
    0 1px 3px rgba(0, 0, 0, 0.06);
}

.aci__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 18px;
  border-bottom: 1px solid var(--border);
}

.aci__title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  line-height: 1.25;
}

.aci__subtitle {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
}

.aci__close {
  flex-shrink: 0;
  border: 0;
  width: 40px;
  height: 40px;
  margin: -7px -7px 0 0;
  border-radius: 9px;
  padding: 0;
  background: none;
  color: var(--muted);
  font: inherit;
  font-size: 24px;
  line-height: 1;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}

.aci__close:hover {
  background: var(--surface2);
  color: var(--text);
}

.aci__body {
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  overscroll-behavior: contain;
}

.aci__state {
  margin: 0;
  padding: 18px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg);
  color: var(--muted);
  font-size: 13px;
  line-height: 1.5;
}

.aci__state--error {
  color: var(--danger);
}

.aci-section {
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg);
  overflow: hidden;
}

.aci-section__summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 13px 14px;
  cursor: pointer;
  user-select: none;
  color: var(--text);
  list-style: none;
}

.aci-section__summary::-webkit-details-marker {
  display: none;
}

.aci-section__chevron {
  flex: none;
  color: var(--muted);
  font-size: 18px;
  line-height: 1;
  transition: transform 0.12s ease;
}

.aci-section[open] .aci-section__chevron {
  transform: rotate(90deg);
}

.aci-section__heading {
  min-width: 0;
}

.aci-section__title {
  display: block;
  color: var(--text);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.35;
}

.aci-section__description {
  display: block;
  margin-top: 2px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 400;
  line-height: 1.4;
}

.aci-section__content {
  margin: 0;
  max-height: min(48vh, 420px);
  overflow: auto;
  word-break: break-word;
  border-top: 1px solid var(--border);
  padding: 14px;
  background: var(--surface);
  color: var(--text);
  font-size: 13px;
  line-height: 1.55;
}

.aci-section__empty {
  margin: 0;
  color: var(--muted);
}

.aci-recent {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.aci-recent__item {
  padding: 11px 12px;
  border: 1px solid var(--border-light);
  border-radius: 10px;
  background: var(--bg);
}

.aci-recent__name {
  margin: 0;
  color: var(--text);
  font-size: 13px;
  font-weight: 600;
  line-height: 1.4;
}

.aci-recent__digest {
  margin: 5px 0 0;
  color: var(--text);
  font-size: 13px;
  line-height: 1.5;
}

.aci-recent__location {
  display: block;
  margin-top: 7px;
  color: var(--muted);
  font-family: var(--mono);
  font-size: 11px;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.aci-section__content .md-blocks {
  gap: 0.6rem;
}

.aci-section__content .md-paragraph,
.aci-section__content .md-heading {
  padding-left: 0;
  padding-right: 0;
}

@media (max-width: 640px) {
  .aci__overlay {
    align-items: end;
    padding: 10px;
  }

  .aci {
    max-height: calc(100dvh - 20px);
    border-radius: 18px;
  }

  .aci__head {
    padding: 16px;
  }

  .aci__body {
    padding: 10px;
  }
}
`

function Section({ title, description, children }) {
  return (
    <details className="aci-section">
      <summary className="aci-section__summary">
        <span className="aci-section__heading">
          <span className="aci-section__title">{title}</span>
          <span className="aci-section__description">{description}</span>
        </span>
        <span className="aci-section__chevron" aria-hidden="true">›</span>
      </summary>
      <div className="aci-section__content">
        {children}
      </div>
    </details>
  )
}

function RecentChats({ entries }) {
  if (!entries.length) {
    return <p className="aci-section__empty">No recent chat summaries are available yet.</p>
  }
  return (
    <div className="aci-recent">
      {entries.map(entry => (
        <article className="aci-recent__item" key={entry.location}>
          <h3 className="aci-recent__name">{entry.name}</h3>
          <p className="aci-recent__digest">{entry.digest}</p>
          <code className="aci-recent__location">{entry.location}</code>
        </article>
      ))}
    </div>
  )
}

function hasText(value) {
  return typeof value === 'string' && value.trim() !== ''
}

export default function AgentContextInspector({ chatId, onClose }) {
  const [state, setState] = useState({ status: 'loading', data: null, error: '' })
  const dialogRef = useRef(null)
  const closeRef = useRef(null)

  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: onClose ? closeRef : dialogRef,
    onClose,
    closeOnEscape: !!onClose,
  })

  useEffect(() => {
    if (!chatId) {
      setState({ status: 'error', data: null, error: 'No chat selected.' })
      return
    }

    const controller = new AbortController()
    setState({ status: 'loading', data: null, error: '' })

    async function load() {
      try {
        const res = await apiFetch(`/chats/${chatId}/agent-context`, {
          signal: controller.signal,
        })
        if (!res.ok) {
          throw new Error(`Request failed (${res.status})`)
        }
        const data = await res.json()
        setState({ status: 'ready', data, error: '' })
      } catch (err) {
        if (err?.name === 'AbortError') return
        setState({
          status: 'error',
          data: null,
          error: err?.message || 'Could not load agent context.',
        })
      }
    }

    load()
    return () => controller.abort()
  }, [chatId])

  const sections = useMemo(() => {
    const data = state.data || {}
    const primary = [
      {
        key: 'system_prompt',
        title: 'System prompt',
        description: 'Rules and capabilities that shape every response.',
        value: data.system_prompt,
        type: 'markdown',
      },
      {
        key: 'recent_chats',
        title: 'Recent chat summaries',
        description: 'Names and digests from your latest conversations.',
        value: Array.isArray(data.recent_chat_entries)
          ? data.recent_chat_entries
          : [],
        type: 'recent',
      },
    ]
    const activeContext = [
      {
        key: 'app_context',
        title: 'Current app context',
        description: 'The app and project details attached to this chat.',
        value: data.app_context,
        type: 'markdown',
      },
      {
        key: 'app_report',
        title: 'App report',
        description: 'Diagnostics or findings an app attached to the next turn.',
        value: data.app_report,
        type: 'markdown',
      },
      {
        key: 'compaction_brief',
        title: 'Compaction handoff',
        description: 'The latest handoff used after this chat was compacted.',
        value: data.compaction_brief,
        type: 'markdown',
      },
    ].filter(section => hasText(section.value))
    return [...primary, ...activeContext]
  }, [state.data])

  return (
    <div
      className="aci__overlay"
      role="presentation"
      onClick={() => onClose?.()}
    >
      <style>{CSS}</style>
      <div
        ref={dialogRef}
        className="aci"
        role="dialog"
        aria-modal="true"
        aria-labelledby="aci-title"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="aci__head">
          <div>
            <h2 id="aci-title" className="aci__title">What the agent knows</h2>
            <p className="aci__subtitle">Core instructions and recent conversation context.</p>
          </div>
          {onClose && (
            <button
              ref={closeRef}
              type="button"
              className="aci__close"
              onClick={onClose}
              aria-label="Close"
            >×</button>
          )}
        </div>

        <div className="aci__body">
          {state.status === 'loading' && (
            <p className="aci__state">Loading agent context…</p>
          )}
          {state.status === 'error' && (
            <p className="aci__state aci__state--error">{state.error}</p>
          )}
          {state.status === 'ready' && sections.map(section => (
            <Section
              key={section.key}
              title={section.title}
              description={section.description}
            >
              {section.type === 'recent'
                ? <RecentChats entries={section.value} />
                : section.value
                  ? <StandardMarkdown text={section.value} />
                  : <p className="aci-section__empty">Not available for this chat.</p>}
            </Section>
          ))}
        </div>
      </div>
    </div>
  )
}
