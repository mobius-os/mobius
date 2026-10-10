import { useEffect, useRef } from 'react'

/** Navigate an explicit Settings request out of any retained detail page. */
export function useSettingsFocus({
  focusTarget, providerReady, selectedProvider, mobiusAccountOpen,
  setSelectedProvider, setMobiusAccountOpen, setManageModelsProvider,
  setupFocusRefs, setAttentionSection,
}) {
  const handled = useRef(null)
  useEffect(() => {
    if (!focusTarget?.section) return
    setSelectedProvider(null)
    setMobiusAccountOpen(false)
    if (focusTarget.section === 'models') setManageModelsProvider('all')
  }, [focusTarget, setSelectedProvider, setMobiusAccountOpen, setManageModelsProvider])

  useEffect(() => {
    const requested = focusTarget?.section
    if (!requested || selectedProvider || mobiusAccountOpen || handled.current === focusTarget) {
      return undefined
    }
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
  }, [focusTarget, providerReady, selectedProvider, mobiusAccountOpen,
    setupFocusRefs, setAttentionSection])
}
