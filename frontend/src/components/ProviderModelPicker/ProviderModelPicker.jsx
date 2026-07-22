/**
 * Static fallback model constants shared by picker surfaces before
 * `/api/models` resolves or when the live registry is unavailable.
 * The provider SDK/CLI registry is the source of truth once loaded.
 *
 * The ProviderModelPicker radio-list component that used to live here was
 * superseded by ChatSettingsPanel's stepper layout and is no longer rendered.
 * The component code is gone; only fallback rows remain.
 */

/** Fallback models per provider. Add here when a new default fallback lands; the value
 *  is what gets passed to ClaudeAgentOptions(model=...) / Codex thread.start.
 *
 *  Anthropic switched to dateless pinned IDs starting with the 4.6 generation —
 *  `claude-opus-4-8` IS the pinned snapshot, no date suffix exists. Dated entries
 *  for older generations stay listed so existing chats that persisted them in
 *  agent_settings_json keep resolving (the API treats them as aliases). */
export const CLAUDE_MODELS = [
  { value: 'claude-fable-5', label: 'Fable 5' },
  { value: 'claude-sonnet-5', label: 'Sonnet 5' },
  { value: 'claude-opus-4-8', label: 'Opus 4.8' },
  { value: 'claude-opus-4-7', label: 'Opus 4.7' },
  { value: 'claude-opus-4-6', label: 'Opus 4.6' },
  { value: 'claude-opus-4-5-20251001', label: 'Opus 4.5' },
  { value: 'claude-sonnet-4-6', label: 'Sonnet 4.6' },
  { value: 'claude-sonnet-4-7-20251215', label: 'Sonnet 4.7' },
  { value: 'claude-sonnet-4-5-20251001', label: 'Sonnet 4.5' },
  { value: 'claude-haiku-4-5-20251001', label: 'Haiku 4.5' },
]

export const CODEX_MODELS = [
  // The live `/api/models` registry is the source of truth. These
  // fallback rows mirror the current Codex CLI catalog for first paint
  // and registry-failure cases.
  { value: 'gpt-5.6-sol', label: 'GPT-5.6 Sol' },
  { value: 'gpt-5.6-terra', label: 'GPT-5.6 Terra' },
  { value: 'gpt-5.6-luna', label: 'GPT-5.6 Luna' },
  { value: 'gpt-5.5', label: 'gpt-5.5' },
  { value: 'gpt-5.4', label: 'gpt-5.4' },
  { value: 'gpt-5.4-mini', label: 'gpt-5.4 mini' },
  { value: 'gpt-5.3-codex-spark', label: 'GPT-5.3 Codex Spark' },
]
