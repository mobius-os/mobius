import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  gallerySwipeTarget,
  groupMarkdownImages,
  projectResolvedGalleryItems,
} from '../markdown/imageGallery.js'

const gallerySource = readFileSync(
  new URL('../markdown/ImageGallery.jsx', import.meta.url),
  'utf8',
)
const lightboxSource = readFileSync(
  new URL('../markdown/ImageLightbox.jsx', import.meta.url),
  'utf8',
)
const markdownCss = readFileSync(
  new URL('../markdown.css', import.meta.url),
  'utf8',
)
const navigationSource = readFileSync(
  new URL('../../../hooks/useNavigation.js', import.meta.url),
  'utf8',
)
const lightboxCss = readFileSync(
  new URL('../lightbox.css', import.meta.url),
  'utf8',
)

const image = (href, text) => ({ type: 'image', href, text })
const paragraph = (...tokens) => ({
  type: 'paragraph',
  tokens,
  raw: tokens.map(token => token.raw || `![${token.text}](${token.href})`).join(' '),
})

test('adjacent image-only paragraphs become one gallery', () => {
  const first = paragraph(image('/one.png', 'One'))
  const second = paragraph(image('/two.png', 'Two'))
  const result = groupMarkdownImages([first, { type: 'space' }, second])

  assert.equal(result.length, 1)
  assert.equal(result[0].type, 'imageGallery')
  assert.deepEqual(
    result[0].images.map(item => item.href),
    ['/one.png', '/two.png'],
  )
})

test('multiple images in one otherwise-whitespace paragraph become a gallery', () => {
  const result = groupMarkdownImages([
    paragraph(
      image('/one.png', 'One'),
      { type: 'text', text: '  ' },
      image('/two.png', 'Two'),
    ),
  ])

  assert.equal(result[0].type, 'imageGallery')
  assert.equal(result[0].images.length, 2)
})

test('a lone image and images mixed with prose retain ordinary paragraphs', () => {
  const single = paragraph(image('/one.png', 'One'))
  const mixed = paragraph(
    { type: 'text', text: 'See ' },
    image('/two.png', 'Two'),
  )
  const result = groupMarkdownImages([single, { type: 'space' }, mixed])

  assert.deepEqual(result, [single, mixed])
})

test('prose ends one gallery run before a later image run', () => {
  const text = paragraph({ type: 'text', text: 'Details' })
  const result = groupMarkdownImages([
    paragraph(image('/one.png', 'One')),
    paragraph(image('/two.png', 'Two')),
    text,
    paragraph(image('/three.png', 'Three')),
    paragraph(image('/four.png', 'Four')),
  ])

  assert.deepEqual(
    result.map(token => token.type),
    ['imageGallery', 'paragraph', 'imageGallery'],
  )
})

test('the rail scrolls freely with native touch and mouse or pen grabbing', () => {
  assert.match(gallerySource, /event\.pointerType === 'touch'\) return/)
  assert.match(gallerySource, /drag\.axis = Math\.abs\(deltaX\)/)
  assert.match(gallerySource, /if \(!drag\.captured\)/)
  assert.match(gallerySource, /drag\.captured = true[\s\S]*setPointerCapture/)
  assert.match(gallerySource, /onLostPointerCapture=\{endPointerDrag\}/)
  assert.match(gallerySource, /clearTimeout\(suppressTimerRef\.current\)/)
  assert.match(gallerySource, /onClickCapture/)
  assert.match(markdownCss, /touch-action:\s*pan-x pan-y/)
  assert.match(markdownCss, /-webkit-overflow-scrolling:\s*touch/)
  assert.doesNotMatch(markdownCss, /scroll-snap-type/)
  assert.doesNotMatch(gallerySource, /md-image-gallery__dots/)
})

test('an image-only assistant reply stretches the gallery instead of collapsing', () => {
  assert.match(
    markdownCss,
    /\.chat__text--assistant:has\(\.md-image-gallery\)\s*\{[^}]*align-self:\s*stretch;[^}]*width:\s*100%;[^}]*\}/s,
  )
})

test('gallery navigation has explicit keyboard and lightbox alternatives', () => {
  assert.match(gallerySource, /event\.key !== 'ArrowLeft'/)
  assert.match(gallerySource, /\[count, syncOverflow\]/)
  assert.match(gallerySource, /aria-label="Previous images"/)
  assert.match(gallerySource, /aria-label="Next images"/)
  assert.match(lightboxSource, /event\.key === 'ArrowLeft'/)
  assert.match(lightboxSource, /event\.key === 'ArrowRight'/)
  assert.match(lightboxSource, /gallerySwipeTarget/)
  assert.match(lightboxSource, /\{index \+ 1\} \/ \{galleryItems\.length\}/)
})

