/**
 * Static fallback model constants shared by picker surfaces before
 * `/api/models` resolves or when the live registry is unavailable.
 * The provider SDK/CLI registry is the source of truth once loaded.
 *
 * The ProviderModelPicker radio-list component that used to live here was
 * superseded by ChatSettingsPanel's stepper layout and is no longer rendered.
 * The component code is gone; only fallback rows remain.
 */

/** Fallback model IDs used only before `/api/models` resolves or when live
 *  discovery is unavailable. Providers own display names; raw IDs are the
 *  honest fallback instead of a second hand-maintained naming catalog.
 *
 *  Anthropic switched to dateless pinned IDs starting with the 4.6 generation —
 *  `claude-opus-4-8` IS the pinned snapshot, no date suffix exists. Dated entries
 *  for older generations stay listed so existing chats that persisted them in
 *  agent_settings_json keep resolving (the API treats them as aliases). */
const rawFallbackModels = (ids) => ids.map((value) => ({ value, label: value }))

export const CLAUDE_MODELS = rawFallbackModels([
  'claude-fable-5',
  'claude-sonnet-5',
  'claude-opus-4-8',
  'claude-opus-4-7',
  'claude-opus-4-6',
  'claude-opus-4-5-20251001',
  'claude-sonnet-4-6',
  'claude-sonnet-4-7-20251215',
  'claude-sonnet-4-5-20251001',
  'claude-haiku-4-5-20251001',
])

export const CODEX_MODELS = rawFallbackModels([
  // The live `/api/models` registry is the source of truth. These
  // fallback rows mirror the current Codex CLI catalog for first paint
  // and registry-failure cases.
  'gpt-5.6-sol',
  'gpt-5.6-terra',
  'gpt-5.6-luna',
  'gpt-5.5',
  'gpt-5.4',
  'gpt-5.4-mini',
  'gpt-5.3-codex-spark',
])
