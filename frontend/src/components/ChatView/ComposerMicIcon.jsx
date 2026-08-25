/**
 * ComposerMicIcon — the OpenAI Apps SDK microphone at the lighter optical
 * weight approved in the composer artifact.
 *
 * The SDK remains the source of the glyph. A small, instance-local morphology
 * filter reduces its nominal 2-unit bands to the artifact's 1.8-unit treatment.
 */

import { useId } from 'react'
import { Mic } from '@openai/apps-sdk-ui/components/Icon'

const SIZE = 22
const EROSION_RADIUS = 0.1

export default function ComposerMicIcon() {
  const filterId = `${useId()}-composer-mic-weight`

  return (
    <svg
      width={SIZE}
      height={SIZE}
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <filter
        id={filterId}
        x="-1"
        y="-1"
        width="26"
        height="26"
        filterUnits="userSpaceOnUse"
      >
        <feMorphology
          in="SourceGraphic"
          operator="erode"
          radius={EROSION_RADIUS}
        />
      </filter>
      <g filter={`url(#${filterId})`}>
        <Mic width="24" height="24" />
      </g>
    </svg>
  )
}
