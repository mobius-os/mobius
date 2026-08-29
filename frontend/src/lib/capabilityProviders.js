import { startMicrophoneCapture } from './microphoneCapture.js'
import {
  createDeviceAssetCacheProvider,
  DEVICE_ASSET_CACHE,
} from './deviceAssetCache.js'

export const MICROPHONE_CAPTURE = 'media.microphone.capture'
export const SPEECH = 'media.speech'
export const SPEECH_MODELS = 'device.speech-models'

export function createSpeechProvider({
  loadRuntime = () => import('./speech/speechProviderRuntime.js'),
} = {}) {
  return {
    version: 1,
    // Catalog reads do not contend with playback. A new synthesis/model stream
    // is an explicit user intent and replaces stale speech work instead of
    // exposing a generic "already in use" dead end.
    contention({ input, activeInput }) {
      const operation = input?.operation || 'synthesize'
      const activeOperation = activeInput?.operation || 'synthesize'
      if (operation === 'catalog' || activeOperation === 'catalog') return 'share'
      return 'replace'
    },
    async open(context) {
      const runtime = await loadRuntime()
      return runtime.openSpeechCapability(context)
    },
  }
}

export function createSpeechModelsProvider({
  appId,
  loadRuntime = () => import('./speech/speechModelsProviderRuntime.js'),
} = {}) {
  return {
    version: 1,
    exclusive: true,
    onDeactivate: 'cancel',
    async open(context) {
      const runtime = await loadRuntime()
      return runtime.openSpeechModelsCapability({ ...context, appId })
    },
  }
}

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
      // Return the control surface before waiting for the first PCM frame, so
      // the app or lifecycle host can still cancel a stalled startup. The
      // capability itself is not ready until the recorder has received audio.
      capture.ready.then(() => {
        channel.ready({ sampleRate: capture.sampleRate })
        return capture.done
      }).then((result) => {
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
    [DEVICE_ASSET_CACHE]: createDeviceAssetCacheProvider(options.deviceAssets),
    [MICROPHONE_CAPTURE]: createMicrophoneProvider(options.microphone),
    [SPEECH]: createSpeechProvider(options.speech),
    [SPEECH_MODELS]: createSpeechModelsProvider({
      appId: options.deviceAssets?.appId,
      ...options.speechModels,
    }),
  }
}
