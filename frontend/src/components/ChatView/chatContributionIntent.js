/* Chat-owned contract for turning recorded edits into private contribution reviews. */

export const CHAT_CONTRIBUTION_PREPARE_PROMPT = [
  'Sort the file changes recorded by this chat into coherent contributions.',
  '',
  'Verify the current source state, then privately prepare every worthwhile contribution for review, grouped by owning project and dependency. Keep personal, experimental, local-only, incoming-only, and duplicate work out. Do not push, publish, or send anything upstream.',
  '',
  'When finished, summarize the prepared contributions and what intentionally stayed local in this chat.',
].join('\n')

export function chatContributionPrepareSubmission() {
  return {
    text: CHAT_CONTRIBUTION_PREPARE_PROMPT,
    options: { attachments: [], preserveComposer: true },
  }
}

export function chatContributionPrepareAction(turnActive = false) {
  return turnActive
    ? {
        label: 'Queue preparation',
        description: 'The agent will sort these into private review cards after the current reply. Nothing is published.',
      }
    : {
        label: 'Prepare contributions',
        description: 'The agent will sort worthwhile changes into individual private review cards. Nothing is published.',
      }
}
