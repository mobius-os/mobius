/* Query ownership for one chat's edits and contribution lifecycle. */

import { api, apiFetch } from '../../api/client.js'
import { chatChangesOverview, chatEditPaths } from './chatChangesLifecycle.js'
import { loadChatDiffEntries, mergeChatDiffEntries } from './chatDiffs.js'
import { contributeAppId, reviewActionKey } from './contributionReviewModel.js'

export function contributionsForChatQueryKey(appId, chatId) {
  return ['contributions-for-chat', appId, chatId]
}

export function chatEditDiffsQueryKey(chatId) {
  return ['chat-edit-diffs', String(chatId || '')]
}

export function chatContributionCoverageQueryKey(appId, chatId, paths) {
  return ['chat-contribution-coverage', appId, String(chatId || ''), paths]
}

const COVERAGE_BATCH_SIZE = 100

export async function loadChatContributionCoverage(
  appId, chatId, paths, { request = api.contributions.coverageForChat } = {},
) {
  const batches = []
  for (let index = 0; index < paths.length; index += COVERAGE_BATCH_SIZE) {
    batches.push(paths.slice(index, index + COVERAGE_BATCH_SIZE))
  }
  const payloads = await Promise.all(batches.map(async batch => {
    const response = await request(appId, chatId, batch)
    if (!response.ok) throw new Error(`Request failed (${response.status})`)
    return response.json()
  }))
  return {
    coverage: payloads.flatMap(payload => (
      Array.isArray(payload?.coverage) ? payload.coverage : []
    )),
  }
}

export function chatContributionCoverageQueryOptions(appId, chatId, paths) {
  return {
    queryKey: chatContributionCoverageQueryKey(appId, chatId, paths),
    queryFn: () => loadChatContributionCoverage(appId, chatId, paths),
    staleTime: 15000,
    retry: false,
  }
}

export function contributionsForChatQueryOptions(appId, chatId) {
  return {
    queryKey: contributionsForChatQueryKey(appId, chatId),
    queryFn: () => api.contributions.forChat(appId, chatId)
      .then(response => (response.ok ? response.json() : null)),
    // Attached contribution work is durable and does not run this source
    // chat. Poll only while its child is live so the chat card and Changes
    // become the progress surface without spending another provider turn.
    refetchInterval: query => {
      const data = query.state.data
      const workActive = [
        'accepted', 'retrying', 'starting', 'running', 'resuming', 'paused',
      ].includes(String(data?.work?.status || ''))
      const publicationPending = [
        ...(data?.records || []),
        ...(data?.stack_units || []).flatMap(unit => unit?.records || []),
      ].some(record => record?.status === 'submitting')
      return workActive || publicationPending ? 1800 : false
    },
    staleTime: 15000,
    retry: false,
  }
}

export function chatEditDiffsQueryOptions(chatId) {
  return {
    queryKey: chatEditDiffsQueryKey(chatId),
    queryFn: ({ signal } = {}) => loadChatDiffEntries(
      chatId,
      { request: apiFetch, signal },
    ),
    staleTime: 15000,
    retry: false,
  }
}

export function invalidateChatChangesQueries(queryClient, chatId) {
  if (!queryClient || !chatId) return Promise.resolve([])
  const targetChatId = String(chatId)
  return Promise.all([
    queryClient.invalidateQueries({
      predicate: query => (
        query?.queryKey?.[0] === 'contributions-for-chat'
        && String(query.queryKey[2] || '') === targetChatId
      ),
    }),
    queryClient.invalidateQueries({
      queryKey: chatEditDiffsQueryKey(chatId),
      exact: true,
    }),
    queryClient.invalidateQueries({
      predicate: query => (
        query?.queryKey?.[0] === 'chat-contribution-coverage'
        && String(query.queryKey[2] || '') === targetChatId
      ),
    }),
  ])
}

export async function refreshChatChangesOverview({
  queryClient,
  apps,
  appId: suppliedAppId = null,
  chatId,
  initialEntries = [],
}) {
  const numericAppId = Number(suppliedAppId)
  const appId = Number.isInteger(numericAppId) && numericAppId > 0
    ? numericAppId
    : contributeAppId(apps)
  if (!queryClient || !appId || !chatId) return null
  try {
    const [contributions, entries] = await Promise.all([
      queryClient.fetchQuery({
        ...contributionsForChatQueryOptions(appId, chatId),
        staleTime: 0,
      }),
      queryClient.fetchQuery({
        ...chatEditDiffsQueryOptions(chatId),
        staleTime: 0,
      }),
    ])
    if (!contributions) return null
    const mergedEntries = mergeChatDiffEntries(entries || [], initialEntries)
    const paths = chatEditPaths(mergedEntries)
    const coverage = paths.length > 0
      ? await queryClient.fetchQuery({
        ...chatContributionCoverageQueryOptions(appId, chatId, paths),
        staleTime: 0,
      })
      : { coverage: [] }
    return {
      ...chatChangesOverview(
        mergedEntries,
        { ...contributions, coverage: coverage.coverage },
      ),
      contributions,
    }
  } catch {
    return null
  }
}

export function chatChangesActionIsCurrent(overview, action) {
  if (!overview || !action) return false
  const revision = String(action.revision || '')
  if (action.kind === 'unsorted') {
    return overview.counts?.unsorted > 0
      && Boolean(revision)
      && overview.unsortedRevision === revision
  }
  if (action.kind === 'workflow') {
    return overview.needsAction === true
      && Boolean(revision)
      && overview.workflowRevision === revision
  }
  if (action.kind === 'records') {
    const expected = (action.recordKeys || []).filter(Boolean)
    if (expected.length === 0) return false
    const current = new Set(
      (overview.contributions?.records || []).map(reviewActionKey),
    )
    return expected.every(key => current.has(key))
  }
  return false
}
