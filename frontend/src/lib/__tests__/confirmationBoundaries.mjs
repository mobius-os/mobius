// External query boundary only; confirmation state, handlers and JSX are real.
export const notificationQueries = {
  list: {
    useQuery: () => ({
      data: { pages: [[{
        id: 'notice-1', title: 'Saved notice', body: 'History to clear',
        source_type: 'agent', sent_at: '2026-10-21T10:00:00Z', read_at: null,
      }]] },
      isLoading: false,
      isError: false,
      hasNextPage: false,
    }),
  },
}

export const useQueryClient = () => ({
  setQueryData() { throw new Error('Unexpected query write in confirmation test') },
})
