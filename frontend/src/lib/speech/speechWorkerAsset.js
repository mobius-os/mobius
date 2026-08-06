// One definition of where the speech worker ships, shared by the script that
// builds it, the Vite config that keeps it out of the precache, the build check
// that enforces that, and the engine that loads it. Drift between any two of
// them is a silent production break: the worker 404s, and a worker whose script
// never loads reports a bare error event with no message at all.
export const SPEECH_WORKER_PATH = 'speech/pocket-tts-worker.js'
export const SPEECH_WORKER_URL = `/${SPEECH_WORKER_PATH}`
export const SPEECH_WASM_URL = '/speech/pocket-tts-xn.wasm'
