import { appBuildFailureAppName } from './appBuildFailure.js'

const REASSURANCE = 'The previous version is still running.'

export function appUpdateStaleMessage(event) {
  const appName = appBuildFailureAppName(event)
  return `The pending update for ${appName} changed upstream. Review the latest update and start again. ${REASSURANCE}`
}
