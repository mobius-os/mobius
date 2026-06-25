/**
 * Theme application library — the SINGLE source of truth for how a
 * Möbius surface paints the active theme onto its own DOM.
 *
 * One file, three audiences:
 *   1. The shell (themeService.applyThemeToDom delegates here).
 *   2. The app-frame iframe (its postMessage `applyTheme` delegates
 *      here in spirit — the frame inlines an equivalent because it
 *      can't import from /src in production; the prepaint IIFE below
 *      is the bytes both share).
 *   3. The pre-paint inline script in index.html + app-frame.html —
 *      `PREPAINT_SRC` is the exact IIFE both HTML files embed, so a
 *      single string is the source of truth for flash-free first paint.
 *
 * Why a standalone module and not a method on themeService: the
 * applier is a pure side-effect over an injectable (doc, store) pair.
 * Making both injectable is what lets the unit tests drive it against
 * a tiny DOM stub instead of jsdom, and what lets `resolveTheme`/
 * `applyTheme` run identically in the shell, a worker, or a test.
 *
 * THEME-AS-DATA HANDOFF: the server no longer injects a <style> block
 * into the served HTML. Instead it serializes the effective theme into
 * `<script type="application/json" id="__mobius-theme__">{css,bg,mode}`
 * and the client paints it. `resolveTheme` reads that slot first, then
 * falls back to persisted localStorage, then to the dark default — so a
 * cold online boot, a warm reload, and a cold-offline reopen all resolve
 * the same way. The pre-paint IIFE does a minimal version of this before
 * first paint; `applyTheme` does the full version (fonts, meta tags,
 * status bar, persistence) once the module loads.
 */

// `bg` is owner-controlled but constrained to a hex color everywhere it
// is produced (server theme.py:get_bg_color, the JSON slot, the toggle
// path). We re-validate at every consumer so a future change upstream
// can't slip a CSS expression into body.style.background or meta content.
export const HEX_RE = /^#[0-9a-fA-F]{3,8}$/

const STYLE_ID = 'mobius-theme'
const FONT_LINK_ATTR = 'data-theme-font'
const SLOT_ID = '__mobius-theme__'
const STORE_KEY = 'mobius-theme'
const STORE_BG_KEY = 'mobius-theme-bg'
const DEFAULT_BG = '#0d0d0d'
const DEFAULT_MODE = 'dark'

/**
 * 'dark' | 'light' for a bg hex by average-RGB luminance, or null when
 * `bg` is missing/unparseable. 128 splits dark/light cleanly enough —
 * exact perceptual luminance isn't needed, just the direction. Short
 * (#RGB / #RGBA) forms expand by doubling each nibble; long forms take
 * the leading six chars (a trailing alpha byte never changes direction).
 * Mirrors backend theme._infer_theme_mode so the server and client agree
 * on mode from the same --bg.
 */
export function inferMode(bg) {
  if (!bg || !HEX_RE.test(bg)) return null
  const raw = bg.slice(1)
  const expanded = raw.length === 3 || raw.length === 4
    ? raw.slice(0, 3).split('').map(c => c + c).join('')
    : raw.slice(0, 6)
  const r = parseInt(expanded.slice(0, 2), 16)
  const g = parseInt(expanded.slice(2, 4), 16)
  const b = parseInt(expanded.slice(4, 6), 16)
  if (r !== r || g !== g || b !== b) return null  // NaN guard
  return (r + g + b) / 3 < 128 ? 'dark' : 'light'
}

/**
 * Resolve the theme this surface should paint, as `{ css?, bg, mode }`.
 *
 * Precedence (first that parses wins):
 *   1. The `__mobius-theme__` JSON slot the server serialized into the
 *      HTML for this request — `{ css, bg, mode }`. Authoritative on a
 *      fresh online navigation.
 *   2. `localStorage['mobius-theme']` — `{ bg, mode }` persisted by
 *      `applyTheme` on every paint. Carries a warm reload + cold-offline
 *      reopen (served from the SW's cached, slot-EMPTY index.html).
 *   3. `localStorage['mobius-theme-bg']` — a bare hex, the legacy key
 *      kept for one more cycle so a pre-upgrade install still themes.
 *   4. The dark default.
 *
 * `css` is only ever present from the slot (offline reloads paint from
 * the SW-cached <style id="mobius-theme"> that a prior online paint left
 * behind, so they don't need css here). `mode` always resolves —
 * d.mode || inferMode(d.bg) || 'dark'.
 */
