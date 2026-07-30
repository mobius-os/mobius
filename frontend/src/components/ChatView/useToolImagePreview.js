/* Resolve and decode a viewed image before its disclosure changes layout. */
import { useEffect, useRef, useState } from 'react'
import { BASE } from '../../api/client.js'
import { mediaTokenParam } from '../../api/mediaToken.js'

const EMPTY_PREVIEW = {
  reference: null,
  status: 'idle',
  src: '',
  width: 0,
  height: 0,
}

async function sourceForReference(reference) {
  if (reference.kind === 'inline') return reference.src

  const tokenParam = await mediaTokenParam(reference.chatId)
  if (!tokenParam) return ''
  const path = `/api/chats/${encodeURIComponent(reference.chatId)}/${reference.collection}/`
    + encodeURIComponent(reference.filename)
  return `${BASE}${path}${tokenParam}`
}

async function decodeImage(src, assignImage) {
  if (!src) throw new Error('Image source unavailable')
  const image = new window.Image()
  assignImage(image)
  image.decoding = 'async'
  image.src = src

  if (typeof image.decode === 'function') {
    try {
      await image.decode()
    } catch (error) {
      // Chromium can reject decode after a successful load during cache
      // turnover. Natural dimensions are the honest readiness signal.
      if (!image.naturalWidth || !image.naturalHeight) throw error
    }
  } else if (!image.complete) {
    await new Promise((resolve, reject) => {
      image.onload = resolve
      image.onerror = () => reject(new Error('Image decode failed'))
    })
  }

  if (!image.naturalWidth || !image.naturalHeight) {
    throw new Error('Image dimensions unavailable')
  }
  return {
    src,
    width: image.naturalWidth,
    height: image.naturalHeight,
  }
}

/** Prepare only the image the owner is interacting with. */
export function useToolImagePreview(reference, {
  enabled = false,
  onSettled,
} = {}) {
  const [preview, setPreview] = useState(EMPTY_PREVIEW)
  const onSettledRef = useRef(onSettled)
  onSettledRef.current = onSettled

  useEffect(() => {
    if (!reference) {
      setPreview(EMPTY_PREVIEW)
      return undefined
    }
    if (!enabled) return undefined
    if (
      preview.reference === reference
      && (preview.status === 'ready' || preview.status === 'failed')
    ) return undefined

    let cancelled = false
    let image = null
    setPreview({
      reference,
      status: 'loading',
      src: '',
      width: 0,
      height: 0,
    })

    sourceForReference(reference)
      .then(src => decodeImage(src, value => { image = value }))
      .then(({ src, width, height }) => {
        if (cancelled) return
        onSettledRef.current?.()
        setPreview({
          reference,
          status: 'ready',
          src,
          width,
          height,
        })
      })
      .catch(() => {
        if (cancelled) return
        onSettledRef.current?.()
        setPreview({
          reference,
          status: 'failed',
          src: '',
          width: 0,
          height: 0,
        })
      })

    return () => {
      cancelled = true
      if (image && !image.complete) image.src = ''
    }
  }, [enabled, reference])

  return preview.reference === reference
    ? preview
    : { ...EMPTY_PREVIEW, reference }
}
