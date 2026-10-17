import { useEffect, useRef } from 'react'

/** Navigate an explicit Settings request out of any open detail page. */
export function useSettingsFocus({
  focusTarget, providerReady, detailPage, setDetailPage,
  setManageModelsProvider, setupFocusRefs, setAttentionSection,
}) {
  const handled = useRef(null)
  useEffect(() => {
    if (!focusTarget?.section) return
    setDetailPage(null)
    if (focusTarget.section === 'models') setManageModelsProvider('all')
  }, [focusTarget, setDetailPage, setManageModelsProvider])

  useEffect(() => {
    const requested = focusTarget?.section
    if (!requested || detailPage || handled.current === focusTarget) return undefined
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
