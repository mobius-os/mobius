/* GitHub collaboration shares an existing published project, never local files. */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, jsonOrThrow } from '../../api/client.js'
import { githubCollaborationLink } from '../../lib/projectGithubSharing.js'

export default function ProjectGithubSharing({ project, onOpenGithub }) {
  const [copied, setCopied] = useState(false)
  const [copyError, setCopyError] = useState('')
  const statusQuery = useQuery({
    queryKey: ['projects', 'git', project.id, 'remote'],
    queryFn: async ({ signal }) => jsonOrThrow(await api.projects.remoteStatus(project.id, { signal }), 'Could not check GitHub:'),
  })
  const status = statusQuery.data
  const link = githubCollaborationLink(status)
  async function copyLink() {
    setCopied(false); setCopyError('')
    try { await navigator.clipboard.writeText(link); setCopied(true) }
    catch { setCopyError('Select the link below and copy it manually.') }
  }
  return <section className="project-sharing__github" aria-label="GitHub collaboration">
    <p>Separate versions, reviewed changes. GitHub accounts needed.</p>
    {statusQuery.isLoading ? <p role="status">Checking the GitHub connection…</p>
      : statusQuery.isError ? <><p role="alert">Could not check the GitHub connection.</p><button type="button" onClick={() => statusQuery.refetch()}>Try again</button></>
        : !link ? <>
          <p>Publish this project on GitHub first.</p>
          <button type="button" onClick={onOpenGithub}>Set up GitHub sharing</button>
        </> : <>
          <p>Only files already on GitHub are shared.</p>
          {(status.dirty || status.ahead > 0) && <p role="status">Some local changes aren’t published yet.</p>}
          <label>GitHub collaboration link<input readOnly value={link} onFocus={event => event.currentTarget.select()} /></label>
          <button type="button" onClick={copyLink}>{copied ? 'Link copied' : 'Copy GitHub link'}</button>
          {copyError && <p role="alert">{copyError}</p>}
          <details><summary>How it works</summary><p>Private projects require access and permission to fork.</p>
            <ol><li>They make their own version on GitHub, called a fork.</li><li>In their Möbius, they open Projects → New project → Import from GitHub and bring in that version.</li><li>They can send their changes back through GitHub for you to review. Your project stays unchanged until you accept them.</li></ol>
            <a href="https://docs.github.com/en/pull-requests/how-tos/work-with-forks/fork-a-repo" target="_blank" rel="noopener noreferrer">GitHub’s guide to working this way</a>
          </details>
          <button type="button" onClick={onOpenGithub}>Review publishing</button>
        </>}
  </section>
}
