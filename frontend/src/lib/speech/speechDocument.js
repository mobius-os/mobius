const MAX_SEGMENTS = 512
const MAX_HINTS = 100
const MAX_PAUSE_MS = 5_000
const MAX_LOCALE_CHARS = 35
const MAX_HINT_WRITTEN_CHARS = 160
const MAX_HINT_SPOKEN_CHARS = 240
const MAX_KIND_CHARS = 40
const MAX_RAW_INLINE_CHARS = 1_024
const DEFAULT_MAX_TEXT_CHARS = 50_000
// One generation must stay short enough to emit a clean end-of-speech. The
// reference pocket_tts pipeline chunks text to ~50 tokens per generation and
// sub-splits any oversized sentence on comma/semicolon/colon boundaries "to
// prevent skipped words". Beyond that length the decoder stops terminating
// cleanly and sustains a resonant tail. ~50 tokens is roughly 200 English
// characters; we chunk at that width. (The worker also hard-caps each
// generation at (tokens/3 + 2) seconds as a backstop, mirroring the reference.)
const MAX_SPEAKABLE_CHARS = 200

// Break a single over-long sentence at its softest interior punctuation
// (comma, semicolon, colon, dash) so no generation runs past the cap. This is
// the reference's guard against long comma-heavy sentences — the case that
// resonates when kept whole. Falls back to a whole-word wrap if a clause is
// still too long, and only ever cuts mid-word as a last resort.
function splitClause(sentence, maxChars) {
  if (sentence.length <= maxChars) return [sentence]
  const clauses = sentence.match(/[^,;:—–-]+(?:[,;:—–-]+|$)/g) || [sentence]
  const out = []
  let current = ''
  for (const clause of clauses) {
    const piece = clause.trim()
    if (!piece) continue
    if (current && current.length + 1 + piece.length > maxChars) {
      out.push(current)
      current = piece
    } else {
      current = current ? `${current} ${piece}` : piece
    }
  }
  if (current) out.push(current)
  // A clause with no interior punctuation can still exceed the cap; wrap it on
  // word boundaries so a runaway string never reaches the decoder whole.
  const wrapped = []
  for (const clause of out) {
    if (clause.length <= maxChars) { wrapped.push(clause); continue }
    let line = ''
    for (const word of clause.split(/\s+/)) {
      if (line && line.length + 1 + word.length > maxChars) {
        wrapped.push(line)
        line = word
      } else {
        line = line ? `${line} ${word}` : word
      }
    }
    if (line) wrapped.push(line)
  }
  return wrapped.length ? wrapped : [sentence]
}

// Split at sentence boundaries, sub-split any single sentence longer than the
// cap on its interior punctuation, then greedily pack the resulting pieces up
// to the cap so a long paragraph becomes a few short generations. Abbreviation
// periods (U.S., Mr.) may over-split, which only makes a generation shorter —
// always safe; under-splitting is what resonates. Never returns an empty list.
function splitSpeakable(text, maxChars = MAX_SPEAKABLE_CHARS) {
  if (text.length <= maxChars) return [text]
  const sentences = text.match(/[^.!?]+(?:[.!?]+|$)/g) || [text]
  const chunks = []
  let current = ''
  for (const sentence of sentences) {
    const trimmed = sentence.trim()
    if (!trimmed) continue
    for (const piece of splitClause(trimmed, maxChars)) {
      if (current && current.length + 1 + piece.length > maxChars) {
        chunks.push(current)
        current = piece
      } else {
        current = current ? `${current} ${piece}` : piece
      }
    }
  }
  if (current) chunks.push(current)
  return chunks.length ? chunks : [text]
}

function invalid(message) {
  const error = new TypeError(message)
  error.code = 'invalid_request'
  return error
}

function cleanInline(value) {
  return String(value || '').replace(/\s+/g, ' ').trim()
}

export function sanitizeSpeechHints(value) {
  if (!Array.isArray(value)) return []
  const result = []
  const seen = new Set()
  for (const item of value) {
    if (result.length >= MAX_HINTS) break
    if (!item || typeof item !== 'object') continue
    if (typeof item.written !== 'string' || typeof item.spoken !== 'string') continue
    if (
      item.written.length > MAX_HINT_WRITTEN_CHARS
      || item.spoken.length > MAX_HINT_SPOKEN_CHARS
    ) continue
    const written = cleanInline(item.written)
    const spoken = cleanInline(item.spoken)
    if (!written || !spoken || written === spoken || seen.has(written)) continue
    seen.add(written)
    result.push({ written, spoken })
  }
  return result
}

function replaceSpeechHint(text, pattern, spoken, maxTextChars) {
  let outputLength = text.length
  let changed = false

  for (const match of text.matchAll(pattern)) {
    changed = true
    outputLength += spoken.length - match[0].length
    if (outputLength > maxTextChars) {
      throw invalid(`Speech text cannot exceed ${maxTextChars.toLocaleString()} characters.`)
    }
  }
  return changed ? text.replace(pattern, () => spoken) : text
}

export function applySpeechHints(value, hints, maxTextChars = DEFAULT_MAX_TEXT_CHARS) {
  if (typeof value === 'string' && value.length > maxTextChars) {
    throw invalid(`Speech text cannot exceed ${maxTextChars.toLocaleString()} characters.`)
  }
  let text = cleanInline(value)
  const ordered = sanitizeSpeechHints(hints).sort((a, b) => b.written.length - a.written.length)
  for (const { written, spoken } of ordered) {
    const escaped = written.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const firstWord = /[\p{L}\p{N}_]/u.test(written[0])
    const lastWord = /[\p{L}\p{N}_]/u.test(written.at(-1))
    const pattern = `${firstWord ? '(?<![\\p{L}\\p{N}_])' : ''}${escaped}${lastWord ? '(?![\\p{L}\\p{N}_])' : ''}`
    text = replaceSpeechHint(text, new RegExp(pattern, 'gu'), spoken, maxTextChars)
  }
  return text
}

