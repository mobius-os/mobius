/**
 * BrainUsageIcon — the canonical OpenAI Apps SDK brain outline with two
 * bottom-up gauges behind its transparent lobes: selected-provider allowance
 * consumed on the left and current context consumed on the right.
 *
 * The visible boundary is the actual installed SDK <Brain>, not a redrawn
 * approximation. Only its outer contour is repeated locally as an inset mask
 * for the colored liquid beneath it. The mask ends at the visible outline's
 * inner edge, so the SDK's transparent lobe spaces reveal the fill without a
 * purple/orange halo escaping around the outside of the brain.
 *
 * Left hemisphere = selected provider's allowance used (purple).
 * Right hemisphere = context window used (orange).
 *
 * Each hemisphere fills bottom-up: `leftPercent`/`rightPercent` (0-100) is
 * the consumed fraction. 100 = fully colored, 0 = transparent, 50 = half
 * transparent/half colored. `null` (not loaded / unavailable) stays
 * transparent instead of guessing.
 *
 * Mask/filter ids MUST be unique per rendered instance (via `useId()`), not a
 * static string. Möbius routinely keeps more than one chat pane mounted at
 * once (background/companion panes), so a static id collided across
 * instances — the browser resolves `url(#id)` to the FIRST matching element
 * in the whole document, and when that first element lived in a
 * `display:none` background pane, Chromium silently failed the reference
 * for every instance, clipping the colored fill to nothing everywhere and
 * leaving only the grey base visible. Caught by comparing computed styles
 * (fill + visibility both correct) against actual painted pixels (grey
 * only) in a live screenshot — the mismatch only makes sense as an SVG
 * resource-id collision.
 */

import { useId } from 'react'
import { Brain } from '@openai/apps-sdk-ui/components/Icon'
import { visibleBrainFillBounds } from './brainUsage.js'

const PROVIDER_COLOR = 'var(--accent)'
const CONTEXT_COLOR = '#d97757'
const DEFAULT_SIZE = 38
// The partner's artifact reference used the pre-v4 middle treatment: eroding
// 0.25 units from the SDK glyph's nominal 2-unit bands. The matching fill inset
// below meets that visible inner edge without letting color escape the brain.
const OUTLINE_EROSION_RADIUS = 0.25
const NOMINAL_BOUNDARY_WIDTH = 2
const FILL_INSET = NOMINAL_BOUNDARY_WIDTH - OUTLINE_EROSION_RADIUS

// Full brain silhouette, both lobes in one contour. Bounding box is roughly
// x:[2.3, 21.7] y:[2.3, 21.7] in the 24x24 viewBox — centered, so the two
// fill rectangles split it evenly at x=12.
const BRAIN_SILHOUETTE_PATH = 'M14.8974 2.29998C15.8303 2.29013 16.802 2.58194 17.5566 3.22577'
  + 'C18.1589 3.73967 18.5845 4.44761 18.7451 5.31073C20.0159 5.68745 21.0027 6.50113 21.4482 7.6037'
  + 'C21.8952 8.70995 21.7263 9.93091 20.9902 10.9914C21.5775 11.9456 21.7666 13.1257 21.6757 14.2189'
  + 'C21.6886 14.283 21.6962 14.3493 21.6962 14.4172C21.6961 16.0908 20.757 17.5402 19.3808 18.2785'
  + 'C18.7896 20.2128 17.1855 21.4914 15.4599 21.6769C14.5141 21.7787 13.5339 21.5479 12.7128 20.9133'
  + 'C12.4502 20.7102 12.2116 20.4715 11.999 20.1994C11.7854 20.4727 11.5463 20.7126 11.2822 20.9162'
  + 'C10.4592 21.5506 9.47721 21.78 8.53022 21.676C6.80047 21.4857 5.19378 20.1976 4.6103 18.2521'
  + 'C3.32727 17.5234 2.6154 16.0905 2.38764 14.7404C2.18065 13.5132 2.32674 12.0996 3.00873 10.9914'
  + 'C2.2728 9.93104 2.10389 8.7098 2.55073 7.6037C2.99615 6.50129 3.98328 5.68752 5.25385 5.31073'
  + 'C5.41438 4.44772 5.84013 3.73967 6.44233 3.22577C7.19688 2.58201 8.16873 2.2902 9.10151 2.29998'
  + 'C10.0348 2.30984 11.0025 2.62141 11.7509 3.2785C11.8376 3.35461 11.9199 3.43566 11.999 3.51971'
  + 'C12.0783 3.43538 12.162 3.35484 12.249 3.2785C12.9972 2.62161 13.9643 2.3099 14.8974 2.29998Z'

// Fill grows bottom-up across the path's actual ink, not the full viewBox.
const TOP = 2.3
const BOTTOM = 21.7
// The mask removes FILL_INSET from the silhouette's edge. Percentages must map
// to the remaining visible interior, not to the now-invisible outer contour;
// otherwise the last ~8% paints above every visible lobe and looks identical.
function HemisphereFill({ percent, side, color }) {
  const { fillHeight, fillY } = visibleBrainFillBounds(percent, {
    top: TOP,
    bottom: BOTTOM,
    inset: FILL_INSET,
  })
  const x = side === 'left' ? 0 : 12

  return (
    <rect x={x} y={fillY} width="12" height={fillHeight} fill={color} />
  )
}

export default function BrainUsageIcon({
  leftPercent = null,
  rightPercent = null,
  width = DEFAULT_SIZE,
  height = DEFAULT_SIZE,
}) {
  // Unique per rendered icon — see the id-collision note above. Multiple
  // mounted chat panes must never resolve this fill to another icon's mask.
  const uid = useId()
  const fillMaskId = `${uid}-fill-mask`
  const outlineFilterId = `${uid}-outline`
  const leftKnown = typeof leftPercent === 'number' && Number.isFinite(leftPercent)
  const rightKnown = typeof rightPercent === 'number' && Number.isFinite(rightPercent)

  return (
    <svg
      width={width}
      height={height}
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <mask
        id={fillMaskId}
        maskUnits="userSpaceOnUse"
        x="0"
        y="0"
        width="24"
        height="24"
      >
        <rect width="24" height="24" fill="black" />
        <path
          d={BRAIN_SILHOUETTE_PATH}
          fill="white"
          stroke="black"
          strokeWidth={FILL_INSET * 2}
          strokeLinejoin="round"
        />
      </mask>
      <filter
        id={outlineFilterId}
        x="-1"
        y="-1"
        width="26"
        height="26"
        filterUnits="userSpaceOnUse"
      >
        <feMorphology
          in="SourceGraphic"
          operator="erode"
          radius={OUTLINE_EROSION_RADIUS}
        />
      </filter>
      <g mask={`url(#${fillMaskId})`}>
        {leftKnown && (
          <HemisphereFill
            percent={leftPercent}
            side="left"
            color={PROVIDER_COLOR}
          />
        )}
        {rightKnown && (
          <HemisphereFill
            percent={rightPercent}
            side="right"
            color={CONTEXT_COLOR}
          />
        )}
      </g>

      {/* Canonical visible boundary. The SDK path supplies every lobe/fold;
          the fill above only shows through its transparent spaces. */}
      <g filter={`url(#${outlineFilterId})`}>
        <Brain width="24" height="24" color="var(--muted)" />
      </g>
    </svg>
  )
}
