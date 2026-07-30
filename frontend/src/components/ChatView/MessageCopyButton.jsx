/* MessageCopyButton — one-tap "copy this message" under a settled chat row.
   Exists because native long-press selection is unreliable on phones. It is a
   plain tap target only: it never intercepts press/hold or the context menu,
   so native text selection and its action menu stay fully available (the
   chatUiPolish test locks that invariant). */
import { useEffect, useRef, useState } from 'react'
import Check from 'lucide-react/dist/esm/icons/check.mjs'
import Copy from 'lucide-react/dist/esm/icons/copy.mjs'
import { copyPlainText } from './messageCopy.js'

export default function MessageCopyButton({ text }) {
  const [copied, setCopied] = useState(false)
  const timerRef = useRef(null)

  useEffect(() => () => clearTimeout(timerRef.current), [])

  async function handleCopy(e) {
    // A tap on a user row also toggles its timestamp via the row's onClick;
    // copying must not double as that.
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
        ? <Check size={14} strokeWidth={2.3} aria-hidden="true" />
        : <Copy size={14} strokeWidth={2} aria-hidden="true" />}
      {copied && <span className="chat__msg-copy-label">Copied</span>}
    </button>
  )
}
