// Shape a recorded voice sample into the prompt Pocket TTS was trained on.
//
// Training prompts end inside the short pause between two words. A prompt that
// stops on speech makes the model continue that utterance (a burst or a wrong
// first word at the start of generated audio), and one that ends on a long
// silence delays the onset. This mirrors upstream pocket_tts `end_on_pause`
// (kyutai-labs/pocket-tts#334), which `get_state_for_audio_prompt` applies to
// every cloning prompt: cut trailing 20 ms frames more than 35 dB below the
// loudest one, fade the last 20 ms of what remains, then append 80 ms of zeros.

const FRAME_SECONDS = 0.02
const PAUSE_SECONDS = 0.08
const FADE_SECONDS = 0.02
const FLOOR_DB = 35

export function endVoicePromptOnPause(samples, sampleRate) {
  const frame = Math.max(1, Math.floor(FRAME_SECONDS * sampleRate))
  const frames = Math.floor(samples.length / frame)
  if (frames === 0) return samples
  const levels = new Float64Array(frames)
  let loudest = -Infinity
  for (let index = 0; index < frames; index += 1) {
    let energy = 0
    for (let offset = index * frame; offset < (index + 1) * frame; offset += 1) {
      energy += samples[offset] * samples[offset]
    }
    levels[index] = 20 * Math.log10(Math.sqrt(energy / frame) + 1e-12)
    loudest = Math.max(loudest, levels[index])
  }
  let lastLoud = frames - 1
  while (lastLoud > 0 && !(levels[lastLoud] > loudest - FLOOR_DB)) lastLoud -= 1
  const end = (lastLoud + 1) * frame
  const pause = Math.floor(PAUSE_SECONDS * sampleRate)
  const prompt = new Float32Array(end + pause)
  prompt.set(samples.subarray(0, end))
  const fade = Math.min(Math.floor(FADE_SECONDS * sampleRate), end)
  for (let index = 0; index < fade; index += 1) {
    // torch.linspace(1, 0, fade): the first faded sample keeps full level and
    // the last reaches exactly zero.
    prompt[end - fade + index] *= fade === 1 ? 0 : 1 - index / (fade - 1)
  }
  return prompt
}
