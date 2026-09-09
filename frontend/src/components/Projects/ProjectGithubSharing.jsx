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
    <p>Work on separate versions, then suggest changes for each other to review. Everyone needs a GitHub account.</p>
    {statusQuery.isLoading ? <p role="status">Checking the GitHub connection…</p>
      : statusQuery.isError ? <><p role="alert">Could not check the GitHub connection.</p><button type="button" onClick={() => statusQuery.refetch()}>Try again</button></>
        : !link ? <>
          <p>Connect this project to GitHub and publish the files you want to share first. Nothing will be uploaded just by opening these settings.</p>
          <button type="button" onClick={onOpenGithub}>Set up GitHub sharing</button>
        </> : <>
          <p>This link lets people make their own version on GitHub. It shares only what’s already on GitHub—not unpublished changes here.</p>
          {(status.dirty || status.ahead > 0) && <p role="status">You have changes here that may not be on GitHub yet. Review publishing if you want to include them.</p>}
          <label>GitHub collaboration link<input readOnly value={link} onFocus={event => event.currentTarget.select()} /></label>
          <button type="button" onClick={copyLink}>{copied ? 'Link copied' : 'Copy GitHub link'}</button>
          {copied && <p role="status">Ready to send.</p>}
          {copyError && <p role="alert">{copyError}</p>}
          <p>For a private GitHub project, people also need access and permission to make their own version.</p>
          <details><summary>What happens when someone opens the link?</summary>
            <ol><li>They make their own version on GitHub, called a fork.</li><li>In their Möbius, they open Projects → New project → Import from GitHub and bring in that version.</li><li>They can send their changes back through GitHub for you to review. Your project stays unchanged until you accept them.</li></ol>
            <a href="https://docs.github.com/en/pull-requests/how-tos/work-with-forks/fork-a-repo" target="_blank" rel="noopener noreferrer">GitHub’s guide to working this way</a>
          </details>
          <button type="button" onClick={onOpenGithub}>Review publishing</button>
        </>}
  </section>
}
