/** Canonical render snapshots for detecting the first visible post-answer change. */

import { lastQuestionIndex } from './questionKey.js'

function stableSerialize(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value)
  if (Array.isArray(value)) {
    return `[${value.map(stableSerialize).join(',')}]`
  }
  return `{${Object.keys(value).sort().map(key => (
    `${JSON.stringify(key)}:${stableSerialize(value[key])}`
  )).join(',')}}`
}

function renderableQuestionItem(item) {
  if (item?.type !== 'question') return item
  const {
    answers: _answers,
    absorbedTool: _absorbedTool,
    absorbedToolUseId: _absorbedToolUseId,
    ...renderable
  } = item
  return renderable
}

/** Snapshot only state that can make the assistant surface visibly change.
 * Answer controls and the absorbed raw question-tool lifecycle are excluded:
 * they settle the submitted card but are not the agent's continuation. */
export function questionResponseActivitySnapshot(items) {
  const renderableItems = Array.isArray(items)
    ? items.map(renderableQuestionItem)
    : []
  return stableSerialize(renderableItems)
}

export function questionResponseActivityChanged(snapshot, items) {
  return typeof snapshot === 'string'
    && snapshot !== questionResponseActivitySnapshot(items)
}

/** Baseline snapshot for an answered question: the surface up to and INCLUDING
 * that question. Everything after it is the agent's continuation, so a later
 * commit that carries such content reads as response activity. Capturing the
 * baseline this way (rather than the whole current surface) is what lets a
 * reconnect snapshot that already contains post-answer text still be detected
 * — otherwise the baseline would include the continuation and the change would
 * never fire. Falls back to the full surface when the question is not found. */
export function questionResponseBaselineSnapshot(items, key) {
  const index = lastQuestionIndex(items, key)
  const baseline = index >= 0 && Array.isArray(items)
    ? items.slice(0, index + 1)
    : items
  return questionResponseActivitySnapshot(baseline)
}
