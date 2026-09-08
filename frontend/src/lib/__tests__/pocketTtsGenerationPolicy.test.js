import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  COMPLETE_POST_EOS_FRAMES,
  completePostEosFrames,
} from '../speech/pocketTtsGenerationPolicy.js'


test('Pocket TTS lets the final phoneme decay without weakening longer model tails', () => {
  assert.equal(COMPLETE_POST_EOS_FRAMES, 6)
  assert.equal(completePostEosFrames(1), 6)
  assert.equal(completePostEosFrames(3), 6)
  assert.equal(completePostEosFrames(6), 6)
  assert.equal(completePostEosFrames(8), 8)
})
