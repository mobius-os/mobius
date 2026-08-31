"""Dependency-free manifest contract shared by install and preflight."""

from collections.abc import Mapping
from urllib.parse import unquote, urlparse
import re

REQUIRED_STRING_FIELDS = ("id", "name", "version", "description", "entry")
# Capability permissions this Möbius build recognizes. An app MAY list any of
# these in `requires` to demand the platform actually provides them; requiring a
# name absent here fails the install loudly instead of granting nothing in
# silence. Extending a capability (e.g. the identity bridge) adds its name here.
RECOGNIZED_CAPABILITIES = (
  "manage_apps",
  "manage_skills",
  "github_access",
  "github_connect",
  "filesystem_access",
  "connections_manage",
  "connect_manage",
  "identity_manage",
  "railway_manage",
)
SOURCE_FILES_COUNT_MAX = 50
SKILLS_COUNT_MAX = 5
MANIFEST_MAX_BYTES = 64 * 1024
ENTRY_MAX_BYTES = 1024 * 1024
SEED_MAX_BYTES = 4 * 1024 * 1024
SEEDS_COUNT_MAX = 64
SEEDS_TOTAL_MAX = 32 * 1024 * 1024
STATIC_ASSET_MAX_BYTES = 16 * 1024 * 1024
STATIC_ASSETS_COUNT_MAX = 256
STATIC_ASSETS_TOTAL_MAX = 64 * 1024 * 1024
SOURCE_FILES_TOTAL_MAX = 8 * 1024 * 1024
ICON_MAX_BYTES = 12 * 1024 * 1024
SKILL_MAX_BYTES = 256 * 1024
SYSTEM_PROMPT_MAX_BYTES = 256 * 1024
PROJECT_TEMPLATES_COUNT_MAX = 12
PROJECT_TEMPLATE_FILES_COUNT_MAX = 64
PROJECT_ARTIFACT_TYPES_COUNT_MAX = 12
PROJECT_ARTIFACT_EXTENSIONS_COUNT_MAX = 16

_SLUG_OK = "abcdefghijklmnopqrstuvwxyz0123456789-_"
_SOURCE_FILES_MANAGED_PREFIXES = (
  "static/", "dist/", ".build/", "node_modules/",
)
_SOURCE_FILES_MANAGED_EXACT = frozenset((
  "index.jsx", ".gitignore", "init-cron.sh", ".mobius-static-assets.json",
))
_CRON_FIELD_OK = re.compile(r"^[\d\*/,\- ]+$")
_SKILL_FILENAME_OK = re.compile(r"^[a-z0-9][a-z0-9._-]*\.md$")


class ManifestContractError(ValueError):
  pass


def _fail(message: str) -> None:
  raise ManifestContractError(message)


def validate_slug_field(value, field: str) -> None:
  if not isinstance(value, str) or not value:
    _fail(f"Manifest `{field}` must be a non-empty string.")
  if any(ch not in _SLUG_OK for ch in value):
    _fail(
      f"Manifest `{field}` {value!r} contains invalid chars "
      "(allow a-z, 0-9, -, _)."
    )
  if value[0] in "-_":
    _fail(f"Manifest `{field}` must not start with '-' or '_', got {value!r}")
  if value.isdigit():
    _fail(
      f"Manifest `{field}` {value!r} must not be purely numeric — bare "
      "integers are reserved for the per-app storage path /data/apps/<id>."
    )


def validate_repo_relative_path(path: str, field: str) -> None:
  seed_hint = (
    " For storage_seeds, a string value is a repo-relative path that the"
    " installer fetches, not inline content. To seed literal text, put it in"
    " a repo file and point this key at that path; to store an inline JSON"
    " value, use a non-string (object/array/number/bool/null)."
  ) if field.startswith("storage_seeds.") else ""
  if not isinstance(path, str) or not path:
    _fail(f"Manifest `{field}` must be a non-empty string.{seed_hint}")
  parsed = urlparse(path)
  if (
    parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
    or path.startswith("/") or "\\" in path
  ):
    _fail(
      f"Manifest `{field}` must be a relative path inside the app repo."
      f"{seed_hint}"
    )
  parts = [unquote(part) for part in path.split("/")]
  if any(part in ("", ".", "..") for part in parts):
    _fail(
      f"Manifest `{field}` must not contain empty, '.', or '..' segments."
      f"{seed_hint}"
    )
  if any("/" in part or "\\" in part for part in parts):
    _fail(
      f"Manifest `{field}` must not contain encoded path separators."
      f"{seed_hint}"
    )


