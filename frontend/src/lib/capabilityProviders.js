import { startMicrophoneCapture } from './microphoneCapture.js'

export const MICROPHONE_CAPTURE = 'media.microphone.capture'

export function createMicrophoneProvider({ startCapture = startMicrophoneCapture } = {}) {
  return {
    version: 1,
    exclusive: true,
    // Navigating away should finish and return the partial recording, matching
    // the visible app's explicit Finish action. Unmount/replacement still uses
    // cancel through capabilityHost.destroy().
    onDeactivate: 'finish',
    async open({ input, declaration, channel }) {
      const declaredMax = Number(declaration?.limits?.max_duration_ms) || 30_000
      const requestedMax = Number(input?.maxDurationMs)
      const maxDurationMs = Math.max(100, Math.min(
        declaredMax,
        Number.isFinite(requestedMax) ? requestedMax : declaredMax,
      ))
      const capture = await startCapture({
        maxSeconds: maxDurationMs / 1000,
        onLevel(level) { channel.event('level', level) },
      })
      channel.ready({ sampleRate: capture.sampleRate })
      capture.done.then((result) => {
        const samples = result.samples
        channel.result(
          { samples, sampleRate: result.sampleRate },
          samples?.buffer ? [samples.buffer] : [],
        )
      }).catch((error) => channel.error(error))
      return {
        control(action) {
          if (action === 'finish') capture.stop()
          else if (action === 'cancel') capture.cancel()
        },
      }
    },
  }
}

export function builtInCapabilityProviders(options = {}) {
  return {
    [MICROPHONE_CAPTURE]: createMicrophoneProvider(options.microphone),
  }
}
