/* Live "is touch the primary pointer?" signal, shared by the composer and every
 * inline chat editor so their Enter-to-send behavior agrees across devices. */

export const TOUCH_PRIMARY_QUERY = '(hover: none) and (pointer: coarse)'

const _mql = typeof matchMedia === 'function' ? matchMedia(TOUCH_PRIMARY_QUERY) : null
let _touchPrimary = _mql?.matches ?? false
_mql?.addEventListener('change', (e) => { _touchPrimary = e.matches })

/** Whether touch is the primary pointer right now. Live: updates if the input
 * mix changes (e.g. a tablet docking to a keyboard). */
export function isTouchPrimary() {
  return _touchPrimary
}
