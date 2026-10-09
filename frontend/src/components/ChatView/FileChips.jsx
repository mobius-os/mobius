import { useRef, useState, useEffect } from 'react'
import ImageLightbox from './markdown/ImageLightbox.jsx'
import ChatPanePortal from './ChatPanePortal.jsx'
import { useHistoryDismiss } from '../../hooks/useHistoryDismiss.jsx'
import { BASE } from '../../api/client.js'
import { mediaTokenParam } from '../../api/mediaToken.js'

/** Classifies a file by extension into a colored badge variant.
 *  Returns {kind, label} where kind = 'pdf' | 'doc' | 'code' and
 *  label is the short tag shown inside the badge. */
function classifyFile(name) {
  const ext = (name.split('.').pop() || '').toLowerCase()
  if (ext === 'pdf') return { kind: 'pdf', label: 'PDF' }
  if (['doc', 'docx', 'rtf', 'odt'].includes(ext)) return { kind: 'doc', label: 'DOC' }
  if (['xls', 'xlsx', 'csv', 'tsv'].includes(ext)) return { kind: 'doc', label: 'XLS' }
  if (['ppt', 'pptx'].includes(ext)) return { kind: 'doc', label: 'PPT' }
  if (['md', 'markdown', 'txt'].includes(ext)) return { kind: 'doc', label: 'TXT' }
  if (['zip', 'tar', 'gz', 'rar', '7z'].includes(ext)) return { kind: 'doc', label: 'ZIP' }
  return { kind: 'code', label: (ext || 'FILE').toUpperCase().slice(0, 4) }
}

/** Strip the trailing `.ext` so the visible name reads like a label
 *  rather than a file. The badge already communicates the type
 *  (PDF / DOC / TXT / etc.), so the extension is redundant and just
 *  eats horizontal room on a fixed-width card. Leaves names
 *  without a dot untouched and
 *  preserves any earlier dots in the name (e.g. `report.v2.pdf`
 *  → `report.v2`). */
function stripExt(name) {
  if (!name) return name
  const idx = name.lastIndexOf('.')
  if (idx <= 0) return name
  return name.slice(0, idx)
}

/** Fixed-box attach cards rendered inside the pill above the input
 *  row when files are attached. Two variants:
 *   - image (PNG/JPEG/etc.): 72×72 square thumbnail; the image IS
 *     the identifier so no filename label.
 *   - file (PDF/DOC/code): 168px-wide rectangle with a colored
 *     type badge and the filename below.
 *  The remove `×` is a 20×20 button floating at the card's top-
 *  right corner (half-overlapping outside). */
