/**
 * Model constants shared between ChatSettingsPanel (the live per-chat picker)
 * and any future surface that needs the canonical model list.
 *
 * The ProviderModelPicker radio-list component that used to live here was
 * superseded by ChatSettingsPanel's stepper layout and is no longer rendered.
 * The data exports below are the live contract; the component code is gone.
 */

/** Known models per provider. Add here when a new model lands; the value
 *  is what gets passed to ClaudeAgentOptions(model=...) / Codex thread.start.
 *
 *  Anthropic switched to dateless pinned IDs starting with the 4.6 generation —
 *  `claude-opus-4-8` IS the pinned snapshot, no date suffix exists. Dated entries
 *  for older generations stay listed so existing chats that persisted them in
 *  agent_settings_json keep resolving (the API treats them as aliases). */
export const CLAUDE_MODELS = [
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
  // gpt-5.5 became the default Codex model on 2026-04-24. gpt-5.4
  // stays as fallback. -codex suffix variants (gpt-5.3-codex etc.)
  // require API-key auth; ChatGPT-account auth (Möbius's Codex
  // bridge) returns 400 for them — drop until per-auth model gating
  // is in place.
  { value: 'gpt-5.5', label: 'gpt-5.5' },
  { value: 'gpt-5.4', label: 'gpt-5.4' },
]
