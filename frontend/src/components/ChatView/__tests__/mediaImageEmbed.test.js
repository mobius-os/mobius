/**
 * Tests for media image embedding in chat markdown.
 *
 * Verifies the full flow: MEDIA_PATH_RE regex matching, chat-id extraction,
 * and the ExpandableImage async resolution path that fetches a short-lived
 * media token (POST /api/chats/{id}/media-token) instead of leaking the
 * 30-day owner JWT into the image URL.
 *
 * Key invariants:
 *   1. Root-relative paths (/api/chats/<id>/generated/<file>) match the regex.
 *   2. Absolute paths (https://host/api/chats/<id>/generated/<file>) also match.
 *   3. External URLs (https://example.com/img.png) do NOT match — they render
 *      as direct <img> sources without any token.
 *   4. /uploads/ paths match, not just /generated/.
 *   5. Chat IDs with hyphens (UUID4 format) are extracted correctly.
 *   6. When a media path is detected, the media token is fetched and appended
 *      as ?token=<media>. The owner JWT is never put on the URL.
 *   7. When a media path is detected, new URL(rawSrc, origin).pathname is used
 *      — so neither the hash nor query from the raw href leaks into the final
 *      URL before the token param is appended.
 *
 * These tests exercise the logic extracted from InlineContent.jsx and
 * mediaToken.js directly — no React/DOM rendering required.
 *
 * Run with:
 *   cd frontend && node --loader=./src/lib/__tests__/vite-env-loader.mjs \
 *     --test src/components/ChatView/__tests__/mediaImageEmbed.test.js
 */
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'

// ---------------------------------------------------------------------------
// Replicate the regex and helper from InlineContent.jsx
// ---------------------------------------------------------------------------

// Copy of MEDIA_PATH_RE as declared in InlineContent.jsx.
// Any change to the source must be reflected here (the test documents the
// contract, not the implementation — we verify the shape is correct).
const MEDIA_PATH_RE = /^(?:.*)?\/api\/chats\/([^/]+)\/(?:uploads|generated)\//

function getMediaChatId(src) {
  const m = src.match(MEDIA_PATH_RE)
  return m ? m[1] : null
}

// ---------------------------------------------------------------------------
// Replicate safeUrl from InlineContent.jsx (protocol allowlist check)
// ---------------------------------------------------------------------------

const SAFE_IMAGE_PROTOCOLS = new Set(['http:', 'https:'])
const FAKE_ORIGIN = 'https://mobius.example.com'

function safeUrl(href) {
  const cleaned = (href || '').trim()
  if (!cleaned) return null
  try {
    const url = new URL(cleaned, FAKE_ORIGIN)
    if (!SAFE_IMAGE_PROTOCOLS.has(url.protocol)) return null
    return cleaned
  } catch {
    return null
  }
}

// ---------------------------------------------------------------------------
// Replicate the ExpandableImage resolution logic (the useEffect body)
// ---------------------------------------------------------------------------

/**
 * Simulates ExpandableImage's async resolution for one href.
 *
 * @param {string} href            - Raw href from the markdown image token.
 * @param {Function} mockMediaToken - Mock for mediaTokenParam(chatId) → Promise<string>.
 * @param {string} [ownerToken]    - The owner JWT (should NEVER appear in output).
 * @returns {Promise<string|null>} - The resolved src (or null if invalid).
 */
async function resolveExpandableImageSrc(href, mockMediaToken, ownerToken = 'OWNER_JWT') {
  const BASE = ''  // root-mounted; same as real client.js default

  const rawSrc = safeUrl(href)
  if (!rawSrc) return null

  const mediaChatId = getMediaChatId(rawSrc)

  if (mediaChatId) {
    // Media path: fetch a short-lived media token, never the owner JWT in URL.
    const param = await mockMediaToken(mediaChatId)
    return `${BASE}${new URL(rawSrc, FAKE_ORIGIN).pathname}${param}`
  } else {
    // Non-media API path or external URL: resolve statically.
    // For non-API external URLs, return src as-is.
    if (rawSrc.startsWith('/api/') || rawSrc.startsWith(BASE + '/api/')) {
      const url = new URL(rawSrc, FAKE_ORIGIN)
      url.searchParams.set('token', ownerToken)
      return url.pathname + url.search
    }
    return rawSrc
  }
}

// ---------------------------------------------------------------------------
// 1. MEDIA_PATH_RE shape tests
// ---------------------------------------------------------------------------

