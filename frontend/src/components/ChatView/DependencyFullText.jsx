/* DependencyFullText reads a dependency's claim texts whole when a view showed them in part. */
import { api, jsonOrThrow } from '../../api/client.js'
import PeerNoteBody from './PeerNoteBody.jsx'

/** The condition and outcome, each under its own label, as one plain text. */
export function dependencyFullText(texts) {
  return [
    texts?.condition && `Shared work:\n${texts.condition}`,
    texts?.outcome && `Outcome:\n${texts.outcome}`,
  ].filter(Boolean).join('\n\n')
}

export default function DependencyFullText({ dependencyId }) {
  const readers = [async () => dependencyFullText(
    await jsonOrThrow(await api.chatWaits.dependencyText(dependencyId), 'Shared work failed:'),
  )]
  return <PeerNoteBody readers={readers} truncated noun="text"
    render={text => <span className="peer-note-full">{text}</span>} />
}
