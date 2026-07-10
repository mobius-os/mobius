import { CUSTOM_START, TOTAL_BEATS } from '../audio.js'
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
  const visibleRows = pads
    .map((pad, padIdx) => ({ pad, padIdx }))
    .filter(({ pad }) => pad.buffer || pad.isPreset)

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
        <button
          type="button"
          style={S.clearBtn}
          onClick={onClear}
          title="Clear pattern"
          aria-label="Clear pattern"
        >
          Clear
        </button>
      </div>

      <div style={S.seqScrollWrapper}>
        <div style={S.seqLabelsCol} aria-hidden="true">
          <div style={{ height: 16, flexShrink: 0 }} />
          {visibleRows.map(({ pad, padIdx }) => {
            return (
              <div
                key={padIdx}
                style={{
                  ...S.seqRowLabel,
                  color: pad.color,
                  borderTop: padIdx === CUSTOM_START ? '1px solid var(--border)' : 'none',
                  marginTop: padIdx === CUSTOM_START ? 3 : 0,
                }}
                title={pad.name || `Pad ${padIdx + 1}`}
              >
                {pad.name || `Rec ${padIdx - CUSTOM_START + 1}`}
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
            {visibleRows.map(({ pad, padIdx }) => {
              return (
                <div
                  key={padIdx}
                  style={{
                    ...S.seqCells,
                    borderTop: padIdx === CUSTOM_START ? '1px solid var(--border)' : 'none',
                    marginTop: padIdx === CUSTOM_START ? 3 : 0,
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
                          cursor: 'pointer',
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