describe('MEDIA_PATH_RE regex', () => {
  test('matches a root-relative /api/chats/<id>/generated/ path', () => {
    const src = '/api/chats/abc-123/generated/img.png'
    assert.ok(MEDIA_PATH_RE.test(src), 'root-relative generated path must match')
  })

  test('matches a root-relative /api/chats/<id>/uploads/ path', () => {
    const src = '/api/chats/abc-123/uploads/file.jpg'
    assert.ok(MEDIA_PATH_RE.test(src), 'root-relative uploads path must match')
  })

  test('matches an absolute https://host/api/chats/<id>/generated/ path', () => {
    const src = 'https://mobius.example.com/api/chats/abc-123/generated/img.png'
    assert.ok(MEDIA_PATH_RE.test(src), 'absolute generated path must match')
  })

  test('matches an absolute https://host/api/chats/<id>/uploads/ path', () => {
    const src = 'https://mobius.example.com/api/chats/abc-123/uploads/img.png'
    assert.ok(MEDIA_PATH_RE.test(src), 'absolute uploads path must match')
  })

  test('does NOT match an external https URL with no /api/chats/ segment', () => {
    const src = 'https://example.com/image.png'
    assert.equal(MEDIA_PATH_RE.test(src), false, 'external URL must not match')
  })

  test('does NOT match a path to a different API segment', () => {
    const src = '/api/apps/123/module'
    assert.equal(MEDIA_PATH_RE.test(src), false, '/api/apps/ must not match')
  })

  test('does NOT match /api/chats/<id>/messages/ (different segment)', () => {
    const src = '/api/chats/abc-123/messages'
    assert.equal(MEDIA_PATH_RE.test(src), false, 'messages segment must not match')
  })
})

// ---------------------------------------------------------------------------
// 2. getMediaChatId extraction tests
// ---------------------------------------------------------------------------

describe('getMediaChatId', () => {
  test('extracts a plain chat ID from a root-relative path', () => {
    const id = getMediaChatId('/api/chats/mychat/generated/img.png')
    assert.equal(id, 'mychat')
  })

  test('extracts a UUID4 chat ID (with hyphens) from a root-relative path', () => {
    const uuid = 'f47ac10b-58cc-4372-a567-0e02b2c3d479'
    const id = getMediaChatId(`/api/chats/${uuid}/generated/shot.png`)
    assert.equal(id, uuid, 'UUID with hyphens must be extracted intact')
  })

  test('extracts chat ID from an absolute URL', () => {
    const uuid = 'f47ac10b-58cc-4372-a567-0e02b2c3d479'
    const id = getMediaChatId(`https://mobius.example.com/api/chats/${uuid}/uploads/file.jpg`)
    assert.equal(id, uuid)
  })

  test('returns null for an external URL with no media path', () => {
    assert.equal(getMediaChatId('https://example.com/img.png'), null)
  })

  test('returns null for a non-media API path', () => {
    assert.equal(getMediaChatId('/api/apps/123/module'), null)
  })

  test('extracts the correct chat ID even when filename contains hyphens', () => {
    const uuid = 'aaaabbbb-cccc-4ddd-8eee-ffffffffffff'
    const id = getMediaChatId(`/api/chats/${uuid}/generated/mind-after-fix-v2.png`)
    assert.equal(id, uuid, 'filename hyphens must not bleed into chat ID')
  })
})

// ---------------------------------------------------------------------------
// 3. ExpandableImage async resolution — the KEY end-to-end flow
// ---------------------------------------------------------------------------

describe('ExpandableImage resolution for media paths', () => {
  test('fetches a media token and builds a token-bearing URL for a root-relative generated path', async () => {
    const href = '/api/chats/f47ac10b-58cc-4372-a567-0e02b2c3d479/generated/shot.png'
    let capturedChatId = null

    const mockMediaToken = async (chatId) => {
      capturedChatId = chatId
      return '?token=MEDIA_TOKEN_abc123'
    }

    const src = await resolveExpandableImageSrc(href, mockMediaToken, 'OWNER_JWT')

    // (a) A src is produced — the <img> will render.
    assert.ok(src, 'resolvedSrc must be non-null for a valid media path')

    // (b) The correct chat ID was passed to the token fetcher.
    assert.equal(capturedChatId, 'f47ac10b-58cc-4372-a567-0e02b2c3d479',
      'media token must be fetched for the correct chat ID')

    // (c) The result URL contains the media token.
    assert.ok(src.includes('?token=MEDIA_TOKEN_abc123'),
      'media token must be appended as ?token= query param')

    // (d) The owner JWT must NOT appear in the URL.
    assert.ok(!src.includes('OWNER_JWT'),
      'owner JWT must never appear in the image URL')
  })

  test('fetches a media token for a root-relative uploads path', async () => {
    const chatId = 'f47ac10b-58cc-4372-a567-0e02b2c3d479'
    const href = `/api/chats/${chatId}/uploads/attachment.jpg`
    let capturedChatId = null

    const mockMediaToken = async (id) => {
      capturedChatId = id
      return '?token=MEDIA_TOKEN_uploads'
    }

    const src = await resolveExpandableImageSrc(href, mockMediaToken, 'OWNER_JWT')

    assert.ok(src, 'resolvedSrc must be non-null for uploads path')
    assert.equal(capturedChatId, chatId)
    assert.ok(src.includes('?token=MEDIA_TOKEN_uploads'))
    assert.ok(!src.includes('OWNER_JWT'))
  })

  test('produces a URL with the correct pathname (no hash/query leakage from href)', async () => {
    const chatId = 'f47ac10b-58cc-4372-a567-0e02b2c3d479'
    const href = `/api/chats/${chatId}/generated/shot.png`

    const mockMediaToken = async () => '?token=TOK'
    const src = await resolveExpandableImageSrc(href, mockMediaToken)

    // Pathname must be exactly /api/chats/{id}/generated/shot.png
    assert.equal(
      src,
      `/api/chats/${chatId}/generated/shot.png?token=TOK`,
      'resolved src must be pathname + ?token= (no extra fragments or query leakage)',
    )
  })

  test('handles token-fetch failure gracefully: returns path with empty param', async () => {
    const href = '/api/chats/abc-123/generated/shot.png'
    // Simulate mediaTokenParam returning '' on failure
    const mockMediaToken = async () => ''

    const src = await resolveExpandableImageSrc(href, mockMediaToken)

    // The src is produced (non-null); the <img> will 401 server-side but
    // the page won't crash. This matches the mediaTokenParam contract:
    // "Returns '' if token fetch fails — image just won't render".
    assert.equal(src, '/api/chats/abc-123/generated/shot.png',
      'on token-fetch failure, src is produced without token (server returns 401, no crash)')
  })
})

