import { TOTAL_BEATS } from '../audio.js'
import { S } from '../styles.js'

export function Sequencer({
  pads,
  grid,
  playing,
  bpm,
  currentBeat,
  seqScrollRef,
  onBpmChange,
  onTogglePlay,
  onClear,
  onToggleCell,
}) {
  return (
    <section style={S.seqSection} aria-label="Step sequencer">
      <div style={S.transport}>
        <button
          type="button"
          style={{ ...S.transportBtn, background: playing ? '#f87171' : 'var(--accent)' }}
          onClick={onTogglePlay}
          aria-pressed={playing}
        >
          {playing ? '■ Stop' : '▶ Play'}
        </button>
        <div style={S.bpmControl}>
          <label style={S.bpmLabel} htmlFor="bm-bpm">BPM</label>
          <input
            id="bm-bpm"
            type="range"
            min={60}
            max={200}
            value={bpm}
            onChange={(event) => onBpmChange(Number(event.target.value))}
            style={S.bpmSlider}
          />
          <span style={S.bpmValue}>{bpm}</span>
        </div>
        <button type="button" style={S.clearBtn} onClick={onClear}>Clear</button>
      </div>

      <div style={S.seqScrollWrapper}>
        <div style={S.seqLabelsCol} aria-hidden="true">
          <div style={{ height: 16, flexShrink: 0 }} />
          {pads.map((pad, idx) => {
            const has = pad.buffer || pad.isPreset
            return (
              <div
                key={idx}
                style={{
                  ...S.seqRowLabel,
                  color: has ? pad.color : 'var(--muted)',
                  opacity: has ? 1 : 0.25,
                  borderTop: idx === 8 ? '1px solid var(--border)' : 'none',
                  marginTop: idx === 8 ? 2 : 0,
                }}
              >
                {pad.name || `${idx + 1}`}
              </div>
            )
          })}
        </div>

        <div className="bm-scroll-skin" style={S.seqScrollArea} ref={seqScrollRef}>
          <div style={S.seqGridInner}>
            <div style={S.beatNumbers} aria-hidden="true">
              {Array.from({ length: TOTAL_BEATS }, (_, beatIdx) => (
                <div
                  key={beatIdx}
                  style={{
                    ...S.beatNum,
                    color: currentBeat === beatIdx ? 'var(--accent)' : 'var(--muted)',
                    fontWeight: currentBeat === beatIdx ? 700 : 400,
                  }}
                >
                  {beatIdx + 1}
                </div>
              ))}
            </div>
            {pads.map((pad, padIdx) => {
              const has = pad.buffer || pad.isPreset
              return (
                <div
                  key={padIdx}
                  style={{
                    ...S.seqCells,
                    borderTop: padIdx === 8 ? '1px solid var(--border)' : 'none',
                    marginTop: padIdx === 8 ? 2 : 0,
                  }}
                  role="row"
                  aria-label={pad.name || `Pad ${padIdx + 1}`}
                >
                  {Array.from({ length: TOTAL_BEATS }, (_, beatIdx) => {
                    const on = grid[padIdx]?.[beatIdx] === true
                    const cur = currentBeat === beatIdx
                    return (
                      <button
                        key={beatIdx}
                        type="button"
                        disabled={!has}
                        onClick={() => onToggleCell(padIdx, beatIdx)}
                        aria-label={`${pad.name || `Pad ${padIdx + 1}`} beat ${beatIdx + 1}`}
                        aria-pressed={on}
                        style={{
                          ...S.cell,
                          background: on
                            ? pad.color
                            : cur
                              ? 'rgba(255,255,255,0.06)'
                              : beatIdx % 8 < 4
                                ? 'var(--surface)'
                                : 'rgba(255,255,255,0.015)',
                          borderColor: cur ? 'var(--accent)' : on ? `${pad.color}66` : 'var(--border)',
                          opacity: has ? 1 : 0.12,
                          cursor: has ? 'pointer' : 'default',
                          boxShadow: on && cur ? `0 0 6px ${pad.color}55` : 'none',
                        }}
                      />
                    )
                  })}
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </section>
  )
}
