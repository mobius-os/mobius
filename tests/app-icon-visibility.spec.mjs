/* Decoded app artwork follows its retained workspace tab's visibility. */
import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
const css = readFileSync(new URL('../frontend/src/components/AppIcon.css', import.meta.url), 'utf8')
test('loaded artwork cannot reveal an inactive tab', async ({ page }) => {
  await page.setContent(`<style>${css}</style><section style="visibility:hidden"><span class="app-icon is-image" style="width:32px;height:32px"><img class="app-icon__image--displayed" alt=""></span></section>`)
  const image = page.locator('img')
  await expect(image).toHaveCSS('visibility', 'hidden')
  await page.locator('section').evaluate(el => { el.style.visibility = 'visible' })
  await expect(image).toHaveCSS('visibility', 'visible')
})
