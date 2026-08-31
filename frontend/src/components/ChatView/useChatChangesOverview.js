/* Shared read-only hook for one chat's recorded edits and contribution lifecycle. */

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { appQueries } from '../../hooks/queries.js'
import {
  contributeApp,
  contributeAppId,
} from './contributionReviewModel.js'
import {
  chatEditPaths,
  chatChangesOverview,
} from './chatChangesLifecycle.js'
import { mergeChatDiffEntries } from './chatDiffs.js'
import {
  chatContributionCoverageQueryOptions,
  chatEditDiffsQueryOptions,
  contributionsForChatQueryOptions,
  contributionsForChatQueryKey,
} from './chatChangesQueries.js'

export function useChatContributions(chatId, { enabled = true } = {}) {
  const appsQuery = appQueries.list.useQuery({ enabled })
  const appId = contributeAppId(appsQuery.data)
  const app = contributeApp(appsQuery.data, appId)
  const queryKey = useMemo(
    () => contributionsForChatQueryKey(appId, chatId),
    [appId, chatId],
  )
  const query = useQuery({
    ...contributionsForChatQueryOptions(appId, chatId),
    queryKey,
    enabled: Boolean(enabled && appId && chatId),
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
  const diffs = useQuery({
    ...chatEditDiffsQueryOptions(chatId),
    enabled: Boolean(enabled && chatId),
  })
  const entries = useMemo(
    () => mergeChatDiffEntries(diffs.data || [], initialEntries),
    [diffs.data, initialEntries],
  )
  const coveragePaths = useMemo(() => chatEditPaths(entries), [entries])
  const coverageRequired = Boolean(contributions.appId && coveragePaths.length > 0)
  const coverage = useQuery({
    ...chatContributionCoverageQueryOptions(
      contributions.appId, chatId, coveragePaths,
    ),
    enabled: Boolean(
      enabled
      && coverageRequired
      && !diffs.isLoading
      && !diffs.isError
      && Array.isArray(contributions.data?.records)
    ),
  })
  const lifecyclePayload = useMemo(() => {
    if (!Array.isArray(contributions.data?.records)) return contributions.data
    return {
      ...contributions.data,
      coverage: Array.isArray(coverage.data?.coverage)
        ? coverage.data.coverage
        : [],
    }
  }, [contributions.data, coverage.data])
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
    coverageQuery: coverage,
    loading: diffs.isLoading || contributions.isLoading
      || (coverageRequired && coverage.isLoading),
    error: diffs.isError || contributions.isError || coverage.isError,
  }
}