def validate_storage_destination(path: str) -> None:
  """Validate a manifest storage destination using the runtime path rules."""
  if not isinstance(path, str) or not path:
    _fail("Manifest `storage_seeds` keys must be paths.")
  if ".." in path or path.startswith("/"):
    _fail(f"Invalid storage path: {path}")
  for character in path:
    if not (character.isalnum() or character in "._-/"):
      _fail(f"Invalid storage path char: {path}")


def validate_cron_expr(expr: str) -> None:
  if not isinstance(expr, str):
    _fail("schedule.default must be a string.")
  if not expr or expr[0] == "-":
    _fail(f"schedule.default must not be empty or start with '-': {expr!r}")
  if not _CRON_FIELD_OK.match(expr):
    _fail(
      f"schedule.default contains disallowed characters: {expr!r}. "
      "Allowed: digits, *, /, ,, -, whitespace."
    )
  if len(expr.split()) != 5:
    _fail(f"schedule.default must have exactly 5 cron fields, got {expr!r}")


def validate_manifest_offline(offline) -> None:
  if offline is None:
    return
  if not isinstance(offline, Mapping):
    _fail("Manifest `offline` must be an object.")
  if "reads" in offline and not isinstance(offline["reads"], bool):
    _fail("Manifest `offline.reads` must be a boolean.")
  if "writes" in offline and offline["writes"] not in ("queued", "none"):
    _fail("Manifest `offline.writes` must be one of queued/none.")
  if (
    "execution" in offline
    and offline["execution"] not in ("full", "partial", "none")
  ):
    _fail("Manifest `offline.execution` must be one of full/partial/none.")
  precache = offline.get("precache")
  if precache is not None:
    if not isinstance(precache, list):
      _fail("Manifest `offline.precache` must be an array.")
    for index, path in enumerate(precache):
      validate_repo_relative_path(path, f"offline.precache[{index}]")


def static_asset_entries(value) -> dict[str, str]:
  if not value:
    return {}
  if isinstance(value, list):
    entries = {}
    for index, path in enumerate(value):
      if not isinstance(path, str):
        _fail(f"Manifest `static_assets[{index}]` must be a path.")
      entries[path] = path
    return entries
  if isinstance(value, Mapping):
    entries = {}
    for dest, src in value.items():
      if not isinstance(dest, str) or not isinstance(src, str):
        _fail("Manifest `static_assets` entries must map paths to paths.")
      entries[dest] = src
    return entries
  _fail("Manifest `static_assets` must be an object or array.")


