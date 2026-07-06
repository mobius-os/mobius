export function StreakBar({ streak }) {
  if (!streak || streak < 1) return null
  const flames = Math.min(streak, 5)
  return (
    <div className="rf-streak-bar">
      <span className="rf-streak-badge">
        <span aria-hidden="true" className="rf-streak-flame">🔥</span>
        <strong className="rf-streak-num">{streak}</strong>
        <span className="rf-streak-unit">
          {streak === 1 ? 'morning in a row' : 'mornings in a row'}
        </span>
        <span aria-hidden="true" className="rf-streak-dots">
          {'•'.repeat(flames)}
        </span>
      </span>
    </div>
  )
}
