/**
 * BrainUsageIcon — a lobed brain silhouette (frontal view), split into two
 * hemispheres that each act as a liquid-style gauge for one provider's
 * remaining usage before it rate-limits.
 *
 * The outline is adapted from the platform's own installed brain glyph
 * (`@openai/apps-sdk-ui/components/Icon`'s `Brain`, MIT-licensed like the
 * rest of that package) rather than hand-drawn from scratch — it already
 * reads as a proper scalloped brain silhouette (see that package's asset),
 * closer to the reference the owner asked for than an earlier bespoke
 * teardrop shape. It's drawn as ONE contour spanning both lobes (no seam
 * down the middle in the path data itself), so the two hemispheres are
 * carved out here with rectangular clips at the vertical center (x=12 of a
 * 24-wide viewBox) rather than by splitting the path — simpler and exactly
 * as precise, since a clip only needs a straight edge.
 *
 * Left hemisphere = Codex (purple), right hemisphere = Claude (orange). These
 * are the two connected coding subscriptions that publish quota windows;
 * Möbius Evolve exposes its trial state in Settings instead of inventing a
 * third quota slice. Claude's own brand mark uses the same warm clay/orange.
 *
 * Each hemisphere fills bottom-up: `leftPercent`/`rightPercent` (0-100) is
 * the remaining fraction of whichever rate-limit window (5-hour or weekly —
 * see BrainUsageButton.jsx) is CLOSER to exhausted for that provider. 100 =
 * fully colored, 0 = fully grey (exhausted), 50 = half grey/half colored.
 * `null` (usage not loaded yet / unavailable) renders a dim neutral outline
 * instead of guessing.
 *
 * clipPath ids MUST be unique per rendered instance (via `useId()`), not a
 * static string. Möbius routinely keeps more than one chat pane mounted at
 * once (background/companion panes), so a static id collided across
 * instances — the browser resolves `url(#id)` to the FIRST matching element
 * in the whole document, and when that first element lived in a
 * `display:none` background pane, Chromium silently failed the reference
 * for every instance, clipping the colored fill to nothing everywhere and
 * leaving only the grey base visible. Caught by comparing computed styles
 * (fill + visibility both correct) against actual painted pixels (grey
 * only) in a live screenshot — the mismatch only makes sense as a clip-path
 * id collision.
 */

import { useId } from 'react'

const GREY = 'var(--muted, #8a8a94)'
const PURPLE = '#8b5cf6'
const ORANGE = '#d97757'

// Full brain silhouette, both lobes in one contour. Bounding box is roughly
// x:[2.3, 21.7] y:[2.3, 21.7] in the 24x24 viewBox — centered, so a clip at
// x=12 splits it evenly.
const BRAIN_PATH = 'M14.8974 2.29998C15.8303 2.29013 16.802 2.58194 17.5566 3.22577'
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
  + 'M10.9999 6.17108C10.9998 5.5022 10.7533 5.06474 10.4306 4.78143C10.0884 4.48116 9.60157 4.30555 9.081 4.29998'
  + 'C8.55965 4.29448 8.07721 4.46058 7.74116 4.74725C7.42624 5.01593 7.18256 5.43636 7.18256 6.09393V6.12225'
  + 'C7.18544 6.6218 6.81963 7.04734 6.32514 7.11834C5.22327 7.27654 4.61609 7.83085 4.40522 8.35272'
  + 'C4.26751 8.69354 4.25234 9.13509 4.5058 9.61639C5.13533 9.26679 5.86003 9.06662 6.6308 9.06659'
  + 'C7.18307 9.06659 7.63077 9.51433 7.6308 10.0666C7.6308 10.6189 7.18309 11.0666 6.6308 11.0666'
  + 'C5.98117 11.0666 5.39367 11.3251 4.96284 11.7473C4.92842 11.781 4.89119 11.8103 4.85346 11.8381'
  + 'C4.39663 12.4129 4.18564 13.3773 4.35932 14.4074C4.55066 15.5418 5.13668 16.3609 5.82123 16.6213'
  + 'C6.14352 16.7438 6.38036 17.0242 6.44721 17.3625C6.7245 18.7647 7.77678 19.5806 8.74799 19.6877'
  + 'C9.2239 19.74 9.68087 19.6256 10.0615 19.3322C10.4299 19.0481 10.794 18.5409 10.9999 17.6867V6.17108Z'
  + 'M12.9999 17.6877C13.2056 18.54 13.5688 19.047 13.9365 19.3312C14.3162 19.6246 14.7714 19.7397 15.246 19.6887'
  + 'C15.8221 19.6267 16.4253 19.3131 16.8808 18.7775C16.3687 18.7272 15.881 18.5901 15.4345 18.3781'
  + 'C14.9357 18.1411 14.7229 17.544 14.9599 17.0451C15.197 16.5466 15.7933 16.3347 16.2919 16.5715'
  + 'C16.6002 16.7179 16.9461 16.8 17.3134 16.8C17.5725 16.8 17.8193 16.7562 18.0507 16.6808'
  + 'C18.0914 16.6584 18.1336 16.6381 18.1777 16.6213C18.8623 16.3609 19.4482 15.5419 19.6396 14.4074'
  + 'C19.8303 13.2768 19.5586 12.2249 19.0058 11.6808C18.8148 11.4929 18.707 11.236 18.707 10.968'
  + 'C18.707 10.7 18.8148 10.443 19.0058 10.2551C19.7383 9.53413 19.7916 8.84018 19.5947 8.35272'
  + 'C19.4539 8.00424 19.1357 7.64175 18.6122 7.39178C18.2868 8.40588 17.5605 9.23979 16.6191 9.70233'
  + 'C16.1235 9.94566 15.5237 9.74089 15.2802 9.2453C15.0368 8.74965 15.2416 8.14994 15.7373 7.90643'
  + 'C16.3692 7.59585 16.8006 6.94772 16.8007 6.20038C16.8007 6.148 16.8057 6.09628 16.8134 6.04608'
  + 'C16.802 5.41578 16.5659 5.0094 16.2587 4.74725C15.9227 4.46055 15.4403 4.29449 14.9189 4.29998'
  + 'C14.398 4.30549 13.9106 4.48092 13.5683 4.78143C13.286 5.02931 13.0623 5.39527 13.0107 5.93084'
  + 'L12.9999 6.17108V17.6877Z'

