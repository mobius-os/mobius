/* The inherited theme is a live read-only resource, not another project file. */
import { useState } from 'react'
import useProjectTheme from '../../hooks/useProjectTheme.js'

export default function ProjectThemeResource({ projectId, linkedApp = false }) {
  const [open, setOpen] = useState(false)
  const theme = useProjectTheme(projectId, open)
  return <details className="project-theme-resource" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>Inherited Möbius theme <span>Read-only</span></summary>
    {open && <div className="project-theme-resource__body">
      <p>{linkedApp
        ? 'The running app already inherits this theme. Use its variables in your styles, or override them locally.'
        : 'Use this shared theme, or keep your own design. To inherit it in an HTML preview, add this to the document:'}</p>
      {!linkedApp && <code>{'<meta name="mobius-theme" content="inherit">'}</code>}
      <p>Colours and typography stay linked to Möbius. This resource cannot change the shell’s theme.</p>
      {theme.isLoading && <p role="status">Loading theme…</p>}
      {theme.isError && <div role="alert"><p>{theme.error.message}</p><button type="button" onClick={() => theme.refetch()}>Retry</button></div>}
      {theme.data && <>
        <small>Current appearance: {theme.data.mode}</small>
        <pre tabIndex={0} aria-label="Read-only inherited theme CSS"><code>{theme.data.css}</code></pre>
        <p>Example: <code>color: var(--text); background: var(--bg); font-family: var(--font);</code></p>
      </>}
    </div>}
  </details>
}
