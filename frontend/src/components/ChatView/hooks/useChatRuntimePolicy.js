import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react'

import { saveAutoResumePolicy } from '../autoResumePolicy.js'
import {
  clearProviderSwitch,
  getProviderSwitchState,
  subscribeProviderSwitch,
} from '../providerSwitch.js'

/** Own per-chat provider selection and automatic-resume policy state. */
export default function useChatRuntimePolicy({
  chatId,
  cached,
  hidden,
  onProviderSwitchSettled,
  request,
}) {
  const [chatInfo, setChatInfo] = useState(() => cached?.chatInfo ?? null)
  const [autoResumeSaving, setAutoResumeSaving] = useState(false)
  const [autoResumeError, setAutoResumeError] = useState('')
  const [autoResumeErrorSource, setAutoResumeErrorSource] = useState('')
  const autoResumeSavingRef = useRef(false)
  const autoResumeRequestRef = useRef(0)

  const subscribeToProviderSwitch = useCallback(
    listener => subscribeProviderSwitch(chatId, listener),
    [chatId],
  )
  const readProviderSwitch = useCallback(
    () => getProviderSwitchState(chatId),
    [chatId],
  )
  const providerSwitchState = useSyncExternalStore(
    subscribeToProviderSwitch,
    readProviderSwitch,
    readProviderSwitch,
  )
  const providerSwitching = providerSwitchState.status === 'switching'

  const clearAutoResumeError = useCallback(() => {
    setAutoResumeError('')
    setAutoResumeErrorSource('')
  }, [])

  const mergeChatInfo = useCallback(({
    agent_settings_json,
    provider,
    effective,
  }) => {
    setChatInfo(previous => previous ? ({
      ...previous,
      agent_settings_json,
      provider: provider || previous.provider,
      effective: effective || previous.effective,
    }) : previous)
  }, [])

  useEffect(() => {
    autoResumeRequestRef.current += 1
    autoResumeSavingRef.current = false
    setAutoResumeSaving(false)
    setAutoResumeError('')
    setAutoResumeErrorSource('')
  }, [chatId])

  const handleAutoResumeChange = useCallback(async (
    next,
    source = 'card',
  ) => {
    if (autoResumeSavingRef.current) return
    autoResumeSavingRef.current = true
    const requestId = ++autoResumeRequestRef.current
    setAutoResumeSaving(true)
    setAutoResumeError('')
    setAutoResumeErrorSource(source)
    try {
      const result = await saveAutoResumePolicy({
        chatId,
        next,
        request,
      })
      if (requestId !== autoResumeRequestRef.current) return
      if (result.value !== null) {
        setChatInfo(previous => previous ? ({
          ...previous,
          auto_resume_on_limit: result.value,
        }) : previous)
      }
      setAutoResumeError(result.error)
    } finally {
      if (requestId === autoResumeRequestRef.current) {
        autoResumeSavingRef.current = false
        setAutoResumeSaving(false)
      }
    }
  }, [chatId, request])

  const handleAutoResumeSettingsChange = useCallback(
    next => handleAutoResumeChange(next, 'settings'),
    [handleAutoResumeChange],
  )

  useEffect(() => {
    if (hidden) return
    if (
      providerSwitchState.status !== 'success'
      || !providerSwitchState.result
    ) return
    mergeChatInfo(providerSwitchState.result)
    onProviderSwitchSettled()
    clearProviderSwitch(chatId)
  }, [
    chatId,
    hidden,
    mergeChatInfo,
    onProviderSwitchSettled,
    providerSwitchState.result,
    providerSwitchState.status,
  ])

  const autoResumeEnabled = !!chatInfo?.auto_resume_on_limit

  return {
    autoResumeEnabled,
    autoResumeError,
    autoResumeErrorSource,
    autoResumeSaving,
    chatInfo,
    clearAutoResumeError,
    handleAutoResumeChange,
    handleAutoResumeSettingsChange,
    mergeChatInfo,
    providerSwitchState,
    providerSwitching,
    setChatInfo,
  }
}
