/**
 * Normalize a FastAPI/HTTP error body's `detail` into a human-readable string.
 *
 * FastAPI returns `detail` in two shapes. A raised `HTTPException` gives
 * `{detail: "<string>"}`, but a request-validation failure (a Pydantic model
 * validator raising `ValueError`, e.g. a provider switch whose target effort
 * does not belong to the target provider) gives
 * `{detail: [{type, loc, msg, input, ctx}, ...]}` — an ARRAY of error objects.
 *
 * That array shape was being stored verbatim and rendered as a React child
 * (`<p>{error}</p>`), which throws React error #31 ("objects are not valid as
 * a React child") and takes down the whole ChatView/shell. Every boundary that
 * turns a response body into user-facing error text must funnel `detail`
 * through here so a validation array can never reach the DOM.
 *
 * @param {unknown} detail - the `detail` field of a parsed error body
 * @param {string} [fallback] - returned when `detail` yields no usable text
 * @returns {string} always a string, never an object or array
 */
export function detailToMessage(detail, fallback = '') {
  if (typeof detail === 'string') {
    return detail.trim() || fallback
  }

  // Pydantic/FastAPI validation error list: surface the human `msg` fields.
  if (Array.isArray(detail)) {
    const msgs = detail
      .map(item => (item && typeof item.msg === 'string' ? item.msg.trim() : ''))
      .filter(Boolean)
    return msgs.length ? msgs.join('; ') : fallback
  }

  // A single object — either one validation entry (`msg`) or a nested error
  // envelope (`message`/`detail`/`error`). Recurse the nested cases so a
  // `{detail: [...]}` wrapper is normalized too.
  if (detail && typeof detail === 'object') {
    if (typeof detail.msg === 'string' && detail.msg.trim()) {
      return detail.msg.trim()
    }
    for (const key of ['message', 'detail', 'error']) {
      const nested = detail[key]
      if (typeof nested === 'string' && nested.trim()) return nested.trim()
      if (nested && typeof nested === 'object') {
        const resolved = detailToMessage(nested, '')
        if (resolved) return resolved
      }
    }
    return fallback
  }

  return fallback
}
