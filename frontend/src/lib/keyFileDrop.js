/*
 * A key file dropped onto its box on a key card or in Settings: read as text
 * into that box, never opened by the browser or attached to a chat message
 * (the chat pane leaves an element marked data-own-file-drop alone).
 */

/** The longest value the key vault keeps for one part. */
export const MAX_KEY_FILE = 8192

/**
 * Props for a textarea that takes a dropped key file: ``onText`` gets its
 * text, ``onError`` a short reason when the file is too long to be a key.
 */
export function keyFileDropProps(onText, onError) {
  return {
    'data-own-file-drop': '',
    onDragOver(event) {
      event.preventDefault()
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'
    },
    onDrop(event) {
      event.preventDefault()
      const file = event.dataTransfer?.files?.[0]
      if (!file) return
      if (file.size > MAX_KEY_FILE) {
        onError?.('That file is too large to be a key file.')
        return
      }
      file.text().then(text => onText(text.trim()), () => onError?.("That file couldn't be read."))
    },
  }
}
