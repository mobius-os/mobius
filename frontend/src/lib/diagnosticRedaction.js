const SECRET_QUERY_VALUE = /([?&](?:access_token|auth|authorization|code|key|password|secret|token)=)[^&#\s"'<>]+/gi
const AUTHORIZATION_VALUE = /\b(authorization\s*[:=]\s*)(?:bearer|basic)\s+[^\s,;]+/gi
const COOKIE_VALUE = /\b((?:set-)?cookie\s*[:=]\s*)[^\r\n]+/gi
const SECRET_ASSIGNMENT = /(["']?)\b((?:[a-z0-9]+[_-])*(?:access[_-]?key[_-]?id|secret[_-]?access[_-]?key|api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|auth[_-]?token|private[_-]?key|token|password|passwd|pwd|secret))\1(\s*[:=]\s*)("[^"\r\n]*"|'[^'\r\n]*'|[^\s,;&#]+)/gi
const JWT_VALUE = /\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g
const PRIVATE_KEY_BLOCK = /-----BEGIN [^-\r\n]*PRIVATE KEY-----[\s\S]*?-----END [^-\r\n]*PRIVATE KEY-----/g
const PROVIDER_TOKEN = /\b(?:A(?:KIA|SIA)[0-9A-Z]{16}|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}|sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{20,})\b/g
const HIGH_ENTROPY_VALUE = /\b[A-Za-z0-9+_-]{32,}={0,2}\b/g
const URL_CREDENTIALS = /([a-z][a-z0-9+.-]*:\/\/)[^\s/@:]+:[^\s/@]+@/gi

function highEntropyReplacement(token) {
  const value = token.replace(/=+$/, '')
  const counts = new Map()
  for (const character of value) {
    counts.set(character, (counts.get(character) || 0) + 1)
  }
  let entropy = 0
  for (const count of counts.values()) {
    const probability = count / value.length
    entropy -= probability * Math.log2(probability)
  }
  return entropy >= 4.5 ? '[redacted-high-entropy-value]' : token
}

export function redactDiagnosticText(value) {
  return String(value || '')
    .replace(PRIVATE_KEY_BLOCK, '[redacted-private-key]')
    .replace(SECRET_QUERY_VALUE, '$1[redacted]')
    .replace(AUTHORIZATION_VALUE, '$1[redacted]')
    .replace(COOKIE_VALUE, '$1[redacted]')
    .replace(SECRET_ASSIGNMENT, (_match, quote, key, separator, rawValue) => {
      const valueQuote = rawValue.startsWith('"') || rawValue.startsWith("'")
        ? rawValue[0]
        : ''
      return `${quote}${key}${quote}${separator}${valueQuote}[redacted]${valueQuote}`
    })
    .replace(JWT_VALUE, '[redacted-jwt]')
    .replace(URL_CREDENTIALS, '$1[redacted]@')
    .replace(PROVIDER_TOKEN, '[redacted-provider-token]')
    .replace(HIGH_ENTROPY_VALUE, highEntropyReplacement)
}
