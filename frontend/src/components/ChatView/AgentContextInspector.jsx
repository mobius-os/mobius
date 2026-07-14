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
  padding: 24px;
  box-sizing: border-box;
  background: rgba(0, 0, 0, 0.45);
}

[data-theme="light"] .aci__overlay {
  background: rgba(15, 18, 25, 0.32);
}

.aci {
  width: min(720px, 100%);
  max-height: calc(100vh - 48px);
  max-height: calc(100dvh - 48px);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  gap: 14px;
  box-sizing: border-box;
  padding: 18px;
  border-radius: 14px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
}

[data-theme="light"] .aci {
  box-shadow:
    0 4px 12px rgba(0, 0, 0, 0.08),
    0 1px 3px rgba(0, 0, 0, 0.06);
}

.aci__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.aci__title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  line-height: 1.25;
}

.aci__close {
  flex-shrink: 0;
  border: 0;
  border-radius: 8px;
  padding: 4px 8px;
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
  gap: 8px;
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

.aci__empty {
  color: var(--muted);
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
  gap: 8px;
  padding: 11px 12px;
  cursor: pointer;
  user-select: none;
  color: var(--text);
  font-size: 13px;
  font-weight: 600;
  line-height: 1.35;
}

.aci-section__summary::-webkit-details-marker {
  display: none;
}

.aci-section__summary::before {
  content: "›";
  color: var(--muted);
  font-size: 18px;
  line-height: 1;
  transition: transform 0.12s ease;
}

.aci-section[open] .aci-section__summary::before {
  transform: rotate(90deg);
}

.aci-section__source {
  margin-left: auto;
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 2px 8px;
  color: var(--muted);
  background: var(--surface);
  font-size: 11px;
  font-weight: 500;
  line-height: 1.4;
}

.aci-section__content {
  margin: 0 12px 12px;
  max-height: min(42vh, 360px);
  overflow: auto;
  word-break: break-word;
  border: 1px solid var(--border-light);
  border-radius: 10px;
  padding: 12px;
  background: var(--surface);
  color: var(--text);
  font-size: 13px;
  line-height: 1.55;
}

.aci-section__content .md-blocks {
  gap: 0.6rem;
}

.aci-section__content .md-paragraph,
.aci-section__content .md-heading {
  padding-left: 0;
  padding-right: 0;
}
`

function hasText(value) {
  return typeof value === 'string' && value.trim() !== ''
}

function Section({ title, source, children }) {
  return (
    <details className="aci-section">
      <summary className="aci-section__summary">
        <span>{title}</span>
        {source && <span className="aci-section__source">{source}</span>}
      </summary>
      <div className="aci-section__content">
        <StandardMarkdown text={children} />
      </div>
    </details>
  )
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
    return [
      {
        key: 'system_prompt',
        title: 'System prompt — static (core.md); held constant across turns so the prompt cache keeps hitting',
        value: data.system_prompt,
        source: data.system_prompt_source
          ? `source: ${data.system_prompt_source}`
          : null,
      },
      {
        key: 'memory_block',
        title: 'Memory — recalled knowledge-graph facts, injected into the FIRST user turn (NOT part of the system prompt, so the cache stays warm)',
        value: data.memory_block,
      },
      { key: 'app_context', title: 'App context', value: data.app_context },
      { key: 'app_report', title: 'Report', value: data.app_report },
      { key: 'compaction_brief', title: 'Compaction', value: data.compaction_brief },
    ].filter(section => hasText(section.value))
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
          <h2 id="aci-title" className="aci__title">System prompt inspector</h2>
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
          {state.status === 'ready' && sections.length === 0 && (
            <p className="aci__state aci__empty">No agent context is available for this chat.</p>
          )}
          {state.status === 'ready' && sections.map(section => (
            <Section
              key={section.key}
              title={section.title}
              source={section.source}
            >
              {section.value}
            </Section>
          ))}
        </div>
      </div>
    </div>
  )
}
