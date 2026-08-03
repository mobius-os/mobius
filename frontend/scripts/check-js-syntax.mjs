import fs from 'node:fs/promises'
import path from 'node:path'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'


const nodeModules = process.env.MOBIUS_FRONTEND_NODE_MODULES
  || path.resolve(import.meta.dirname, '..', 'node_modules')
const requireFromFrontend = createRequire(path.join(nodeModules, 'package.json'))
const transformUrl = pathToFileURL(
  requireFromFrontend.resolve('rolldown/utils'),
).href
const { transform } = await import(transformUrl)

let failed = false
for (const file of process.argv.slice(2)) {
  try {
    const source = await fs.readFile(file, 'utf8')
    const result = await transform(path.resolve(file), source, {
      lang: 'jsx',
      jsx: 'preserve',
      sourcemap: false,
    })
    if (result.errors.length) {
      failed = true
      for (const error of result.errors) {
        console.error(`${file}: ${error.message || String(error)}`)
      }
    }
  } catch (error) {
    failed = true
    console.error(`${file}: ${error?.message || String(error)}`)
  }
}

if (failed) process.exitCode = 1
