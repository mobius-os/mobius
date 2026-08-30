import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  SPEECH_PITCH_WORKLET_PATH,
  SPEECH_PITCH_WORKLET_SHA256,
  SPEECH_PITCH_WORKLET_VERSION,
} from '../src/lib/speech/speechPitchAsset.js'


const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const worklet = await readFile(path.join(frontendDir, 'public', SPEECH_PITCH_WORKLET_PATH))
const actual = createHash('sha256').update(worklet).digest('hex')
if (actual !== SPEECH_PITCH_WORKLET_SHA256) {
  console.error(
    `Speech pitch worklet ${SPEECH_PITCH_WORKLET_VERSION} digest mismatch: `
    + `expected ${SPEECH_PITCH_WORKLET_SHA256}, got ${actual}.`,
  )
  process.exitCode = 1
} else {
  console.log(`speech pitch worklet ${SPEECH_PITCH_WORKLET_VERSION} OK: ${actual}`)
}
