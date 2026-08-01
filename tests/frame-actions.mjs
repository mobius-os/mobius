/**
 * Activate a control inside an iframe when the desktop shell uses root zoom.
 *
 * Chromium delivers physical pointer input correctly, but Playwright's
 * actionability check can project nested-frame coordinates through root zoom
 * twice. The resulting synthetic point lands on a containing element (or just
 * outside the viewport) even though the control is visible. These callers own
 * the behavior after activation; painted shell pointer geometry is covered by
 * navigation.spec.mjs.
 */
export async function activateFrameControl(locator) {
  await locator.evaluate((element) => {
    if (!(element instanceof HTMLElement)) {
      throw new TypeError('Frame control must be an HTMLElement')
    }
    element.click()
  })
}
