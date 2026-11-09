/* MessageCopyButton — one-tap "copy this message" in a settled message's
   revealable metadata row.
   Exists because native long-press selection is unreliable on phones. It is a
   plain tap target only: it never intercepts press/hold or the context menu,
   so native text selection and its action menu stay fully available (the
   chatUiPolish test locks that invariant).
   `read(button)` returns `{ plainText, markdown? }` at tap time, so a reply can
   read its rendered answer; a failed copy says so instead of doing nothing. */
import { useEffect, useRef, useState } from 'react'
import { Check, Copy } from '@openai/apps-sdk-ui/components/Icon'
import { writeClipboard } from './messageCopy.js'

const COPY_STATUS = { copied: 'Copied', failed: 'Couldn’t copy' }

export default function MessageCopyButton({ read }) {
  const [copyState, setCopyState] = useState('idle')
  const timerRef = useRef(null)

  useEffect(() => () => clearTimeout(timerRef.current), [])

  async function handleCopy(e) {
    // The containing message row owns reveal timing; copying must not restart
    // that timer or double as another message-row click.
    e.stopPropagation()
    const payload = read(e.currentTarget)
    const copied = payload ? await writeClipboard(payload) : false
    setCopyState(copied ? 'copied' : 'failed')
    clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => setCopyState('idle'), 1600)
  }

  const status = COPY_STATUS[copyState] || ''
  return (
    <>
      <button
        type="button"
        className={`chat__msg-copy chat__msg-copy--${copyState}`}
        onClick={handleCopy}
        aria-label={status || 'Copy message'}
        title={copyState === 'failed' ? 'Try copying again' : 'Copy message'}
      >
        {copyState === 'copied'
          ? <Check width={14} height={14} aria-hidden="true" />
          : <Copy width={14} height={14} aria-hidden="true" />}
        {copyState === 'failed' && <span aria-hidden="true">{status}</span>}
      </button>
      <span className="chat__sr-only" role="status" aria-live="polite">{status}</span>
    </>
  )
}
