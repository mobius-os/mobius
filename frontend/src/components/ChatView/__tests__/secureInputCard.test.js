import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import SecureInputCard, {
  collectAndClearSecureFields,
} from '../SecureInputCard.jsx'


const fields = [
  {
    name: 'proxy_login',
    label: 'Proxy login',
    type: 'text',
    autocomplete: 'off',
  },
  {
    name: 'proxy_password',
    label: 'Proxy password',
    type: 'password',
    autocomplete: 'off',
  },
]


function renderCard(overrides = {}, interactive = false) {
  const block = {
    type: 'secure_input',
    request_id: 'request-1',
    mode: 'sealed',
    title: 'Private connection',
    description: 'Values bypass model context.',
    fields,
    status: 'pending',
    ...overrides,
  }
  return renderToStaticMarkup(createElement(SecureInputCard, {
    block,
    chatId: 'chat-1',
    interactive,
  }))
}


test('pending secure input renders one uncontrolled field per prompt', () => {
  const html = renderCard({}, true)

  assert.equal((html.match(/<input/g) || []).length, 2)
  assert.match(html, /data-secure-field="proxy_login"/)
  assert.match(html, /data-secure-field="proxy_password"/)
  assert.doesNotMatch(html, /type="password"/)
  assert.equal((html.match(/type="text"/g) || []).length, 2)
  assert.equal((html.match(/data-secure-masked="true"/g) || []).length, 1)
  assert.equal((html.match(/<input[^>]*autoComplete="off"/g) || []).length, 2)
  assert.doesNotMatch(html, /autoComplete="(?:username|current-password)"/)
  assert.match(html, /<form[^>]*autoComplete="off"/)
  assert.doesNotMatch(html, /<input[^>]* name=/)
  assert.match(html, /data-chat-inline-editor="secure-input"/)
  assert.match(html, />Enter securely</)
  assert.match(html, /values bypass the chat and AI/)
  assert.doesNotMatch(html, /value=/)
})


test('an explicit owner credential flow keeps its password-manager contract', () => {
  const html = renderCard({
    fields: [
      {
        name: 'current_password',
        label: 'Current password',
        type: 'password',
        autocomplete: 'current-password',
      },
      {
        name: 'new_password',
        label: 'New password',
        type: 'password',
        autocomplete: 'new-password',
      },
    ],
  }, true)

  assert.match(html, /<form[^>]*autoComplete="on"/)
  assert.match(html, /name="current_password"/)
  assert.equal((html.match(/type="password"/g) || []).length, 2)
  assert.match(html, /autoComplete="current-password"/)
  assert.match(html, /name="new_password"/)
  assert.match(html, /autoComplete="new-password"/)
  assert.doesNotMatch(html, /data-secure-masked/)
})


test('secure values are collected and cleared before the form can disappear', () => {
  const inputs = [
    {
      dataset: { secureField: 'proxy_login' },
      value: 'owner',
      blur() { assert.equal(this.value, '') },
    },
    {
      dataset: { secureField: 'proxy_password' },
      value: 'secret',
      blur() { assert.equal(this.value, '') },
    },
  ]
  const form = {
    querySelectorAll() { return inputs },
    reset() {
      // Deliberately restore defaults to prove the explicit clearing wins.
      for (const input of inputs) input.value = 'restored-default'
    },
  }

  const values = collectAndClearSecureFields(form, fields)

  assert.deepEqual(values, {
    proxy_login: 'owner',
    proxy_password: 'secret',
  })
  assert.deepEqual(inputs.map(input => input.value), ['', ''])
})


test('settled secure input renders locked prompt receipts without fields', () => {
  const html = renderCard({ status: 'completed' })

  assert.match(html, />Private connection</)
  assert.match(html, />Proxy login</)
  assert.match(html, />Proxy password</)
  assert.equal((html.match(/Provided securely/g) || []).length, 2)
  assert.equal((html.match(/secure-card__receipt-lock/g) || []).length, 2)
  assert.match(html, /Receipt saved · entered values omitted/)
  assert.doesNotMatch(html, /<input/)
})


test('failed receipt says values were used while expired input was not provided', () => {
  const failed = renderCard({ status: 'failed' })
  assert.equal(
    (failed.match(/Used securely/g) || []).length,
    2,
  )
  assert.match(failed, /Operation failed · entered values omitted/)
  assert.doesNotMatch(failed, /Not used/)
  assert.equal(
    (renderCard({ status: 'expired' }).match(/Not provided/g) || []).length,
    2,
  )
})


test('reveal mode is visually distinct and requires explicit confirmation', () => {
  const html = renderCard({ mode: 'reveal' }, true)

  assert.match(html, /secure-card--reveal/)
  assert.match(html, /name="reveal_confirmed"/)
  assert.match(html, /sent to the AI provider/)
  assert.match(html, />Reveal for this turn</)
})
