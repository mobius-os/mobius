// Vite server for component tests that render real .jsx through
// `vite.ssrLoadModule` and react-dom/server. Create every such server here so
// they all share one memory-bounded configuration: a test server renders once
// and exits, so it skips the background work a dev server does for a browser.
import { createServer } from 'vite'

export function createSsrRenderServer({
  noExternal = [],
  plugins = [],
  configFile,
} = {}) {
  return createServer({
    configFile,
    // The default loader bundles vite.config.js to a temporary file in the
    // nearest node_modules, which may be a read-only borrowed tree. The
    // runner evaluates it in memory, as the production build already does.
    configLoader: 'runner',
    appType: 'custom',
    logLevel: 'error',
    server: {
      middlewareMode: true,
      hmr: false,
      ws: false,
      // Watch nothing. The watcher follows symlinks under the project root,
      // so a linked dependency tree (as scripts/wt-npm.sh lends a worktree)
      // made each server crawl all of it. `watch: null` cannot express this:
      // Vite drops null values when merging inline config over vite.config.js.
      watch: { ignored: () => true },
    },
    // The SDK UI package uses extensionless internal ESM imports that native
    // Node resolution cannot load, so server rendering bundles it.
    ssr: { noExternal: ['@openai/apps-sdk-ui', ...noExternal] },
    // Server rendering never uses the browser dependency pre-bundle. Left on,
    // every server wrote it into the one shared node_modules/.vite cache and
    // rebuilt it whenever the previous test's configuration differed, roughly
    // doubling the process to about 1 GB. `noDiscovery` without an `include`
    // list is Vite's documented switch for turning the optimizer off.
    optimizeDeps: { noDiscovery: true },
    plugins,
  })
}
