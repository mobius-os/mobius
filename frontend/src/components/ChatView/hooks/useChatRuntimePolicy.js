import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react'

import {
  saveAutoResumePolicy,
  saveRestartResumePolicy,
} from '../autoResumePolicy.js'
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
  const [restartResumeSaving, setRestartResumeSaving] = useState(false)
  const [restartResumeError, setRestartResumeError] = useState('')
  const autoResumeSavingRef = useRef(false)
  const autoResumeRequestRef = useRef(0)
  const restartResumeSavingRef = useRef(false)
  const restartResumeRequestRef = useRef(0)

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
    restartResumeRequestRef.current += 1
    restartResumeSavingRef.current = false
    setRestartResumeSaving(false)
    setRestartResumeError('')
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

  const handleRestartResumeChange = useCallback(async next => {
    if (restartResumeSavingRef.current) return
    restartResumeSavingRef.current = true
    const requestId = ++restartResumeRequestRef.current
    setRestartResumeSaving(true)
    setRestartResumeError('')
    try {
      const result = await saveRestartResumePolicy({
        chatId,
        next,
        request,
      })
      if (requestId !== restartResumeRequestRef.current) return
      if (result.value !== null) {
        setChatInfo(previous => previous ? ({
          ...previous,
          auto_resume_on_restart: result.value,
        }) : previous)
      }
      setRestartResumeError(result.error)
    } finally {
      if (requestId === restartResumeRequestRef.current) {
        restartResumeSavingRef.current = false
        setRestartResumeSaving(false)
      }
    }
  }, [chatId, request])

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
  const restartResumeEnabled = !!chatInfo?.auto_resume_on_restart

  return {
    autoResumeEnabled,
    autoResumeError,
    autoResumeErrorSource,
    autoResumeSaving,
    chatInfo,
    clearAutoResumeError,
    handleAutoResumeChange,
    handleAutoResumeSettingsChange,
    handleRestartResumeChange,
    mergeChatInfo,
    providerSwitchState,
    providerSwitching,
    restartResumeEnabled,
    restartResumeError,
    restartResumeSaving,
    setChatInfo,
  }
}
