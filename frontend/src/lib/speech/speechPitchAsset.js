// One pinned definition for the pitch-preserving worklet exposed to apps.
// Keep the upstream version and digest beside its URL so an accidental vendor
// edit cannot silently change the DSP that generated speech depends on.
export const SPEECH_PITCH_WORKLET_PATH = 'speech/soundtouch-processor.js'
export const SPEECH_PITCH_WORKLET_URL = `/${SPEECH_PITCH_WORKLET_PATH}`
export const SPEECH_PITCH_WORKLET_VERSION = '2.1.1'
export const SPEECH_PITCH_WORKLET_SHA256 = '59635b112697ad403f7aebb46fc5386231d5db06877707afcec19f555b4d68ca'
