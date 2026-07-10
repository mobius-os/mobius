import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AudioEngine,
  CUSTOM_START,
  MAX_RECORD_SECONDS,
  PADS,
  TOTAL_BEATS,
  createInitialPads,
  createRecordingBuffer,
  drawLiveWaveform,
  drawWaveform,
  installKitBuffers,
  restoreSavedAudio,
  serializeCustomPads,
} from './audio.js'
import { createEmptyGrid, loadBeatState, saveBeatState, useOnline } from './storage.js'
import { CSS, S } from './styles.js'
import { ControlPanel } from './ui/ControlPanel.jsx'
import { Header } from './ui/Header.jsx'
import { PadBanks } from './ui/PadBanks.jsx'
import { Sequencer } from './ui/Sequencer.jsx'

const SAVE_DEBOUNCE_MS = 800
const LOOKAHEAD_MS = 25
const SCHEDULE_AHEAD_SECONDS = 0.1

function signal(name, payload) {
  try { window.mobius?.signal?.(name, payload) } catch {}
}

export default function BeatMachine({ appId, token }) {
  const online = useOnline()

  const engineRef = useRef(null)
  const presetsLoadedRef = useRef(false)
  const schedulerRef = useRef(null)
  const nextBeatTimeRef = useRef(0)
  const currentBeatRef = useRef(0)
  const playRef = useRef(false)
  const activeSrcRef = useRef(null)

  const [pads, setPadsState] = useState(createInitialPads)
  const padsRef = useRef(pads)
  const setPads = useCallback((updater) => {
    const next = typeof updater === 'function' ? updater(padsRef.current) : updater
    padsRef.current = next
    setPadsState(next)
  }, [])

  const [grid, setGridState] = useState(createEmptyGrid)
  const gridRef = useRef(grid)
  const setGrid = useCallback((updater) => {
    const next = typeof updater === 'function' ? updater(gridRef.current) : updater
    gridRef.current = next
    setGridState(next)
  }, [])

  const [volumes, setVolumes] = useState(() => new Array(PADS).fill(0.8))
  const volumesRef = useRef(volumes)
  const [echo, setEcho] = useState(0)
  const echoRef = useRef(echo)
  const [reverb, setReverb] = useState(0)
  const reverbRef = useRef(reverb)
  const [bpm, setBpm] = useState(120)
  const bpmRef = useRef(bpm)

  const [playing, setPlaying] = useState(false)
  const [currentBeat, setCurrentBeat] = useState(-1)
  const [selectedPad, setSelectedPad] = useState(null)
  const [activePadIdx, setActivePadIdx] = useState(null)
  const [isRecording, setIsRecording] = useState(false)
  const isRecordingRef = useRef(false)
  const [recordTarget, setRecordTarget] = useState(null)
  const recordTargetRef = useRef(null)
  const recordIntentRef = useRef(null)
  const [renamingPad, setRenamingPad] = useState(null)
  const [renameVal, setRenameVal] = useState('')
  const [stateLoaded, setStateLoaded] = useState(false)
  const [toast, setToast] = useState('')

  const saveTimerRef = useRef(null)
  const readySignalRef = useRef(false)
  const recordingTimerRef = useRef(null)
  const recProcessorRef = useRef(null)
  const recSilentRef = useRef(null)
  const recChunksRef = useRef([])
  const analyserRef = useRef(null)
  const animFrameRef = useRef(null)
  const streamRef = useRef(null)
  const recSourceRef = useRef(null)
  const liveCanvasRef = useRef(null)
  const waveCanvasRef = useRef(null)
  const seqScrollRef = useRef(null)

  useEffect(() => { padsRef.current = pads }, [pads])
  useEffect(() => { gridRef.current = grid }, [grid])
  useEffect(() => { volumesRef.current = volumes }, [volumes])
  useEffect(() => { bpmRef.current = bpm }, [bpm])
  useEffect(() => { echoRef.current = echo }, [echo])
  useEffect(() => { reverbRef.current = reverb }, [reverb])
  useEffect(() => { recordTargetRef.current = recordTarget }, [recordTarget])

  const showToast = useCallback((message) => {
    setToast(message)
    window.clearTimeout(showToast.timer)
    showToast.timer = window.setTimeout(() => setToast(''), 2600)
  }, [])

  const getEngine = useCallback(() => {
    if (!engineRef.current) engineRef.current = new AudioEngine()
    engineRef.current.init()
    return engineRef.current
  }, [])

  const initPresets = useCallback(() => {
    const engine = getEngine()
    if (!presetsLoadedRef.current) {
      presetsLoadedRef.current = true
      setPads((prev) => installKitBuffers(prev, engine.ctx))
    }
    volumesRef.current.forEach((value, idx) => engine.setVolume(idx, value))
    engine.setEcho(echoRef.current)
    engine.setReverb(reverbRef.current)
    return engine
  }, [getEngine, setPads])

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const saved = await loadBeatState(appId, token)
        if (!alive) return
        setGrid(saved.grid)
        setBpm(saved.bpm)
        setVolumes(saved.volumes)
        setEcho(saved.echo)
        setReverb(saved.reverb)
        if (saved.customPads.length) {
          const engine = getEngine()
          setPads((prev) => {
            const next = [...prev]
            for (const item of saved.customPads) {
              const buffer = restoreSavedAudio(engine.ctx, item.audio)
              if (!buffer) continue
              next[item.idx] = {
                ...next[item.idx],
                name: item.name || `Rec ${item.idx - CUSTOM_START + 1}`,
                color: item.color || next[item.idx].color,
                buffer,
                savedAudio: item.audio,
                isPreset: false,
              }
            }
            return next
          })
        }
        setStateLoaded(true)
      } catch (err) {
        if (!alive) return
        showToast("Couldn't load saved beat")
        signal('error', { operation: 'load_state', message: String(err?.message || err) })
      }
    })()
    return () => { alive = false }
  }, [appId, token, getEngine, setGrid, setPads, showToast])

  useEffect(() => {
    if (engineRef.current) {
      volumes.forEach((value, idx) => engineRef.current.setVolume(idx, value))
    }
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
          grid,
          bpm,
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
  }, [appId, token, grid, bpm, volumes, echo, reverb, pads, stateLoaded, showToast])

  const activePads = useMemo(
    () => pads.filter((pad) => pad.buffer || pad.isPreset).length,
    [pads],
  )

  useEffect(() => {
    if (!stateLoaded || readySignalRef.current) return
    readySignalRef.current = true
    signal('app_ready', { ready_pads: activePads })
  }, [activePads, stateLoaded])

  const scheduler = useCallback(() => {
    const engine = engineRef.current
    if (!engine?.ctx) return
    while (nextBeatTimeRef.current < engine.currentTime + SCHEDULE_AHEAD_SECONDS) {
      const beat = currentBeatRef.current
      const rows = gridRef.current
      const currentPads = padsRef.current
      for (let idx = 0; idx < PADS; idx += 1) {
        if (rows[idx]?.[beat] && currentPads[idx]?.buffer) {
          engine.play(idx, currentPads[idx].buffer, nextBeatTimeRef.current)
        }
      }
      setCurrentBeat(beat)
      nextBeatTimeRef.current += 60 / bpmRef.current / 4
      currentBeatRef.current = (currentBeatRef.current + 1) % TOTAL_BEATS
    }
  }, [])

  const stopPlayback = useCallback(() => {
    playRef.current = false
    setPlaying(false)
    setCurrentBeat(-1)
    currentBeatRef.current = 0
    if (seqScrollRef.current) seqScrollRef.current.scrollLeft = 0
    window.clearTimeout(schedulerRef.current)
  }, [])

  const startPlayback = useCallback(() => {
    const engine = initPresets()
    playRef.current = true
    setPlaying(true)
    currentBeatRef.current = 0
    nextBeatTimeRef.current = engine.currentTime + 0.05
    const loop = () => {
      if (!playRef.current) return
      scheduler()
      schedulerRef.current = window.setTimeout(loop, LOOKAHEAD_MS)
    }
    loop()
    signal('playback_started')
  }, [initPresets, scheduler])

  const cleanupRecording = useCallback(() => {
    recordIntentRef.current = null
    window.clearTimeout(recordingTimerRef.current)
    recordingTimerRef.current = null
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
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => track.stop())
      streamRef.current = null
    }
    analyserRef.current = null
  }, [])

  const stopRecording = useCallback(() => {
    const target = recordTargetRef.current
    const engine = engineRef.current
    const chunks = recChunksRef.current
    cleanupRecording()
    recChunksRef.current = []
    setIsRecording(false)
    isRecordingRef.current = false
    setRecordTarget(null)
    recordTargetRef.current = null

    if (!engine || target === null || chunks.length === 0) return
    const buffer = createRecordingBuffer(engine.ctx, chunks, MAX_RECORD_SECONDS)
    if (!buffer) return
    setPads((prev) => {
      const next = [...prev]
      next[target] = {
        ...next[target],
        buffer,
        savedAudio: null,
        name: next[target].name || `Rec ${target - CUSTOM_START + 1}`,
        isPreset: false,
      }
      return next
    })
    setSelectedPad(target)
    signal('item_created', { type: 'sample' })
  }, [cleanupRecording, setPads])

  const startRecording = useCallback(async (padIdx) => {
    if (
      padIdx < CUSTOM_START ||
      isRecordingRef.current ||
      recordIntentRef.current !== null
    ) return
    recordIntentRef.current = padIdx
    try {
      const engine = initPresets()
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
      if (recordIntentRef.current !== padIdx) {
        stream.getTracks().forEach((track) => track.stop())
        return
      }
      streamRef.current = stream
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

      recSourceRef.current = source
      analyserRef.current = analyser
      recProcessorRef.current = processor
      recSilentRef.current = silent
      recChunksRef.current = []
      processor.onaudioprocess = (event) => {
        recChunksRef.current.push(new Float32Array(event.inputBuffer.getChannelData(0)))
      }

      setRecordTarget(padIdx)
      recordTargetRef.current = padIdx
      setIsRecording(true)
      isRecordingRef.current = true
      recordIntentRef.current = null
      setSelectedPad(padIdx)
      recordingTimerRef.current = window.setTimeout(() => {
        showToast('Recording saved')
        stopRecording()
      }, MAX_RECORD_SECONDS * 1000)

      const drawLoop = () => {
        drawLiveWaveform(liveCanvasRef.current, analyserRef.current)
        animFrameRef.current = window.requestAnimationFrame(drawLoop)
      }
      drawLoop()
      signal('record_started')
    } catch (err) {
      cleanupRecording()
      setIsRecording(false)
      isRecordingRef.current = false
      setRecordTarget(null)
      recordTargetRef.current = null
      setActivePadIdx(null)
      showToast(String(err?.message || 'Microphone permission was not granted'))
      signal('record_failed', { message: String(err?.message || err) })
    }
  }, [cleanupRecording, initPresets, showToast, stopRecording])

  const playPad = useCallback((padIdx) => {
    const engine = initPresets()
    const pad = padsRef.current[padIdx]
    if (!pad?.buffer) return null
    engine.setEcho(echoRef.current)
    engine.setReverb(reverbRef.current)
    signal('pad_played', { kind: padIdx < CUSTOM_START ? 'kit' : 'sample' })
    return engine.play(padIdx, pad.buffer)
  }, [initPresets])

  const handlePadDown = useCallback((padIdx) => {
    if (isRecordingRef.current || recordIntentRef.current !== null) return
    initPresets()
    setActivePadIdx(padIdx)
    const pad = padsRef.current[padIdx]
    if (padIdx >= CUSTOM_START && !pad?.buffer) {
      startRecording(padIdx)
    } else if (pad?.buffer) {
      setSelectedPad(padIdx)
      activeSrcRef.current = playPad(padIdx)
    }
  }, [initPresets, playPad, startRecording])

  const handlePadUp = useCallback(() => {
    if (!isRecordingRef.current && recordIntentRef.current !== null) {
      recordIntentRef.current = null
    }
    if (isRecordingRef.current) stopRecording()
    if (activeSrcRef.current) {
      try { activeSrcRef.current.stop() } catch {}
      activeSrcRef.current = null
    }
    setActivePadIdx(null)
  }, [stopRecording])

  useEffect(() => {
    const up = () => handlePadUp()
    const stopIfHidden = () => {
      if (document.visibilityState === 'hidden') handlePadUp()
    }
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', up)
    window.addEventListener('blur', up)
    document.addEventListener('visibilitychange', stopIfHidden)
    return () => {
      window.removeEventListener('pointerup', up)
      window.removeEventListener('pointercancel', up)
      window.removeEventListener('blur', up)
      document.removeEventListener('visibilitychange', stopIfHidden)
    }
  }, [handlePadUp])

  const toggleCell = useCallback((padIdx, beatIdx) => {
    initPresets()
    setGrid((prev) => {
      const next = prev.map((row) => [...row])
      next[padIdx][beatIdx] = !next[padIdx][beatIdx]
      return next
    })
  }, [initPresets, setGrid])

  const clearGrid = useCallback(() => {
    setGrid(createEmptyGrid())
  }, [setGrid])

  const clearPad = useCallback((padIdx) => {
    if (padIdx < CUSTOM_START) return
    setPads((prev) => {
      const next = [...prev]
      next[padIdx] = { ...next[padIdx], buffer: null, savedAudio: null, name: '', isPreset: false }
      return next
    })
    setGrid((prev) => {
      const next = prev.map((row) => [...row])
      next[padIdx] = new Array(TOTAL_BEATS).fill(false)
      return next
    })
    if (selectedPad === padIdx) setSelectedPad(null)
    signal('item_deleted', { type: 'sample' })
  }, [selectedPad, setGrid, setPads])

  useEffect(() => {
    if (selectedPad !== null && pads[selectedPad]?.buffer) {
      window.requestAnimationFrame(() => {
        drawWaveform(waveCanvasRef.current, pads[selectedPad].buffer, pads[selectedPad].color)
      })
    }
  }, [selectedPad, pads])

  useEffect(() => {
    if (currentBeat >= 0 && seqScrollRef.current) {
      const cellWidth = 30
      const el = seqScrollRef.current
      el.scrollLeft = Math.max(0, currentBeat * cellWidth - el.clientWidth / 2 + cellWidth / 2)
    }
  }, [currentBeat])

  const startRename = useCallback((padIdx) => {
    if (padIdx < CUSTOM_START) return
    setRenamingPad(padIdx)
    setRenameVal(padsRef.current[padIdx]?.name || `Rec ${padIdx - CUSTOM_START + 1}`)
  }, [])

  const finishRename = useCallback(() => {
    const name = renameVal.trim().slice(0, 12)
    if (renamingPad !== null && name) {
      setPads((prev) => {
        const next = [...prev]
        next[renamingPad] = { ...next[renamingPad], name }
        return next
      })
    }
    setRenamingPad(null)
  }, [renameVal, renamingPad, setPads])

  const setPadVolume = useCallback((padIdx, value) => {
    setVolumes((prev) => {
      const next = [...prev]
      next[padIdx] = value
      return next
    })
  }, [])

  useEffect(() => () => {
    window.clearTimeout(schedulerRef.current)
    window.clearTimeout(saveTimerRef.current)
    window.clearTimeout(showToast.timer)
    window.cancelAnimationFrame(animFrameRef.current)
    cleanupRecording()
    if (engineRef.current) engineRef.current.dispose()
  }, [cleanupRecording])

  return (
    <div className="bm-root" style={S.root}>
      <style>{CSS}</style>
      <Header appId={appId} activePads={activePads} online={online} />

      <Sequencer
        pads={pads}
        grid={grid}
        playing={playing}
        bpm={bpm}
        currentBeat={currentBeat}
        seqScrollRef={seqScrollRef}
        onBpmChange={setBpm}
        onTogglePlay={playing ? stopPlayback : startPlayback}
        onClear={clearGrid}
        onToggleCell={toggleCell}
      />

      <div style={S.bottomSection}>
        <PadBanks
          pads={pads}
          selectedPad={selectedPad}
          activePadIdx={activePadIdx}
          isRecording={isRecording}
          recordTarget={recordTarget}
          onPadDown={handlePadDown}
          onPadUp={handlePadUp}
          onClearPad={clearPad}
        />
        <ControlPanel
          pads={pads}
          selectedPad={selectedPad}
          volumes={volumes}
          echo={echo}
          reverb={reverb}
          isRecording={isRecording}
          recordTarget={recordTarget}
          renamingPad={renamingPad}
          renameVal={renameVal}
          liveCanvasRef={liveCanvasRef}
          waveCanvasRef={waveCanvasRef}
          onRenameValChange={setRenameVal}
          onRenameFinish={finishRename}
          onRenameCancel={() => setRenamingPad(null)}
          onStartRename={startRename}
          onClearPad={clearPad}
          onVolumeChange={setPadVolume}
          onEchoChange={setEcho}
          onReverbChange={setReverb}
        />
      </div>

      {toast && (
        <div
          role="status"
          style={{
            position: 'absolute',
            left: 16,
            right: 16,
            bottom: 16,
            minHeight: 38,
            padding: '9px 12px',
            borderRadius: 10,
            background: 'var(--surface)',
            border: '1px solid #f87171',
            color: 'var(--text)',
            fontSize: 12,
            fontWeight: 650,
            textAlign: 'center',
          }}
        >
          {toast}
        </div>
      )}
    </div>
  )
}
