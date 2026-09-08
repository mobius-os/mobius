/** Existing-work rows must retain their wrapped content height in a scrolling menu. */
import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'

const css = readFileSync(new URL('../frontend/src/components/Projects/ProjectCreateMenu.css', import.meta.url), 'utf8')

for (const width of [260, 360]) {
  test(`long existing-work list keeps text inside each ${width}px row`, async ({ page }) => {
    await page.setContent(`<style>${css}</style>
      <div class="project-create-menu__popover" style="width:${width}px">
        <div class="project-create-menu__sources">
          <div class="project-create-menu__source-list"><h3>Pages</h3></div>
        </div>
      </div>`)
    await page.locator('.project-create-menu__source-list').evaluate(list => {
      for (let i = 0; i < 40; i++) {
        const button = document.createElement('button')
        button.innerHTML = '<span class="project-create-menu__icon">P</span><span><strong></strong><small></small></span>'
        button.querySelector('strong').textContent = i === 1 ? 'VeryLongUnbrokenProjectName'.repeat(8) : 'Contribute — from local work to shared progress'
        button.querySelector('small').textContent = 'Interactive project journeys: preparation, review, assignment, questions and safe updates. All controls are interactive.'
        list.append(button)
      }
    })
    const layout = await page.locator('.project-create-menu__source-list').evaluate(list => {
      const rows = [...list.querySelectorAll('button')]
      return {
        scrolls: list.scrollHeight > list.clientHeight,
        contained: rows.every(row => {
          const outer = row.getBoundingClientRect()
          const text = row.lastElementChild.getBoundingClientRect()
          return text.top >= outer.top && text.bottom <= outer.bottom && text.right <= outer.right
        }),
        horizontalOverflow: list.scrollWidth > list.clientWidth,
      }
    })
    expect(layout).toEqual({ scrolls: true, contained: true, horizontalOverflow: false })
    await page.locator('.project-create-menu__source-list button').last().focus()
    await expect(page.locator('.project-create-menu__source-list button').last()).toBeInViewport()
  })
}
