import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { DRAWER_ROW_HEIGHT } from '../../Drawer/drawerRowWindow.js'

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
  const drawerItem = ruleBody(drawerCss, '.drawer__item')
  const drawerRow = ruleBody(drawerCss, '.drawer__row')
  const desktopDrawerBody = ruleBody(drawerCss, '.drawer__body', 1)
  const desktopDrawerItem = ruleBody(drawerCss, '.drawer__item,\n  .drawer__item--new')
  const desktopRail = ruleBody(shellCss, '.shell__rail-actions', 1)
  const desktopRailAction = ruleBody(shellCss, '.shell__rail-action')

  assert.equal(px(drawerItem, 'min-height'), DRAWER_ROW_HEIGHT)
  assert.equal(px(drawerRow, 'height'), DRAWER_ROW_HEIGHT)
  assert.doesNotMatch(newChat, /(?:min-height|padding|border|box-shadow)\s*:/)
  assert.equal(58 + px(desktopDrawerBody, 'padding'), 12 + px(desktopRail, 'margin-top'))
  assert.equal(px(desktopDrawerItem, 'min-height'), px(desktopRailAction, 'height'))
  assert.match(
    desktopRail,
    /gap:\s*0;/,
  )
})

test('phone drawer increases row type without changing the section-title scale', () => {
  const drawerItem = ruleBody(drawerCss, '.drawer__item')
  const drawerLabel = ruleBody(drawerCss, '.drawer__label')

  assert.equal(px(drawerItem, 'font-size'), 15)
  assert.equal(px(drawerLabel, 'font-size'), 14)
})

test('now-playing actions keep full touch targets', () => {
  const control = ruleBody(drawerCss, '.drawer__now-playing-control', 1)
  const speed = ruleBody(drawerCss, '.drawer__now-playing-speed')

  assert.equal(px(control, 'width'), 44)
  assert.equal(px(control, 'height'), 44)
  assert.equal(px(speed, 'min-width'), 44)
})

test('the docked notifications panel covers the drawer content column', () => {
  const panel = ruleBody(shellCss, '.shell--drawer-docked .notifications')
  const desktopDrawerBody = ruleBody(drawerCss, '.drawer__body', 1)
  // Second shorthand value: the horizontal gutter drawer rows already sit in.
  const gutterMatch = desktopDrawerBody.match(/padding:\s*\d+px\s+(\d+)px/)
  assert.ok(gutterMatch, 'Missing desktop drawer body padding')
  const gutter = Number(gutterMatch[1])

  // Uncapped width tracks the resizable drawer, so widening it can never leave
  // drawer rows visible beside the panel.
  assert.equal(px(panel, 'left'), gutter)
  assert.match(
    panel,
    new RegExp(`width:\\s*calc\\(var\\(--desktop-sidebar-width\\) - ${gutter * 2}px\\)`),
  )
  assert.doesNotMatch(panel, /max-width/)
})
