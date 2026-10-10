/* Saved helper questions are visible to the Goal owner, answered by the direct parent. */
export default function GoalHelperQuestions({ helpers }) {
  const questions = helpers.filter(node => node.status === 'needs_input' && node.question?.text)
  if (!questions.length) return null
  return <section className="chat__goal-questions" aria-label="Helper questions" tabIndex={0}>
    {questions.map(node => <div key={node.id} className="chat__goal-question">
      <strong>{node.title || 'Helper work'} · Needs an answer</strong>
      <p>{node.question.text}</p>
      {!!node.question.options?.length && <ul>
        {node.question.options.map((option, index) => <li key={index}>{option}</li>)}
      </ul>}
      <span className="chat__goal-task-meta">Waiting for its parent’s answer</span>
    </div>)}
  </section>
}
