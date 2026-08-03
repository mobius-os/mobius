/**
 * Activate a control inside an iframe when the desktop shell uses root zoom.
 *
 * Chromium delivers physical pointer input correctly, but Playwright projects
 * nested-frame actionability coordinates through root zoom twice. The synthetic
 * point can land on a containing element even though the control is visible.
 * Frame behavior after activation remains asserted by each caller; shell-level
 * pointer geometry has separate physical mouse regressions.
 */
export async function activateFrameControl(locator) {
  await locator.evaluate((element) => {
    if (!(element instanceof HTMLElement)) {
      throw new TypeError('Frame control must be an HTMLElement')
    }
    element.click()
  })
}
