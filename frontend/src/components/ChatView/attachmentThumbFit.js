// Attachment thumbnails are fixed squares that crop to their centre. Past this
// aspect ratio the centre slice can miss the content entirely: a wide header
// strip shows only the empty gap between its title and its buttons. Such
// images are fitted whole instead. Phone screenshots (about 1:2.2) stay cropped.
const MAX_CROPPED_ASPECT = 2.5

export function attachmentThumbFit(width, height) {
  if (!(width > 0 && height > 0)) return 'cover'
  const ratio = width / height
  return ratio > MAX_CROPPED_ASPECT || ratio < 1 / MAX_CROPPED_ASPECT ? 'contain' : 'cover'
}

/** `onLoad` for a square attachment thumbnail; CSS reads `data-fit`. */
export function markAttachmentThumbFit(event) {
  const img = event.currentTarget
  img.dataset.fit = attachmentThumbFit(img.naturalWidth, img.naturalHeight)
}
