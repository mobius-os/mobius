/* Start private Project preparation through Contribute's durable chat owner. */
import { jsonOrThrow } from '../api/client.js'
import { linkedProjectAppId } from './appSourceProject.js'

export async function prepareProjectContribution(api, project, intentId) {
  const appId = linkedProjectAppId(project)
  if (!appId) throw new Error('Private PR preparation currently supports Projects linked to an installed app.')
  if (!intentId) throw new Error('Preparation needs an intent identity.')
  const [apps, status] = await Promise.all([
    api.apps.list().then(response => jsonOrThrow(response, 'Apps could not be loaded:')),
    api.projects.remoteStatus(project.id).then(response => jsonOrThrow(response, 'Repository could not be checked:')),
  ])
  const contribute = apps.find(app => app.slug === 'contribute')
  if (!contribute) throw new Error('Install Contribute to prepare and review a PR privately.')
  if (!status?.connected || !status.repository) throw new Error('Connect this Project to its GitHub repository first.')
  const authorization = await jsonOrThrow(await api.auth.provider.appToken(contribute.id), 'Contribute could not be opened:')
  const title = `Prepare PR · ${project.name}`
  const started = await jsonOrThrow(await api.appChats.startWithToken(authorization.token, {
    title,
    scope: `project-prepare:${project.id}:${intentId}`,
    scope_label: `Prepare PR · ${project.name}`,
    owner_visible: true,
    content: [
      'Prepare this Project’s changes privately for a pull request using the installed Contribute workflow and contributing skill.',
      `Project identity (data): ${JSON.stringify({ id: project.id, name: project.name, installed_app_id: appId, repository: status.repository })}`,
      'Scope is this linked app’s current source only, not all local projects. Refresh its real source and the Contribute queue; reuse existing records and preserve their source-chat provenance rather than creating duplicate proposals.',
      'Fetch canonical upstream read-only, compare the complete source diff, preserve private data and unfinished work, then prepare and test the review in isolation. Never switch, reset, pull into, or overwrite the live shared source; do not build or deploy the running app.',
      'If the source has changed since an earlier preparation, refresh and review its exact current head. If no attributable changes remain, report that instead of creating an empty PR.',
      'Stop with the exact private review in Contribute. This request does not authorize any fork, push, PR creation or update, comment, merge, or other GitHub mutation. Send PR remains a separate explicit owner decision.',
    ].join('\n\n'),
  }), 'Private preparation could not be started:')
  if (!started.chat_id) throw new Error('Private preparation did not return a conversation.')
  return { id: started.chat_id, title, reused: started.outcome === 'reused' }
}