// Fill grows bottom-up across the path's actual ink, not the full viewBox.
const TOP = 2.3
const BOTTOM = 21.7

function HemisphereFill({ id, percent, color, side }) {
  const known = typeof percent === 'number' && Number.isFinite(percent)
  const clamped = known ? Math.min(100, Math.max(0, percent)) : 100
  const fillHeight = ((BOTTOM - TOP) * clamped) / 100
  const fillY = BOTTOM - fillHeight
  const x = side === 'left' ? 0 : 12

  return (
    <clipPath id={id}>
      <rect x={x} y={fillY} width="12" height={fillHeight} />
    </clipPath>
  )
}

export default function BrainUsageIcon({
  leftPercent = null,
  rightPercent = null,
  width = 26,
  height = 26,
}) {
  // Base id unique to THIS rendered icon instance — see the id-collision
  // note above. Every hemisphere's clipPath id is derived from it so two
  // simultaneously-mounted chat panes never share one.
  const uid = useId()
  const leftId = `${uid}-left`
  const rightId = `${uid}-right`
  const leftKnown = typeof leftPercent === 'number' && Number.isFinite(leftPercent)
  const rightKnown = typeof rightPercent === 'number' && Number.isFinite(rightPercent)
  const leftHalfId = `${uid}-half-left`
  const rightHalfId = `${uid}-half-right`

  return (
    <svg
      width={width}
      height={height}
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <clipPath id={leftHalfId}><rect x="0" y="0" width="12" height="24" /></clipPath>
      <clipPath id={rightHalfId}><rect x="12" y="0" width="12" height="24" /></clipPath>

      {/* Grey base — each half dimmed independently while its provider's
          usage is unknown, since the two are unrelated data points. */}
      <path d={BRAIN_PATH} fill={GREY} opacity={leftKnown ? 1 : 0.35} clipPath={`url(#${leftHalfId})`} />
      <path d={BRAIN_PATH} fill={GREY} opacity={rightKnown ? 1 : 0.35} clipPath={`url(#${rightHalfId})`} />

      {leftKnown && (
        <>
          <HemisphereFill id={leftId} percent={leftPercent} side="left" />
          <path d={BRAIN_PATH} fill={PURPLE} clipPath={`url(#${leftId})`} />
        </>
      )}
      {rightKnown && (
        <>
          <HemisphereFill id={rightId} percent={rightPercent} side="right" />
          <path d={BRAIN_PATH} fill={ORANGE} clipPath={`url(#${rightId})`} />
        </>
      )}
    </svg>
  )
}
