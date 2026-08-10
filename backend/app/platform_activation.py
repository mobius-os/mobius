"""Classify platform source changes by the deployment action they require.

This module is intentionally dependency-free.  The running updater imports it,
and ``scripts/test-image-fingerprint.sh`` executes its small CLI so image inputs
and owner-facing update semantics cannot grow separate path tables.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Literal, TypedDict


class ActivationLevel(str, Enum):
  """Ordered activation boundary for a changed platform path."""

  LIVE = "live"
  SERVER_RESTART = "server_restart"
  # In-place dependency install (pip) plus a server restart to load them. More
  # than a bare restart, but still completable in-product — no image rebuild.
  DEPENDENCY_SYNC = "dependency_sync"
  PROXY_RELOAD = "proxy_reload"
  CONTAINER_RECREATE = "container_recreate"
  IMAGE_REBUILD = "image_rebuild"
  HOST_MAINTENANCE = "host_maintenance"


LEVEL_ORDER = {
  level: index for index, level in enumerate(ActivationLevel)
}
DeploymentKind = Literal["railway", "self_hosted"]
DeploymentScope = Literal["all", "railway", "self_hosted"]


class ActivationReason(TypedDict):
  """One independently actionable reason within a platform update."""

  code: str
  summary: str
  paths: list[str]


class PlatformActivationImpact(TypedDict):
  """Owner-readable and machine-ordered activation result."""

  level: str
  deployment: DeploymentKind
  reasons: list[ActivationReason]
  guidance: list[str]


@dataclass(frozen=True)
class _Rule:
  code: str
  level: ActivationLevel
  summary: str
  exact: tuple[str, ...] = ()
  prefixes: tuple[str, ...] = ()
  deployment: DeploymentScope = "all"
  dependency_fingerprint: bool = False

  def matches(self, path: str) -> bool:
    return path in self.exact or any(path.startswith(prefix) for prefix in self.prefixes)


def _normalize_path(path: str) -> str:
  normalized = path.strip()
  return normalized[2:] if normalized.startswith("./") else normalized


# Ordered first-match rules.  Narrow baked/deployment inputs precede the broad
# backend runtime rule.  A path may require only one owning activation boundary;
# mixed updates still report every distinct rule they touch.
_RULES = (
  _Rule(
    "host_operator_tooling",
    ActivationLevel.HOST_MAINTENANCE,
    "Host-operated deployment tooling changed.",
    exact=("scripts/deploy-prod.sh",),
    deployment="self_hosted",
  ),
  _Rule(
    "container_image_definition",
    ActivationLevel.IMAGE_REBUILD,
    "The container image definition changed.",
    exact=("Dockerfile",),
    dependency_fingerprint=True,
  ),
  _Rule(
    "container_build_context",
    ActivationLevel.IMAGE_REBUILD,
    "The container build context changed.",
    exact=(".dockerignore",),
  ),
  _Rule(
    "python_dependencies",
    ActivationLevel.DEPENDENCY_SYNC,
    "Python dependencies changed; Apply installs them in place, then restart.",
    exact=("backend/requirements.txt", "backend/requirements.lock"),
    dependency_fingerprint=True,
  ),
  _Rule(
    "frontend_dependencies",
    ActivationLevel.IMAGE_REBUILD,
    "Frontend or app-compiler dependencies changed and need a new image.",
    exact=("frontend/package.json", "frontend/package-lock.json"),
    dependency_fingerprint=True,
  ),
  _Rule(
    "legacy_python_runtime",
    ActivationLevel.IMAGE_REBUILD,
    "The image-installed compatibility runtime changed.",
    prefixes=("backend/legacy_runtime/",),
    dependency_fingerprint=True,
  ),
  _Rule(
    "baked_runtime",
    ActivationLevel.IMAGE_REBUILD,
    "Baked scripts, supervisors, or protected-file rules changed.",
    exact=("protected-files.txt",),
    prefixes=(
      "backend/scripts/",
      "backend/runtime/",
      "backend/static/",
    ),
  ),
  _Rule(
    "self_hosted_topology",
    ActivationLevel.CONTAINER_RECREATE,
    "Self-hosted container topology or runtime configuration changed.",
    exact=("docker-compose.yml", "docker-compose.prod.yml"),
    deployment="self_hosted",
  ),
  _Rule(
    "railway_topology",
    ActivationLevel.CONTAINER_RECREATE,
    "Railway build or deployment configuration changed.",
    exact=("railway.toml",),
    deployment="railway",
  ),
  _Rule(
    "self_hosted_proxy",
    ActivationLevel.PROXY_RELOAD,
    "The self-hosted Caddy routing or TLS policy changed.",
    exact=("Caddyfile",),
    deployment="self_hosted",
  ),
  _Rule(
    "backend_development_source",
    ActivationLevel.LIVE,
    "Backend tests and evaluation tooling do not alter the running server.",
    exact=(
      "backend/pyproject.toml",
      "backend/requirements-static.txt",
      "backend/test_app_fixtures.py",
    ),
    prefixes=("backend/tests/", "backend/memeval/"),
  ),
  _Rule(
    "server_runtime",
    ActivationLevel.SERVER_RESTART,
    "Server runtime code or the cached agent constitution changed.",
    exact=("backend", "backend/app", "skill/core.md"),
    prefixes=("backend/",),
  ),
)


def deployment_kind(environ: dict[str, str] | None = None) -> DeploymentKind:
  """Detect Railway without inventing a generic provider-control contract."""
  env = os.environ if environ is None else environ
  railway_markers = (
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_DEPLOYMENT_ID",
    "RAILWAY_PUBLIC_DOMAIN",
  )
  on_railway = any((env.get(key) or "").strip() for key in railway_markers)
  return "railway" if on_railway else "self_hosted"


def _rule_for_path(path: str) -> _Rule | None:
  normalized = _normalize_path(path)
  return next((rule for rule in _RULES if rule.matches(normalized)), None)


def backend_import_probe_required(paths: Iterable[str]) -> bool:
  """Whether changed source can alter imports in a fresh FastAPI process."""
  for path in paths:
    normalized = _normalize_path(path)
    rule = _rule_for_path(normalized)
    if (
      rule
      and rule.level in (
        ActivationLevel.SERVER_RESTART,
        ActivationLevel.DEPENDENCY_SYNC,
      )
      and normalized != "skill/core.md"
    ):
      return True
  return False


def _applies(rule: _Rule, deployment: DeploymentKind) -> bool:
  return rule.deployment == "all" or rule.deployment == deployment


def _guidance(level: ActivationLevel, deployment: DeploymentKind) -> str:
  if level is ActivationLevel.LIVE:
    return "No deployment action is required; live source is rebuilt or read on demand."
  if level is ActivationLevel.SERVER_RESTART:
    return "Restart Möbius after Apply so the running server loads the new source."
  if level is ActivationLevel.DEPENDENCY_SYNC:
    return (
      "Apply installs the new Python dependencies in place, then restart to load "
      "them — no image rebuild needed."
    )
  if deployment == "railway":
    if level is ActivationLevel.CONTAINER_RECREATE:
      return (
        "Trigger a Railway deployment; an in-product restart cannot apply "
        "deployment configuration."
      )
    if level is ActivationLevel.IMAGE_REBUILD:
      return (
        "Trigger a Railway image rebuild and deployment; Restart cannot "
        "install dependencies or baked tools."
      )
  else:
    if level is ActivationLevel.PROXY_RELOAD:
      return (
        "Refresh the host checkout, then reload Caddy; restarting Möbius "
        "does not reload the proxy."
      )
    if level is ActivationLevel.CONTAINER_RECREATE:
      return "Refresh the host checkout, then recreate the affected Docker Compose services."
    if level is ActivationLevel.IMAGE_REBUILD:
      return "Refresh the host checkout, rebuild the image, then recreate the app container."
    if level is ActivationLevel.HOST_MAINTENANCE:
      return "Update the host-operated tooling and complete its maintenance outside the container."
  return "Complete this deployment action outside Möbius; an in-product restart is insufficient."


def classify_activation(
  paths: Iterable[str], *, deployment: DeploymentKind | None = None,
) -> PlatformActivationImpact:
  """Classify the actions that apply to this installation."""
  active_deployment = deployment or deployment_kind()
  grouped: dict[str, tuple[_Rule | None, list[str]]] = {}
  for raw_path in paths:
    path = _normalize_path(raw_path)
    if not path:
      continue
    rule = _rule_for_path(path)
    key = rule.code if rule else "live_source"
    if key not in grouped:
      grouped[key] = (rule, [])
    grouped[key][1].append(path)

  reasons: list[ActivationReason] = []
  action_levels: set[ActivationLevel] = set()
  for code, (rule, grouped_paths) in grouped.items():
    level = rule.level if rule else ActivationLevel.LIVE
    if rule is not None and not _applies(rule, active_deployment):
      continue
    if level is not ActivationLevel.LIVE:
      action_levels.add(level)
    reasons.append(ActivationReason(
      code=code,
      summary=(
        rule.summary
        if rule
        else "These files are rebuilt, read on demand, or do not affect the running installation."
      ),
      paths=sorted(set(grouped_paths)),
    ))

  required_level = max(
    action_levels,
    default=ActivationLevel.LIVE,
    key=LEVEL_ORDER.get,
  )
  ordered_actions = sorted(action_levels, key=LEVEL_ORDER.get)
  guidance = [_guidance(level, active_deployment) for level in ordered_actions]
  if not guidance:
    guidance = [_guidance(ActivationLevel.LIVE, active_deployment)]

  return PlatformActivationImpact(
    level=required_level.value,
    deployment=active_deployment,
    reasons=reasons,
    guidance=guidance,
  )


def dependency_fingerprint_paths(root: Path) -> list[str]:
  """Enumerate the image dependency inputs declared by the classifier."""
  paths: set[str] = set()
  for rule in _RULES:
    if not rule.dependency_fingerprint:
      continue
    paths.update(rule.exact)
    for prefix in rule.prefixes:
      base = root / prefix
      if base.is_file():
        paths.add(prefix.rstrip("/"))
      elif base.is_dir():
        paths.update(
          str(path.relative_to(root))
          for path in base.rglob("*")
          if path.is_file()
        )
  return sorted(paths)


def _main() -> int:
  parser = argparse.ArgumentParser()
  parser.add_argument("root", type=Path)
  args = parser.parse_args()
  for path in dependency_fingerprint_paths(args.root.resolve()):
    print(path)
  return 0


if __name__ == "__main__":
  raise SystemExit(_main())
