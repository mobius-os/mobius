// Node ESM loader hook that substitutes Vite's `import.meta.env`
// for modules under test. Without this, api/client.js (and
// anything that transitively imports it) crashes under node:test
// because `import.meta.env` is a Vite-specific construct that
// only exists in the dev/build pipeline.
//
// Used by themeService.toggleTheme.test.js (themeService imports
// themeQueries from hooks/queries.js → api/client.js).
//
// Usage:
//   node --loader=./src/lib/__tests__/vite-env-loader.mjs --test ...

import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { transformWithEsbuild } from 'vite'

const REACT_SHIM = new URL(
  '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs',
  import.meta.url,
).href

export async function resolve(specifier, context, nextResolve) {
  if (
    specifier === 'react'
    && context.parentURL?.endsWith('/components/Shell/useAppIntentNavigation.js')
  ) {
    return { url: REACT_SHIM, shortCircuit: true, format: 'module' }
  }
  return nextResolve(specifier, context)
}

export async function load(url, context, nextLoad) {
  // Only intercept project sources — leave node_modules alone.
  if (
    url.startsWith('file://')
    && url.includes('/src/')
    && (url.endsWith('.js') || url.endsWith('.jsx'))
  ) {
    const path = fileURLToPath(url)
    const raw = await readFile(path, 'utf8')
    const patched = raw
      .replace(/import\.meta\.env\.BASE_URL/g, "'/'")
      .replace(/import\.meta\.env\.MODE/g, "'test'")
      .replace(/import\.meta\.env\.DEV/g, 'false')
      .replace(/import\.meta\.env\.PROD/g, 'false')
    if (url.endsWith('.jsx')) {
      const transformed = await transformWithEsbuild(patched, path, {
        loader: 'jsx',
        format: 'esm',
        jsx: 'automatic',
      })
      return {
        format: 'module',
        source: transformed.code,
        shortCircuit: true,
      }
    }
    return {
      format: 'module',
      source: patched,
      shortCircuit: true,
    }
  }
  return nextLoad(url, context)
}
