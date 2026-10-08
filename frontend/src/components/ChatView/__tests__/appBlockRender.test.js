import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import AppBlock, { PullSnapshot } from '../markdown/AppBlock.jsx'
import { appBlockFromToken } from '../markdown/appBlock.js'
import { appQueries } from '../../../hooks/queries.js'

function render(value, apps = null) {
  const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify(value) })
  const client = new QueryClient()
  if (apps) client.setQueryData(appQueries.list.key, apps)
  return renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(AppBlock, { block })))
}

test('a PR snapshot renders as a GitHub-style row with colored labels', () => {
  const html = render({ app: 'contribute', intent: 'pull-request:owner/repo#42', title: 'Fix the empty state', inline: false,
    pull: { repo: 'owner/repo', number: 42, state: 'draft', author: 'octocat', files: 4, additions: 94, deletions: 1,
      labels: [{ name: 'bug', color: 'd73a4a' }, { name: 'area: backend', color: '0E8A16' }], url: 'https://github.com/owner/repo/pull/42' } })
  assert.match(html, /md-app-pull__badge is-neutral">Draft</)
  // The title opens the app; the row has no second Open link or state icon.
  assert.doesNotMatch(html, /md-app-pull__open|md-app-pull__state|>Open in/)
  assert.match(html, /class="md-app-pull__title"[^>]*>Fix the empty state</)
  assert.match(html, /--md-label-dark-bg:rgba\(215,58,74,0\.18\)[^"]*">bug</)
  assert.match(html, /href="https:\/\/github\.com\/owner\/repo\/pull\/42"[^>]*>owner\/repo#42</)
  assert.match(html, />Draft</)
  assert.match(html, /4 files <ins>\+94<\/ins> <del>−1<\/del>/)
})

test('a link-only block renders its facts and opens fact links outside the transcript', () => {
  const html = render({ app: 'contribute', intent: 'pull-request:owner/repo#7', title: '#7 Fact block', inline: false,
    facts: [{ label: 'Author', value: 'octocat' }, { label: 'Source', value: 'owner/repo', href: 'https://github.com/owner/repo' }] })
  assert.match(html, /<dt>Author<\/dt><dd>octocat<\/dd>/)
  assert.match(html, /<a href="https:\/\/github\.com\/owner\/repo" target="_blank" rel="noopener noreferrer">owner\/repo<\/a>/)
  assert.doesNotMatch(html, /md-app-block__toggle/)
})

test('an inline block for an app that is not installed keeps its snapshot and offers no view', () => {
  const html = render({ app: 'missing', intent: 'open:item', title: 'Item' }, [{ id: 80, slug: 'contribute', name: 'Contribute' }])
  assert.match(html, /missing is not available\. The saved snapshot remains here\./)
  assert.doesNotMatch(html, /md-app-block__toggle|md-app-block__view/)
})

test('an inline block offers the app-named expand control without mounting the app', () => {
  const block = { app: 'contribute', intent: 'chat-prepared:rec-1', title: 'Fix the thing', expand_label: 'Review and send',
    facts: [{ label: 'Review', value: 'All clear' }] }
  const html = render(block, [{ id: 80, slug: 'contribute', name: 'Contribute' }])
  assert.match(html, /aria-expanded="false"[^>]*>.*Review and send<\/button>/)
  assert.match(html, /<dt>Review<\/dt><dd>All clear<\/dd>/)
  assert.doesNotMatch(html, /md-app-block__view/)
  assert.match(render({ ...block, expand_label: undefined }, [{ id: 80, slug: 'contribute', name: 'Contribute' }]), /Show details here<\/button>/)
})

test('a proposed PR renders as a colored row with its repository linked, badges and the app action', () => {
  const html = render({ app: 'contribute', intent: 'chat-prepared:rec-1', title: 'Fix the empty state',
    action: { label: 'Contribute', intent: 'chat-send:rec-1' }, expand_label: 'Review details',
    pull: { repo: 'owner/repo', state: 'proposed', files: 3, additions: 5, deletions: 1, badges: [{ label: 'All clear', tone: 'success' }] } },
    [{ id: 80, slug: 'contribute', name: 'Contribute' }])
  // Purple is for links: the not-yet-sent state is a neutral pill.
  assert.match(html, /md-app-pull__badge is-neutral">Not sent yet</)
  assert.match(html, /<a class="md-app-pull__repo" href="https:\/\/github\.com\/owner\/repo" target="_blank" rel="noopener noreferrer">owner\/repo<\/a>/)
  assert.match(html, /md-app-pull__badge is-success">All clear</)
  assert.match(html, /class="md-app-block__action"[^>]*>Contribute<\/button>/)
  // A PR row's only inline opener is its action; no details toggle.
  assert.doesNotMatch(html, /Review details|md-app-block__toggle/)
  // Nothing mounts the app until the reader asks.
  assert.doesNotMatch(html, /md-app-block__view/)
})

test('a batch renders every item as its own linked row with one shared action', () => {
  const html = render({ app: 'contribute', intent: 'review:batch', title: 'Ready to contribute',
    action: { label: 'Contribute all', intent: 'chat-send-batch:a,b' },
    items: [
      { title: 'First change', intent: 'review:a', action: { label: 'Contribute', intent: 'chat-send:a' }, pull: { repo: 'owner/repo', state: 'proposed', badges: [{ label: 'All clear', tone: 'success' }] } },
      { title: 'Second change', intent: 'review:b', pull: { repo: 'owner/app', state: 'proposed' } },
    ] }, [{ id: 80, slug: 'contribute', name: 'Contribute' }])
  assert.match(html, /md-app-block--batch/)
  assert.match(html, /intent=review%3Aa"[^>]*>First change</)
  assert.match(html, /intent=review%3Ab"[^>]*>Second change</)
  // One shared action plus each item's own action where it has one.
  assert.equal((html.match(/class="md-app-block__action"/g) || []).length, 2)
  assert.match(html, />Contribute all<\/button>/)
  assert.match(html, />Contribute<\/button>/)
  assert.doesNotMatch(html, /md-app-block__view/)
})

test('inline session waits for its exact read instead of flashing a stale saved action', () => {
  const html = render({ app: 'contribute', intent: 'review:1', title: 'Fix it', interaction: 'inline',
    action: { label: 'Contribute', intent: 'chat-send:1' },
    pull: { repo: 'owner/repo', state: 'proposed' } },
  [{ id: 80, slug: 'contribute', name: 'Contribute' }])
  assert.match(html, /md-app-block__pending/)
  assert.match(html, />Loading…<\/span>/)
  assert.doesNotMatch(html, />Contribute<\/button>/)
  assert.doesNotMatch(html, /md-app-block__view|md-app-block__toggle|md-app-block__session-host/)
})

test('a twelve-record batch keeps its shared control instead of silently falling back to individual actions', () => {
  const ids = Array.from({ length: 12 }, (_, i) => `00000000-0000-4000-8000-${i.toString(16).padStart(12, '0')}`)
  const html = render({ app: 'contribute', intent: `review:${ids[0]}`, title: 'Prepared contributions',
    action: { label: 'Contribute all', intent: `chat-send-batch:${ids.join(',')}` },
    items: ids.map(id => ({ title: id, intent: `review:${id}`,
      action: { label: 'Contribute', intent: `chat-send:${id}` }, pull: { repo: 'owner/repo', state: 'proposed' } })),
  }, [{ id: 80, slug: 'contribute', name: 'Contribute' }])
  assert.equal((html.match(/class="md-app-block__action"/g) || []).length, 13)
  assert.match(html, />Contribute all<\/button>/)
})

test('compact live receipts preserve tags and diff counts but replace obsolete saved badges', () => {
  const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({
    app: 'contribute', intent: 'review:a', title: 'Fix it', interaction: 'inline',
    pull: { repo: 'owner/repo', state: 'open', number: 42, url: 'https://github.com/owner/repo/pull/42', files: 5, additions: 91, deletions: 16,
      labels: [{ name: 'bug', color: 'd73a4a' }], badges: [{ label: '3 linked PRs', tone: 'neutral' }] },
  }) })
  const html = renderToStaticMarkup(createElement(PullSnapshot, { block, pull: block.pull,
    href: block.href, compact: true, session: { status: 'Open', statusTone: 'neutral', badges: [],
      links: [{ label: 'View PR #42', url: 'https://github.com/owner/repo/pull/42' }] } }))
  assert.match(html, /md-app-pull--compact/)
  assert.doesNotMatch(html, /md-app-pull__identity"/)
  assert.match(html, />Open</)
  assert.match(html, />bug</)
  assert.match(html, /href="https:\/\/github\.com\/owner\/repo"[^>]*>owner\/repo</)
  assert.match(html, /href="https:\/\/github\.com\/owner\/repo\/pull\/42"[^>]*>View PR #42</)
  assert.match(html, /5 files <ins>\+91<\/ins> <del>−16<\/del>/)
  assert.match(html, /View PR #42/)
  assert.doesNotMatch(html, /3 linked PRs|Review details|Not sent yet/)
})
