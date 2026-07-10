import { CUSTOM_START } from '../audio.js'
import { S } from '../styles.js'

const ACTIVATION_KEYS = new Set([' ', 'Enter'])

function PadFace({ pad, idx, active, recording }) {
  if (recording) {
    return (
      <div style={S.padInner}>
        <span style={{ color: '#f87171', fontSize: 18 }}>●</span>
        <span style={{ fontSize: 7, color: '#f87171' }}>REC</span>
      </div>
    )
  }
  if (pad.buffer || pad.isPreset) {
    return (
      <div style={S.padInner}>
        <span style={{ fontSize: 20, color: pad.color }}>♪</span>
        <span style={S.padName}>{pad.name || `Rec ${idx - CUSTOM_START + 1}`}</span>
      </div>
    )
  }
  return (
    <div style={S.padInner}>
      <span style={{ fontSize: 20, opacity: 0.22 }}>+</span>
    </div>
  )
}

export function PadBanks({
  pads,
  selectedPad,
  activePadIdx,
  isRecording,
  recordTarget,
  onPadDown,
  onPadUp,
  onClearPad,
}) {
  const handleKeyDown = (event, idx) => {
    if (!ACTIVATION_KEYS.has(event.key) || event.repeat) return
    event.preventDefault()
    onPadDown(idx)
  }

  const handleKeyUp = (event) => {
    if (!ACTIVATION_KEYS.has(event.key)) return
    event.preventDefault()
    onPadUp()
  }

  return (
    <section style={S.padArea} aria-label="Beat pads">
      <div style={S.sectionLabel}>Drum kit</div>
      <div style={S.padGrid}>
        {pads.slice(0, CUSTOM_START).map((pad, idx) => (
          <button
            key={idx}
            type="button"
            aria-label={`${pad.name} pad ${idx + 1}`}
            aria-pressed={selectedPad === idx}
            onPointerDown={(event) => {
              event.preventDefault()
              onPadDown(idx)
            }}
            onKeyDown={(event) => handleKeyDown(event, idx)}
            onKeyUp={handleKeyUp}
            onContextMenu={(event) => event.preventDefault()}
            style={{
              ...S.pad,
              background: activePadIdx === idx ? `${pad.color}44` : `linear-gradient(135deg, ${pad.color}15, ${pad.color}05)`,
              borderColor: activePadIdx === idx ? pad.color : selectedPad === idx ? pad.color : `${pad.color}28`,
              boxShadow: activePadIdx === idx ? `0 0 10px ${pad.color}33` : 'none',
              transform: activePadIdx === idx ? 'scale(0.94)' : 'scale(1)',
            }}
          >
            <PadFace pad={pad} idx={idx} active={activePadIdx === idx} recording={false} />
          </button>
        ))}
      </div>

      <div style={{ ...S.sectionLabel, marginTop: 4 }}>Custom - hold rec</div>
      <div style={S.padGrid}>
        {pads.slice(CUSTOM_START).map((pad, offset) => {
          const idx = offset + CUSTOM_START
          const active = activePadIdx === idx
          const recording = isRecording && recordTarget === idx
          return (
            <button
              key={idx}
              type="button"
              aria-label={`${pad.name || `Custom pad ${offset + 1}`} pad ${idx + 1}`}
              aria-pressed={selectedPad === idx}
              onPointerDown={(event) => {
                event.preventDefault()
                onPadDown(idx)
              }}
              onKeyDown={(event) => handleKeyDown(event, idx)}
              onKeyUp={handleKeyUp}
              onContextMenu={(event) => {
                event.preventDefault()
                if (pad.buffer) onClearPad(idx)
              }}
              style={{
                ...S.pad,
                background: active
                  ? recording ? 'rgba(248,113,113,0.18)' : `${pad.color}44`
                  : pad.buffer ? `linear-gradient(135deg, ${pad.color}18, ${pad.color}06)` : 'var(--surface)',
                borderColor: active
                  ? recording ? '#f87171' : pad.color
                  : selectedPad === idx ? pad.color : pad.buffer ? `${pad.color}28` : 'var(--border)',
                boxShadow: active ? `0 0 10px ${recording ? '#f8717128' : `${pad.color}33`}` : 'none',
                transform: active ? 'scale(0.94)' : 'scale(1)',
              }}
            >
              <PadFace pad={pad} idx={idx} active={active} recording={recording} />
            </button>
          )
        })}
      </div>
    </section>
  )
}
