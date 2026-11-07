import { useEffect, useRef } from 'react'
import { settingsSectionPage } from '../../lib/settingsSections.js'

const NOWHERE = { page: undefined, anchor: null }

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

/** Navigate an explicit Settings request once: open its page, then focus its section there. */
export function useSettingsFocus({
  focusTarget, providerReady, modelsProvider = null, detailPage, setDetailPage,
  setupFocusRefs, setAttentionSection,
}) {
  const navigated = useRef(null)
  const handled = useRef(null)
  const section = focusTarget?.section
  // `models` needs the provider list to pick its page.
  const waiting = section === 'models' && !providerReady
  const { page, anchor } = section && !waiting ? destination(section, modelsProvider) : NOWHERE

  useEffect(() => {
    if (!section || waiting || navigated.current === focusTarget) return
    navigated.current = focusTarget
    setDetailPage(page)
  }, [focusTarget, section, waiting, page, setDetailPage])

  useEffect(() => {
    if (!anchor || detailPage !== page || handled.current === focusTarget) return undefined
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
  }, [focusTarget, anchor, page, providerReady, detailPage, setupFocusRefs, setAttentionSection])
}
