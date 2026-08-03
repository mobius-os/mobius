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
const workspaceChrome = readFileSync(
  resolve(src, 'components/Shell/WorkspaceChrome.jsx'),
  'utf8',
)
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
  const dragImports = drawer.match(
    /import \{([\s\S]*?)\} from '\.\.\/Shell\/dragController\.js'/,
  )?.[1] || ''
  assert.match(dragImports, /DRAWER_MENU_HOLD_MS/)
  assert.match(dragImports, /PRE_HOLD_MOVE_PX/)
  assert.match(drawer, /setTimeout\(\(\) => \{[\s\S]*?openItemMenuAt[\s\S]*?DRAWER_MENU_HOLD_MS\)/)
  assert.doesNotMatch(drawer, /520/)
  assert.match(drawer, /menuPlacement=\{openMenu/)
  assert.match(itemActionMenu, /placeContextMenu/)
  assert.match(itemActionMenu, /stopImmediatePropagation/)
  const phoneMenu = drawerCss.match(
    /@media \(max-width: 720px\) \{[\s\S]*?\n\}/,
  )?.[0] || ''
  assert.match(phoneMenu, /drawer__item-action-menu[\s\S]*?width:\s*224px/)
  assert.doesNotMatch(phoneMenu, /\n\s*bottom:|backdrop-filter|drawer-item-sheet-in/)
  assert.match(shell, /const navigationSurfaceOpen = modalDrawerOpen/)
})

test('chat and app rows share one placed action menu contract', () => {
  assert.match(drawer, /<DrawerItemActionMenu[\s\S]*?itemKind=\{kind\}[\s\S]*?itemName=\{label\}/)
  assert.doesNotMatch(drawer, /@openai\/apps-sdk-ui\/components\/Menu/)
  assert.doesNotMatch(drawer, /triggerHidden|drawer__menu-anchor/)
  assert.match(itemActionMenu, /itemKind === 'chat' \? 'chats' : 'apps'/)
  assert.doesNotMatch(itemActionMenu, /drawer__item-action-header|drawer__item-action-handle/)
  assert.match(itemActionMenu, /itemKind === 'app' && \(/,
    'Delete data must stay app-only')
})

test('desktop density keeps the shell at native document scale', () => {
  const desktop = shellCss.match(/@media \(min-width: 1024px\) \{[\s\S]*$/)?.[0] || ''
  assert.doesNotMatch(desktop, /(?:^|[;{])\s*zoom\s*:/)
  assert.match(shellCss, /document remains at native[\s\S]*geometry share one space/)
  assert.doesNotMatch(drawer, /clientDeltaToLocal/)
  assert.doesNotMatch(workspaceChrome, /clientPointToLocal/)
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
