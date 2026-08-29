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
  chatChangesOverview,
} from './chatChangesLifecycle.js'
import {
  loadChatDiffEntries,
  mergeChatDiffEntries,
} from './chatDiffs.js'

export function useChatContributions(chatId, { enabled = true } = {}) {
  const appsQuery = appQueries.list.useQuery()
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
  const overview = useMemo(
    () => chatChangesOverview(entries, contributions.data),
    [entries, contributions.data],
  )
  const lifecycleAvailable = !contributions.isLoading
    && !contributions.isError
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
    loading: diffs.isLoading || contributions.isLoading,
    error: diffs.isError || contributions.isError,
  }
}
