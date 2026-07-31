/* MessageCopyButton — one-tap "copy this message" in a settled message's
   revealable metadata row.
   Exists because native long-press selection is unreliable on phones. It is a
   plain tap target only: it never intercepts press/hold or the context menu,
   so native text selection and its action menu stay fully available (the
   chatUiPolish test locks that invariant). */
import { useEffect, useRef, useState } from 'react'
import { Check, Copy } from '@openai/apps-sdk-ui/components/Icon'
import { copyPlainText } from './messageCopy.js'

export default function MessageCopyButton({ text }) {
  const [copied, setCopied] = useState(false)
  const timerRef = useRef(null)

  useEffect(() => () => clearTimeout(timerRef.current), [])

  async function handleCopy(e) {
    // The containing message row owns reveal timing; copying must not restart
    // that timer or double as another message-row click.
    e.stopPropagation()
    if (!(await copyPlainText(text))) return
    setCopied(true)
    clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => setCopied(false), 1600)
  }

  return (
    <button
      type="button"
      className={`chat__msg-copy${copied ? ' chat__msg-copy--copied' : ''}`}
      onClick={handleCopy}
      aria-label={copied ? 'Copied' : 'Copy message'}
      title="Copy message"
    >
      {copied
        ? <Check width={14} height={14} aria-hidden="true" />
        : <Copy width={14} height={14} aria-hidden="true" />}
    </button>
  )
}
