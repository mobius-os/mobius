import fs from 'node:fs/promises'
import path from 'node:path'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'


function diagnosticText(error) {
  const diagnostics = Array.isArray(error?.errors) && error.errors.length
    ? error.errors
    : [error]
  return diagnostics
    .map((diagnostic) => [diagnostic?.message || String(diagnostic), diagnostic?.frame]
      .filter(Boolean)
      .join('\n'))
    .join('\n')
}


async function main() {
  if (process.argv.length !== 3) {
    throw new Error('usage: node rolldown_compile.mjs <json-config>')
  }
  const config = JSON.parse(process.argv[2])
  const requireFromRuntime = createRequire(
    path.join(path.resolve(config.nodeModules), 'package.json'),
  )
  const rolldownUrl = pathToFileURL(requireFromRuntime.resolve('rolldown')).href
  const { rolldown } = await import(rolldownUrl)
  const entryId = path.resolve(config.entry)

  const bundle = await rolldown({
    input: config.entry,
    platform: 'browser',
    tsconfig: false,
    resolve: { alias: Object.fromEntries(config.aliases) },
    transform: {
      define: { 'process.env.NODE_ENV': "'production'" },
      jsx: 'react-jsx',
      target: 'es2022',
    },
    onLog(level, log, defaultHandler) {
      // Unlike the previous compiler, Rolldown warns and externalizes missing
      // packages by default. Mini-app typos must remain hard compile failures.
      if (log.code === 'UNRESOLVED_IMPORT') {
        defaultHandler('error', log)
        return
      }
      defaultHandler(level, log)
    },
    plugins: [{
      name: 'mobius-mini-app-contract',
      resolveId(source) {
        if (/\.css(?:$|[?#])/.test(source)) throw new Error(config.cssImportError)
        return null
      },
      transform(code, id) {
        if (path.resolve(id) !== entryId) return null
        return {
          code: `import ${JSON.stringify(config.runtimeInject)};\n${code}`,
          map: null,
        }
      },
    }],
  })

  let generated
  try {
    generated = await bundle.generate({
      format: 'es',
      codeSplitting: false,
      minify: true,
      sourcemap: false,
      comments: { legal: true },
      // Ordinary banner comments are removed by Oxc minification. postBanner
      // is inserted afterward and therefore remains the byte-zero ABI marker.
      postBanner: config.banner,
    })
  } finally {
    await bundle.close()
  }

  const chunks = generated.output.filter((item) => item.type === 'chunk')
  const entry = chunks.find((item) => item.isEntry)
  const report = {
    inputs: [...new Set(chunks.flatMap((item) => item.moduleIds))].sort(),
    outputs: generated.output.map((item) => item.type === 'chunk' ? {
      type: item.type,
      fileName: item.fileName,
      isEntry: item.isEntry,
      exports: item.exports,
      imports: item.imports,
      // With code splitting disabled Rolldown records an inlined dynamic import
      // as a self-reference. It is not an external request, so normalize it out.
      dynamicImports: item.dynamicImports.filter((name) => name !== item.fileName),
    } : {
      type: item.type,
      fileName: item.fileName,
    }),
  }
  await fs.writeFile(config.report, JSON.stringify(report), 'utf8')
  if (entry) {
    await fs.mkdir(path.dirname(config.output), { recursive: true })
    await fs.writeFile(config.output, entry.code, 'utf8')
  }
}


main().catch((error) => {
  console.error(diagnosticText(error))
  process.exitCode = 1
})
