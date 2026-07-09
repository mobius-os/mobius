import { CUSTOM_START } from '../audio.js'

export function hasPadAudio(pad) {
  return Boolean(pad?.buffer || pad?.savedAudio || pad?.isPreset)
}

export function sampleName(idx) {
  return `Sample ${idx - CUSTOM_START + 1}`
}

export function padKind(pad, idx) {
  if (idx < CUSTOM_START) return 'Kit'
  return hasPadAudio(pad) ? 'Sample' : 'Record'
}

function padLabel(pad, idx) {
  const name = pad.name || sampleName(idx)
  return `${name}, ${padKind(pad, idx)} pad ${idx + 1}`
}

export function PadButton({
  pad,
  idx,
  selected,
  active,
  recording,
  onTrigger,
}) {
  const empty = idx >= CUSTOM_START && !hasPadAudio(pad)
  const className = [
    'bm-pad',
    empty ? 'is-empty' : '',
    selected ? 'is-selected' : '',
    active ? 'is-active' : '',
    recording ? 'is-recording' : '',
  ].filter(Boolean).join(' ')

  return (
    <button
      type="button"
      className={className}
      style={{ '--pad-color': pad.color }}
      aria-label={padLabel(pad, idx)}
      aria-pressed={selected}
      onPointerDown={() => onTrigger(idx)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onTrigger(idx)
        }
      }}
    >
      <span className="bm-pad-index">{String(idx + 1).padStart(2, '0')}</span>
      <span>
        <span className="bm-pad-name">{pad.name || (idx >= CUSTOM_START ? sampleName(idx) : 'Pad')}</span>
        <span className="bm-pad-kind">{padKind(pad, idx)}</span>
      </span>
    </button>
  )
}
