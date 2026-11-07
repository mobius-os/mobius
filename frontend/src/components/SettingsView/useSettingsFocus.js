import { useEffect, useRef } from 'react'
import { settingsSectionPage } from '../../lib/settingsSections.js'

/** Navigate an explicit Settings request: open its page, or leave any open page and focus its section. */
export function useSettingsFocus({
  focusTarget, providerReady, detailPage, setDetailPage,
  setManageModelsProvider, setupFocusRefs, setAttentionSection,
}) {
  const handled = useRef(null)
  useEffect(() => {
    if (!focusTarget?.section) return
    // A section that is a whole page opens it; any other leaves the open page.
    setDetailPage(settingsSectionPage(focusTarget.section))
    if (focusTarget.section === 'models') setManageModelsProvider('all')
  }, [focusTarget, setDetailPage, setManageModelsProvider])

  useEffect(() => {
    const requested = focusTarget?.section
    if (!requested || settingsSectionPage(requested) || detailPage || handled.current === focusTarget) return undefined
    const section = requested === 'models' ? 'ai-providers' : requested
    let clearTimer = null
    const raf = requestAnimationFrame(() => {
      const node = setupFocusRefs.current[section]
      if (!node) return
      handled.current = focusTarget
      node.scrollIntoView({ behavior: 'smooth', block: 'start' })
      node.focus({ preventScroll: true })
      setAttentionSection(section)
      clearTimer = setTimeout(() => {
        setAttentionSection(current => current === section ? '' : current)
      }, 1800)
    })
    return () => {
      cancelAnimationFrame(raf)
      if (clearTimer) clearTimeout(clearTimer)
    }
  }, [focusTarget, providerReady, detailPage, setupFocusRefs, setAttentionSection])
}
