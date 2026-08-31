/* Shared read-only hook for one chat's recorded edits and contribution lifecycle. */

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, apiFetch } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import {
  contributeApp,
  contributeAppId,
} from './contributionReviewModel.js'
import {
  chatEditPaths,
  chatChangesOverview,
} from './chatChangesLifecycle.js'
import {
  loadChatDiffEntries,
  mergeChatDiffEntries,
} from './chatDiffs.js'

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

export function useChatContributions(chatId, { enabled = true } = {}) {
  const appsQuery = appQueries.list.useQuery({ enabled })
  const appId = contributeAppId(appsQuery.data)
  const app = contributeApp(appsQuery.data, appId)
  const queryKey = useMemo(
    () => ['contributions-for-chat', appId, chatId],
    [appId, chatId],
  )
  const query = useQuery({
    queryKey,
    queryFn: () => api.contributions.forChat(appId, chatId)
      .then(response => (response.ok ? response.json() : null)),
    enabled: Boolean(enabled && appId && chatId),
    staleTime: 15000,
    retry: false,
  })
  return {
    appId,
    app,
    queryKey,
    ...query,
    isLoading: appsQuery.isLoading || query.isLoading,
    isError: appsQuery.isError || query.isError,
  }
}

export function useChatChangesOverview(chatId, initialEntries = [], { enabled = true } = {}) {
  const contributions = useChatContributions(chatId, { enabled })
  const diffsQueryKey = useMemo(
    () => ['chat-edit-diffs', String(chatId || '')],
    [chatId],
  )
  const diffs = useQuery({
    queryKey: diffsQueryKey,
    // This owner route scans the complete persisted transcript. The message
    // window mounted in a cold or long chat is only a live supplement below.
    queryFn: ({ signal }) => loadChatDiffEntries(
      chatId,
      { request: apiFetch, signal },
    ),
    enabled: Boolean(enabled && chatId),
    staleTime: 15000,
    retry: false,
  })
  const entries = useMemo(
    () => mergeChatDiffEntries(diffs.data || [], initialEntries),
    [diffs.data, initialEntries],
  )
  const coveragePaths = useMemo(() => chatEditPaths(entries), [entries])
  const coverageQueryKey = useMemo(
    () => ['chat-contribution-coverage', contributions.appId, chatId, coveragePaths],
    [contributions.appId, chatId, coveragePaths],
  )
  const coverageRequired = Boolean(contributions.appId && coveragePaths.length > 0)
  const coverage = useQuery({
    queryKey: coverageQueryKey,
    queryFn: () => loadChatContributionCoverage(
      contributions.appId, chatId, coveragePaths,
    ),
    enabled: Boolean(
      enabled
      && coverageRequired
      && !diffs.isLoading
      && !diffs.isError
      && Array.isArray(contributions.data?.records)
    ),
    staleTime: 15000,
    retry: false,
  })
  const lifecyclePayload = useMemo(() => {
    if (!Array.isArray(contributions.data?.records)) return contributions.data
    if (coveragePaths.length === 0) {
      return { ...contributions.data, coverage: [] }
    }
    if (!Array.isArray(coverage.data?.coverage)) return contributions.data
    return { ...contributions.data, coverage: coverage.data.coverage }
  }, [contributions.data, coverage.data, coveragePaths.length])
  const overview = useMemo(
    () => chatChangesOverview(entries, lifecyclePayload),
    [entries, lifecyclePayload],
  )
  const coverageAvailable = !coverageRequired
    || (!coverage.isLoading && !coverage.isError
      && Array.isArray(coverage.data?.coverage))
  const lifecycleAvailable = !contributions.isLoading
    && !contributions.isError
    && coverageAvailable
    && (
      !contributions.appId
      || Array.isArray(contributions.data?.records)
    )
  return {
    ...overview,
    lifecycleAvailable,
    contributeApp: contributions.app,
    contributeAppId: contributions.appId,
    contributions: contributions.data,
    contributionsQuery: contributions,
    diffsQuery: diffs,
    diffsQueryKey,
    coverageQuery: coverage,
    coverageQueryKey,
    loading: diffs.isLoading || contributions.isLoading
      || (coverageRequired && coverage.isLoading),
    error: diffs.isError || contributions.isError || coverage.isError,
  }
}
