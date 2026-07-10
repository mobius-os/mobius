import { CUSTOM_START } from '../audio.js'
import { S } from '../styles.js'

function SliderRow({ label, value, onChange, accentColor }) {
  const pct = Math.round((value ?? 0) * 100)
  return (
    <div style={S.sliderRow}>
      <span style={S.sliderLabel}>{label}</span>
      <input
        type="range"
        min={0}
        max={100}
        value={pct}
        aria-label={label}
        onChange={(event) => onChange(Number(event.target.value) / 100)}
        style={{ ...S.slider, accentColor }}
      />
      <span style={S.sliderVal}>{pct}%</span>
    </div>
  )
}

export function ControlPanel({
  pads,
  selectedPad,
  volumes,
  echo,
  reverb,
  isRecording,
  recordTarget,
  renamingPad,
  renameVal,
  liveCanvasRef,
  waveCanvasRef,
  onRenameValChange,
  onRenameFinish,
  onRenameCancel,
  onStartRename,
  onClearPad,
  onVolumeChange,
  onEchoChange,
  onReverbChange,
}) {
  const selected = selectedPad !== null ? pads[selectedPad] : null
  return (
    <aside style={S.rightPanel} aria-label="Sample controls and effects">
      {isRecording ? (
        <div style={S.waveArea}>
          <div style={S.recLabel}>
            <span aria-hidden="true">●</span>
            Rec {recordTarget - CUSTOM_START + 1}
          </div>
          <canvas ref={liveCanvasRef} width={300} height={40} style={S.waveCanvas} />
        </div>
      ) : renamingPad !== null ? (
        <div style={S.waveArea}>
          <input
            autoFocus
            value={renameVal}
            onChange={(event) => onRenameValChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') onRenameFinish()
              if (event.key === 'Escape') onRenameCancel()
            }}
            style={S.renameInput}
            maxLength={12}
            aria-label="Sample name"
          />
          <button type="button" style={S.renameBtn} onClick={onRenameFinish}>OK</button>
        </div>
      ) : selected?.buffer ? (
        <div style={S.waveArea}>
          <div style={S.selRow}>
            <span style={{ color: selected.color }} aria-hidden="true">♪</span>
            <span style={S.selName}>{selected.name}</span>
            {selected.isPreset && <span style={S.presetTag}>KIT</span>}
            {selectedPad >= CUSTOM_START && (
              <>
                <button
                  type="button"
                  style={S.tinyBtn}
                  onClick={() => onStartRename(selectedPad)}
                  title="Rename sample"
                  aria-label="Rename sample"
                >
                  Ren
                </button>
                <button
                  type="button"
                  style={{ ...S.tinyBtn, color: '#f87171' }}
                  onClick={() => onClearPad(selectedPad)}
                  title="Delete sample"
                  aria-label="Delete sample"
                >
                  Del
                </button>
              </>
            )}
          </div>
          <canvas ref={waveCanvasRef} style={S.waveCanvasTall} />
          <SliderRow
            label="Vol"
            value={volumes[selectedPad] ?? 0.8}
            accentColor={selected.color}
            onChange={(value) => onVolumeChange(selectedPad, value)}
          />
        </div>
      ) : (
        <div style={S.emptyHint}>Tap a sound to preview{'\n'}Hold an empty custom pad to record</div>
      )}

      <div style={S.fxArea}>
        <SliderRow label="Echo" value={echo} accentColor="#60a5fa" onChange={onEchoChange} />
        <SliderRow label="Reverb" value={reverb} accentColor="#c084fc" onChange={onReverbChange} />
      </div>
    </aside>
  )
}
