import test from 'node:test'
import assert from 'node:assert/strict'
import { appLinkCardFromParagraph } from '../markdown/appLinkCard.js'

const paragraph = (href, text = 'Open "Where to eat" →') => ({
  type: 'paragraph',
  tokens: [{ type: 'link', href, tokens: [{ type: 'text', text }] }],
})

test('internal artifact and map links become app preview cards', () => {
  assert.deepEqual(
    appLinkCardFromParagraph(
      paragraph('/shell/?app=mapbook&intent=map:city-restaurants'),
    ),
    {
      href: '/shell/?app=mapbook&intent=map:city-restaurants',
      app: 'mapbook',
      appName: 'Maps',
      intent: 'map:city-restaurants',
      itemId: 'city-restaurants',
      kindKey: 'map',
      kind: 'Saved map',
      title: 'Where to eat',
      iconSrc: '/apps/mapbook/icon-192.png',
    },
  )
  assert.equal(
    appLinkCardFromParagraph(
      paragraph('/shell/?app=artifacts&intent=artifact:tip-calculator', 'Open "Tip Calculator" →'),
    )?.kind,
    'Artifact',
  )
  assert.equal(
    appLinkCardFromParagraph(paragraph('/shell/?app=maps&intent=map:future-native-map'))?.appName,
    'Maps',
  )
})

test('ordinary, mixed, external, and malformed links stay ordinary markdown', () => {
  assert.equal(appLinkCardFromParagraph(paragraph('https://example.com/shell/?app=mapbook&intent=map:x')), null)
  assert.equal(appLinkCardFromParagraph(paragraph('/shell/?app=mapbook')), null)
  assert.equal(appLinkCardFromParagraph(paragraph('/shell/?app=mapbook&intent=artifact:x')), null)
  assert.equal(appLinkCardFromParagraph(paragraph('/shell/?app=artifacts&intent=artifact:../private')), null)
  assert.equal(appLinkCardFromParagraph(paragraph('/shell/?app=notes&intent=artifact:x')), null)
  assert.equal(appLinkCardFromParagraph({
    type: 'paragraph',
    tokens: [
      { type: 'text', text: 'See ' },
      { type: 'link', href: '/shell/?app=mapbook&intent=map:x', tokens: [{ type: 'text', text: 'map' }] },
    ],
  }), null)
})
