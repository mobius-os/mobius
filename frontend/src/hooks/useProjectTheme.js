/* Read the effective theme through the same project-scoped permission as source. */
import { useQuery } from '@tanstack/react-query'
import { themeQueries } from './queries.js'
import { api, jsonOrThrow } from '../api/client.js'

export default function useProjectTheme(projectId, enabled = true) {
  return useQuery({
    queryKey: [...themeQueries.keys.all, 'project', projectId],
    queryFn: async ({ signal }) => jsonOrThrow(
      await api.projects.theme(projectId, { signal }), 'Inherited theme unavailable:',
    ),
    enabled: !!projectId && enabled,
  })
}
