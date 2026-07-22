import { useState, useRef, useEffect } from 'react'
import useScreenWakeLock from '../../hooks/useScreenWakeLock.js'

/**
 * Hook encapsulating Web Speech API voice input.
 *
 * Uses continuous=false + manual restart on onend — continuous=true is
 * broken on Android Chrome (fires duplicate onresult events, resultIndex
 * unreliable, e.results array grows unbounded).
 *
 * voiceFinalRef accumulates all finalized transcripts across the entire
 * recording session (including across automatic restarts). onresult loops
 * from e.resultIndex — not from 0 — so each event only processes the
 * newly-changed result. Finals are appended to voiceFinalRef; the current
 * interim is shown live.
 *
 * Sessions restart on onend (mobile Chrome ends sessions after a few seconds
 * of silence). Manual textarea edits deliberately retire the current speech
 * session and restart after a short idle window. That makes the owner's edit
 * the new authoritative base instead of letting a later interim result
 * overwrite it.
 *
 * @param {object} options
 * @param {(text: string) => void} options.onTranscript
 *   Called with the concatenated final+interim transcript on every
 *   onresult event. Caller commits this into the controlled composer; the
 *   composer layout effect owns its post-commit textarea sizing.
 * @param {React.RefObject<HTMLTextAreaElement>} options.inputRef
 *   The composer textarea — used to seed voiceFinalRef with the current value
 *   on start and to preserve the current value in permission-error copy.
 *
 * @returns {{
 *   listening: boolean,
 *   listeningRef: React.MutableRefObject<boolean>,
 *   startVoice: () => void,
 *   stopVoice: () => void,
 *   toggleVoice: () => void,
 *   acceptManualEdit: (text: string) => void,
 * }}
 *   `listeningRef` is the synchronous mirror of `listening`.
 */
export default function useVoiceInput({ onTranscript, inputRef }) {
  const [listening, setListening] = useState(false)
  const recognitionRef = useRef(null)
  const listeningRef = useRef(false)
  const voiceFinalRef = useRef('')  // accumulated finals across all sessions
  // Handle for the restart setTimeout in onend. Stored so unmount can
  // cancel it and prevent a session restart from firing after teardown.
  const restartTimerRef = useRef(null)

  // Each mounted chat owns its own voice session. Keeping the wake lock here
  // avoids a shell-wide boolean race when multiple panes are mounted, and ties
  // release directly to the same state that stops microphone recognition.
  useScreenWakeLock(listening)

  // Cleanup on unmount.
  useEffect(() => () => {
    listeningRef.current = false
    if (restartTimerRef.current !== null) {
      clearTimeout(restartTimerRef.current)
      restartTimerRef.current = null
    }
    recognitionRef.current?.abort()
  }, [])

  function startVoiceSession() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition
    if (!SR) return

    const rec = new SR()
    rec.continuous = false  // continuous=true is broken on Android Chrome — fires
    rec.interimResults = true  // duplicate onresult events; false + manual restart is equivalent
    recognitionRef.current = rec

    rec.onresult = (e) => {
      // A manual edit retires its in-flight recognition object before aborting
      // it. Ignore any result Chrome delivers during that abort race.
      if (recognitionRef.current !== rec) return
      let interim = ''
      for (let i = e.resultIndex; i < e.results.length; i++) {
        // Android Chrome bug: duplicate final events fire with confidence=0 — skip them.
        if (e.results[i].isFinal && e.results[i][0].confidence === 0) return
        if (e.results[i].isFinal) {
          voiceFinalRef.current += e.results[i][0].transcript
        } else {
          interim += e.results[i][0].transcript
        }
      }
      const text = voiceFinalRef.current + interim
      onTranscript(text)
    }

    rec.onerror = (e) => {
      if (recognitionRef.current !== rec) return
      if (e.error === 'not-allowed') {
        listeningRef.current = false
        setListening(false)
        recognitionRef.current = null
        voiceFinalRef.current = ''
        const prev = inputRef.current?.value || ''
        onTranscript(
          prev +
          (prev ? ' ' : '') +
          '[Microphone access denied — enable it in your browser site settings]'
        )
      }
    }

    rec.onend = () => {
      if (recognitionRef.current !== rec) return
      recognitionRef.current = null
      if (!listeningRef.current) return  // user stopped — don't restart
      if (voiceFinalRef.current && !voiceFinalRef.current.endsWith(' ')) {
        voiceFinalRef.current += ' '
      }
      restartTimerRef.current = setTimeout(() => {
        restartTimerRef.current = null
        if (listeningRef.current) startVoiceSession()
      }, 100)
    }

    try { rec.start() } catch { /* InvalidStateError race guard */ }
  }

  function startVoice() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition
    if (!SR) return

    listeningRef.current = true
    setListening(true)
    voiceFinalRef.current = inputRef.current?.value.trimEnd() || ''
    startVoiceSession()
  }

  function stopVoice() {
    listeningRef.current = false
    if (restartTimerRef.current !== null) {
      clearTimeout(restartTimerRef.current)
      restartTimerRef.current = null
    }
    recognitionRef.current?.abort()
    recognitionRef.current = null
    setListening(false)
    voiceFinalRef.current = ''
  }

  function toggleVoice() {
    if (listeningRef.current) {
      stopVoice()
    } else {
      startVoice()
    }
  }

  function acceptManualEdit(text) {
    if (!listeningRef.current) return

    // Preserve the exact owner-edited value as the new dictation base. Add a
    // separator only inside the speech buffer, so the next spoken word does
    // not run into the manually typed text.
    voiceFinalRef.current = text && !text.endsWith(' ') ? text + ' ' : text

    // The current result set may still contain an interim derived from the
    // pre-edit value. Retire it synchronously before aborting so a late result
    // cannot clobber the edit, then debounce the replacement session while the
    // owner is still typing.
    const rec = recognitionRef.current
    recognitionRef.current = null
    rec?.abort()
    if (restartTimerRef.current !== null) {
      clearTimeout(restartTimerRef.current)
    }
    restartTimerRef.current = setTimeout(() => {
      restartTimerRef.current = null
      if (listeningRef.current) startVoiceSession()
    }, 250)
  }

  return {
    listening,
    listeningRef,
    startVoice,
    stopVoice,
    toggleVoice,
    acceptManualEdit,
  }
}
