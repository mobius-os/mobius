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
const drawerCss = readFileSync(resolve(src, 'components/Drawer/Drawer.css'), 'utf8')
const itemActionMenu = readFileSync(
  resolve(src, 'components/Drawer/DrawerItemActionMenu.jsx'),
  'utf8',
)
const shell = readFileSync(resolve(src, 'components/Shell/Shell.jsx'), 'utf8')
const shellCss = readFileSync(resolve(src, 'components/Shell/Shell.css'), 'utf8')
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
  for (const action of ['Install to home screen', 'Share app', 'Delete data', 'Rename']) {
    assert.match(itemActionMenu, new RegExp(action))
  }
})

test('phone and web share one searchable launcher tab', () => {
  assert.doesNotMatch(directory, /Back to navigation/)
  assert.match(directory, /aria-label="Search installed apps"/)
  assert.match(directory, /matchMedia\?\.\('\(pointer: fine\)'\)/)
  assert.match(tabModel, /APPS_TAB_KEY = 'apps:apps'/)
  assert.match(shell, /const APPS_KEY = tabModel\.APPS_TAB_KEY/)
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?grid-template-columns: repeat\(4/)
  assert.equal((drawer.match(/onContextMenu=\{openItemMenu\}/g) || []).length, 2,
    'launcher cards and drawer rows must enter the same context-menu path')
  assert.match(drawer, /DRAWER_HOLD_MS/)
  assert.match(drawer, /PRE_HOLD_MOVE_PX/)
  assert.match(drawer, /setTimeout\(\(\) => \{[\s\S]*?toggleMenu[\s\S]*?DRAWER_HOLD_MS\)/)
  assert.doesNotMatch(drawer, /520/)
  assert.match(drawer, /menuPlacement=\{openMenu/)
  assert.match(itemActionMenu, /placeContextMenu/)
  assert.match(itemActionMenu, /stopImmediatePropagation/)
  assert.match(drawerCss, /@media \(max-width: 720px\)[\s\S]*?drawer__item-action-menu/)
  assert.match(drawerCss, /bottom: max\(12px, env\(safe-area-inset-bottom\)\)/)
  assert.match(shell, /const navigationSurfaceOpen = modalDrawerOpen/)
})

test('chat and app rows share one placed action menu contract', () => {
  assert.match(drawer, /<DrawerItemActionMenu[\s\S]*?itemKind=\{kind\}[\s\S]*?itemName=\{label\}/)
  assert.doesNotMatch(drawer, /@openai\/apps-sdk-ui\/components\/Menu/)
  assert.doesNotMatch(drawer, /triggerHidden|drawer__menu-anchor/)
  assert.match(itemActionMenu, /itemKind === 'chat' \? 'Chat' : 'App'/)
  assert.match(itemActionMenu, /itemKind === 'chat' \? 'chats' : 'apps'/)
  assert.match(itemActionMenu, /<Chat width=\{20\} height=\{20\}/)
  assert.match(itemActionMenu, /itemKind === 'app' && \(/,
    'Delete data must stay app-only')
})

test('desktop productivity density is 90% with explicit pointer-geometry bridges', () => {
  const desktop = shellCss.match(/@media \(min-width: 1024px\) \{[\s\S]*$/)?.[0] || ''
  assert.match(desktop, /:root\s*\{\s*zoom:\s*0\.9;/)
  assert.match(desktop, /\.shell__tab-open\s*\{\s*font-size:\s*13\.5px;/)
  assert.match(shellCss, /\.shell__tab-open\s*\{[\s\S]*?font-size:\s*12\.5px;/,
    'phone keeps the compact tab size without desktop zoom')
  assert.match(shellCss, /pointer interactions bridge client pixels back into layout pixels/)
  assert.match(drawer, /clientDeltaToLocal/)
})

test('the app directory distinguishes loading, errors, and confirmed emptiness', () => {
  assert.match(drawer, /status=\{appsStatus\}/)
  assert.match(directory, /status === 'loading'/)
  assert.match(directory, /status === 'error'/)
  assert.match(directory, /Apps unavailable/)
  assert.match(directory, /onClick=\{onRetry\}/)
  assert.match(directory, /Your apps/)
  assert.match(directory, /Everything installed in this Möbius workspace/)
  assert.match(directory, /No installed apps yet/)
})

test('expanded and collapsed navigation share one ChatGPT SDK icon vocabulary', () => {
  for (const icon of ['ComposeEditSquare', 'Grid', 'SettingsSlider']) {
    assert.match(navigationIcons, new RegExp(icon))
  }
  assert.match(drawer, /from '\.\.\/navigationIcons\.js'/)
  assert.match(shell, /from '\.\.\/navigationIcons\.js'/)
})
