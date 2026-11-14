// Attachment thumbnails are fixed squares that crop to their centre. Past this
// aspect ratio the centre slice can miss the content entirely: a wide header
// strip shows only the empty gap between its title and its buttons, so it is
// fitted whole. A very tall capture fitted whole would be a sliver, so it keeps
// the crop anchored to its top, where a full-page screenshot's header is.
// Phone screenshots (about 1:2.2) keep the centre crop.
const MAX_CROPPED_ASPECT = 2.5

/** 'contain' (wide strip), 'top' (very tall) or 'cover' (centre crop). */
export function attachmentThumbFit(width, height) {
  if (!(width > 0 && height > 0)) return 'cover'
  const ratio = width / height
  if (ratio > MAX_CROPPED_ASPECT) return 'contain'
  if (ratio < 1 / MAX_CROPPED_ASPECT) return 'top'
  return 'cover'
}

/** `onLoad` for a square attachment thumbnail; CSS reads `data-fit`. */
export function markAttachmentThumbFit(event) {
  const img = event.currentTarget
  img.dataset.fit = attachmentThumbFit(img.naturalWidth, img.naturalHeight)
}
