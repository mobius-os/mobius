import { CUSTOM_START, MAX_RECORD_SECONDS } from '../audio.js'
import { sampleName } from './PadButton.jsx'

export function EmptyState({ title, text }) {
  return (
    <div className="bm-empty">
      <div className="bm-empty-mark" aria-hidden="true"><span /><span /><span /><span /></div>
      <div className="bm-empty-title">{title}</div>
      {text && <p className="bm-empty-text">{text}</p>}
    </div>
  )
}

export function SliderRow({ label, value, onChange, accentColor }) {
  const pct = Math.round((value ?? 0) * 100)
  return (
    <div className="bm-slider-row">
      <span className="bm-slider-label">{label}</span>
      <input
        className="bm-slider"
        type="range"
        aria-label={label}
        min="0"
        max="100"
        value={pct}
        style={accentColor ? { accentColor } : undefined}
        onChange={(event) => onChange(Number(event.target.value) / 100)}
      />
      <span className="bm-slider-value">{pct}%</span>
    </div>
  )
}

export function EffectsMixer({ echo, reverb, onEchoChange, onReverbChange }) {
  return (
    <section className="bm-mixer" aria-label="Effects">
      <SliderRow label="Echo" value={echo} onChange={onEchoChange} />
      <SliderRow label="Reverb" value={reverb} onChange={onReverbChange} />
    </section>
  )
}

export function PadDetail({
  selected,
  selectedPad,
  recordTarget,
  isRecording,
  liveCanvasRef,
  waveCanvasRef,
  volumes,
  renaming,
  renameValue,
  onRenameValueChange,
  onRenameFinish,
  onRenameCancel,
  onRenameStart,
  onVolumeChange,
  onRecordStart,
  onRecordStop,
  onClear,
}) {
  if (isRecording && recordTarget !== null) {
    return (
      <div className="bm-detail">
        <div className="bm-detail-head">
          <div>
            <span className="bm-detail-kicker">Recording</span>
            <h2 className="bm-detail-title">{sampleName(recordTarget)}</h2>
          </div>
          <span className="bm-pill is-recording">Max {MAX_RECORD_SECONDS}s</span>
        </div>
        <div className="bm-wave-wrap">
          <canvas ref={liveCanvasRef} className="bm-wave" />
        </div>
        <div className="bm-controls">
          <button type="button" className="bm-btn bm-btn-danger" onClick={onRecordStop}>
            Stop
          </button>
        </div>
      </div>
    )
  }

  if (!selected) {
    return <EmptyState title="No pad selected" text="Choose a kit pad or sample slot." />
  }

  const isCustom = selectedPad >= CUSTOM_START
  const selectedHasAudio = selected.buffer || selected.savedAudio || selected.isPreset

  if (!selectedHasAudio && isCustom) {
    return (
      <div className="bm-detail">
        <div className="bm-detail-head">
          <div>
            <span className="bm-detail-kicker">Empty sample</span>
            <h2 className="bm-detail-title">{sampleName(selectedPad)}</h2>
          </div>
          <span className="bm-pill">Ready</span>
        </div>
        <EmptyState title="Record a sample" text="Audio is saved to this pad." />
        <div className="bm-controls">
          <button type="button" className="bm-btn bm-btn-primary" onClick={() => onRecordStart(selectedPad)}>
            Record
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="bm-detail">
      <div className="bm-detail-head">
        <div>
          <span className="bm-detail-kicker">{isCustom ? 'Sample' : 'Kit'}</span>
          {renaming ? (
            <input
              className="bm-input"
              value={renameValue}
              autoFocus
              maxLength={18}
              onChange={(event) => onRenameValueChange(event.target.value)}
              onBlur={onRenameFinish}
              onKeyDown={(event) => {
                if (event.key === 'Enter') onRenameFinish()
                if (event.key === 'Escape') onRenameCancel()
              }}
            />
          ) : (
            <h2 className="bm-detail-title">{selected.name || sampleName(selectedPad)}</h2>
          )}
        </div>
        <span className="bm-pill">{String(selectedPad + 1).padStart(2, '0')}</span>
      </div>
      <div className="bm-wave-wrap">
        {selected.buffer ? (
          <canvas ref={waveCanvasRef} className="bm-wave" />
        ) : (
          <div className="bm-empty">
            <div className="bm-empty-title">Sample cached</div>
            <p className="bm-empty-text">Tap the pad to draw the waveform.</p>
          </div>
        )}
      </div>
      <SliderRow
        label="Volume"
        value={volumes[selectedPad] ?? 0.8}
        accentColor={selected.color}
        onChange={(value) => onVolumeChange(selectedPad, value)}
      />
      {isCustom && (
        <div className="bm-controls">
          <button type="button" className="bm-btn bm-btn-secondary" onClick={() => onRenameStart(selectedPad)}>
            Rename
          </button>
          <button type="button" className="bm-btn bm-btn-secondary" onClick={() => onRecordStart(selectedPad)}>
            Record
          </button>
          <button type="button" className="bm-btn bm-btn-danger" onClick={() => onClear(selectedPad)}>
            Clear
          </button>
        </div>
      )}
    </div>
  )
}
