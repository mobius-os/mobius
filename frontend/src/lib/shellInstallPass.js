import { api, getToken } from '../api/client.js'
import { detectInstallPlatform, isStandaloneDisplay } from '../utils/installPlatform.js'
import { createShellInstallPassPreparer } from './shellInstallPassPreparer.js'

const shellInstallPassPreparer = createShellInstallPassPreparer({
  request: (options) => api.auth.shellInstallPass.prepare(options),
  isIos: () => detectInstallPlatform().ios,
  isStandalone: () => isStandaloneDisplay(),
  hasToken: () => Boolean(getToken()),
})

export const prepareShellInstallPass = shellInstallPassPreparer.prepare
export const stopShellInstallPassPreparation = shellInstallPassPreparer.stop
