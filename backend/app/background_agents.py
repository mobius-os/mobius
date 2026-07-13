"""Canonical resolution of which background AI agent a scheduled app runs with.

A "background agent" is a nightly cron app (Reflection, Memory/dreaming, News)
that drives a Claude/Codex turn, with a fallback provider for the nights the
primary is unavailable (usage limit, outage). Every one of them used to hand-roll
its own copy of this resolution in its runner script, and those copies drifted
into five divergent models across the platform repo and the per-app catalog
repos. This module is the ONE source of truth: the runners import
``resolve_background_agents`` instead of carrying their own copy, so the logic
can never diverge again even if a catalog runner file goes stale.

Two layers:

- **System** — the owner's Settings > background agents, in
  ``/data/shared/agent-settings.json`` under ``background_agents``: a
  ``providers`` list (one row per provider, ordered, with enabled flags) is the
  source of truth, with legacy ``primary``/``fallback`` dicts as a fallback.

- **Per-app override** — an app's own ``settings.json`` may pin its primary
  and/or its fallback source. TWO shapes are supported on purpose, because the
  apps have different Settings UIs (do not collapse to mode-only):
    * Explicit mode (Reflection, Memory): ``primary_agent_mode`` /
      ``secondary_agent_mode`` = ``"app"`` (use the app's choice) or ``"system"``
      (defer to the system default).
    * Presence (News): a bare ``{provider, model}`` / ``{fallback_*}`` with no
      mode — picking a provider IS the override. Exception: the bare default
      ``{"provider": "claude"}`` with no model/effort means "inherit the system
      default" (else it would drop the system primary's model to the SDK default).

A "choice" is ``{"provider", "model", "effort"}``; model/effort stay None when
unset so the provider SDK uses its own default (this is deliberately NOT
``providers.background_agent_settings``, which fills ``effort="medium"`` for the
Settings UI's display — the runner path wants the SDK default). This module is
stdlib + ``app.providers`` only (both stdlib-at-import), so a cron script with a
near-empty environment can import it after putting the backend root on sys.path.
"""

from __future__ import annotations

import logging

from app import providers

log = logging.getLogger(__name__)

DEFAULT_PROVIDER = providers.DEFAULT_PROVIDER
_PROVIDERS = ("claude", "codex")
_FALLBACK_KEYS = ("fallback_provider", "fallback_model", "fallback_effort")


def _clean_choice(raw: dict | None, *, fallback_provider: str | None = None,
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
    provider = fallback_provider if fallback_provider in _PROVIDERS else None
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


def _has_app_primary_override(app_settings: dict) -> bool:
  # Two settings shapes are BOTH live and supported — do not "simplify" this to
  # mode-only, it would break the second:
  #   1. Explicit mode (Reflection, Memory): their Settings UIs write
  #      primary_agent_mode "app"/"system" directly.
  #   2. Presence (News): its Settings UI writes a bare {provider, model} with NO
  #      mode — picking a provider IS the override. The heuristic below reads that.
  mode = app_settings.get("primary_agent_mode")
  if mode == "system":
    return False
  if mode == "app":
    return True
  # Presence path. EXCLUDE the default {"provider": "claude"} with no
  # model/effort: that shape means "inherit the system default" (e.g. the stale
  # reflection app-56 row), so treating it as an override would replace the
  # system primary's model with None — dropping opus-4-8 to the SDK default. A
  # real presence override names a non-default provider, or a model/effort.
  provider = app_settings.get("provider")
  model = app_settings.get("model")
  effort = app_settings.get("effort")
  if provider == DEFAULT_PROVIDER and not model and not effort:
    return False
  return bool(provider or model or effort)


def _system_choices(data_dir: str) -> list[dict]:
  """The ordered, de-duplicated system provider choices from Settings."""
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
                            fallback_provider=DEFAULT_PROVIDER, label="system primary")
    fallback = _clean_choice(background.get("fallback"), label="system fallback")
    if primary:
      choices.append(primary)
    if fallback and not _same_choice(primary, fallback):
      choices.append(fallback)

  if not choices:
    primary = _clean_choice(
      {"provider": DEFAULT_PROVIDER, "model": global_settings.get("model"),
       "effort": global_settings.get("effort")},
      fallback_provider=DEFAULT_PROVIDER, label="system default")
    if primary:
      choices.append(primary)

  if not choices:
    choices.append({"provider": DEFAULT_PROVIDER, "model": None, "effort": None})
  return choices


def resolve_background_agents(data_dir: str, app_settings: dict | None = None) -> dict:
  """Resolve ``{"primary", "fallback"}`` choices for a background-agent run.

  ``app_settings`` is the app's own ``settings.json`` (None/empty → system
  defaults only). ``fallback`` is None when there is no distinct second agent.
  """
  app = app_settings if isinstance(app_settings, dict) else {}

  choices = _system_choices(data_dir)
  primary = choices[0]
  fallback = choices[1] if len(choices) > 1 else None

  if _has_app_primary_override(app):
    app_primary = _clean_choice(
      {"provider": app.get("provider"), "model": app.get("model"), "effort": app.get("effort")},
      fallback_provider=(primary or {}).get("provider") or DEFAULT_PROVIDER,
      label="app primary",
    )
    if app_primary:
      primary = app_primary

  # Same two shapes as the primary override: explicit secondary_agent_mode
  # (Reflection/Memory), or presence of fallback_* fields with no mode (News).
  secondary_mode = app.get("secondary_agent_mode")
  if secondary_mode == "app" or (
    secondary_mode != "system" and any(app.get(k) for k in _FALLBACK_KEYS)
  ):
    fallback = _clean_choice(
      {"provider": app.get("fallback_provider"), "model": app.get("fallback_model"),
       "effort": app.get("fallback_effort")},
      label="app fallback",
    )

  if _same_choice(primary, fallback):
    fallback = None
  return {"primary": primary, "fallback": fallback}