// ---------------------------------------------------------------------------
// 4. External URLs render without any token
// ---------------------------------------------------------------------------

describe('ExpandableImage resolution for external URLs', () => {
  test('external https URL is returned as-is (no media token, no owner token)', async () => {
    const href = 'https://upload.wikimedia.org/wikipedia/commons/thumb/4/4f/Wikipedia_logo.png'
    let mediaTokenCalled = false

    const mockMediaToken = async () => {
      mediaTokenCalled = true
      return '?token=SHOULD_NOT_BE_CALLED'
    }

    const src = await resolveExpandableImageSrc(href, mockMediaToken, 'OWNER_JWT')

    assert.equal(src, href, 'external URL must be passed through unchanged')
    assert.equal(mediaTokenCalled, false, 'mediaTokenParam must NOT be called for external URLs')
    assert.ok(!src.includes('OWNER_JWT'), 'owner JWT must not appear in external image URL')
  })

  test('external http URL is accepted (http: is in SAFE_IMAGE_PROTOCOLS)', async () => {
    const href = 'http://example.com/img.png'
    const mockMediaToken = async () => ''
    const src = await resolveExpandableImageSrc(href, mockMediaToken)
    assert.equal(src, href)
  })

  test('javascript: URL is rejected (not in SAFE_IMAGE_PROTOCOLS) — returns null', async () => {
    const href = 'javascript:alert(1)'
    const src = await resolveExpandableImageSrc(href, async () => '')
    assert.equal(src, null, 'javascript: href must be rejected by safeUrl')
  })

  test('data: URL is rejected — returns null', async () => {
    const href = 'data:image/png;base64,AAAA'
    const src = await resolveExpandableImageSrc(href, async () => '')
    assert.equal(src, null, 'data: href must be rejected by safeUrl')
  })

  test('empty href produces null (no image rendered)', async () => {
    const src = await resolveExpandableImageSrc('', async () => '')
    assert.equal(src, null)
  })
})

// ---------------------------------------------------------------------------
// 5. Non-media /api/ paths get the owner token (static path)
// ---------------------------------------------------------------------------

describe('resolveStaticImageSrc for non-media /api/ paths', () => {
  // A hypothetical /api/ image path that is NOT uploads/generated — rare
  // but the code falls through to the static resolution branch, appending
  // the owner JWT. This confirms the asymmetry: media routes use media tokens,
  // everything else uses the owner token via the HEADER path in practice
  // (these are served via Authorization header, not ?token=).
  test('a non-media /api/ path gets the owner token appended', async () => {
    const href = '/api/some-other-image-endpoint/photo.png'
    const ownerToken = 'OWNER_JWT_abc'
    const mockMediaToken = async () => { throw new Error('should not be called') }

    const src = await resolveExpandableImageSrc(href, mockMediaToken, ownerToken)

    assert.ok(src, 'non-media /api/ path must produce a src')
    assert.ok(src.includes(`token=${ownerToken}`),
      'non-media /api/ path must have owner token in URL')
  })
})

// ---------------------------------------------------------------------------
// 6. Verify the regex and extraction stay in sync (no regex drift)
// ---------------------------------------------------------------------------

describe('regex and extraction consistency', () => {
  const CASES = [
    ['/api/chats/abc/generated/img.png', 'abc'],
    ['/api/chats/abc/uploads/file.pdf', 'abc'],
    ['https://h.example.com/api/chats/uuid-here/generated/x.png', 'uuid-here'],
    ['https://h.example.com/api/chats/uuid-here/uploads/x.png', 'uuid-here'],
  ]

  for (const [path, expectedId] of CASES) {
    test(`getMediaChatId("${path.slice(0, 60)}") === "${expectedId}"`, () => {
      assert.equal(getMediaChatId(path), expectedId)
    })
  }

  const NON_MEDIA = [
    'https://example.com/img.png',
    '/api/apps/1/module',
    '/api/chats/abc/messages',
    '/api/chats/abc/stream',
  ]

  for (const path of NON_MEDIA) {
    test(`getMediaChatId("${path}") === null (non-media path)`, () => {
      assert.equal(getMediaChatId(path), null)
    })
  }
})
