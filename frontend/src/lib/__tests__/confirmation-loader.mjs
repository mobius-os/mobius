// Opt in only these two component modules to the existing hook harness. The
// ordinary Vite test loader still compiles their real JSX and dependencies.
const shim = new URL('../../components/ChatView/hooks/__tests__/react-hook-shim.mjs', import.meta.url).href
const boundaries = new URL('./confirmationBoundaries.mjs', import.meta.url).href
const notifications = '/components/NotificationsView/NotificationsView.jsx'
const updates = '/components/SettingsView/PlatformUpdates.jsx'

export function resolve(specifier, context, nextResolve) {
  const parent = context.parentURL || ''
  if (specifier === 'react' && (parent.endsWith(notifications) || parent.endsWith(updates))) {
    return { url: shim, shortCircuit: true }
  }
  if (parent.endsWith(notifications) && (
    specifier === '@tanstack/react-query' || specifier === '../../hooks/queries.js'
  )) {
    return { url: boundaries, shortCircuit: true }
  }
  return nextResolve(specifier, context)
}
