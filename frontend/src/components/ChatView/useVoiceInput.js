import { useState, useRef, useEffect } from 'react'

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
 * of silence). listeningRef stays true across restart gaps so the onChange
 * guard blocks Chrome's textarea direct-fill throughout.
 *
 * @param {object} options
 * @param {(text: string) => void} options.onTranscript
 *   Called with the concatenated final+interim transcript on every
 *   onresult event. Caller writes this into the composer textarea.
 * @param {React.RefObject<HTMLTextAreaElement>} options.inputRef
 *   The composer textarea — used for auto-height resize on transcript
 *   growth and to seed voiceFinalRef with the current value on start.
 *
 * @returns {{
 *   listening: boolean,
 *   listeningRef: React.MutableRefObject<boolean>,
 *   startVoice: () => void,
 *   stopVoice: () => void,
 *   toggleVoice: () => void,
 * }}
 *   `listeningRef` is the synchronous mirror of `listening`; gate any
 *   `onChange` handlers on it to block Chrome's OS dictation layer
 *   from racing with `onresult` mid-session.
 */
export default function useVoiceInput({ onTranscript, inputRef }) {
  const [listening, setListening] = useState(false)
  const recognitionRef = useRef(null)
  const listeningRef = useRef(false)
  const voiceFinalRef = useRef('')  // accumulated finals across all sessions
  // Handle for the restart setTimeout in onend. Stored so unmount can
  // cancel it and prevent a session restart from firing after teardown.
  const restartTimerRef = useRef(null)

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
      requestAnimationFrame(() => {
        if (inputRef.current) {
          inputRef.current.style.height = 'auto'
          inputRef.current.style.height = Math.min(inputRef.current.scrollHeight, 160) + 'px'
        }
      })
    }

    rec.onerror = (e) => {
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

  return { listening, listeningRef, startVoice, stopVoice, toggleVoice }
}
