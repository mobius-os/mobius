/* Derive a safe, generic app-preview card from a standalone internal Markdown link. */

const ITEM_ID_RE = /^[A-Za-z0-9_-]{1,64}$/
const CARD_TYPES = Object.freeze({
  artifacts: { kindKey: 'artifact', appName: 'Artifacts', kind: 'Artifact' },
  mapbook: { kindKey: 'map', appName: 'Maps', kind: 'Saved map' },
  maps: { kindKey: 'map', appName: 'Maps', kind: 'Saved map' },
})

function inlineText(tokens) {
  return (tokens || []).map((token) => {
    if (typeof token?.text === 'string') return token.text
    return inlineText(token?.tokens)
  }).join('')
}

function previewTitle(value) {
  const text = String(value || '').trim().replace(/\s*→\s*$/, '')
  const quoted = /^Open\s+["“](.+)["”]$/i.exec(text)
  if (quoted) return quoted[1].trim()
  return text.replace(/^Open\s+/i, '').trim() || 'Open app item'
}

export function appLinkCardFromParagraph(token, base = 'https://mobius.local') {
  if (token?.type !== 'paragraph') return null
  const meaningful = (token.tokens || []).filter((child) => (
    child?.type !== 'text' || String(child.text || '').trim()
  ))
  if (meaningful.length !== 1 || meaningful[0]?.type !== 'link') return null

  let url
  let root
  try {
    root = new URL(base)
    url = new URL(meaningful[0].href, root)
  } catch {
    return null
  }
  if (url.origin !== root.origin || !/^\/shell\/?$/.test(url.pathname)) return null

  const app = String(url.searchParams.get('app') || '')
  const intent = String(url.searchParams.get('intent') || '')
  const type = CARD_TYPES[app]
  if (!type || !/^[a-z][a-z0-9-]*:[^\s]{1,256}$/.test(intent)) return null

  const kindKey = intent.slice(0, intent.indexOf(':'))
  const itemId = intent.slice(intent.indexOf(':') + 1)
  if (kindKey !== type.kindKey || !ITEM_ID_RE.test(itemId)) return null
  return {
    href: `${url.pathname}${url.search}`,
    app,
    appName: type.appName,
    intent,
    itemId,
    kindKey,
    kind: type.kind,
    title: previewTitle(inlineText(meaningful[0].tokens)),
    iconSrc: `/apps/${encodeURIComponent(app)}/icon-192.png`,
  }
}
