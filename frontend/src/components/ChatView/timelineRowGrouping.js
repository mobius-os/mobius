// Adjacent out-of-band activity of the same kind shares one quiet summary
// until a reader asks for the individual events. A helper run and a peer-mail
// run remain distinct because their summaries answer different questions.
export function groupTimelineRows(notes = []) {
  const groups = []
  let run = []
  let runType = null
  const flush = () => {
    if (run.length) groups.push(run)
    run = []
    runType = null
  }
  for (const note of notes) {
    const type = note?.type === 'helper_result' || note?.type === 'peer_message'
      ? note.type
      : null
    if (type && (!runType || runType === type)) {
      runType = type
      run.push(note)
    } else {
      flush()
      if (type) {
        runType = type
        run.push(note)
      } else {
        groups.push([note])
      }
    }
  }
  flush()
  return groups
}
