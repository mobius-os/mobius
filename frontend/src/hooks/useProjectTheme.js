/* Read the effective theme through the same project-scoped permission as source. */
import { useQuery } from '@tanstack/react-query'
import { themeQueries } from './queries.js'
import { api, apiFetch, jsonOrThrow } from '../api/client.js'

export default function useProjectTheme(projectId, enabled = true) {
  return useQuery({
    queryKey: [...themeQueries.keys.all, 'project', projectId],
    queryFn: async ({ signal }) => jsonOrThrow(
      await api.projects.theme(projectId, { signal }), 'Inherited theme unavailable:',
    ),
    enabled: !!projectId && enabled,
  })
}

// Source inspection is distinct from the effective CSS used by preview frames.
export function useProjectThemeSource(projectId, enabled = true) {
  return useQuery({
    queryKey: [...themeQueries.keys.all, 'project', projectId, 'source'],
    queryFn: async ({ signal }) => jsonOrThrow(
      await apiFetch(`/projects/${encodeURIComponent(projectId)}/theme/source`, { signal }),
      'Theme source unavailable:',
    ),
    enabled: !!projectId && enabled,
  })
}
