import path from 'node:path'


/**
 * Resolve where an ordinary `npm run build` may write.
 *
 * The live Möbius checkout has a persistent watcher lease. Its `dist/` is the
 * currently served shell, so a validation build must never empty or mutate it:
 * the watcher is the sole publisher and already stages + atomically swaps its
 * generations. Source checkouts without that lease (CI, image builds, ordinary
 * clones) keep Vite's normal `dist/` output contract.
 */
export function resolveBuildOutput({
  frontendDir,
  servedShellPresent,
  watcherLeaseHeld,
  tempDir,
}) {
  if (servedShellPresent && watcherLeaseHeld) {
    if (!tempDir) throw new Error('a temporary output directory is required')
    return {
      outDir: path.resolve(tempDir),
      transient: true,
      reason: 'live watcher lease present',
    }
  }
  return {
    outDir: path.join(path.resolve(frontendDir), 'dist'),
    transient: false,
    reason: 'standalone checkout',
  }
}
