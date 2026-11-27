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

/** A Goal row keeps exact claim texts readable behind its authored summary. */
export function DependencyRowTexts({ dependency, actions, meta }) {
  const texts = useFullPeerNote(dependency.truncated ? [() => readDependencyTexts(dependency.id)] : [])
  const full = texts.full
  const report = dependency.target_kind === 'claim_acquisition' && dependency.disposition
    ? dependency.outcome?.work_report : null
  const evidence = full?.outcome || dependency.outcome?.outcome
  return <>
    <span className="chat__goal-task-title">{report?.purpose || (full?.condition ? <FullNoteText text={full.condition} /> : dependency.condition)}</span>
    {actions}
    <span className="chat__goal-task-meta">{meta}</span>
    {report && <span className="chat__goal-task-meta">{report.result}{report.next_step && ` Next: ${report.next_step}`}</span>}
    {(report || evidence) && <details className="chat__goal-task-meta">
      <summary>Technical handoff</summary>
      {report && <><strong>Original work</strong><br /><FullNoteText text={full?.condition || dependency.condition} /></>}
      {evidence && <>{report && <br />}<strong>Outcome</strong><br /><FullNoteText text={evidence} /></>}
    </details>}
    {dependency.truncated && !full && <ReadFullAction note={texts} noun="text" />}
  </>
}
