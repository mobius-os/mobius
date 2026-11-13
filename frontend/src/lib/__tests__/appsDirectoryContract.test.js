import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const src = resolve(here, '../..')
const drawer = readFileSync(resolve(src, 'components/Drawer/Drawer.jsx'), 'utf8')
const appsStrip = readFileSync(resolve(src, 'components/Drawer/DrawerAppsStrip.jsx'), 'utf8')
const directory = readFileSync(resolve(src, 'components/Drawer/AppsDirectory.jsx'), 'utf8')
const css = readFileSync(resolve(src, 'components/Drawer/AppsDirectory.css'), 'utf8')
const drawerCss = readFileSync(resolve(src, 'components/Drawer/Drawer.css'), 'utf8')
const itemMenuGestures = readFileSync(
  resolve(src, 'components/Drawer/useItemMenuGestures.js'), 'utf8',
)
const itemActionMenu = readFileSync(
  resolve(src, 'components/Drawer/DrawerItemActionMenu.jsx'),
  'utf8',
)
const shell = readFileSync(resolve(src, 'components/Shell/Shell.jsx'), 'utf8')
const tabModel = readFileSync(resolve(src, 'components/Shell/tabModel.js'), 'utf8')
const navigationIcons = readFileSync(resolve(src, 'components/navigationIcons.js'), 'utf8')

test('Apps is a single drawer destination and the old full app list is gone', () => {
  // The Apps section heading is the sidebar's one entry to the Apps screen.
  assert.match(drawer, /<DrawerAppsStrip[\s\S]*?onOpenAll=\{openApps\}/)
  assert.match(appsStrip, /className=\{`drawer__apps-card-open[\s\S]*?onClick=\{onOpenAll\}/)
  assert.doesNotMatch(drawer, /drawer__group--apps/)
  assert.match(drawer, /pinnedItems\.map\(\(\{ kind, item \}\)/)
})

// Apps-row icons use the shell's one press-and-hold owner, like every sidebar
// row: the drag controller finds them by data-drag-key (hold for the menu,
// lift into the workspace) under their own gesture key, and a finger pan of
// the row hands its release click to the drawer's one-shot suppressor.
test('Apps-row icons share the sidebar press-and-hold and drag owner', () => {
  assert.match(appsStrip, /data-drag-key=\{`app:\$\{app\.id\}`\}/)
  assert.match(appsStrip, /data-drawer-key=\{gestureKey\}/)
  assert.match(appsStrip, /registry\.set\(gestureKey, gestureHandlerRef\)/)
  assert.match(appsStrip, /useItemMenuGestures\(openMenuAt\)/)
  assert.match(appsStrip, /suppressReleaseClick\?\.\(\)/)
  assert.doesNotMatch(appsStrip, /HOLD_MS = |suppressClickRef|clickGuard/)
  assert.match(drawer, /<DrawerAppsStrip[\s\S]*?drawerRowGesturesRef=\{drawerRowGesturesRef\}[\s\S]*?suppressReleaseClick=\{suppressReleaseClick\}/)
})

test('directory cards preserve app management through shared row actions', () => {
  assert.match(drawer, /variant="card"[\s\S]*?actions=\{rowActions\}/,
    'app cards must keep the shared row action surface')
  for (const action of ['Install to home screen', 'Share app', 'Delete data', 'Copy name', 'Rename']) {
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
  assert.match(drawer, /onContextMenu=\{openItemMenu\}/)
  assert.match(drawer, /\{\.\.\.cardMenuTriggerProps\}/)
  assert.match(itemMenuGestures, /onContextMenu: openItemMenu,/,
    'launcher cards and drawer rows must enter the same context-menu path')
  const dragImports = itemMenuGestures.match(
    /import \{([\s\S]*?)\} from '\.\.\/Shell\/dragController\.js'/,
  )?.[1] || ''
  assert.match(dragImports, /PRESS_MENU_HOLD_MS/)
  assert.match(dragImports, /PRE_HOLD_MOVE_PX/)
  assert.match(itemMenuGestures, /setTimeout\(\(\) => \{[\s\S]*?openMenuAtRef\.current[\s\S]*?PRESS_MENU_HOLD_MS\)/)
  assert.doesNotMatch(drawer, /520/)
  assert.match(drawer, /placement=\{menu\?\.placement\}/)
  assert.match(itemActionMenu, /placeContextMenu/)
  assert.match(itemActionMenu, /stopImmediatePropagation/)
  const phoneMenu = drawerCss.match(
    /@media \(max-width: 720px\) \{[\s\S]*?\n\}/,
  )?.[0] || ''
  assert.match(phoneMenu, /drawer__item-action-menu[\s\S]*?width:\s*224px/)
  assert.doesNotMatch(phoneMenu, /\n\s*bottom:|backdrop-filter|drawer-item-sheet-in/)
  assert.match(shell, /id="main-content"[\s\S]*?inert=\{modalDrawerOpen\}/)
})

test('chat and app rows share one placed action menu contract', () => {
  assert.match(drawer, /<DrawerItemActionMenu[\s\S]*?itemKind=\{kind\}[\s\S]*?itemName=\{label\}/)
  assert.doesNotMatch(drawer, /@openai\/apps-sdk-ui\/components\/Menu/)
  assert.doesNotMatch(drawer, /triggerHidden|drawer__menu-anchor/)
  assert.match(itemActionMenu, /itemKind === 'chat' \? 'chats' : 'apps'/)
  assert.doesNotMatch(itemActionMenu, /drawer__item-action-header|drawer__item-action-handle/)
  assert.match(itemActionMenu, /itemKind === 'app' && \(/,
    'Delete data must stay app-only')
  assert.match(drawer, /onCopy=\{\(\) => actions\.copyName\(label\)\}/)
  assert.match(drawer, /writeClipboardText\(value\)/)
  assert.doesNotMatch(drawer, /Name copied/,
    'successful name copies should not create a routine confirmation notice')
  assert.match(drawer, /if \(!copied\)[\s\S]*?Couldn't copy the name\./,
    'clipboard failures must remain visible')
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
  assert.match(navigationIcons, /navigationIcon\(/)
  assert.match(navigationIcons, /navigation-action-icon/)
  assert.match(drawer, /from '\.\.\/navigationIcons\.js'/)
  assert.match(shell, /from '\.\.\/navigationIcons\.js'/)
})