test('lightbox dismissal is owned by the shell Back stack', () => {
  assert.match(gallerySource, /historyDismiss\.open\(\)/)
  assert.match(gallerySource, /onClose=\{historyDismiss\.close\}/)
  assert.match(navigationSource, /pushShellEntry\('dismissible'/)
  assert.match(
    navigationSource,
    /if \(source\?\.kind === 'dismissible'\) \{[\s\S]{0,300}?dismissal\?\.onDismiss\(\)/,
  )
})

test('lightbox navigation keeps the painted image until its replacement decodes', () => {
  assert.match(lightboxSource, /const \[paintedSrc, setPaintedSrc\] = useState\(activeSrc\)/)
  assert.match(lightboxSource, /const imageIsPending = paintedSrc !== activeSrc/)
  assert.match(lightboxSource, /await image\.decode\?\.\(\)/)
  assert.match(lightboxSource, /imgRef\.current !== image/)
  assert.match(lightboxSource, /lightbox-image\$\{hasGallery [^}]+\} lightbox-image--previous/)
  assert.match(lightboxSource, /key=\{paintedSrc\}/)
  assert.match(lightboxSource, /imageIsPending \? ' is-pending' : ''/)
  assert.match(lightboxSource, /if \(nextIndex !== null\) goToIndex\(nextIndex\)/)
  assert.match(lightboxCss, /\.lightbox-image\.is-pending\s*\{[^}]*opacity:\s*0;[^}]*pointer-events:\s*none;/s)
  assert.match(lightboxCss, /\.lightbox-image--previous\s*\{[^}]*pointer-events:\s*none;/s)
  assert.doesNotMatch(lightboxCss, /lightbox-image-in/)
})

test('lightbox fills its actual overlay and dismisses from every backdrop edge', () => {
  assert.match(
    lightboxSource,
    /className="lightbox-overlay" role="presentation" onClick=\{onClose\}/,
    'the full overlay owns backdrop dismissal, including space outside the dialog stage',
  )
  assert.match(
    lightboxCss,
    /\.lightbox-content\s*\{[^}]*position:\s*absolute;[^}]*inset:\s*0;/s,
    'the stage should inherit the fixed overlay bounds instead of recalculating a zoomed viewport',
  )
  assert.match(lightboxCss, /max-width:\s*calc\(100% - 32px\)/)
  assert.match(lightboxCss, /max-height:\s*calc\(100% - 32px\)/)
  assert.doesNotMatch(
    lightboxCss,
    /\.lightbox-(?:content|image)\s*\{[^}]*(?:100vw|100vh|100dvh)/s,
    'the fixed overlay already owns the correct bounds without a second viewport calculation',
  )
})

test('zoomed touch pan keeps its gesture snapshot through a queued render', () => {
  assert.match(
    lightboxSource,
    /const pan = panRef\.current[\s\S]{0,220}const point = \w+\([^)]*pan\.space\)[\s\S]{0,180}setTransform\(\(current\) =>[\s\S]{0,180}point\.x - pan\.x[\s\S]{0,80}point\.y - pan\.y/,
    'lifting a finger may clear panRef before React evaluates the queued state update',
  )
  assert.doesNotMatch(
    lightboxSource,
    /setTransform\(\(current\) =>[\s\S]{0,220}panRef\.current\.[xy]/,
  )
})

test('resolved images follow their URL rather than a streamed array position', () => {
  const sources = new Map([
    ['/one.png', '/resolved/one.png'],
    ['/two.png', '/resolved/two.png'],
  ])
  const projected = projectResolvedGalleryItems([
    image('/new.png', 'New'),
    image('/two.png', 'Two revised'),
  ], sources)

  assert.deepEqual(projected, [
    {
      key: '["/new.png","New"]:0',
      href: '/new.png',
      src: null,
      alt: 'New',
    },
    {
      key: '["/two.png","Two revised"]:0',
      href: '/two.png',
      src: '/resolved/two.png',
      alt: 'Two revised',
    },
  ])
})

test('viewer identities survive insertions and distinguish exact duplicates', () => {
  const before = projectResolvedGalleryItems([
    image('/one.png', 'One'),
    image('/same.png', 'Repeated'),
    image('/same.png', 'Repeated'),
  ])
  const after = projectResolvedGalleryItems([
    image('/new.png', 'New'),
    image('/one.png', 'One'),
    image('/same.png', 'Repeated'),
    image('/same.png', 'Repeated'),
  ])

  assert.equal(after[1].key, before[0].key)
  assert.equal(after[2].key, before[1].key)
  assert.equal(after[3].key, before[2].key)
  assert.notEqual(after[2].key, after[3].key)
})

test('viewer swipes use the latest adjacent-image readiness', () => {
  const pending = [{ src: '/one.png' }, { src: null }]
  const ready = [{ src: '/one.png' }, { src: '/two.png' }]

  assert.equal(gallerySwipeTarget({
    deltaX: -80, deltaY: 4, index: 0, items: pending,
  }), null)
  assert.equal(gallerySwipeTarget({
    deltaX: -80, deltaY: 4, index: 0, items: ready,
  }), 1)
  assert.equal(gallerySwipeTarget({
    deltaX: -20, deltaY: 0, index: 0, items: ready,
  }), null)
  assert.equal(gallerySwipeTarget({
    deltaX: -80, deltaY: 80, index: 0, items: ready,
  }), null)
})

test('the compact strip names the gallery without adding cards or pagination dots', () => {
  assert.match(gallerySource, /md-image-gallery__header/)
  assert.match(gallerySource, />Gallery</)
  assert.match(gallerySource, /\{count\} \{count === 1 \? 'image' : 'images'\}/)
  assert.match(markdownCss, /\.md-image-gallery__header\s*\{/)
  assert.doesNotMatch(gallerySource, /gallery__dots|gallery__dot/)

  const itemRule = markdownCss.match(/\.md-image-gallery__item\s*\{([^}]*)\}/)?.[1] || ''
  assert.doesNotMatch(itemRule, /\bborder\s*:/)
  assert.doesNotMatch(itemRule, /\bbox-shadow\s*:/)
  assert.match(itemRule, /border-radius:\s*10px/)
})
