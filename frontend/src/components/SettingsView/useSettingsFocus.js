import { useEffect, useRef } from 'react'
import { settingsSectionPage } from '../../lib/settingsSections.js'

// Where a request lands: the page to show (null for the overview) and the
// section on it to focus (null when the request is the page itself). Chat
// models live on each provider's page, so `models` opens the first connected
// provider's, or the AI providers list while none is connected.
function destination(section, modelsProvider) {
  if (section === 'models') {
    return modelsProvider
      ? { page: modelsProvider, anchor: 'chat-models' }
      : { page: null, anchor: 'ai-providers' }
  }
  const page = settingsSectionPage(section)
  return page ? { page, anchor: null } : { page: null, anchor: section }
}

/**
 * Navigate an explicit Settings request once: open its page, then focus its
 * section there. The destination is fixed when the request is taken, so a
 * later change (say, a fresh read reordering the providers before the focus
 * frame) cannot leave the open page waiting for a section it no longer shows.
 */
export function useSettingsFocus({
  focusTarget, providerReady, modelsProvider = null, detailPage, setDetailPage,
  setupFocusRefs, setAttentionSection,
}) {
  // { request, page, anchor } for the request being navigated.
  const taken = useRef(null)
  const handled = useRef(null)
  const section = focusTarget?.section
  // `models` needs the provider list to pick its page.
  const waiting = section === 'models' && !providerReady

  useEffect(() => {
    if (!section || waiting || taken.current?.request === focusTarget) return
    taken.current = { request: focusTarget, ...destination(section, modelsProvider) }
    setDetailPage(taken.current.page)
  }, [focusTarget, section, waiting, modelsProvider, setDetailPage])

  useEffect(() => {
    const target = taken.current
    if (target?.request !== focusTarget || !target.anchor || detailPage !== target.page) return undefined
    if (handled.current === focusTarget) return undefined
    const { anchor } = target
    let clearTimer = null
    const raf = requestAnimationFrame(() => {
      const node = setupFocusRefs.current[anchor]
      if (!node) return
      handled.current = focusTarget
      node.scrollIntoView({ behavior: 'smooth', block: 'start' })
      node.focus({ preventScroll: true })
      setAttentionSection(anchor)
      clearTimer = setTimeout(() => {
        setAttentionSection(current => current === anchor ? '' : current)
      }, 1800)
    })
    return () => {
      cancelAnimationFrame(raf)
      if (clearTimer) clearTimeout(clearTimer)
    }
  }, [focusTarget, providerReady, detailPage, setupFocusRefs, setAttentionSection])
}
