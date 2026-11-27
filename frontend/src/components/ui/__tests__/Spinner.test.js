import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import Spinner from '../Spinner.jsx'

const render = props => renderToStaticMarkup(createElement(Spinner, props))

test('a spinner is a native indeterminate progress, hidden when text beside it already speaks', () => {
  assert.equal(render({}), '<progress class="spinner" aria-hidden="true"></progress>')
})

test('a labelled spinner announces itself instead of hiding', () => {
  assert.equal(render({ label: 'Loading app' }), '<progress class="spinner" aria-label="Loading app"></progress>')
})

test('size, accent tone and caller placement shape the one shared spinner', () => {
  assert.equal(
    render({ size: 30, tone: 'accent', className: 'chat__attach-card-spin' }),
    '<progress class="spinner spinner--accent chat__attach-card-spin" style="--spinner-size:30px" aria-hidden="true"></progress>',
  )
})
