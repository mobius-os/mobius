export function supportsShellReloadNavigationTransition(
  windowValue = globalThis.window,
  documentValue = globalThis.document,
) {
  return typeof documentValue?.startViewTransition === 'function'
    && !!windowValue
    && 'onpageswap' in windowValue
}

export function shellReloadNavigationTransitionIsActive(
  root,
  windowValue = globalThis.window,
  documentValue = globalThis.document,
) {
  return root?.hasAttribute?.('data-shell-reload-transition') === true
    && supportsShellReloadNavigationTransition(windowValue, documentValue)
}
