import { useState, useRef, useEffect } from 'react'
import useScreenWakeLock from '../../hooks/useScreenWakeLock.js'
import { acquireAudioSession } from '../../lib/audioSession.js'

// Web Speech errors that a restart can't fix. Chromium's recognition only
// works in Google's own Chrome: elsewhere (Electron, Brave) it fails with
// "network" or "service-not-allowed", and restarting would spin silently.
const FATAL_SPEECH_ERRORS = {
  'network': "[Dictation couldn't reach the speech service]",
  'service-not-allowed': "[Dictation isn't available in this browser]",
  'language-not-supported': "[Dictation doesn't support this language]",
}

// Why the app's own dictation ended, as a note in the draft. "declined" (the
// owner just answered the system prompt) and a plain end stay silent.
const NATIVE_DICTATION_ENDINGS = {
  blocked: '[Dictation is off for Möbius — allow the microphone and speech recognition in your system privacy settings]',
  unavailable: "[Dictation isn't available on this device]",
  failed: '[Dictation stopped — tap the mic to try again]',
}

/**
 * The app's own dictation, when the Möbius app around the shell has one (the
 * desktop app on a Mac). `startDictation(listener)` reports
 * `{ type: 'text', text }` with everything heard since that start, and
 * `{ type: 'end', error }` if the session ends by itself;
 * `stopDictation()` ends it silently.
 */
function nativeDictation() {
  const native = window.MobiusNative
  return typeof native?.startDictation === 'function' && typeof native?.stopDictation === 'function'
    ? native
    : null
}

// The app has one dictation session for the whole shell, but every chat pane
// has its own mic. The pane that started it last owns it: starting in another
// pane first ends the owner's listening, and only the owner may stop it.
let nativeOwner = null

function speechRecognition() {
  return window.SpeechRecognition || window.webkitSpeechRecognition
}

/**
 * Hook encapsulating voice input: the app's own dictation when it has one,
 * otherwise the Web Speech API.
 *
 * The Web Speech path:
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
  // This pane's claim on the app's dictation session (see nativeOwner).
  const nativeClaim = useRef({}).current
  nativeClaim.displace = endListening

  // Each mounted chat owns its own voice session. Keeping the wake lock and
  // recording audio session here avoids a shell-wide boolean race when
  // multiple panes are mounted, and ties release directly to the same state
  // that stops microphone recognition.
  useScreenWakeLock(listening)
  useEffect(() => (listening ? acquireAudioSession('play-and-record') : undefined), [listening])

  // Cleanup on unmount.
  // eslint-disable-next-line react-hooks/exhaustive-deps -- stopVoice reads only refs and stable setters.
  useEffect(() => () => stopVoice(), [])

  function appendNote(note) {
    const prev = inputRef.current?.value || ''
    onTranscript(prev + (prev ? ' ' : '') + note)
  }

  function endListening() {
    listeningRef.current = false
    if (restartTimerRef.current !== null) {
      clearTimeout(restartTimerRef.current)
      restartTimerRef.current = null
    }
    setListening(false)
    recognitionRef.current = null
    voiceFinalRef.current = ''
    if (nativeOwner === nativeClaim) nativeOwner = null
  }

  // The app keeps listening across pauses and reports everything heard since
  // this start, so a session only needs the draft it began from. A manual
  // edit retires the session and starts another from the edited draft.
  // The claim outlives a manual-edit restart, so another pane starting during
  // that pause still takes over from this one.
  function startNativeSession(native) {
    if (nativeOwner !== nativeClaim) {
      nativeOwner?.displace()
      nativeOwner = nativeClaim
    }
    const base = voiceFinalRef.current
    const session = {
      abort: () => {
        if (nativeOwner === nativeClaim) native.stopDictation().catch(() => {})
      },
    }
    recognitionRef.current = session
    const ended = (error) => {
      if (recognitionRef.current !== session) return
      endListening()
      const note = NATIVE_DICTATION_ENDINGS[error]
      if (note) appendNote(note)
    }
    native.startDictation((update) => {
      if (recognitionRef.current !== session) return
      if (update.type === 'text') {
        const text = update.text || ''
        onTranscript(base && text ? base + (base.endsWith(' ') ? '' : ' ') + text : base + text)
      } else if (update.type === 'end') {
        ended(update.error)
      }
    }).catch(() => ended('failed'))
  }

  function startVoiceSession() {
    const native = nativeDictation()
    if (native) return startNativeSession(native)
    const SR = speechRecognition()
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
        endListening()
        // In the Möbius phone app the denial came from the OS permission
        // prompt the owner just answered, and there are no browser site
        // settings to point at. Just stop listening.
        if (window.MobiusNative) return
        appendNote('[Microphone access denied — enable it in your browser site settings]')
      } else if (FATAL_SPEECH_ERRORS[e.error]) {
        endListening()
        appendNote(FATAL_SPEECH_ERRORS[e.error])
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
    if (!nativeDictation() && !speechRecognition()) return

    listeningRef.current = true
    setListening(true)
    voiceFinalRef.current = inputRef.current?.value.trimEnd() || ''
    startVoiceSession()
  }

  function stopVoice() {
    const rec = recognitionRef.current
    recognitionRef.current = null
    rec?.abort()
    endListening()
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
