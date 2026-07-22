import MarkerCard from './MarkerCard.jsx'

// A synthetic provider-facing "continue" is stored as a user message so the
// next turn and future provider history remain truthful. In the transcript it
// is a product event, though, not text the owner typed. Render the durable
// reason with the same quiet marker language as compaction.
export default function AutoContinuationCard({ msg }) {
  const restarted = msg?.continuation_reason === 'restart'
  const title = restarted
    ? 'Server restarted — continuing automatically'
    : 'Usage available again — continuing automatically'

  return (
    <MarkerCard title={title} icon={
      <svg
        aria-hidden="true"
        viewBox="0 0 16 16"
        width="14"
        height="14"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M13 7a5 5 0 1 0-1.5 4" />
        <path d="M10.5 8H13V5.5" />
      </svg>
    } />
  )
}