export function resolveTheme({ doc = globalThis.document, store = globalThis.localStorage } = {}) {
  // 1. Server-serialized slot.
  try {
    const el = doc && doc.getElementById(SLOT_ID)
    const txt = el && el.textContent && el.textContent.trim()
    if (txt) {
      const d = JSON.parse(txt)
      const bg = d.bg && HEX_RE.test(d.bg) ? d.bg : DEFAULT_BG
      return { css: typeof d.css === 'string' ? d.css : undefined, bg, mode: d.mode || inferMode(bg) || DEFAULT_MODE }
    }
  } catch {}
  // 2. Persisted {bg, mode}.
  try {
    const raw = store && store.getItem(STORE_KEY)
    if (raw) {
      const d = JSON.parse(raw)
      const bg = d.bg && HEX_RE.test(d.bg) ? d.bg : DEFAULT_BG
      return { bg, mode: d.mode || inferMode(bg) || DEFAULT_MODE }
    }
  } catch {}
  // 3. Legacy bare-hex key.
  try {
    const bg = store && store.getItem(STORE_BG_KEY)
    if (bg && HEX_RE.test(bg)) return { bg, mode: inferMode(bg) || DEFAULT_MODE }
  } catch {}
  // 4. Default.
  return { bg: DEFAULT_BG, mode: DEFAULT_MODE }
}

/**
 * Apply a theme to the DOM and persist it:
 *   - Strip `@import url(...)` from the CSS body and re-inject the safe
 *     (http/https only) ones as `<link data-theme-font>` so fonts load
 *     reliably (a `javascript:`/`data:` URL can't become a stylesheet).
 *   - Inject the remaining CSS into a single `<style id="mobius-theme">`
 *     re-appended to the END of <head> so it wins the cascade.
 *   - Mirror bg onto body.style.background, <meta theme-color>, the
 *     inline `--bg` on <html> (beats `:root{}`, keeps the pre-paint var
 *     in lockstep), and `color-scheme`/`data-theme`/iOS status bar from
 *     the mode.
 *   - Persist BOTH `mobius-theme` ({bg,mode}, the new key resolveTheme +
 *     the pre-paint IIFE read) AND `mobius-theme-bg` (bare hex, the
 *     legacy key one-cycle-compatible with the old splash script).
 *
 * `doc`/`store` are injectable for tests; default to globals.
 */
