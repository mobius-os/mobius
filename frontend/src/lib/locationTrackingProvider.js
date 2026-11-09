// `location.tracking` capability: lets a visible app start, stop, pace, or
// read the shell's native location controller (lib/nativeLocation.js). The
// controller and `window.MobiusNative` live only in the shell's top frame, so
// this provider is the app's sole route to them. On a surface without the phone
// app (a laptop), `get` reports what the owner's phones last reported instead,
// read-only: whether any phone is tracking and at what pace. Recorded positions
// are read from the server under the separate `location_history_read`
// permission.
import * as nativeLocation from './nativeLocation.js'

export const LOCATION_TRACKING = 'location.tracking'

const OPERATIONS = new Set(['get', 'start', 'stop', 'set-options'])

function unavailable() {
  const error = new Error('Location tracking runs only in the Möbius phone app.')
  error.name = 'NotSupportedError'
  error.code = 'unavailable'
  return error
}

async function remoteSnapshot(controller) {
  const reported = await controller.fetchReportedTracking()
  return {
    available: false,
    enabled: reported?.enabled === true,
    status: null,
    options: reported?.options || {},
  }
}

function snapshot(controller) {
  const state = controller.getLocationState()
  return {
    available: state.available === true,
    enabled: state.enabled === true,
    status: state.status ?? null,
    options: controller.getTrackingOptions(),
  }
}

export function createLocationTrackingProvider({ controller = nativeLocation } = {}) {
  return {
    version: 1,
    async open({ input, channel }) {
      const operation = input?.operation
      if (!OPERATIONS.has(operation)) {
        throw new TypeError('Unknown location tracking operation.')
      }
      if (!controller.isNativeLocationAvailable()) {
        if (operation !== 'get') throw unavailable()
        channel.result(await remoteSnapshot(controller))
        return {}
      }
      if (operation === 'start') await controller.startTracking()
      else if (operation === 'stop') await controller.stopTracking()
      else if (operation === 'set-options') {
        const options = input?.options
        if (!options || typeof options !== 'object' || Array.isArray(options)) {
          throw new TypeError('Location tracking options must be an object.')
        }
        await controller.setTrackingOptions(options)
      }
      channel.result(snapshot(controller))
      return {}
    },
  }
}
