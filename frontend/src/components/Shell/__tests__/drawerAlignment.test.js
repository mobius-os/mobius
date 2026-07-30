import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const drawerCss = readFileSync(new URL('../../Drawer/Drawer.css', import.meta.url), 'utf8')
const shellCss = readFileSync(new URL('../Shell.css', import.meta.url), 'utf8')

const ruleBody = (css, selector, occurrence = 0) => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const matches = [...css.matchAll(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, 'g'))]
  assert.ok(matches[occurrence], `Missing CSS rule: ${selector}`)
  return matches[occurrence][1]
}

const px = (body, property) => {
  const match = body.match(new RegExp(`${property}:\\s*(\\d+)px`))
  assert.ok(match, `Missing pixel declaration: ${property}`)
  return Number(match[1])
}

test('collapsed and expanded drawer actions share one vertical rhythm', () => {
  const newChat = ruleBody(drawerCss, '.drawer__item--new')
  const desktopDrawerBody = ruleBody(drawerCss, '.drawer__body', 1)
  const desktopDrawerItem = ruleBody(drawerCss, '.drawer__item,\n  .drawer__item--new')
  const desktopRail = ruleBody(shellCss, '.shell__rail-actions', 1)
  const desktopRailAction = ruleBody(shellCss, '.shell__rail-action')

  assert.equal(px(newChat, 'min-height'), 44)
  assert.doesNotMatch(newChat, /(?:border|box-shadow)\s*:/)
  assert.equal(58 + px(desktopDrawerBody, 'padding'), 12 + px(desktopRail, 'margin-top'))
  assert.equal(px(desktopDrawerItem, 'min-height'), px(desktopRailAction, 'height'))
  assert.match(
    desktopRail,
    /gap:\s*0;/,
  )
})

test('the notifications panel follows the resizable drawer edge', () => {
  assert.match(
    shellCss,
    /\.shell--drawer-docked \.notifications\s*\{[\s\S]*?--notifications-panel-max-width:\s*390px;[\s\S]*?left:\s*max\([\s\S]*?var\(--desktop-sidebar-width\)[\s\S]*?- var\(--notifications-panel-max-width\)[\s\S]*?width:\s*min\([\s\S]*?var\(--notifications-panel-max-width\)[\s\S]*?var\(--desktop-sidebar-width\)/,
  )
})
