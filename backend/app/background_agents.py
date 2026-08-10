"""Canonical resolution of which background AI agent a scheduled app uses.

A "background agent" is a nightly cron app (Reflection, Memory/dreaming, News)
that drives a Claude/Codex turn, with a fallback provider for the nights the
primary is unavailable (usage limit, outage). The platform once carried several
copies of this resolution and they drifted. This module is the source of truth
for the system ordering and for the normalization every background agent shares.

Two layers:

- **System** — the owner's Settings > background agents, in
  ``/data/shared/agent-settings.json`` under ``background_agents``: a
  ``providers`` list (one row per provider, ordered, with enabled flags) is the
  source of truth, with legacy ``primary``/``fallback`` dicts as a fallback.

- **Caller override** — a background agent may declare its own pick in ONE
  uniform shape (see :func:`resolve_background_agents`). Each app owns the
  translation from its own settings screen into that shape, so this module
  knows no app's name, settings format, or UI conventions. Normalization stays
  here so every background agent cleans a choice identically: a per-app copy of
  that step is exactly what drifted before and must not grow back.

A "choice" is ``{"provider", "model", "effort"}``; model/effort stay None when
unset so the provider SDK uses its own default (this is deliberately NOT
``providers.background_agent_settings``, which fills ``effort="medium"`` for the
Settings UI's display — the runner path wants the SDK default).

This scheduled-agent policy is intentionally separate from the optional
Subagents app. That app governs explicit, bounded coding delegation from a live
chat, including its own enable switches and recursion limit; it is not a
dependency of Memory, Reflection, or another scheduled app.
"""

from __future__ import annotations

import logging

from app import providers

log = logging.getLogger(__name__)

DEFAULT_PROVIDER = providers.DEFAULT_PROVIDER
_PROVIDERS = ("claude", "codex")


def _clean_choice(raw: dict | None, *, default_provider: str | None = None,
                  label: str = "settings") -> dict | None:
  """Normalize one ``{provider, model, effort}`` choice, or None if unusable.

  Drops a model that clearly belongs to the other provider (a stale cross-
  provider pin) and honors an explicit ``enabled: false``. model/effort are left
  None when unset — the SDK then uses its own default.
  """
  if not isinstance(raw, dict):
    return None
  if raw.get("enabled") is False:
    return None
  provider = raw.get("provider")
  if provider not in _PROVIDERS:
    provider = default_provider if default_provider in _PROVIDERS else None
  if provider not in _PROVIDERS:
    return None
  model = raw.get("model")
  model = model.strip() if isinstance(model, str) and model.strip() else None
  if model and providers._model_belongs_to_other_provider(model, provider):
    log.info("%s model %r mismatches provider %r; dropping", label, model, provider)
    model = None
  effort = raw.get("effort")
  effort = effort.strip() if isinstance(effort, str) and effort.strip() else None
  return {"provider": provider, "model": model, "effort": effort}


def _same_choice(a: dict | None, b: dict | None) -> bool:
  if not a or not b:
    return False
  return (
    a.get("provider") == b.get("provider")
    and (a.get("model") or None) == (b.get("model") or None)
    and (a.get("effort") or None) == (b.get("effort") or None)
  )


def _system_choices(data_dir: str) -> list[dict]:
  """The ordered, de-duplicated system provider choices from Settings."""
  default_provider = providers.resolve_default_provider(data_dir)
  global_settings = providers._load_agent_settings(data_dir)
  raw = global_settings.get("background_agents")
  background = raw if isinstance(raw, dict) else {}

  choices: list[dict] = []
  raw_choices = background.get("providers")
  if isinstance(raw_choices, list):
    for index, raw_choice in enumerate(raw_choices):
      choice = _clean_choice(raw_choice, label=f"system provider {index + 1}")
      if choice and not any(_same_choice(choice, existing) for existing in choices):
        choices.append(choice)

  if not choices:
    primary = _clean_choice(background.get("primary"),
                            default_provider=default_provider, label="system primary")
    fallback = _clean_choice(background.get("fallback"), label="system fallback")
    if primary:
      choices.append(primary)
    if fallback and not _same_choice(primary, fallback):
      choices.append(fallback)

  if not choices:
    primary = _clean_choice(
      {"provider": default_provider, "model": global_settings.get("model"),
       "effort": global_settings.get("effort")},
      default_provider=default_provider, label="system default")
    if primary:
      choices.append(primary)

  if not choices:
    choices.append({"provider": default_provider, "model": None, "effort": None})
  return choices


def resolve_background_agents(data_dir: str, override: dict | None = None) -> dict:
  """Resolve ``{"primary", "fallback"}`` choices for a background-agent run.

  ``override`` is the caller's own declaration, in one uniform shape shared by
  every background agent::

    {"primary":  {"provider", "model", "effort"} | None,
     "fallback": {"provider", "model", "effort"} | None}

  An ABSENT key means "no preference — inherit the system default". A PRESENT
  key owns that slot outright, so an explicit ``None`` fallback means "run
  without a second agent" rather than "inherit one". A present primary that
  normalizes to nothing usable falls back to the system primary rather than
  leaving the run with no agent at all.

  Each app translates its own settings screen into this shape; this module
  deliberately knows no app's name or settings format. ``fallback`` is None
  when there is no distinct second agent.
  """
  declared = override if isinstance(override, dict) else {}

  choices = _system_choices(data_dir)
  primary = choices[0]
  fallback = choices[1] if len(choices) > 1 else None

  if "primary" in declared:
    declared_primary = _clean_choice(
      declared.get("primary"),
      default_provider=(primary or {}).get("provider") or DEFAULT_PROVIDER,
      label="caller primary",
    )
    if declared_primary:
      primary = declared_primary

  if "fallback" in declared:
    fallback = _clean_choice(declared.get("fallback"), label="caller fallback")

  if _same_choice(primary, fallback):
    fallback = None
  return {"primary": primary, "fallback": fallback}