def validate_manifest_contract(manifest) -> None:
  """Validate every manifest shape/path rule enforced before installation."""
  if not isinstance(manifest, Mapping):
    _fail("Manifest must be a JSON object.")
  invalid_required = [
    field for field in REQUIRED_STRING_FIELDS
    if not isinstance(manifest.get(field), str) or not manifest[field].strip()
  ]
  if invalid_required:
    _fail(
      "Manifest required fields must be non-empty strings: "
      + ", ".join(invalid_required)
      + "."
    )

  mid = manifest["id"]
  validate_slug_field(mid, "id")
  previous_id = manifest.get("previous_id")
  if previous_id is not None:
    validate_slug_field(previous_id, "previous_id")
    if previous_id == mid:
      _fail("Manifest `previous_id` must differ from `id`.")

  validate_repo_relative_path(manifest["entry"], "entry")
  if manifest["entry"] != "index.jsx":
    _fail(
      "Manifest `entry` must be `index.jsx`; the editor and explicit "
      "app-apply lifecycle use that canonical entrypoint."
    )
  if manifest.get("icon") is not None:
    validate_repo_relative_path(manifest["icon"], "icon")

  for field in ("offline_capable", "embeds_agent", "system_app"):
    if field in manifest and not isinstance(manifest[field], bool):
      _fail(f"Manifest `{field}` must be a boolean.")

  permissions = manifest.get("permissions", {})
  if not isinstance(permissions, Mapping):
    _fail("Manifest `permissions` must be an object.")
  for field in ("cross_app_access", "share_with_apps", "shared_memory"):
    if permissions.get(field, "none") not in ("none", "read", "write"):
      _fail(f"Manifest `permissions.{field}` must be one of none/read/write.")
  if permissions.get("chat_log_access", "none") not in (
    "none", "summary", "summary_with_deleted",
  ):
    _fail(
      "Manifest `permissions.chat_log_access` must be one of "
      "none/summary/summary_with_deleted."
    )
  removed_job_permissions = {
    "background_agent", "job_authority",
  }.intersection(permissions)
  if removed_job_permissions:
    names = ", ".join(
      f"`permissions.{name}`" for name in sorted(removed_job_permissions)
    )
    _fail(
      f"Manifest permission {names} has been removed; server-side app jobs "
      "run as ordinary Möbius processes."
    )
  for field in RECOGNIZED_CAPABILITIES:
    if field in permissions and not isinstance(permissions[field], bool):
      _fail(f"Manifest `permissions.{field}` must be a boolean.")

  project_templates = manifest.get("project_templates")
  if project_templates is not None:
    if not isinstance(project_templates, list):
      _fail("Manifest `project_templates` must be an array.")
    if len(project_templates) > PROJECT_TEMPLATES_COUNT_MAX:
      _fail(
        "Manifest has too many project_templates "
        f"(max {PROJECT_TEMPLATES_COUNT_MAX})."
      )
    seen_template_ids = set()
    for index, template in enumerate(project_templates):
      field = f"project_templates[{index}]"
      if not isinstance(template, Mapping):
        _fail(f"Manifest `{field}` must be an object.")
      template_id = template.get("id")
      validate_slug_field(template_id, f"{field}.id")
      if template_id in seen_template_ids:
        _fail(f"Manifest `{field}.id` duplicates {template_id!r}.")
      seen_template_ids.add(template_id)
      if not isinstance(template.get("name"), str) or not template["name"].strip():
        _fail(f"Manifest `{field}.name` must be a non-empty string.")
      for text_field in ("description", "guidance"):
        value = template.get(text_field)
        if value is not None and not isinstance(value, str):
          _fail(f"Manifest `{field}.{text_field}` must be a string.")
      for list_field in ("skills", "dependencies"):
        values = template.get(list_field, [])
        if not isinstance(values, list) or any(
          not isinstance(value, str) or not value.strip() for value in values
        ):
          _fail(f"Manifest `{field}.{list_field}` must be an array of strings.")
      previews = template.get("previews", [])
      if not isinstance(previews, list) or len(previews) > 8:
        _fail(f"Manifest `{field}.previews` must be an array with at most 8 entries.")
      seen_preview_ids = set()
      for preview_index, preview in enumerate(previews):
        preview_field = f"{field}.previews[{preview_index}]"
        if not isinstance(preview, Mapping):
          _fail(f"Manifest `{preview_field}` must be an object.")
        preview_id = preview.get("id")
        validate_slug_field(preview_id, f"{preview_field}.id")
        if preview_id in seen_preview_ids:
          _fail(f"Manifest `{preview_field}.id` duplicates {preview_id!r}.")
        seen_preview_ids.add(preview_id)
        if preview.get("kind") not in {"html", "pdf", "image"}:
          _fail(f"Manifest `{preview_field}.kind` must be html, pdf, or image.")
        if not isinstance(preview.get("name"), str) or not preview["name"].strip():
          _fail(f"Manifest `{preview_field}.name` must be a non-empty string.")
        validate_repo_relative_path(preview.get("path"), f"{preview_field}.path")
      actions = template.get("actions", [])
      if not isinstance(actions, list) or len(actions) > 8:
        _fail(f"Manifest `{field}.actions` must be an array with at most 8 entries.")
      seen_action_ids = set()
      for action_index, action in enumerate(actions):
        action_field = f"{field}.actions[{action_index}]"
        if not isinstance(action, Mapping):
          _fail(f"Manifest `{action_field}` must be an object.")
        action_id = action.get("id")
        validate_slug_field(action_id, f"{action_field}.id")
        if action_id in seen_action_ids:
          _fail(f"Manifest `{action_field}.id` duplicates {action_id!r}.")
        seen_action_ids.add(action_id)
        if not isinstance(action.get("name"), str) or not action["name"].strip():
          _fail(f"Manifest `{action_field}.name` must be a non-empty string.")
        prompt = action.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
          _fail(f"Manifest `{action_field}.prompt` must be 1-4000 characters.")
      files = template.get("files", {})
      if not isinstance(files, Mapping):
        _fail(f"Manifest `{field}.files` must be an object.")
      if len(files) > PROJECT_TEMPLATE_FILES_COUNT_MAX:
        _fail(
          f"Manifest `{field}.files` has too many entries "
          f"(max {PROJECT_TEMPLATE_FILES_COUNT_MAX})."
        )
      for destination, source in files.items():
        validate_repo_relative_path(destination, f"{field}.files.{destination}")
        validate_repo_relative_path(source, f"{field}.files.{destination}")

      artifact_types = template.get("artifact_types", [])
      if (
        not isinstance(artifact_types, list)
        or len(artifact_types) > PROJECT_ARTIFACT_TYPES_COUNT_MAX
      ):
        _fail(
          f"Manifest `{field}.artifact_types` must be an array with at most "
          f"{PROJECT_ARTIFACT_TYPES_COUNT_MAX} entries."
        )
      seen_artifact_type_ids = set()
      for artifact_index, artifact_type in enumerate(artifact_types):
        artifact_field = f"{field}.artifact_types[{artifact_index}]"
        if not isinstance(artifact_type, Mapping):
          _fail(f"Manifest `{artifact_field}` must be an object.")
        artifact_type_id = artifact_type.get("id")
        validate_slug_field(artifact_type_id, f"{artifact_field}.id")
        if artifact_type_id in seen_artifact_type_ids:
          _fail(f"Manifest `{artifact_field}.id` duplicates {artifact_type_id!r}.")
        seen_artifact_type_ids.add(artifact_type_id)
        if (
          not isinstance(artifact_type.get("name"), str)
          or not artifact_type["name"].strip()
        ):
          _fail(f"Manifest `{artifact_field}.name` must be a non-empty string.")
        extensions = artifact_type.get("extensions")
        if (
          not isinstance(extensions, list)
          or not extensions
          or len(extensions) > PROJECT_ARTIFACT_EXTENSIONS_COUNT_MAX
          or any(
            not isinstance(extension, str)
            or re.fullmatch(r"[a-z0-9]{1,16}", extension) is None
            for extension in extensions
          )
        ):
          _fail(
            f"Manifest `{artifact_field}.extensions` must be 1-"
            f"{PROJECT_ARTIFACT_EXTENSIONS_COUNT_MAX} lowercase file extensions."
          )
        if artifact_type.get("preview") not in {"html", "pdf", "image"}:
          _fail(f"Manifest `{artifact_field}.preview` must be html, pdf, or image.")
        script = artifact_type.get("script")
        validate_repo_relative_path(script, f"{artifact_field}.script")
        if not script.endswith(".sh"):
          _fail(
            f"Manifest `{artifact_field}.script` must be a .sh source file."
          )
        declared_sources = manifest.get("source_files")
        if not isinstance(declared_sources, list) or script not in declared_sources:
          _fail(
            f"Manifest `{artifact_field}.script` must also be listed in "
            "`source_files` so every install contains the reviewed builder."
          )
        output = artifact_type.get("output")
        if not isinstance(output, str) or not output or len(output) > 256:
          _fail(f"Manifest `{artifact_field}.output` must be a non-empty string.")
        placeholders = set(re.findall(r"\{([^{}]+)\}", output))
        if placeholders - {"source", "stem"}:
          _fail(
            f"Manifest `{artifact_field}.output` uses unsupported placeholders."
          )
        rendered_output = output.replace("{source}", "source/index.html").replace(
          "{stem}", "index",
        )
        validate_repo_relative_path(rendered_output, f"{artifact_field}.output")

  # An app declares the platform capabilities it cannot function without. A name
  # this build does not recognize means the running Möbius predates the feature
  # the app needs, so refuse the install loudly here rather than let it land in a
  # permanently broken state where the capability is silently ungranted.
  requires = manifest.get("requires", [])
  if not isinstance(requires, list) or not all(
    isinstance(name, str) for name in requires
  ):
    _fail("Manifest `requires` must be a list of capability names.")
  unmet = [name for name in requires if name not in RECOGNIZED_CAPABILITIES]
  if unmet:
    _fail(
      "This app requires capabilities this Möbius build does not provide: "
      + ", ".join(sorted(set(unmet)))
      + ". Update Möbius, then install."
    )

  # Runtime capabilities are normalized by the same canonical registry used
  # to build the owner-reviewable install contract. Keep a single definition
  # of names, versions, limits, and failure semantics.
  from app.app_capabilities import normalize_public_access
  try:
    normalize_public_access(dict(manifest))
  except ValueError as exc:
    _fail(str(exc))
  from app.app_capabilities import normalize_runtime_capabilities
  try:
    normalize_runtime_capabilities(dict(manifest))
  except ValueError as exc:
    _fail(str(exc))

  validate_manifest_offline(manifest.get("offline"))

  seeds = manifest.get("storage_seeds", {})
  if seeds is not None and not isinstance(seeds, Mapping):
    _fail("Manifest `storage_seeds` must be an object.")
  for sub, value in (seeds or {}).items():
    validate_storage_destination(sub)
    if isinstance(value, str):
      validate_repo_relative_path(value, f"storage_seeds.{sub}")

  static_assets = manifest.get("static_assets", {})
  for dest, src in static_asset_entries(static_assets).items():
    validate_repo_relative_path(dest, f"static_assets.{dest}")
    validate_repo_relative_path(src, f"static_assets.{dest}")

  source_files = manifest.get("source_files")
  if source_files is not None:
    if not isinstance(source_files, list):
      _fail("Manifest `source_files` must be an array.")
    if len(source_files) > SOURCE_FILES_COUNT_MAX:
      _fail(
        "Manifest has too many source_files "
        f"(max {SOURCE_FILES_COUNT_MAX})."
      )
    schedule = manifest.get("schedule")
    declared_job = schedule.get("job") if isinstance(schedule, Mapping) else None
    for index, path in enumerate(source_files):
      validate_repo_relative_path(path, f"source_files[{index}]")
      if (
        path in _SOURCE_FILES_MANAGED_EXACT
        or path == declared_job
        or path.endswith(".bak")
        or path[0].isdigit()
        or any(path.startswith(prefix) for prefix in _SOURCE_FILES_MANAGED_PREFIXES)
      ):
        _fail(
          f"Manifest `source_files[{index}]` {path!r} collides with an "
          "install-managed path (entry, .gitignore, static/, dist/, .build/, "
          "node_modules/, the cron/job scripts, .bak snapshots, or the "
          "numeric-id storage tree)."
        )

  skills = manifest.get("skills")
  if skills is not None:
    if not isinstance(skills, list):
      _fail("Manifest `skills` must be an array.")
    if len(skills) > SKILLS_COUNT_MAX:
      _fail(f"Manifest has too many skills (max {SKILLS_COUNT_MAX}).")
    root_sources = {
      path for path in (source_files or [])
      if isinstance(path, str) and "/" not in path
    }
    for index, path in enumerate(skills):
      if not isinstance(path, str) or _SKILL_FILENAME_OK.fullmatch(path) is None:
        _fail(
          f"Manifest `skills[{index}]` must match "
          "`^[a-z0-9][a-z0-9._-]*\\.md$`."
        )
      if path not in root_sources:
        _fail(
          f"Manifest `skills[{index}]` {path!r} must also be listed in "
          "`source_files` as a root-level file — the installer reads skill "
          "bytes from the installed source tree, so a skill that is not a "
          "source file has nothing to install."
        )

  system_prompt = manifest.get("system_prompt")
  if system_prompt is not None:
    if manifest.get("system_app") is not True:
      _fail(
        "Manifest `system_prompt` requires `system_app: true` so global "
        "agent-prompt authority is explicit and owner-reviewable."
      )
    if (
      not isinstance(system_prompt, str)
      or not system_prompt.endswith(".md")
      or system_prompt == ".md"
      or "/" in system_prompt
      or "\\" in system_prompt
      or ".." in system_prompt
      or system_prompt.startswith(".")
    ):
      _fail(
        "Manifest `system_prompt` must be a bare `<name>.md` filename — "
        "no directories, traversal, or dotfiles."
      )
    root_sources = {
      path for path in (source_files or [])
      if isinstance(path, str) and "/" not in path
    }
    if system_prompt not in root_sources:
      _fail(
        "Manifest `system_prompt` must also be listed in `source_files` as "
        "a root-level file."
      )

  schedule = manifest.get("schedule")
  if schedule is not None:
    if not isinstance(schedule, Mapping):
      _fail("Manifest `schedule` must be an object.")
    expression = schedule.get("default")
    if expression is not None:
      validate_cron_expr(expression)
    job = schedule.get("job")
    if job is not None and (
      not isinstance(job, str) or "/" in job or ".." in job
    ):
      _fail(
        "Manifest `schedule.job` must be a bare filename (no path "
        "separators): cron registration and the run-job endpoint both use "
        "only the basename, so a nested path would silently register/run a "
        "different file than the manifest names."
      )
    if job is not None:
      validate_repo_relative_path(job, "schedule.job")
    for field in ("user_configurable", "initialize_on_install"):
      if field in schedule and not isinstance(schedule[field], bool):
        _fail(f"Manifest `schedule.{field}` must be a boolean.")
    if schedule.get("initialize_on_install") is True and job is None:
      _fail(
        "Manifest `schedule.initialize_on_install` requires `schedule.job`."
      )
