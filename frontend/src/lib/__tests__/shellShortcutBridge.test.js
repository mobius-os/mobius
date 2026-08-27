import assert from 'node:assert/strict'
import test from 'node:test'
import { attributedShellShortcutAction } from '../../components/AppCanvas/appFrameProtocol.js'

test('the frame bridge accepts only action ids advertised by its shell host', () => {
  const advertised = [{ actionId: 'search.open', binding: { key: 'k', mod: true } }]
  assert.equal(attributedShellShortcutAction({
    type: 'moebius:shell-shortcut', actionId: 'search.open',
  }, advertised), 'search.open')
  assert.equal(attributedShellShortcutAction({
    type: 'moebius:shell-shortcut', actionId: 'chat.new',
  }, advertised), null)
  assert.equal(attributedShellShortcutAction({ type: 'other' }, advertised), null)
})
