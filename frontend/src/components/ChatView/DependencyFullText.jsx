/* DependencyFullText reads a dependency's claim texts whole when a view showed them in part. */
import { api, jsonOrThrow } from '../../api/client.js'
import PeerNoteBody, { FullNoteText, ReadFullAction } from './PeerNoteBody.jsx'
import useFullPeerNote from './hooks/useFullPeerNote.js'

async function readDependencyTexts(dependencyId) {
  return jsonOrThrow(await api.chatWaits.dependencyText(dependencyId), 'Could not load the full text')
}

/** The condition and outcome, each under its own label, as one plain text. */
export function dependencyFullText(texts) {
  return [
    texts?.condition && `Shared work:\n${texts.condition}`,
    texts?.outcome && `Outcome:\n${texts.outcome}`,
  ].filter(Boolean).join('\n\n')
}

/** Both texts whole below a receipt that showed them in part. */
export default function DependencyFullText({ dependencyId }) {
  const readers = [async () => dependencyFullText(await readDependencyTexts(dependencyId))]
  return <PeerNoteBody readers={readers} truncated noun="text"
    render={text => <span className="peer-note-full">{text}</span>} />
}

/** A Goal row's condition and outcome, each replaced in place by its whole
 *  text once read. */
export function DependencyRowTexts({ dependency, actions, meta }) {
  const texts = useFullPeerNote(dependency.truncated ? [() => readDependencyTexts(dependency.id)] : [])
  const full = texts.full
  return <>
    <span className="chat__goal-task-title">{full?.condition ? <FullNoteText text={full.condition} /> : dependency.condition}</span>
    {actions}
    <span className="chat__goal-task-meta">{full?.outcome ? <FullNoteText text={`Outcome: ${full.outcome}`} /> : meta}</span>
    {dependency.truncated && !full && <ReadFullAction note={texts} noun="text" />}
  </>
}