export default function FileChips({ files, onRemove, chatId, disabled = false }) {
  const trayRef = useRef(null)
  const [tokenState, setTokenState] = useState({
    chatId: null,
    param: '',
    failed: false,
  })
  // Index into the attached-image gallery currently shown full-screen.
  const [lightboxIndex, setLightboxIndex] = useState(null)
  const historyDismiss = useHistoryDismiss(() => setLightboxIndex(null))
  const hasRestoredImage = files?.some(file => (
    file.mime_type?.startsWith('image/') && !file.objectUrl
  ))

  useEffect(() => {
    if (!hasRestoredImage || !chatId) {
      setTokenState({ chatId: null, param: '', failed: false })
      return undefined
    }
    let cancelled = false
    setTokenState({ chatId, param: '', failed: false })
    mediaTokenParam(chatId).then(param => {
      if (!cancelled) setTokenState({ chatId, param, failed: !param })
    }).catch(() => {
      if (!cancelled) setTokenState({ chatId, param: '', failed: true })
    })
    return () => { cancelled = true }
  }, [chatId, hasRestoredImage])

  // Never reuse a previous chat's token during the effect boundary.
  const currentTokenState = tokenState.chatId === chatId
    ? tokenState
    : { param: '', failed: false }

  if (!files?.length) return null

  const cards = files.map(chip => {
    const isImage = !!chip.objectUrl || chip.mime_type?.startsWith('image/')
    const previewSrc = chip.objectUrl || (
      isImage && currentTokenState.param
        ? `${BASE}/api/chats/${chatId}/uploads/${encodeURIComponent(chip.name)}${currentTokenState.param}`
        : ''
    )
    return {
      chip,
      isImage,
      previewSrc,
      previewFailed: !!(isImage && !chip.objectUrl && currentTokenState.failed),
    }
  })
  // Every viewable attached image, in tray order, so the full-screen
  // viewer can page/swipe between them like a sent-message gallery. Each
  // card records its own gallery position: two attachments can share a
  // preview URL (same restored filename), so looking the index up by src
  // would open the wrong one.
  const gallery = []
  for (const card of cards) {
    if (!card.isImage || !card.previewSrc) continue
    card.galleryIndex = gallery.length
    gallery.push({ src: card.previewSrc, alt: card.chip.name })
  }
  // Removing an attachment while open can invalidate the index; treat
  // an out-of-range index as closed rather than showing the wrong image.
  const openIndex = lightboxIndex !== null && lightboxIndex < gallery.length
    ? lightboxIndex
    : null

  return (
    <div ref={trayRef} className="chat__attach-tray">
      {cards.map(({ chip, isImage, previewSrc, previewFailed, galleryIndex }) => {
        const cls = classifyFile(chip.name || '')
        const errorMark = chip.status === 'error' ? ' chat__attach-card--error' : ''
        return (
          <div
            key={chip.id}
            className={
              'chat__attach-card'
              + (isImage ? ' chat__attach-card--image' : ' chat__attach-card--file')
              + errorMark
            }
            title={previewFailed
              ? `${chip.name} — preview unavailable; attachment is still ready to send`
              : (chip.status === 'error' ? chip.error : chip.name)}
          >
            {isImage && previewSrc ? (
              <button
                type="button"
                className="chat__attach-card-thumb-button"
                // Preserve the textarea until the dialog mounts. The shared
                // lightbox then moves focus into itself deliberately and
                // restores this button/text-entry context when it closes.
                onPointerDown={(e) => e.preventDefault()}
                onClick={() => {
                  historyDismiss.open()
                  setLightboxIndex(galleryIndex)
                }}
                aria-label={`Preview ${chip.name}`}
              >
                <img className="chat__attach-card-thumb" src={previewSrc} alt="" />
              </button>
            ) : previewFailed ? (
              <span className="chat__attach-card-preview-error" role="status">
                Preview unavailable
              </span>
            ) : isImage ? (
              <span className="chat__attach-card-spin" aria-hidden="true" />
            ) : (
              <>
                <span className={`chat__attach-card-icon chat__attach-card-icon--${cls.kind}`}>
                  {cls.label}
                </span>
                <span className="chat__attach-card-name">{stripExt(chip.name)}</span>
              </>
            )}
            {chip.status === 'uploading' && (
              <span className="chat__attach-card-spin" aria-hidden="true" />
            )}
            <button
              type="button"
              className="chat__attach-card-remove"
              disabled={disabled}
              // Keep the soft keyboard up — without preventDefault on
              // pointerdown the tap shifts focus off the textarea and
              // iOS collapses the keyboard. Matches the same trick
              // used on the `+` trigger, the popover rows, and every
              // other interactive element inside the composer.
              onPointerDown={(e) => e.preventDefault()}
              onClick={() => onRemove(chip.id)}
              aria-label={`Remove ${chip.name}`}
            >×</button>
          </div>
        )
      })}
      {openIndex !== null && <ChatPanePortal anchorRef={trayRef}>
        <ImageLightbox
          src={gallery[openIndex].src}
          alt={gallery[openIndex].alt}
          items={gallery}
          index={openIndex}
          onNavigate={setLightboxIndex}
          onClose={historyDismiss.close}
        />
      </ChatPanePortal>}
    </div>
  )
}
