/** Probe a live Möbius shell/app launch for text revealed before owned fonts settle. */

import process from 'node:process'
import { chromium } from '@playwright/test'

const HELP = `Usage: npm run probe:font-handoff -- --app <id> [options]

Opens a fresh browser with service workers disabled, delays Möbius's bundled
font responses, and fails if shell or app title geometry changes after reveal.

Options:
  --app <id>          App id to open (required)
  --delay-ms <ms>     Artificial delay per bundled font request (default: 350)
  --settle-ms <ms>    Observation time after app reveal (default: 1500)
  --selector <css>    Extra app text selector to observe
  --json              Print only the machine-readable result
  --help              Show this help

Environment:
  API_BASE_URL        Möbius origin (default: http://localhost:8000)
  AGENT_TOKEN         Owner-scoped bearer token (required)
  VIEWPORT_WIDTH      Browser width (default: 1280)
  VIEWPORT_HEIGHT     Browser height (default: 800)
`

function parseArgs(argv) {
  const options = {
    app: '',
    delayMs: 350,
    settleMs: 1500,
    selector: '',
    json: false,
  }
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (arg === '--help') {
      process.stdout.write(HELP)
      process.exit(0)
    }
    if (arg === '--json') {
      options.json = true
      continue
    }
    const key = {
      '--app': 'app',
      '--delay-ms': 'delayMs',
      '--settle-ms': 'settleMs',
      '--selector': 'selector',
    }[arg]
    if (!key || index + 1 >= argv.length) {
      throw new Error(`Unknown or incomplete option: ${arg}`)
    }
    options[key] = argv[index + 1]
    index += 1
  }
  if (!/^\d+$/.test(String(options.app))) {
    throw new Error('--app must be a numeric app id')
  }
  for (const key of ['delayMs', 'settleMs']) {
    options[key] = Number(options[key])
    if (!Number.isFinite(options[key]) || options[key] < 0) {
      throw new Error(`--${key === 'delayMs' ? 'delay-ms' : 'settle-ms'} must be non-negative`)
    }
  }
  return options
}

