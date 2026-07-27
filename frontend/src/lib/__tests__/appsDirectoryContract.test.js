import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const src = resolve(here, '../..')
const drawer = readFileSync(resolve(src, 'components/Drawer/Drawer.jsx'), 'utf8')
const directory = readFileSync(resolve(src, 'components/Drawer/AppsDirectory.jsx'), 'utf8')
const css = readFileSync(resolve(src, 'components/Drawer/AppsDirectory.css'), 'utf8')
const shell = readFileSync(resolve(src, 'components/Shell/Shell.jsx'), 'utf8')
const tabModel = readFileSync(resolve(src, 'components/Shell/tabModel.js'), 'utf8')
const navigationIcons = readFileSync(resolve(src, 'components/navigationIcons.js'), 'utf8')

test('Apps is a single drawer destination and the old full app list is gone', () => {
  assert.match(drawer, /className=\{`drawer__item drawer__item--apps/)
  assert.match(drawer, /<span className="drawer__item-text">Apps<\/span>/)
  assert.doesNotMatch(drawer, /drawer__group--apps/)
  assert.match(drawer, /pinnedItems\.map\(\(\{ kind, item \}\)/)
})

test('the directory preserves app management on every card', () => {
  assert.match(drawer, /variant="card"/)
  assert.match(drawer, /<DrawerItemMenu[\s\S]*?surface=\{surface\}/)
  for (const action of ['Install to home screen', 'Delete data', 'Rename']) {
    assert.match(drawer, new RegExp(action))
  }
})

test('phone and web share one searchable launcher tab', () => {
  assert.doesNotMatch(directory, /Back to navigation/)
  assert.match(directory, /aria-label="Search installed apps"/)
  assert.match(directory, /matchMedia\?\.\('\(pointer: fine\)'\)/)
  assert.match(tabModel, /APPS_TAB_KEY = 'apps:apps'/)
  assert.match(shell, /const APPS_KEY = tabModel\.APPS_TAB_KEY/)
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?grid-template-columns: repeat\(4/)
  assert.match(drawer, /onContextMenu=\{openCardMenu\}/)
  assert.match(drawer, /setTimeout\(\(\) => \{[\s\S]*?toggleMenu[\s\S]*?520\)/)
  assert.match(
    drawer,
    /triggerClassName="drawer__more apps-directory__card-menu-anchor"\s+triggerHidden/,
    'the invisible menu anchor must not add a ghost keyboard focus stop',
  )
  assert.match(shell, /const navigationSurfaceOpen = modalDrawerOpen/)
})

test('expanded and collapsed navigation share one ChatGPT SDK icon vocabulary', () => {
  for (const icon of ['ComposeEditSquare', 'Grid', 'SettingsSlider']) {
    assert.match(navigationIcons, new RegExp(icon))
  }
  assert.match(drawer, /from '\.\.\/navigationIcons\.js'/)
  assert.match(shell, /from '\.\.\/navigationIcons\.js'/)
})
