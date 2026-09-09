/* A public copy link explains independent ownership before sending the recipient to their Möbius. */
import { useEffect, useState } from 'react'
import { copyByteLabel, projectCopyDestination, readPublicProjectCopy } from '../../lib/projectCopies.js'
import './ProjectCopy.css'

export default function ProjectCopyPage() {
  const [copy, setCopy] = useState(null)
  const [error, setError] = useState('')
  const [address, setAddress] = useState('')
  const [addressError, setAddressError] = useState('')
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    const splash = document.getElementById('splash')
    if (splash) splash.remove()
    const token = window.location.hash.slice(1)
    setError('')
    if (!token) setError('This link is incomplete. Ask the sender to copy the full link again.')
    else readPublicProjectCopy(token, controller.signal).then(setCopy).catch(cause => { if (cause.name !== 'AbortError') setError(cause.message) })
    return () => controller.abort()
  }, [attempt])

  function continueToMobius(event) {
    event.preventDefault()
    try { window.location.assign(projectCopyDestination(address, window.location.href)) }
    catch (cause) { setAddressError(cause.message) }
  }
  return <main className="project-copy-page"><article className="project-copy">
    <h1>{copy ? `Make ${copy.name} your own` : 'A project to make your own'}</h1>
    {!copy && !error && <p role="status">Checking the copy link…</p>}
    {error && <div role="alert"><p className="project-copy__error">{error}</p><button onClick={() => setAttempt(value => value + 1)}>Check again</button></div>}
    {copy && <>
      <p>Save an editable copy in your own Möbius. Change it however you like—the original project won’t change, and you won’t receive its later updates.</p>
      <p>No GitHub account needed.</p>
      <details><summary>{copy.files.length} project files · {copyByteLabel(copy.total_bytes)}</summary><div className="project-copy__files">{copy.files.map(file => <div key={file.path}><span>{file.path}</span><small>{copyByteLabel(file.size)}</small></div>)}</div></details>
      <section><h2>Where is your Möbius?</h2><p>Enter the address you normally use to open your own Möbius. You’ll review this copy there before saving anything.</p>
        <form onSubmit={continueToMobius}><label htmlFor="project-copy-destination">Your Möbius address</label><input id="project-copy-destination" type="url" inputMode="url" autoComplete="url" required placeholder="https://your-mobius.example" value={address} onChange={event => { setAddress(event.target.value); setAddressError('') }} />
          {addressError && <p className="project-copy__error" role="alert">{addressError}</p>}
          <button className="project-copy__primary" type="submit">Continue to my Möbius</button>
        </form>
      </section>
      <p className="project-copy__boundary">Only save projects from someone you trust. Nothing is installed or run by opening this link. This is a separate copy, not an invitation to work on the original.</p>
    </>}
  </article></main>
}
