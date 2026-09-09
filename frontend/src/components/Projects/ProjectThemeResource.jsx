/* Inspect complete read-only theme sources in the Project's file viewing pane. */
import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, ChevronRight } from '@openai/apps-sdk-ui/components/Icon'
import { useProjectThemeSource } from '../../hooks/useProjectTheme.js'
import themeDefinitionSource from '../../theme.js?raw'

export default function ProjectThemeResource({ onOpen }) {
  return <button type="button" className="project-theme-resource" onClick={onOpen}>
    <span>Inherited Möbius theme <small>Read-only</small></span>
    <ChevronRight width={16} height={16} aria-hidden="true" />
  </button>
}

export function ProjectThemeSourceView({ projectId, linkedApp = false, onClose }) {
  const backRef = useRef(null)
  useEffect(() => { backRef.current?.focus() }, [])
  const [selectedName, setSelectedName] = useState('theme.js')
  const theme = useProjectThemeSource(projectId)
  const files = [
    { name: 'theme.js', content: themeDefinitionSource },
    ...(theme.data?.files || []),
  ]
  const selected = files.find(file => file.name === selectedName) || files[0]
  return <section className="project-theme-source" aria-label="Inherited Möbius theme source">
    <header className="project-theme-source__toolbar">
      <button ref={backRef} type="button" onClick={onClose}><ArrowLeft width={16} height={16} aria-hidden="true" /> Back to files</button>
      <label className="project-theme-resource__files">Source file
        <select value={selected.name} onChange={event => setSelectedName(event.target.value)}>
          {files.map(file => <option key={file.name} value={file.name}>{file.name}</option>)}
        </select>
      </label>
      <span>Read-only</span>
    </header>
    <div className="project-theme-source__body">
      <p>{linkedApp
        ? 'The app inherits these styles. Use the variables in your code or override them locally.'
        : 'To inherit these styles in an HTML preview, add:'}</p>
      {!linkedApp && <code>{'<meta name="mobius-theme" content="inherit">'}</code>}
      <p>{selected.name === 'theme.js'
        ? 'Complete light and dark palettes and the helpers that build the theme.'
        : selected.name === 'theme.css'
          ? 'Saved stylesheet, exactly as written. Changing appearance rewrites this file; both palettes remain in theme.js.'
          : 'Built-in CSS used when no saved stylesheet is present.'}</p>
      {theme.isLoading && <p role="status">Loading saved stylesheet…</p>}
      {theme.isError && <div role="alert"><p>Saved stylesheet unavailable: {theme.error.message}</p><button type="button" onClick={() => theme.refetch()}>Retry</button></div>}
      <pre tabIndex={0} aria-label={`Read-only ${selected.name} source`}><code>{selected.content}</code></pre>
    </div>
  </section>
}
