/* Lazy references retain successful pages and stable reads across reply projection. */
import { useEffect, useMemo, useRef, useState } from 'react'
import { apiFetch, jsonOrThrow } from '../../../api/client.js'
import { combineMessageSources, messageSources, messageSourcesUrl } from '../messageSources.js'

// A streaming answer re-renders every frame while only its live blocks
// change, so each blocks array is parsed for sources once.
const parsedBlocks = new WeakMap()
function blockSources(blocks) {
  if (!blocks || typeof blocks !== 'object') return messageSources(blocks)
  let parsed = parsedBlocks.get(blocks)
  if (!parsed) {
    parsed = messageSources(blocks)
    parsedBlocks.set(blocks, parsed)
  }
  return parsed
}

const EMPTY_PAGE = Object.freeze([])
const sameItems = (a, b) => a?.length === b.length && a.every((item, index) => item === b[index])

/** `combine(parts)` again only when a part changed; the same list otherwise. */
function useCombined(parts) {
  const ref = useRef(null)
  if (!sameItems(ref.current?.parts, parts)) ref.current = { parts, combined: combineMessageSources(parts) }
  return ref.current.combined
}

export default function useMessageSources({ chatId, groups, refs, open, request = apiFetch }) {
  const refsKey = JSON.stringify([...new Map((refs || [])
    .filter(ref => Number.isInteger(ref?.message_index) && ref.count > 0)
    .map(ref => [ref.message_index, ref])).values()])
  const plan = useMemo(() => JSON.parse(refsKey), [refsKey])
  const inline = useCombined((groups || []).map(blockSources))
  const key = JSON.stringify([chatId, refsKey])
  const cacheRef = useRef(null)
  if (cacheRef.current?.key !== key) cacheRef.current = { key, pages: new Map(), failed: new Set() }
  const cache = cacheRef.current
  const [, render] = useState(0)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (!open) return undefined
    const missing = plan.filter(ref => !cache.pages.has(ref.message_index) && !cache.failed.has(ref.message_index))
    if (!missing.length) return undefined
    const controller = new AbortController()
    let current = true
    Promise.allSettled(missing.map(ref => request(messageSourcesUrl(chatId, ref.message_index), {
      signal: controller.signal,
    }).then(response => jsonOrThrow(response, 'References failed to load')))).then(results => {
      if (!current || cacheRef.current !== cache) return
      results.forEach((result, index) => {
        const messageIndex = missing[index].message_index
        if (result.status === 'fulfilled') cache.pages.set(messageIndex, messageSources([{
          type: 'tool', sources: Array.isArray(result.value?.sources) ? result.value.sources : [],
        }]))
        else cache.failed.add(messageIndex)
      })
      render(value => value + 1)
    })
    return () => { current = false; controller.abort() }
  }, [open, key, cache, plan, chatId, request, attempt])

  const retry = () => { cache.failed.clear(); setAttempt(value => value + 1) }
  const complete = plan.every(ref => cache.pages.has(ref.message_index))
  const sources = useCombined([inline, ...plan.map(ref => cache.pages.get(ref.message_index) || EMPTY_PAGE)])
  return {
    sources,
    hasSources: plan.length > 0 || inline.length > 0,
    count: complete ? sources.length
      : plan.length === 1 && !inline.length ? plan[0].count : null,
    complete,
    failed: cache.failed.size > 0,
    retry,
  }
}
