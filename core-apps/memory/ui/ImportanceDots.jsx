import { S } from '../constants.js'

// Importance 1..5 rendered as filled/empty pips — calmer than a raw number,
// and it reads as a rating at a glance.
export function ImportanceDots({ value }) {
  const v = Math.max(1, Math.min(5, value | 0));
  return (
    <span style={S.dotsWrap} title={`importance ${v}/5`}>
      {[1, 2, 3, 4, 5].map((i) => (
        <span key={i} style={{ ...S.pip, ...(i <= v ? S.pipOn : {}) }} />
      ))}
    </span>
  );
}