function positiveInteger(value, fallback) {
  const parsed = Number.parseInt(String(value || ''), 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback
}

function targetShifts(records, revealAt, frameKind) {
  const history = new Map()
  for (const record of records) {
    if (record.kind !== 'sample' || record.at < revealAt) continue
    if (frameKind === 'shell' && !record.top) continue
    if (frameKind === 'app' && record.top) continue
    for (const target of record.targets || []) {
      const entries = history.get(target.key) || []
      const signature = `${target.family}|${target.weight}|${target.size}|${target.width}|${target.height}`
      if (!entries.some((entry) => entry.signature === signature)) {
        entries.push({ at: record.at, signature })
        history.set(target.key, entries)
      }
    }
  }
  return [...history.entries()]
    .filter(([, entries]) => entries.length > 1)
    .map(([target, entries]) => ({ target, states: entries }))
}

function summarize(records, fontRequests, delayMs, probeStartAt) {
  const shellSamples = records.filter((record) => (
    record.kind === 'sample' && record.top && record.at >= probeStartAt
  ))
  const shellReveal = shellSamples.find((record) => (
    record.shellPresent && !record.splashVisible
  ))
  const appReveal = shellSamples.find((record) => (
    record.shellPresent && record.framePresent
      && !record.splashVisible && !record.coverPresent
  ))
  const appSamples = records.filter((record) => (
    record.kind === 'sample' && !record.top && record.path.includes('/frame')
      && (record.targets || []).length > 0
  ))
  const firstVisibleAppText = appReveal
    ? appSamples.find((record) => record.at >= appReveal.at)
    : null
  const shellShifts = shellReveal
    ? targetShifts(records, shellReveal.at, 'shell')
    : []
  const appShifts = appReveal
    ? targetShifts(records, appReveal.at, 'app')
    : []
  const failures = []
  if (!shellReveal) failures.push('shell never revealed')
  if (!appReveal) failures.push('app never revealed')
  if (!firstVisibleAppText) failures.push('no visible app title text was observed')
  if (firstVisibleAppText && !firstVisibleAppText.ownedFontsSettled) {
    failures.push('app text became visible before Möbius-owned fonts settled')
  }
  if (shellShifts.length) failures.push('shell text geometry changed after reveal')
  if (appShifts.length) failures.push('app text geometry changed after reveal')

  return {
    ok: failures.length === 0,
    artificialFontDelayMs: delayMs,
    bundledFontRequests: fontRequests,
    shellRevealAt: shellReveal?.at || null,
    appRevealAt: appReveal?.at || null,
    firstVisibleAppTextAt: firstVisibleAppText?.at || null,
    firstVisibleAppTextFontStatus: firstVisibleAppText?.fontStatus || null,
    firstVisibleAppOwnedFonts: firstVisibleAppText?.ownedFonts || [],
    firstVisibleAppTargets: firstVisibleAppText?.targets || [],
    shellShifts,
    appShifts,
    failures,
    observedPaths: [...new Set(records
      .filter((record) => record.at >= probeStartAt)
      .map((record) => record.path))],
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2))
  const token = process.env.AGENT_TOKEN || ''
  if (!token) throw new Error('AGENT_TOKEN is required')
  const baseUrl = new URL(process.env.API_BASE_URL || 'http://localhost:8000')
  const browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({
    serviceWorkers: 'block',
    ignoreHTTPSErrors: true,
    viewport: {
      width: positiveInteger(process.env.VIEWPORT_WIDTH, 1280),
      height: positiveInteger(process.env.VIEWPORT_HEIGHT, 800),
    },
  })
  const records = []
  const fontRequests = []
  const page = await context.newPage()
  let probeStartAt = Date.now()

  try {
    await context.route('**/vendor/fonts/*.woff2', async (route) => {
      fontRequests.push(new URL(route.request().url()).pathname)
      if (options.delayMs) {
        await new Promise((resolve) => setTimeout(resolve, options.delayMs))
      }
      await route.continue()
    })
    await page.exposeBinding('__mobiusFontProbeRecord', (source, record) => {
      records.push({ ...record, frameUrl: source.frame?.url() || '' })
    })
    await page.addInitScript((value) => localStorage.setItem('token', value), token)
    await page.addInitScript(({ extraSelector }) => {
      const selectors = [
        '.shell__wordmark',
        'h1',
        'h2',
        'h3',
        '[class*="title" i]',
        extraSelector,
      ].filter(Boolean).join(',')
      const visible = (element) => {
        const style = getComputedStyle(element)
        const rect = element.getBoundingClientRect()
        return style.visibility !== 'hidden' && style.display !== 'none'
          && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0
      }
      const overlayVisible = (element) => element ? visible(element) : false
      const sample = () => {
        const ownedFonts = [...(document.fonts || [])]
          .filter((face) => ['Inter', 'JetBrains Mono'].includes(
            String(face.family || '').replace(/^['"]|['"]$/g, ''),
          ))
          .map((face) => ({
            family: String(face.family || '').replace(/^['"]|['"]$/g, ''),
            weight: face.weight,
            style: face.style,
            status: face.status,
          }))
        const targets = [...document.querySelectorAll(selectors)]
          .filter(visible)
          .slice(0, 24)
          .map((element) => {
            const style = getComputedStyle(element)
            const rect = element.getBoundingClientRect()
            const text = (element.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 80)
            return {
              key: `${element.tagName.toLowerCase()}.${String(element.className).slice(0, 80)}|${text}`,
              family: style.fontFamily,
              weight: style.fontWeight,
              size: style.fontSize,
              width: Number(rect.width.toFixed(2)),
              height: Number(rect.height.toFixed(2)),
            }
          })
        window.__mobiusFontProbeRecord({
          kind: 'sample',
          at: Date.now(),
          top: window === window.top,
          path: location.pathname,
          fontStatus: document.fonts?.status || 'unsupported',
          ownedFonts,
          ownedFontsSettled: ownedFonts.length > 0
            && ownedFonts.every((face) => ['loaded', 'error'].includes(face.status)),
          shellPresent: Boolean(document.querySelector('.shell')),
          framePresent: Boolean(document.querySelector('iframe[src*="/frame"]')),
          splashVisible: overlayVisible(document.getElementById('splash')),
          coverPresent: Boolean(document.querySelector('.canvas-loading')),
          targets,
        })
      }
      document.fonts?.addEventListener('loading', () => {
        window.__mobiusFontProbeRecord({
          kind: 'font-loading', at: Date.now(), top: window === window.top,
          path: location.pathname,
        })
      })
      document.fonts?.addEventListener('loadingdone', () => {
        window.__mobiusFontProbeRecord({
          kind: 'font-loaded', at: Date.now(), top: window === window.top,
          path: location.pathname,
        })
      })
      const observer = new MutationObserver(sample)
      observer.observe(document, { childList: true, subtree: true, attributes: true })
      const timer = setInterval(sample, 16)
      addEventListener('pagehide', () => {
        clearInterval(timer)
        observer.disconnect()
      }, { once: true })
      sample()
    }, { extraSelector: options.selector })

    probeStartAt = Date.now()
    const appUrl = new URL(`/app/${options.app}?font-probe=${Date.now()}`, baseUrl)
    await page.goto(appUrl.toString(), {
      waitUntil: 'domcontentloaded',
    })
    await page.waitForFunction(() => {
      const splash = document.getElementById('splash')
      const cover = document.querySelector('.canvas-loading')
      const hidden = (element) => !element
        || getComputedStyle(element).display === 'none'
        || getComputedStyle(element).visibility === 'hidden'
        || Number(getComputedStyle(element).opacity) === 0
      return document.querySelector('.shell')
        && document.querySelector('iframe[src*="/frame"]')
        && hidden(splash) && !cover
    }, { timeout: 15_000 })
    await page.waitForTimeout(options.settleMs)
  } finally {
    await context.close()
    await browser.close()
  }

  const result = summarize(records, fontRequests, options.delayMs, probeStartAt)
  if (options.json) {
    process.stdout.write(`${JSON.stringify(result)}\n`)
  } else {
    process.stdout.write(`${result.ok ? 'PASS' : 'FAIL'} font handoff: `
      + `${result.bundledFontRequests.length} delayed font requests, `
      + `${result.appShifts.length + result.shellShifts.length} post-reveal shifts\n`)
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`)
  }
  if (!result.ok) process.exitCode = 1
}

main().catch((error) => {
  process.stderr.write(`font handoff probe: ${error.message}\n`)
  process.exitCode = 1
})
