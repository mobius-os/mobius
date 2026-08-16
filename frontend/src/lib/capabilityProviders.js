import { startMicrophoneCapture } from './microphoneCapture.js'
import {
  createDeviceAssetCacheProvider,
  DEVICE_ASSET_CACHE,
} from './deviceAssetCache.js'

export const MICROPHONE_CAPTURE = 'media.microphone.capture'
export const SPEECH = 'media.speech'
export const SPEECH_MODELS = 'device.speech-models'
export const WORKSPACE_SHORTCUTS = 'workspace.shortcuts'

export function createSpeechProvider({
  loadRuntime = () => import('./speech/speechProviderRuntime.js'),
} = {}) {
  return {
    version: 1,
    exclusive: true,
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

// The app that DECLARES workspace.shortcuts is also the only reasonable
// caller of its pause/resume action (it owns the toggle UI), but the write
// belongs entirely to the shell: an opaque app frame cannot itself call
// PATCH /api/apps/{id} (no origin-authenticated fetch is exposed to it, by
// design — see runtime/index.js's module doc), so this provider does the
// authenticated update.apps.update() on the app's behalf and lets its
// existing list-cache invalidation carry the new paused state back to
// hasWorkspaceShortcutProvider() in useWorkspaceShortcuts.js.
export function createWorkspaceShortcutsPauseProvider({ appId, api }) {
  return {
    version: 1,
    async open({ input, channel }) {
      const paused = input?.paused === true
      try {
        const response = await api.apps.update(appId, {
          capability_pause: { [WORKSPACE_SHORTCUTS]: paused },
        })
        if (!response.ok) {
          throw new Error(`Could not update shortcut state (${response.status}).`)
        }
        channel.result({ paused })
      } catch (error) {
        channel.error(error)
      }
      return { control() {} }
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
    [WORKSPACE_SHORTCUTS]: createWorkspaceShortcutsPauseProvider({
      appId: options.deviceAssets?.appId,
      ...options.workspaceShortcuts,
    }),
  }
}
