import { test, expect } from '@playwright/test'
import { IDENTITY_STYLES } from '../frontend/src/components/SettingsView/identity/identity-styles.js'

// This is an isolated CSS/native-button contract, not a live account preview.
// No profile requests, file picker, authentication or backend are involved.
for (const width of [390, 1440]) {
  for (const theme of ['dark', 'light']) {
    test(`profile photo badge keeps its corner geometry and 44px target at ${width}px in ${theme}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 844 })
      await page.setContent(`
        <style>${IDENTITY_STYLES}
          * { animation: none !important; transition: none !important; }
        </style>
        <div data-theme="${theme}"><div class="settings">
          <div class="id-root id-root--settings">
            <div class="id-card-3d"><div class="id-cardid">
              <div class="id-avatar">
                E
                <button type="button" class="id-avatar-edit" aria-label="Change profile picture">
                  <span class="id-avatar-edit-badge" aria-hidden="true">
                    <svg width="16" height="16" viewBox="0 0 16 16"></svg>
                  </span>
                </button>
              </div>
            </div></div>
          </div>
        </div></div>
      `)
      const button = page.getByRole('button', { name: 'Change profile picture', exact: true })
      const geometry = await button.evaluate(node => {
        const hit = node.getBoundingClientRect()
        const badge = node.querySelector('.id-avatar-edit-badge').getBoundingClientRect()
        const avatar = node.parentElement.getBoundingClientRect()
        return {
          hit: [hit.width, hit.height], badge: [badge.width, badge.height],
          centerOffset: [badge.x + badge.width / 2 - hit.x - hit.width / 2,
            badge.y + badge.height / 2 - hit.y - hit.height / 2],
          cornerOffset: [badge.right - avatar.right, badge.bottom - avatar.bottom],
          background: getComputedStyle(node).backgroundColor,
          badgeBackground: getComputedStyle(node.querySelector('.id-avatar-edit-badge')).backgroundColor,
          badgeBorder: getComputedStyle(node.querySelector('.id-avatar-edit-badge')).borderColor,
        }
      })
      expect(geometry).toEqual({
        hit: [44, 44], badge: [26, 26], centerOffset: [0, 0],
        cornerOffset: [1, 1], background: 'rgba(0, 0, 0, 0)',
        badgeBackground: theme === 'light' ? 'rgb(255, 255, 255)' : 'rgba(255, 255, 255, 0.92)',
        badgeBorder: theme === 'light' ? 'rgb(242, 237, 249)' : 'rgb(23, 23, 28)',
      })
      await button.evaluate(node => {
        node.dataset.activations = '0'
        node.addEventListener('click', () => { node.dataset.activations = String(+node.dataset.activations + 1) })
      })
      // Activate the transparent area outside the 26px badge.
      await button.click({ position: { x: 3, y: 22 } })
      await expect(button).toHaveAttribute('data-activations', '1')
      await button.focus()
      await expect(button).toBeFocused()
      await button.press('Enter')
      await button.press('Space')
      await expect(button).toHaveAttribute('data-activations', '3')
      await button.evaluate(node => { node.disabled = true })
      await expect(button).toBeDisabled()
    })
  }
}
