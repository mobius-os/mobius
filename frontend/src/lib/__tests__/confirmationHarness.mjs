import assert from 'node:assert/strict'
import { register } from 'node:module'

register('./confirmation-loader.mjs', import.meta.url)
export { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'

export function findNode(node, matches) {
  if (Array.isArray(node)) return node.map(child => findNode(child, matches)).find(Boolean)
  if (!node || typeof node !== 'object') return null
  return matches(node) ? node : findNode(node.props?.children, matches)
}

export function button(view, label) {
  const node = findNode(view.result.current, child => (
    child.type === 'button' && child.props.children === label
  ))
  assert.ok(node, `Expected button: ${label}`)
  return node.props
}

export function click(view, label) {
  const props = button(view, label)
  assert.ok(!props.disabled, `Button is disabled: ${label}`)
  return props.onClick()
}