export function normalizeSpeechInput(input, maxTextChars = DEFAULT_MAX_TEXT_CHARS) {
  if (!input || typeof input !== 'object') throw invalid('Speech input is required.')
  if (input.document == null) {
    if (typeof input.text !== 'string') throw invalid('Speech text is required.')
    if (input.text.length > maxTextChars) {
      throw invalid(`Speech text cannot exceed ${maxTextChars.toLocaleString()} characters.`)
    }
    const text = cleanInline(input.text)
    if (!text) throw invalid('Speech text cannot be empty.')
    return {
      version: 1,
      locale: '',
      hints: [],
      segments: splitSpeakable(text).map((piece) => ({
        text: piece, kind: 'paragraph', pauseAfterMs: 0,
      })),
    }
  }
  const document = input.document
  if (!document || typeof document !== 'object' || Array.isArray(document) || document.version !== 1) {
    throw invalid('Speech Document version 1 is required.')
  }
  if (Object.hasOwn(document, 'locale') && typeof document.locale !== 'string') {
    throw invalid('Speech Document locale must be a string.')
  }
  if ((document.locale || '').length > MAX_RAW_INLINE_CHARS) {
    throw invalid(`Speech Document locale cannot exceed ${MAX_RAW_INLINE_CHARS.toLocaleString()} characters.`)
  }
  if (Object.hasOwn(document, 'hints') && !Array.isArray(document.hints)) {
    throw invalid('Speech Document hints must be an array.')
  }
  if ((document.hints || []).length > MAX_HINTS) {
    throw invalid(`Speech Document cannot exceed ${MAX_HINTS} hints.`)
  }
  for (const hint of document.hints || []) {
    if (
      !hint
      || typeof hint !== 'object'
      || Array.isArray(hint)
      || typeof hint.written !== 'string'
      || typeof hint.spoken !== 'string'
    ) {
      throw invalid('Speech Document hints require string written and spoken values.')
    }
    if (hint.written.length > MAX_HINT_WRITTEN_CHARS) {
      throw invalid(`Speech Document hint written cannot exceed ${MAX_HINT_WRITTEN_CHARS} characters.`)
    }
    if (hint.spoken.length > MAX_HINT_SPOKEN_CHARS) {
      throw invalid(`Speech Document hint spoken cannot exceed ${MAX_HINT_SPOKEN_CHARS} characters.`)
    }
  }
  if (!Array.isArray(document.segments) || !document.segments.length) {
    throw invalid('Speech Document segments are required.')
  }
  if (document.segments.length > MAX_SEGMENTS) {
    throw invalid(`Speech Document cannot exceed ${MAX_SEGMENTS} segments.`)
  }
  let rawTextTotal = 0
  for (const raw of document.segments) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      throw invalid('Each Speech Document segment must be an object.')
    }
    if (typeof raw.text !== 'string') {
      throw invalid('Speech Document segment text must be a string.')
    }
    if (Object.hasOwn(raw, 'kind') && typeof raw.kind !== 'string') {
      throw invalid('Speech Document segment kind must be a string.')
    }
    if ((raw.kind || '').length > MAX_RAW_INLINE_CHARS) {
      throw invalid(`Speech Document segment kind cannot exceed ${MAX_RAW_INLINE_CHARS.toLocaleString()} characters.`)
    }
    if (
      Object.hasOwn(raw, 'pauseAfterMs')
      && (typeof raw.pauseAfterMs !== 'number' || !Number.isFinite(raw.pauseAfterMs))
    ) {
      throw invalid('Speech Document segment pauseAfterMs must be a finite number.')
    }
    if (raw.text.length > maxTextChars || rawTextTotal > maxTextChars - raw.text.length) {
      throw invalid(`Speech text cannot exceed ${maxTextChars.toLocaleString()} characters.`)
    }
    rawTextTotal += raw.text.length
  }

  const hints = sanitizeSpeechHints(document.hints)
  const segments = []
  let total = 0
  for (const raw of document.segments) {
    const text = applySpeechHints(raw.text, hints, maxTextChars)
    if (!text) continue
    total += text.length
    if (total > maxTextChars) throw invalid(`Speech text cannot exceed ${maxTextChars.toLocaleString()} characters.`)
    const pause = Number(raw.pauseAfterMs)
    const kind = cleanInline(raw.kind).slice(0, MAX_KIND_CHARS) || 'paragraph'
    const pauseAfterMs = Number.isFinite(pause)
      ? Math.max(0, Math.min(MAX_PAUSE_MS, Math.round(pause)))
      : 0
    // A long block becomes several sentence-sized generations. The block's
    // editorial pause belongs only after its final piece; the pieces before it
    // run back-to-back, separated by their own natural sentence-final silence.
    const pieces = splitSpeakable(text)
    pieces.forEach((piece, index) => {
      segments.push({
        text: piece,
        kind,
        pauseAfterMs: index === pieces.length - 1 ? pauseAfterMs : 0,
      })
    })
  }
  if (!segments.length) throw invalid('Speech Document has no readable segments.')
  return {
    version: 1,
    locale: cleanInline(document.locale).slice(0, MAX_LOCALE_CHARS),
    hints,
    segments,
  }
}
