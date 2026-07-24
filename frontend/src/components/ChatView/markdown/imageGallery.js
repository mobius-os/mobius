/**
 * Turn adjacent image-only paragraphs into one gallery block.
 *
 * The authoring contract stays ordinary Markdown: two or more images with no
 * prose between them form a gallery. A lone image and images mixed into prose
 * keep the existing paragraph rendering.
 */
function paragraphImages(token) {
  if (token?.type !== 'paragraph' || !Array.isArray(token.tokens)) return null

  const images = []
  for (const inline of token.tokens) {
    if (inline.type === 'image') {
      images.push(inline)
      continue
    }
    if (inline.type === 'text' && !(inline.text || '').trim()) continue
    if (inline.type === 'br') continue
    return null
  }

  return images.length ? images : null
}

export function groupMarkdownImages(tokens = []) {
  const blocks = []
  let run = []

  function flushRun() {
    if (!run.length) return

    const images = run.flatMap(({ images }) => images)
    if (images.length > 1) {
      blocks.push({
        type: 'imageGallery',
        images,
        raw: run.map(({ token }) => token.raw || '').join('\n\n'),
      })
    } else {
      blocks.push(run[0].token)
    }
    run = []
  }

  for (const token of tokens) {
    if (token.type === 'space') continue

    const images = paragraphImages(token)
    if (images) {
      run.push({ token, images })
      continue
    }

    flushRun()
    blocks.push(token)
  }

  flushRun()
  return blocks
}

export function projectResolvedGalleryItems(images = [], resolvedSources = new Map()) {
  const occurrences = new Map()
  return images.map((image) => {
    const identity = JSON.stringify([image.href || '', image.text || ''])
    const occurrence = occurrences.get(identity) || 0
    occurrences.set(identity, occurrence + 1)
    return {
      key: `${identity}:${occurrence}`,
      href: image.href,
      src: resolvedSources.get(image.href) || null,
      alt: image.text || '',
    }
  })
}

export function gallerySwipeTarget({ deltaX, deltaY, index, items = [] }) {
  if (Math.abs(deltaX) < 48 || Math.abs(deltaX) <= Math.abs(deltaY) * 1.2) {
    return null
  }
  const nextIndex = deltaX > 0 ? index - 1 : index + 1
  if (nextIndex < 0 || nextIndex >= items.length || !items[nextIndex]?.src) {
    return null
  }
  return nextIndex
}
