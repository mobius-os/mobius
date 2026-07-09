import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AudioEngine,
  CUSTOM_START,
  MAX_RECORD_SECONDS,
  PADS,
  createInitialPads,
  createRecordingBuffer,
  drawLiveWaveform,
  drawWaveform,
  installKitBuffers,
  restoreSavedAudio,
  serializeCustomPads,
} from './audio.js'
import { CSS } from './theme.js'
import { loadBeatState, saveBeatState, useOnline } from './storage.js'
import { EffectsMixer, PadDetail } from './ui/DetailPanel.jsx'
import { BeatHeader } from './ui/Header.jsx'
import { PadButton, hasPadAudio, sampleName } from './ui/PadButton.jsx'

const SAVE_DEBOUNCE_MS = 700

function signal(name, payload) {
  try { window.mobius?.signal?.(name, payload) } catch {}
}

export default function BeatMachine({ appId, token }) {
  const online = useOnline()
  const engineRef = useRef(null)
  const kitLoadedRef = useRef(false)
  const volumesRef = useRef(new Array(PADS).fill(0.8))
  const echoRef = useRef(0)
  const reverbRef = useRef(0)

  const [padsState, setPadsState] = useState(createInitialPads)
  const padsRef = useRef(padsState)
  const setPads = useCallback((updater) => {
    const next = typeof updater === 'function' ? updater(padsRef.current) : updater
    padsRef.current = next
    setPadsState(next)
  }, [])

  const [volumes, setVolumes] = useState(() => new Array(PADS).fill(0.8))
  const [echo, setEcho] = useState(0)
  const [reverb, setReverb] = useState(0)
  const [selectedPad, setSelectedPad] = useState(null)
  const [activePad, setActivePad] = useState(null)
  const [isRecording, setIsRecording] = useState(false)
  const isRecordingRef = useRef(false)
  const recordTargetRef = useRef(null)
  const [renaming, setRenaming] = useState(false)
  const [renameValue, setRenameValue] = useState('')
  const [stateLoaded, setStateLoaded] = useState(false)
  const [toast, setToast] = useState('')

  const recProcessorRef = useRef(null)
  const recSourceRef = useRef(null)
  const recSilentRef = useRef(null)
  const recStreamRef = useRef(null)
  const recChunksRef = useRef([])
  const analyserRef = useRef(null)
  const animFrameRef = useRef(null)
  const stopTimerRef = useRef(null)
  const stopRecordingRef = useRef(null)
  const liveCanvasRef = useRef(null)
  const waveCanvasRef = useRef(null)
  const saveTimerRef = useRef(null)
  const readySignalRef = useRef(false)

  useEffect(() => { padsRef.current = padsState }, [padsState])
  useEffect(() => { volumesRef.current = volumes }, [volumes])
  useEffect(() => { echoRef.current = echo }, [echo])
  useEffect(() => { reverbRef.current = reverb }, [reverb])

  const showToast = useCallback((message) => {
    setToast(message)
    window.clearTimeout(showToast.timer)
    showToast.timer = window.setTimeout(() => setToast(''), 3200)
  }, [])

  const getEngine = useCallback(() => {
    if (!engineRef.current) engineRef.current = new AudioEngine()
    engineRef.current.init()
    return engineRef.current
  }, [])

  const ensureEngineReady = useCallback(() => {
    const engine = getEngine()
    let next = padsRef.current
    let changed = false

    if (!kitLoadedRef.current) {
      next = installKitBuffers(next, engine.ctx)
      kitLoadedRef.current = true
      changed = true
    }

    next = next.map((pad) => {
      if (!pad.savedAudio || pad.buffer) return pad
      const buffer = restoreSavedAudio(engine.ctx, pad.savedAudio)
      if (!buffer) return pad
      changed = true
      return { ...pad, buffer }
    })

    volumesRef.current.forEach((value, idx) => engine.setVolume(idx, value))
    engine.setEcho(echoRef.current)
    engine.setReverb(reverbRef.current)

    if (changed) setPads(next)
    return engine
  }, [getEngine, setPads])

  useEffect(() => {
    let alive = true
    ;(async () => {
      const saved = await loadBeatState(appId, token)
      if (!alive) return
      setVolumes(saved.volumes)
      setEcho(saved.echo)
      setReverb(saved.reverb)
      if (saved.customPads.length) {
        setPads((prev) => {
          const next = [...prev]
          for (const item of saved.customPads) {
            next[item.idx] = {
              ...next[item.idx],
              name: item.name,
              color: item.color || next[item.idx].color,
              savedAudio: item.audio,
              buffer: null,
              isPreset: false,
            }
          }
          return next
        })
      }
      setStateLoaded(true)
    })()
    return () => { alive = false }
  }, [appId, token, setPads])

  useEffect(() => {
    if (!engineRef.current) return
    volumes.forEach((value, idx) => engineRef.current.setVolume(idx, value))
  }, [volumes])

  useEffect(() => {
    if (engineRef.current) engineRef.current.setEcho(echo)
  }, [echo])

  useEffect(() => {
    if (engineRef.current) engineRef.current.setReverb(reverb)
  }, [reverb])

  useEffect(() => {
    if (!stateLoaded) return undefined
    window.clearTimeout(saveTimerRef.current)
    saveTimerRef.current = window.setTimeout(async () => {
      try {
        await saveBeatState(appId, token, {
          volumes,
          echo,
          reverb,
          customPads: serializeCustomPads(padsRef.current),
        })
      } catch (err) {
        showToast("Couldn't save changes")
        signal('error', { operation: 'save_state', message: String(err?.message || err) })
      }
    }, SAVE_DEBOUNCE_MS)
    return () => window.clearTimeout(saveTimerRef.current)
  }, [appId, token, volumes, echo, reverb, padsState, stateLoaded, showToast])

  const customCount = useMemo(
    () => padsState.slice(CUSTOM_START).filter((pad) => pad.buffer || pad.savedAudio).length,
    [padsState],
  )
  const readyCount = CUSTOM_START + customCount

  useEffect(() => {
    if (!stateLoaded || readySignalRef.current) return
    readySignalRef.current = true
    signal('app_ready', { custom_pads: customCount, ready_pads: readyCount })
  }, [customCount, readyCount, stateLoaded])

  const playPad = useCallback((idx) => {
    try {
      const engine = ensureEngineReady()
      const pad = padsRef.current[idx]
      const buffer = pad?.buffer || null
      if (!buffer) return
      engine.play(idx, buffer)
      signal('pad_played', { kind: idx < CUSTOM_START ? 'kit' : 'sample' })
    } catch (err) {
      showToast(String(err?.message || 'Audio is unavailable'))
      signal('error', { operation: 'play_pad', message: String(err?.message || err) })
    }
  }, [ensureEngineReady, showToast])

  const triggerPad = useCallback((idx) => {
    if (isRecordingRef.current) return
    setSelectedPad(idx)
    setRenaming(false)
    setActivePad(idx)
    window.setTimeout(() => {
      setActivePad((current) => (current === idx ? null : current))
    }, 150)

    const pad = padsRef.current[idx]
    if (hasPadAudio(pad)) playPad(idx)
  }, [playPad])

  const cleanupRecordingNodes = useCallback(() => {
    window.clearTimeout(stopTimerRef.current)
    window.cancelAnimationFrame(animFrameRef.current)
    if (recProcessorRef.current) {
      recProcessorRef.current.onaudioprocess = null
      try { recProcessorRef.current.disconnect() } catch {}
      recProcessorRef.current = null
    }
    if (recSilentRef.current) {
      try { recSilentRef.current.disconnect() } catch {}
      recSilentRef.current = null
    }
    if (recSourceRef.current) {
      try { recSourceRef.current.disconnect() } catch {}
      recSourceRef.current = null
    }
    if (recStreamRef.current) {
      recStreamRef.current.getTracks().forEach((track) => track.stop())
      recStreamRef.current = null
    }
    analyserRef.current = null
  }, [])

  const stopRecording = useCallback(() => {
    const engine = engineRef.current
    const target = recordTargetRef.current
    const chunks = recChunksRef.current
    cleanupRecordingNodes()
    recChunksRef.current = []
    recordTargetRef.current = null
    isRecordingRef.current = false
    setIsRecording(false)
    setActivePad(null)

    if (!engine || target == null || chunks.length === 0) return
    const buffer = createRecordingBuffer(engine.ctx, chunks, MAX_RECORD_SECONDS)
    if (!buffer) return

    setPads((prev) => {
      const next = [...prev]
      next[target] = {
        ...next[target],
        name: next[target].name || sampleName(target),
        buffer,
        savedAudio: null,
        isPreset: false,
      }
      return next
    })
    setSelectedPad(target)
    signal('item_created', { type: 'sample' })
  }, [cleanupRecordingNodes, setPads])

  useEffect(() => {
    stopRecordingRef.current = stopRecording
  }, [stopRecording])

  const startRecording = useCallback(async (idx) => {
    if (idx < CUSTOM_START || isRecordingRef.current) return
    try {
      const engine = ensureEngineReady()
      if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error('Microphone recording is unavailable in this browser.')
      }
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        },
      })

      const source = engine.ctx.createMediaStreamSource(stream)
      const analyser = engine.ctx.createAnalyser()
      analyser.fftSize = 2048
      source.connect(analyser)

      const processor = engine.ctx.createScriptProcessor(4096, 1, 1)
      const silent = engine.ctx.createGain()
      silent.gain.value = 0
      source.connect(processor)
      processor.connect(silent)
      silent.connect(engine.ctx.destination)

      recChunksRef.current = []
      processor.onaudioprocess = (event) => {
        recChunksRef.current.push(new Float32Array(event.inputBuffer.getChannelData(0)))
        const frames = recChunksRef.current.reduce((sum, chunk) => sum + chunk.length, 0)
        if (frames >= engine.ctx.sampleRate * MAX_RECORD_SECONDS) {
          stopRecordingRef.current?.()
        }
      }

      recStreamRef.current = stream
      recSourceRef.current = source
      recProcessorRef.current = processor
      recSilentRef.current = silent
      analyserRef.current = analyser
      recordTargetRef.current = idx
      isRecordingRef.current = true
      setSelectedPad(idx)
      setActivePad(idx)
      setIsRecording(true)
      setRenaming(false)

      const drawLoop = () => {
        drawLiveWaveform(liveCanvasRef.current, analyserRef.current)
        animFrameRef.current = window.requestAnimationFrame(drawLoop)
      }
      drawLoop()
      stopTimerRef.current = window.setTimeout(() => {
        stopRecordingRef.current?.()
      }, MAX_RECORD_SECONDS * 1000 + 160)
      signal('record_started')
    } catch (err) {
      cleanupRecordingNodes()
      isRecordingRef.current = false
      setIsRecording(false)
      showToast(String(err?.message || 'Microphone permission was not granted'))
      signal('record_failed', { message: String(err?.message || err) })
    }
  }, [cleanupRecordingNodes, ensureEngineReady, showToast])

  const clearPad = useCallback((idx) => {
    if (idx < CUSTOM_START) return
    setPads((prev) => {
      const next = [...prev]
      next[idx] = {
        ...next[idx],
        name: '',
        buffer: null,
        savedAudio: null,
        isPreset: false,
      }
      return next
    })
    setRenaming(false)
    signal('item_deleted', { type: 'sample' })
  }, [setPads])

  const startRename = useCallback((idx) => {
    if (idx < CUSTOM_START) return
    setRenameValue(padsRef.current[idx]?.name || sampleName(idx))
    setRenaming(true)
  }, [])

  const finishRename = useCallback(() => {
    if (selectedPad == null || selectedPad < CUSTOM_START) {
      setRenaming(false)
      return
    }
    const nextName = renameValue.trim().slice(0, 18)
    if (nextName) {
      setPads((prev) => {
        const next = [...prev]
        next[selectedPad] = { ...next[selectedPad], name: nextName }
        return next
      })
    }
    setRenaming(false)
  }, [renameValue, selectedPad, setPads])

  const setPadVolume = useCallback((idx, value) => {
    setVolumes((prev) => {
      const next = [...prev]
      next[idx] = value
      return next
    })
  }, [])

  useEffect(() => {
    if (selectedPad !== null && padsState[selectedPad]?.buffer) {
      window.requestAnimationFrame(() => {
        drawWaveform(waveCanvasRef.current, padsState[selectedPad].buffer, padsState[selectedPad].color)
      })
    }
  }, [padsState, selectedPad])

  useEffect(() => () => {
    window.clearTimeout(saveTimerRef.current)
    window.clearTimeout(showToast.timer)
    cleanupRecordingNodes()
    if (engineRef.current) engineRef.current.dispose()
  }, [cleanupRecordingNodes])

  const selected = selectedPad == null ? null : padsState[selectedPad]
  const recordTarget = recordTargetRef.current

  return (
    <div className="bm-root">
      <style>{CSS}</style>
      <BeatHeader appId={appId} readyCount={readyCount} online={online} />

      <div className="bm-scroll">
        <main className="bm-main">
          <section className="bm-bank" aria-label="Beat pads">
            <div className="bm-bank-group">
              <div className="bm-section-head">
                <h2 className="bm-section-title">Kit</h2>
                <span className="bm-section-meta">8 built-in</span>
              </div>
              <div className="bm-pad-grid">
                {padsState.slice(0, CUSTOM_START).map((pad, idx) => (
                  <PadButton
                    key={idx}
                    pad={pad}
                    idx={idx}
                    selected={selectedPad === idx}
                    active={activePad === idx}
                    recording={isRecording && recordTarget === idx}
                    onTrigger={triggerPad}
                  />
                ))}
              </div>
            </div>
            <div className="bm-bank-group">
              <div className="bm-section-head">
                <h2 className="bm-section-title">Samples</h2>
                <span className="bm-section-meta">{customCount}/8 loaded</span>
              </div>
              <div className="bm-pad-grid">
                {padsState.slice(CUSTOM_START).map((pad, offset) => {
                  const idx = offset + CUSTOM_START
                  return (
                    <PadButton
                      key={idx}
                      pad={pad}
                      idx={idx}
                      selected={selectedPad === idx}
                      active={activePad === idx}
                      recording={isRecording && recordTarget === idx}
                      onTrigger={triggerPad}
                    />
                  )
                })}
              </div>
            </div>
          </section>

          <aside className="bm-panel" aria-label="Pad details and effects">
            <PadDetail
              selected={selected}
              selectedPad={selectedPad}
              recordTarget={recordTarget}
              isRecording={isRecording}
              liveCanvasRef={liveCanvasRef}
              waveCanvasRef={waveCanvasRef}
              volumes={volumes}
              renaming={renaming}
              renameValue={renameValue}
              onRenameValueChange={setRenameValue}
              onRenameFinish={finishRename}
              onRenameCancel={() => setRenaming(false)}
              onRenameStart={startRename}
              onVolumeChange={setPadVolume}
              onRecordStart={startRecording}
              onRecordStop={stopRecording}
              onClear={clearPad}
            />
            <EffectsMixer
              echo={echo}
              reverb={reverb}
              onEchoChange={setEcho}
              onReverbChange={setReverb}
            />
          </aside>
        </main>
      </div>

      {toast && <div className="bm-toast" role="status">{toast}</div>}
    </div>
  )
}
