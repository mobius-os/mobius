/* A model row's provider logo opens that provider's setup, separately from the
   button that selects the model (ModelPickerRow.jsx). */
import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ModelPickerRow, { providerSetupLink } from '../ModelPickerRow.jsx'
import { providerSetupTarget } from '../providerRegistry.jsx'

const Logo = () => createElement('svg', { 'aria-hidden': 'true' })

function buttons(node, found = []) {
  if (!node || typeof node !== 'object') return found
  if (Array.isArray(node)) { node.forEach(child => buttons(child, found)); return found }
  if (node.type === 'button') found.push(node.props)
  buttons(node.props?.children, found)
  return found
}

function row(props) {
  const selections = []
  const opened = []
  const handlers = {
    onOpenProviderSettings: page => opened.push(['settings', page]),
    onOpenProviderApp: appId => opened.push(['app', appId]),
  }
  const element = ModelPickerRow({
    Logo,
    modelLabel: 'Model A',
    subtitle: 'Provider',
    onSelect: () => selections.push('Model A'),
    ...props({ handlers }),
  })
  return { element, selections, opened, html: renderToStaticMarkup(element) }
}

test('setup targets: built-ins open their Settings page, app providers their app', () => {
  assert.deepEqual(providerSetupTarget('claude'), { kind: 'settings', page: 'claude' })
  assert.deepEqual(providerSetupTarget('codex'), { kind: 'settings', page: 'codex' })
  assert.deepEqual(providerSetupTarget('mobius'), { kind: 'settings', page: 'mobius' })
  assert.deepEqual(providerSetupTarget('app-12'), { kind: 'app', appId: 12 })
  // Settings has no page for anything else, so there is no link to it.
  assert.equal(providerSetupTarget('app-x'), null)
  assert.equal(providerSetupTarget('other'), null)
  assert.equal(providerSetupTarget(undefined), null)
})

test('the logo opens setup and the model button selects, without crossing over', () => {
  const { element, selections, opened, html } = row(({ handlers }) => ({
    setup: providerSetupLink('claude', 'Claude Code', handlers),
  }))
  const [setup, model] = buttons(element)
  assert.equal(setup['aria-label'], 'Open Claude Code setup in Settings')
  assert.equal(model['aria-pressed'], false)
  setup.onClick()
  assert.deepEqual(opened, [['settings', 'claude']])
  assert.deepEqual(selections, [], 'opening setup leaves the selected model alone')
  model.onClick()
  assert.deepEqual(selections, ['Model A'])
  assert.deepEqual(opened, [['settings', 'claude']])
  assert.match(html, /<button type="button" class="csp-row__model-button" aria-pressed="false">.*Model A.*Provider/)
})

test('a disconnected provider keeps its setup link while its model cannot be picked', () => {
  const { element, opened } = row(({ handlers }) => ({
    setup: providerSetupLink('codex', 'OpenAI Codex', handlers),
    disabled: true,
  }))
  const [setup, model] = buttons(element)
  assert.equal(model.disabled, true)
  assert.equal(setup.disabled, undefined)
  setup.onClick()
  assert.deepEqual(opened, [['settings', 'codex']])
})

test('an app provider opens its app, never a Settings page', () => {
  const { element, opened } = row(({ handlers }) => ({
    setup: providerSetupLink('app-7', 'Sample Provider', handlers),
  }))
  const [setup] = buttons(element)
  assert.equal(setup['aria-label'], 'Open the Sample Provider app')
  setup.onClick()
  assert.deepEqual(opened, [['app', 7]])
})

test('without a way to navigate (embedded chats) the logo is plain, not a dead button', () => {
  for (const id of ['claude', 'app-7', 'other']) {
    const setup = providerSetupLink(id, 'Provider', {})
    assert.equal(setup, null)
    const { element, html } = row(() => ({ setup }))
    assert.equal(buttons(element).length, 1, 'only the model button remains')
    assert.doesNotMatch(html, /setup in Settings|disabled=""/)
    assert.match(html, /class="csp-row__icon-slot"/)
  }
  // An app provider link needs the app opener; Settings alone is not enough.
  assert.equal(providerSetupLink('app-7', 'Provider', { onOpenProviderSettings: () => {} }), null)
})

test('a selected row marks the model button pressed', () => {
  const { element, html } = row(() => ({ selected: true }))
  assert.equal(buttons(element)[0]['aria-pressed'], true)
  assert.match(html, /class="csp-row csp-row--selected"/)
})
