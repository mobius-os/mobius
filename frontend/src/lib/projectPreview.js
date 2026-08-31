const SAFE_PROJECT_PREVIEW_CSP = [
  "default-src 'none'",
  "style-src 'unsafe-inline'",
  "script-src 'unsafe-inline'",
  'img-src data: blob:',
  'font-src data:',
  'media-src data: blob:',
  "form-action 'none'",
  "base-uri 'none'",
].join('; ')

// Project previews are intentionally not installed apps, but they still need a
// truthful place for disposable test data. The opaque frame delegates this
// tiny storage surface to its parent; ProjectPreviewFrame owns the browser-
// private namespace and never hands the frame a shell or project credential.
function projectPreviewRuntime(dataScope = 'personal') {
  const scope = dataScope === 'shared' ? 'shared' : 'personal'
  return `<script data-mobius-project-preview-runtime>
(() => {
  const pending = new Map();
  const listeners = new Map();
  let connected = false;
  let sequence = 0;
  const call = (method, path, value) => new Promise((resolve, reject) => {
    const requestId = 'preview-' + (++sequence);
    const message = { type: 'mobius:project-preview-storage', requestId, method, path, value };
    const timeout = setTimeout(() => {
      pending.delete(requestId);
      reject(new Error('${scope === 'shared' ? 'Shared app data' : 'Personal preview data'} did not connect. Reload the preview and try again.'));
    }, 5000);
    pending.set(requestId, { resolve, reject, timeout, message });
    if (connected) parent.postMessage(message, '*');
  });
  addEventListener('message', event => {
    if (event.source !== parent || !event.data) return;
    const message = event.data;
    if (message.type === 'mobius:project-preview-storage-connected') {
      connected = true;
      for (const request of pending.values()) parent.postMessage(request.message, '*');
      return;
    }
    if (message.type === 'mobius:project-preview-storage-result') {
      const request = pending.get(message.requestId);
      if (!request) return;
      clearTimeout(request.timeout);
      pending.delete(message.requestId);
      if (message.error) request.reject(new Error(message.error));
      else request.resolve(message.value);
      return;
    }
    if (message.type === 'mobius:project-preview-storage-changed') {
      for (const listener of listeners.get(message.path) || []) listener(message.value);
    }
  });
  const storage = {
    get: path => call('get', path),
    set: (path, value) => call('set', path, value),
    delete: path => call('delete', path),
    list: (prefix = '') => call('list', prefix),
    subscribe(path, listener) {
      if (typeof listener !== 'function') return () => {};
      const group = listeners.get(path) || new Set();
      group.add(listener); listeners.set(path, group);
      return () => { group.delete(listener); if (!group.size) listeners.delete(path); };
    },
  };
  window.mobius = Object.freeze({ ...(window.mobius || {}), storage, preview: Object.freeze({ dataScope: '${scope}' }) });
  dispatchEvent(new CustomEvent('mobius:preview-ready'));
})();
</script>`
}

export function safeProjectHtmlDocument(source, dataScope = 'personal') {
  const csp = `<meta http-equiv="Content-Security-Policy" content="${SAFE_PROJECT_PREVIEW_CSP}">`
  // Keep the policy ahead of every untrusted byte. Inserting it after a
  // literal <head> is not sufficient: malformed HTML can place a fetching
  // element before that tag, causing the parser to issue a request (and ignore
  // the now-late head) before it ever encounters the policy.
  return `${csp}${projectPreviewRuntime(dataScope)}${String(source ?? '')}`
}

export function projectPreviewSandbox() {
  // Scripts may run so a built web project can be exercised, but the frame is
  // deliberately kept on an opaque origin. CSP blocks network access and the
  // absent sandbox grants block forms, popups, downloads, and parent access.
  return 'allow-scripts'
}

function localProjectPath(reference, entryPath) {
  const raw = String(reference || '').trim()
  if (!raw || raw.startsWith('#') || raw.startsWith('/') || raw.startsWith('//')) return null
  try {
    const base = new URL(entryPath, 'https://project.invalid/')
    const resolved = new URL(raw, base)
    if (resolved.origin !== base.origin) return null
    return decodeURIComponent(resolved.pathname.replace(/^\/+/, ''))
  } catch {
    return null
  }
}

function escapeInlineScript(source) {
  return String(source).replace(/<\/script/gi, '<\\/script')
}

/**
 * Inline a preview's local stylesheets and scripts before placing it in an
 * opaque srcDoc frame. This gives small multi-file projects a faithful,
 * interactive preview without exposing a project directory as a public URL.
 * Missing or remote dependencies stay in the document and are blocked by CSP.
 */
export async function assembleProjectHtmlPreview(source, entryPath, loadText, loadDataUri = null, dataScope = 'personal') {
  let document = String(source ?? '')
  const stylesheet = /<link\b([^>]*\brel=["']?stylesheet["']?[^>]*)>/gi
  const script = /<script\b([^>]*\bsrc=["']([^"']+)["'][^>]*)><\/script\s*>/gi

  const styles = [...document.matchAll(stylesheet)]
  for (const match of styles) {
    const href = /\bhref=["']([^"']+)["']/i.exec(match[1])?.[1]
    const path = localProjectPath(href, entryPath)
    if (!path) continue
    try {
      const content = await loadText(path)
      document = document.replace(match[0], `<style data-project-file="${path}">${content}</style>`)
    } catch { /* the CSP-blocked original makes a missing dependency visible */ }
  }

  const scripts = [...document.matchAll(script)]
  for (const match of scripts) {
    const path = localProjectPath(match[2], entryPath)
    if (!path) continue
    try {
      const content = await loadText(path)
      document = document.replace(
        match[0],
        `<script data-project-file="${path}">${escapeInlineScript(content)}</script>`,
      )
    } catch { /* the CSP-blocked original makes a missing dependency visible */ }
  }

  // Inline local binary assets (images, fonts, CSS background images) as data:
  // URIs when a binary loader is supplied. The srcDoc frame is an opaque origin
  // whose CSP is default-src 'none' + img-src/font-src data:, so a relative
  // asset URL would otherwise be blocked; a data: URI is the only way a built
  // site's own images and fonts can render. Each local path is fetched once.
  // Remote/absolute refs are left untouched (and stay CSP-blocked, so a missing
  // asset is visible rather than silently wrong).
  if (loadDataUri) {
    const assetCache = new Map()
    const resolveAsset = async (reference) => {
      const path = localProjectPath(reference, entryPath)
      if (!path) return null
      if (!assetCache.has(path)) {
        try { assetCache.set(path, await loadDataUri(path)) }
        catch { assetCache.set(path, null) }
      }
      return assetCache.get(path)
    }

    const image = /<img\b[^>]*?\bsrc=(["'])([^"']+)\1[^>]*>/gi
    for (const match of [...document.matchAll(image)]) {
      const uri = await resolveAsset(match[2])
      if (!uri) continue
      const replaced = match[0].replace(
        `src=${match[1]}${match[2]}${match[1]}`,
        `src=${match[1]}${uri}${match[1]}`,
      )
      document = document.replace(match[0], replaced)
    }

    const cssUrl = /url\(\s*(["']?)([^)"']+)\1\s*\)/gi
    for (const match of [...document.matchAll(cssUrl)]) {
      const uri = await resolveAsset(match[2])
      if (!uri) continue
      document = document.split(match[0]).join(`url(${uri})`)
    }
  }

  return safeProjectHtmlDocument(document, dataScope)
}
