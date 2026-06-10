import './MenuButton.css'

export default function MenuButton({ onClick, 'aria-label': ariaLabel, 'aria-expanded': ariaExpanded }) {
  return (
    <button
      className="menu-btn"
      onClick={onClick}
      aria-label={ariaLabel ?? 'Toggle navigation'}
      aria-expanded={ariaExpanded}
    >
      <span className="menu-btn__bar" aria-hidden="true" />
      <span className="menu-btn__bar" aria-hidden="true" />
      <span className="menu-btn__bar" aria-hidden="true" />
    </button>
  )
}
