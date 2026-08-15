/* ChatWorkingIndicator is the shared, accessible in-progress mark for chat titles. */
import './ChatWorkingIndicator.css'

export default function ChatWorkingIndicator({ className = '' }) {
  return (
    <span
      className={`chat-working-indicator${className ? ` ${className}` : ''}`}
      role="img"
      aria-label="Möbius is working"
      title="Möbius is working…"
    />
  )
}