export function applyTheme(theme, { doc = globalThis.document, store = globalThis.localStorage } = {}) {
  const css = theme && typeof theme.css === 'string' ? theme.css : ''
  const bg = theme && theme.bg
  // Mode precedence: an explicit theme.mode, else inferMode(bg), else the
  // --bg parsed out of the CSS body (so a css-only apply with no bg arg still
  // resolves a mode — this preserves the old themeService._inferThemeMode
  // css-parse fallback now that the shell delegates here).
  const mode = (theme && theme.mode)
    || inferMode(bg)
    || inferMode(css.match(/--bg:\s*(#[0-9a-fA-F]{3,8})/)?.[1])

  if (css) {
    doc.querySelectorAll(`link[${FONT_LINK_ATTR}]`).forEach(l => l.remove())

    const imports = []
    // Cover all four CSS @import spellings: url('X')/url("X") (quoted),
    // url(X) (bare, unquoted), and "X"/'X' (no url()). The old regex only
    // saw quoted url(), so a bare or no-url() import slipped past the http(s)
    // allowlist unmatched. The url group excludes ) and whitespace; the
    // bare-quoted group runs to its closing quote. A single trailing `;` is
    // required, so an @import-looking substring inside a quoted value (e.g.
    // a `content:` string) isn't matched as a real rule.
    const cssBody = css.replace(
      /@import\s+(?:url\(\s*(?:"([^"]*)"|'([^']*)'|([^"'()\s]+))\s*\)|"([^"]*)"|'([^']*)')\s*;[^\S\n]*\n?/g,
      (m, u1, u2, u3, q1, q2) => { imports.push(u1 ?? u2 ?? u3 ?? q1 ?? q2); return '' }
    )
    imports.filter(url => /^https?:\/\//i.test(url)).forEach(url => {
      const link = doc.createElement('link')
      link.rel = 'stylesheet'
      link.href = url
      link.dataset.themeFont = '1'
      doc.head.appendChild(link)
    })

    let el = doc.getElementById(STYLE_ID)
    if (!el) {
      el = doc.createElement('style')
      el.id = STYLE_ID
    }
    // Re-append so this block is the LAST <head> child and wins the
    // cascade. appendChild on an attached node moves it; cheap.
    doc.head.appendChild(el)
    el.textContent = cssBody
  }

  if (bg && HEX_RE.test(bg)) {
    if (doc.body) doc.body.style.background = bg
    const meta = doc.querySelector('meta[name="theme-color"]')
    if (meta) meta.setAttribute('content', bg)
    doc.documentElement.style.setProperty('--bg', bg)
  }

  if (mode) {
    doc.documentElement.setAttribute('data-theme', mode)
    // color-scheme drives UA-native surfaces (form controls, scrollbars,
    // canvas defaults) — without it a dark shell on a light OS flashes
    // light native widgets. data-theme covers our own CSS tokens.
    doc.documentElement.style.colorScheme = mode
    // iOS PWA status bar: `default` is a light bar with dark glyphs
    // (light mode), `black` is opaque dark (dark mode).
    const sb = doc.querySelector('meta[name="apple-mobile-web-app-status-bar-style"]')
    if (sb) sb.setAttribute('content', mode === 'light' ? 'default' : 'black')
  }

  // Persist for the next boot: the new {bg,mode} key the pre-paint IIFE +
  // resolveTheme read, and the legacy bare-hex key for one-cycle compat.
  // Persist only from the top-level shell — the same-origin app-frame shares
  // this store and must not clobber the shell-owned theme key.
  if (bg && HEX_RE.test(bg) && (typeof window === 'undefined' || window.parent === window)) {
    try {
      store.setItem(STORE_KEY, JSON.stringify({ bg, mode: mode || DEFAULT_MODE }))
      store.setItem(STORE_BG_KEY, bg)
    } catch {}
  }
}

/**
 * The EXACT inline IIFE both index.html and app-frame.html embed in
 * <head> to paint the theme before first paint (flash-free). Self-
 * contained — no imports, every read in try/catch — because it runs
 * as a bare classic script before any module loads.
 *
 * It does the minimum needed to avoid a wrong-mode/wrong-bg flash:
 * resolve {css?, bg, mode} by the SAME precedence as resolveTheme
 * (slot -> mobius-theme -> mobius-theme-bg -> dark default), inject the
 * slot css into <style id="mobius-theme"> when present, and set --bg /
 * data-theme / color-scheme on <html>. The full applyTheme (fonts, meta
 * tags, status bar, persistence) runs once the module loads.
 *
 * `applyTheme.prepaint.test.js` asserts the inline script in both HTML
 * files is byte-identical to this string, so they can never drift.
 */
export const PREPAINT_SRC = `(function () {
  try {
    var HEX = /^#[0-9a-fA-F]{3,8}$/;
    var root = document.documentElement;
    function infer(bg) {
      if (!bg || !HEX.test(bg)) return null;
      var raw = bg.slice(1);
      var x = raw.length === 3 || raw.length === 4
        ? raw.slice(0, 3).replace(/(.)/g, '$1$1')
        : raw.slice(0, 6);
      var r = parseInt(x.slice(0, 2), 16);
      var g = parseInt(x.slice(2, 4), 16);
      var b = parseInt(x.slice(4, 6), 16);
      if (r !== r || g !== g || b !== b) return null;
      return (r + g + b) / 3 < 128 ? 'dark' : 'light';
    }
    var css, bg, mode;
    try {
      var slot = document.getElementById('__mobius-theme__');
      var txt = slot && slot.textContent && slot.textContent.trim();
      if (txt) {
        var d = JSON.parse(txt);
        if (typeof d.css === 'string') css = d.css;
        if (d.bg && HEX.test(d.bg)) bg = d.bg;
        mode = d.mode || infer(bg);
      }
    } catch (e) {}
    if (!bg) {
      try {
        var raw = localStorage.getItem('mobius-theme');
        if (raw) {
          var p = JSON.parse(raw);
          if (p.bg && HEX.test(p.bg)) { bg = p.bg; mode = mode || p.mode || infer(p.bg); }
        }
      } catch (e) {}
    }
    if (!bg) {
      try {
        var legacy = localStorage.getItem('mobius-theme-bg');
        if (legacy && HEX.test(legacy)) { bg = legacy; mode = mode || infer(legacy); }
      } catch (e) {}
    }
    if (!bg) bg = '#0d0d0d';
    if (!mode) mode = 'dark';
    if (css) {
      // Match applyTheme.js: pull CSS @import-url rules out of the body and
      // re-inject only the http(s) ones as <link data-theme-font> (a
      // javascript:/data: URL must not become active CSS), so the pre-painted
      // <style> never carries a raw rule the live applier would re-strip.
      var fontUrls = [];
      // Cover all four @import-rule forms (quoted url(), bare url(), and
      // no-url() "X"/'X') so the http(s) allowlist below can't be bypassed by
      // a syntax the quoted-url-only regex didn't see. Backslashes are DOUBLED
      // here because this is a template literal — the evaluated string carries
      // single backslashes (see applyTheme.prepaint.test.js byte-equality).
      css = css.replace(
        /@import\\s+(?:url\\(\\s*(?:"([^"]*)"|'([^']*)'|([^"'()\\s]+))\\s*\\)|"([^"]*)"|'([^']*)')\\s*;[^\\S\\n]*\\n?/g,
        function (m, u1, u2, u3, q1, q2) {
          fontUrls.push(u1 !== undefined ? u1 : u2 !== undefined ? u2 : u3 !== undefined ? u3 : q1 !== undefined ? q1 : q2);
          return '';
        }
      );
      for (var i = 0; i < fontUrls.length; i++) {
        if (/^https?:\\/\\//i.test(fontUrls[i])) {
          var link = document.createElement('link');
          link.rel = 'stylesheet';
          link.href = fontUrls[i];
          link.setAttribute('data-theme-font', '1');
          document.head.appendChild(link);
        }
      }
      var el = document.getElementById('mobius-theme');
      if (!el) {
        el = document.createElement('style');
        el.id = 'mobius-theme';
        document.head.appendChild(el);
      }
      el.textContent = css;
    }
    root.style.setProperty('--bg', bg);
    root.setAttribute('data-theme', mode);
    var m = document.querySelector('meta[name="theme-color"]');
    if (m) m.setAttribute('content', bg);
    root.style.colorScheme = mode;
    // Persist (TOP-LEVEL SHELL ONLY) so a frame's pre-paint can read the
    // correct {bg,mode}. The same-origin app-frame shares this localStorage but
    // has an EMPTY slot, so it must NOT write — else it clobbers the owner's
    // real theme with the dark default and the shell re-reads it (drawer bleed).
    try {
      if (window.parent === window) {
        localStorage.setItem('mobius-theme', JSON.stringify({ bg: bg, mode: mode }));
        localStorage.setItem('mobius-theme-bg', bg);
      }
    } catch (e) {}
  } catch (e) {}
})();`
