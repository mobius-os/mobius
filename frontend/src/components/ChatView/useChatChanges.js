/* Shared read-only hook for one chat's recorded edits. */

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { chatChanges } from './chatChanges.js'
import { mergeChatDiffEntries } from './chatDiffs.js'
import { chatEditDiffsQueryOptions } from './chatChangesQueries.js'

export function useChatChanges(chatId, initialEntries = [], { enabled = true } = {}) {
  const diffs = useQuery({
    ...chatEditDiffsQueryOptions(chatId),
    enabled: Boolean(enabled && chatId),
  })
  const changes = useMemo(
    () => chatChanges(mergeChatDiffEntries(diffs.data || [], initialEntries)),
    [diffs.data, initialEntries],
  )
  return {
    ...changes,
    loading: diffs.isLoading,
    // The mounted transcript still supplies its own edits when the complete
    // history cannot be read, so an error only qualifies what is shown.
    error: diffs.isError,
  }
}
