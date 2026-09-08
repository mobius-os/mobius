// Pocket TTS emits audio at 12.5 model frames per second. Its default tail is
// only one frame for longer text, which can stop the last phoneme mid-decay.
export const COMPLETE_POST_EOS_FRAMES = 6


export function completePostEosFrames(modelFrames) {
  return Math.max(modelFrames, COMPLETE_POST_EOS_FRAMES)
}
