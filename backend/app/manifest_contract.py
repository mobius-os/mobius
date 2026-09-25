"""Dependency-free manifest contract shared by install and preflight."""

from collections.abc import Mapping
import json
from urllib.parse import unquote, urlparse
import re
import shlex

REQUIRED_STRING_FIELDS = ("id", "name", "version", "description", "entry")
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
AGENT_ACTIVITIES_COUNT_MAX = 16
SERVICE_REQUEST_MAX_BYTES = 8 * 1024 * 1024
SERVICE_ALIASES_MAX = 4
AGENT_TOOLS_MAX = 16
AGENT_TOOL_DESCRIPTION_MAX = 2000
AGENT_TOOL_SCHEMA_MAX_BYTES = 8 * 1024
MAX_JOB_SHEBANG_BYTES = 256
_SLUG_OK = "abcdefghijklmnopqrstuvwxyz0123456789-_"
_SOURCE_FILES_MANAGED_PREFIXES = (
  "static/", "dist/", ".build/", "node_modules/",
)
_SOURCE_FILES_MANAGED_EXACT = frozenset((
  "index.jsx", ".gitignore", "init-cron.sh", ".mobius-static-assets.json",
))
_CRON_FIELD_OK = re.compile(r"[0-9\*/,\- ]+", re.ASCII)
_SKILL_FILENAME_OK = re.compile(r"^[a-z0-9][a-z0-9._-]*\.md$")
# A folder skill is `<id>/`: `<id>/SKILL.md` plus sibling markdown the entry
# links to relatively, mirroring installed ecosystem skills so progressive
# disclosure travels with the skill instead of pointing into app source.
_SKILL_FOLDER_OK = re.compile(r"^([a-z0-9][a-z0-9._-]*)/$")
FOLDER_SKILL_ENTRY = "SKILL.md"
_PACKAGE_ID_OK = re.compile(r"^[a-z0-9][a-z0-9._:-]{2,127}$")
_AGENT_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class ManifestContractError(ValueError):
  pass


def is_folder_skill_member(name: str) -> bool:
  """Whether a file directly inside a `<id>/` folder skill may ship with it."""
  return name == FOLDER_SKILL_ENTRY or _SKILL_FILENAME_OK.fullmatch(name) is not None


def skill_member_paths(manifest: dict) -> list[str]:
  """Every source file a validated manifest's `skills` materializes, in order.

  A root `<id>.md` entry is itself; a `<id>/` folder entry is `<id>/SKILL.md`
  followed by the other `source_files` directly inside that folder.
  """
  declared = [
    path for path in (manifest.get("source_files") or [])
    if isinstance(path, str)
  ]
  members: list[str] = []
  for entry in manifest.get("skills") or []:
    if not isinstance(entry, str):
      continue
    if _SKILL_FOLDER_OK.fullmatch(entry) is None:
      members.append(entry)
      continue
    members.append(entry + FOLDER_SKILL_ENTRY)
    members.extend(
      path for path in declared
      if path.startswith(entry) and path != entry + FOLDER_SKILL_ENTRY
    )
  return list(dict.fromkeys(members))


def _fail(message: str) -> None:
  raise ManifestContractError(message)


def job_interpreter(job: bytes) -> tuple[str, ...]:
  """Return the interpreter declared by one accepted scheduled job.

  Runtime choice belongs to the app package. Keeping the byte-level contract
  here lets Store install, local Apply, and execution reject the same invalid
  declaration instead of discovering it only when cron fires.
  """
  first_line = job.splitlines(keepends=True)[:1]
  line = first_line[0] if first_line else b""
  if len(line) > MAX_JOB_SHEBANG_BYTES:
    _fail(f"Schedule job shebang exceeds {MAX_JOB_SHEBANG_BYTES} bytes.")
  if not line.startswith(b"#!"):
    _fail("Schedule job is missing a shebang.")
  try:
    interpreter = tuple(shlex.split(line[2:].decode("utf-8").strip()))
  except (UnicodeDecodeError, ValueError) as exc:
    raise ManifestContractError("Schedule job has an invalid shebang.") from exc
  if not interpreter or not interpreter[0].startswith("/"):
    _fail("Schedule job shebang must name an absolute interpreter.")
  return interpreter


def require_executable_job(mode: int) -> None:
  """Reject a scheduled job that its accepted package cannot execute."""
  if not mode & 0o111:
    _fail("Schedule job is not executable.")


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
  if not _CRON_FIELD_OK.fullmatch(expr):
    _fail(
      f"schedule.default contains disallowed characters: {expr!r}. "
      "Allowed: digits, *, /, ,, -, whitespace."
    )
  fields = expr.split()
  if len(fields) != 5:
    _fail(f"schedule.default must have exactly 5 cron fields, got {expr!r}")
  bounds = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 7),
  )
  for field, (label, lower, upper) in zip(fields, bounds, strict=True):
    for item in field.split(","):
      if not item:
        _fail(f"schedule.default has an empty {label} item: {expr!r}")
      base, separator, step_text = item.partition("/")
      if separator:
        if "/" in step_text or not step_text.isdigit():
          _fail(f"schedule.default has an invalid {label} step: {expr!r}")
        try:
          step = int(step_text)
        except ValueError:
          _fail(f"schedule.default has an invalid {label} step: {expr!r}")
        if not 1 <= step <= upper - lower + 1:
          _fail(f"schedule.default has an out-of-range {label} step: {expr!r}")
      if base == "*":
        continue
      start_text, dash, end_text = base.partition("-")
      if not start_text.isdigit() or (dash and not end_text.isdigit()) or "-" in end_text:
        _fail(f"schedule.default has an invalid {label} value: {expr!r}")
      try:
        start = int(start_text)
        end = int(end_text) if dash else start
      except ValueError:
        _fail(f"schedule.default has an invalid {label} value: {expr!r}")
      if not lower <= start <= end <= upper:
        _fail(f"schedule.default has an out-of-range {label} value: {expr!r}")


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


def validate_agent_tools(tools, *, has_service: bool) -> None:
  """Validate the tools an app contributes to every agent run.

  A tool is only a declaration: the platform calls it through the app's own
  reviewed `service`, so there is no second execution path to review.
  """
  if not has_service:
    _fail(
      "Manifest `tools` requires a `service`: the platform calls each tool "
      "through the app's service."
    )
  if not isinstance(tools, list) or len(tools) > AGENT_TOOLS_MAX:
    _fail(f"Manifest `tools` must be an array with at most {AGENT_TOOLS_MAX} entries.")
  names: set[str] = set()
  for index, tool in enumerate(tools):
    field = f"tools[{index}]"
    if not isinstance(tool, Mapping) or set(tool) != {
      "name", "description", "input_schema",
    }:
      _fail(
        f"Manifest `{field}` must contain exactly `name`, `description`, "
        "and `input_schema`."
      )
    name = tool["name"]
    if not isinstance(name, str) or _AGENT_TOOL_NAME.fullmatch(name) is None:
      _fail(f"Manifest `{field}.name` must match `^[a-z][a-z0-9_]{{0,39}}$`.")
    if name in names:
      _fail(f"Manifest `tools` repeats the name {name!r}.")
    names.add(name)
    description = tool["description"]
    if (
      not isinstance(description, str)
      or not description.strip()
      or len(description) > AGENT_TOOL_DESCRIPTION_MAX
    ):
      _fail(
        f"Manifest `{field}.description` must be 1-"
        f"{AGENT_TOOL_DESCRIPTION_MAX} characters."
      )
    schema = tool["input_schema"]
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
      _fail(f"Manifest `{field}.input_schema` must be a JSON Schema object type.")
    try:
      encoded = json.dumps(schema, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
      raise ManifestContractError(
        f"Manifest `{field}.input_schema` must be plain JSON."
      ) from exc
    if len(encoded) > AGENT_TOOL_SCHEMA_MAX_BYTES:
      _fail(
        f"Manifest `{field}.input_schema` exceeds "
        f"{AGENT_TOOL_SCHEMA_MAX_BYTES} bytes."
      )


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
  model_provider = manifest.get("model_provider")
  if model_provider is not None:
    if not isinstance(model_provider, Mapping):
      _fail("Manifest `model_provider` must be an object.")
    broker = model_provider.get("transport") == "identity_broker"
    expected = {"name", "base_url", "models", "default_model"}
    expected |= {"transport"} if broker else {"secret_name"}
    if set(model_provider) != expected:
      _fail("Manifest `model_provider` has invalid fields for its transport.")
    if not isinstance(model_provider["name"], str) or not 1 <= len(model_provider["name"].strip()) <= 80:
      _fail("Manifest `model_provider.name` must be 1–80 characters.")
    url = urlparse(model_provider["base_url"] if isinstance(model_provider["base_url"], str) else "")
    if broker:
      if (manifest.get("id") != "identity"
          or (manifest.get("permissions") or {}).get("identity_manage") is not True
          or model_provider["base_url"] != "http://127.0.0.1:8765/v1"):
        _fail("The protected identity broker is only available to the Möbius · You integration.")
    else:
      if (url.scheme != "https" or not url.hostname or url.username or url.password
          or url.query or url.fragment or url.params):
        _fail("Manifest `model_provider.base_url` must be an HTTPS API base URL without credentials or query.")
      secret_name = model_provider["secret_name"]
      if not isinstance(secret_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", secret_name):
        _fail("Manifest `model_provider.secret_name` must name one app secret.")
    entries = model_provider["models"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
      _fail("Manifest `model_provider.models` must contain 1–32 models.")
    ids = set()
    for index, entry in enumerate(entries):
      if not isinstance(entry, Mapping) or set(entry) - {"id", "label", "effort_levels", "context_window", "input_modalities", "auto_compact_token_limit"} or not {"id", "label"}.issubset(entry):
        _fail(f"Manifest `model_provider.models[{index}]` has invalid fields.")
      model_id = entry["id"]
      if not isinstance(model_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model_id) or model_id in ids:
        _fail("Manifest model ids must be unique, bounded wire IDs.")
      ids.add(model_id)
      if not isinstance(entry["label"], str) or not 1 <= len(entry["label"].strip()) <= 100:
        _fail("Manifest model labels must be 1–100 characters.")
      efforts = entry.get("effort_levels")
      if efforts is not None and (not isinstance(efforts, list) or not efforts or len(efforts) > 8
          or not all(isinstance(value, str) and value in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"} for value in efforts)
          or len(set(efforts)) != len(efforts)):
        _fail("Manifest model effort_levels contains unsupported or duplicate values.")
      window = entry.get("context_window")
      if window is not None and (isinstance(window, bool) or not isinstance(window, int) or not 1024 <= window <= 10_000_000):
        _fail("Manifest model context_window must be an integer between 1024 and 10000000.")
      modalities = entry.get("input_modalities")
      if modalities is not None and (not isinstance(modalities, list)
          or modalities not in (["text"], ["text", "image"])):
        _fail("Manifest model input_modalities must be text or text and image.")
      compact = entry.get("auto_compact_token_limit")
      if compact is not None and (isinstance(compact, bool) or not isinstance(compact, int)
          or not 1024 <= compact <= (window or 10_000_000)):
        _fail("Manifest model auto_compact_token_limit must fit its context window.")
    if model_provider["default_model"] not in ids:
      _fail("Manifest `model_provider.default_model` must name a declared model.")
  package_id = manifest.get("package_id")
  if package_id is not None and (
    not isinstance(package_id, str)
    or _PACKAGE_ID_OK.fullmatch(package_id) is None
  ):
    _fail(
      "Manifest `package_id` must be 3-128 lowercase letters, digits, or "
      "the characters '.', '_', ':', and '-', starting with a letter or digit."
    )
  moved_to = manifest.get("moved_to")
  if moved_to is not None:
    if package_id is None:
      _fail("Manifest `moved_to` requires `package_id`.")
    if not isinstance(moved_to, Mapping) or set(moved_to) != {"manifest_url"}:
      _fail("Manifest `moved_to` must contain only `manifest_url`.")
    moved_url = moved_to.get("manifest_url")
    if not isinstance(moved_url, str) or not moved_url:
      _fail("Manifest `moved_to.manifest_url` must be a non-empty string.")
    parsed_moved = urlparse(moved_url)
    if (
      parsed_moved.scheme != "https"
      or not parsed_moved.netloc
      or parsed_moved.username is not None
      or parsed_moved.password is not None
      or parsed_moved.query
      or parsed_moved.fragment
    ):
      _fail(
        "Manifest `moved_to.manifest_url` must be an absolute HTTPS URL "
        "without credentials, query, or fragment."
      )
  previous_id = manifest.get("previous_id")
  if previous_id is not None:
    validate_slug_field(previous_id, "previous_id")
    if previous_id == mid:
      _fail("Manifest `previous_id` must differ from `id`.")
  previous_manifest_url = manifest.get("previous_manifest_url")
  if previous_manifest_url is not None:
    if previous_id is None:
      _fail("Manifest `previous_manifest_url` requires `previous_id`.")
    if not isinstance(previous_manifest_url, str) or not previous_manifest_url:
      _fail("Manifest `previous_manifest_url` must be a non-empty string.")
    parsed_previous = urlparse(previous_manifest_url)
    if (
      parsed_previous.scheme != "https"
      or not parsed_previous.netloc
      or parsed_previous.username is not None
      or parsed_previous.password is not None
      or parsed_previous.query
      or parsed_previous.fragment
    ):
      _fail(
        "Manifest `previous_manifest_url` must be an absolute HTTPS URL "
        "without credentials, query, or fragment."
      )

  validate_repo_relative_path(manifest["entry"], "entry")
  if manifest["entry"] != "index.jsx":
    _fail(
      "Manifest `entry` must be `index.jsx`; the editor and explicit "
      "app-apply lifecycle use that canonical entrypoint."
    )
  if manifest.get("icon") is not None:
    validate_repo_relative_path(manifest["icon"], "icon")

  for field in ("offline_capable", "embeds_agent"):
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
      if "retired" in template and not isinstance(template["retired"], bool):
        _fail(f"Manifest `{field}.retired` must be a boolean.")
      for text_field in ("description", "guidance", "kind"):
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
      raw_artifact_types = template.get("artifact_types", [])
      declared_artifact_type_ids = {
        value.get("id") for value in raw_artifact_types
        if isinstance(value, Mapping)
      } if isinstance(raw_artifact_types, list) else set()
      for preview_index, preview in enumerate(previews):
        preview_field = f"{field}.previews[{preview_index}]"
        if not isinstance(preview, Mapping):
          _fail(f"Manifest `{preview_field}` must be an object.")
        preview_id = preview.get("id")
        validate_slug_field(preview_id, f"{preview_field}.id")
        if preview_id in seen_preview_ids:
          _fail(f"Manifest `{preview_field}.id` duplicates {preview_id!r}.")
        seen_preview_ids.add(preview_id)
        if not isinstance(preview.get("name"), str) or not preview["name"].strip():
          _fail(f"Manifest `{preview_field}.name` must be a non-empty string.")
        validate_repo_relative_path(preview.get("source"), f"{preview_field}.source")
        builder = preview.get("builder")
        validate_slug_field(builder, f"{preview_field}.builder")
        if builder not in declared_artifact_type_ids:
          _fail(
            f"Manifest `{preview_field}.builder` must name one of this "
            "template's artifact_types."
          )
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

  # Required platform capabilities fail loudly on older Möbius builds rather
  # than installing an app whose essential grant can never become effective.
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
  static_assets_entries = static_asset_entries(static_assets)
  if len(static_assets_entries) > STATIC_ASSETS_COUNT_MAX:
    _fail(
      "Manifest has too many static_assets "
      f"(max {STATIC_ASSETS_COUNT_MAX})."
    )
  for dest, src in static_assets_entries.items():
    validate_repo_relative_path(dest, f"static_assets.{dest}")
    validate_repo_relative_path(src, f"static_assets.{dest}")
    if dest == "store" or dest.startswith("store/"):
      _fail(
        f"Manifest `static_assets.{dest}` collides with the author-owned "
        "static/store listing-media tree."
      )

  source_files = manifest.get("source_files")
  if source_files is not None:
    if not isinstance(source_files, list):
      _fail("Manifest `source_files` must be an array.")
    # No file-count cap: the manifest byte cap bounds how many paths can be
    # listed, and fetch enforces the per-file and total source byte caps.
    schedule = manifest.get("schedule")
    declared_job = schedule.get("job") if isinstance(schedule, Mapping) else None
    seen_sources: set[str] = set()
    for index, path in enumerate(source_files):
      validate_repo_relative_path(path, f"source_files[{index}]")
      if path in seen_sources:
        _fail(f"Manifest `source_files[{index}]` repeats {path!r}.")
      seen_sources.add(path)
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

  agent_activities = manifest.get("agent_activities", {})
  if not isinstance(agent_activities, Mapping):
    _fail("Manifest `agent_activities` must be an object.")
  if len(agent_activities) > AGENT_ACTIVITIES_COUNT_MAX:
    _fail(
      "Manifest has too many agent_activities "
      f"(max {AGENT_ACTIVITIES_COUNT_MAX})."
    )
  declared_sources = set(source_files or []) if isinstance(source_files, list) else set()
  activity_entries: set[str] = set()
  for activity_id, activity in agent_activities.items():
    validate_slug_field(activity_id, f"agent_activities.{activity_id}")
    field = f"agent_activities.{activity_id}"
    if not isinstance(activity, Mapping) or set(activity) != {
      "entry", "arguments", "running_label",
    }:
      _fail(
        f"Manifest `{field}` must contain only entry, arguments, and "
        "running_label."
      )
    entry = activity.get("entry")
    validate_repo_relative_path(entry, f"{field}.entry")
    if entry not in declared_sources:
      _fail(
        f"Manifest `{field}.entry` must also be listed in source_files."
      )
    if entry in activity_entries:
      _fail("Manifest agent_activities must use distinct entry paths.")
    activity_entries.add(entry)
    arguments = activity.get("arguments")
    if (
      isinstance(arguments, bool)
      or not isinstance(arguments, int)
      or not 0 <= arguments <= 16
    ):
      _fail(f"Manifest `{field}.arguments` must be an integer from 0 to 16.")
    running_label = activity.get("running_label")
    if (
      not isinstance(running_label, str)
      or not running_label.strip()
      or len(running_label) > 160
    ):
      _fail(f"Manifest `{field}.running_label` must be 1-160 characters.")

  service = manifest.get("service")
  if service is not None:
    if not isinstance(service, Mapping) or set(service) - {
      "id", "aliases", "entry", "access",
    }:
      _fail(
        "Manifest `service` must contain only `id`, `aliases`, `entry`, "
        "and `access`."
      )
    if package_id is not None and "id" not in service:
      _fail("Manifest `service.id` is required when `package_id` is declared.")
    service_id = service.get("id", mid)
    validate_slug_field(service_id, "service.id")
    aliases = service.get("aliases", [])
    if not isinstance(aliases, list) or len(aliases) > SERVICE_ALIASES_MAX:
      _fail(
        f"Manifest `service.aliases` must be an array with at most "
        f"{SERVICE_ALIASES_MAX} entries."
      )
    if aliases and "id" not in service:
      _fail("Manifest `service.aliases` requires an explicit `service.id`.")
    seen_aliases = set()
    for index, alias in enumerate(aliases):
      validate_slug_field(alias, f"service.aliases[{index}]")
      if alias == service_id:
        _fail("Manifest `service.aliases` must not repeat `service.id`.")
      if alias in seen_aliases:
        _fail(f"Manifest `service.aliases` duplicates {alias!r}.")
      seen_aliases.add(alias)
    entry = service.get("entry")
    if not isinstance(entry, str):
      _fail("Manifest `service.entry` must be a string.")
    if "/" in entry or "\\" in entry:
      _fail("Manifest `service.entry` must be a bare filename.")
    validate_repo_relative_path(entry, "service.entry")
    if not entry.endswith(".py"):
      _fail("Manifest `service.entry` must be a Python file.")
    if not isinstance(source_files, list) or entry not in source_files:
      _fail(
        "Manifest `service.entry` must also be listed in `source_files` so "
        "every install contains the reviewed service."
      )
    if service.get("access", "self") not in {"self", "apps", "public"}:
      _fail("Manifest `service.access` must be `self`, `apps`, or `public`.")

  skills = manifest.get("skills")
  if skills is not None:
    if not isinstance(skills, list):
      _fail("Manifest `skills` must be an array.")
    if len(skills) > SKILLS_COUNT_MAX:
      _fail(f"Manifest has too many skills (max {SKILLS_COUNT_MAX}).")
    declared = [path for path in (source_files or []) if isinstance(path, str)]
    skill_ids: set[str] = set()
    for index, entry in enumerate(skills):
      folder = (
        _SKILL_FOLDER_OK.fullmatch(entry) if isinstance(entry, str) else None
      )
      if folder is None and (
        not isinstance(entry, str) or _SKILL_FILENAME_OK.fullmatch(entry) is None
      ):
        _fail(
          f"Manifest `skills[{index}]` must be a root `<id>.md` file "
          "(`^[a-z0-9][a-z0-9._-]*\\.md$`) or a `<id>/` folder skill."
        )
      skill_id = folder.group(1) if folder else entry[:-len(".md")]
      if skill_id in skill_ids:
        _fail(f"Manifest `skills[{index}]` repeats skill id {skill_id!r}.")
      skill_ids.add(skill_id)
      if folder is None:
        if entry not in declared:
          _fail(
            f"Manifest `skills[{index}]` {entry!r} must also be listed in "
            "`source_files` as a root-level file — the installer reads skill "
            "bytes from the installed source tree, so a skill that is not a "
            "source file has nothing to install."
          )
        continue
      for path in declared:
        if not path.startswith(entry):
          continue
        member = path[len(entry):]
        if not is_folder_skill_member(member):
          _fail(
            f"Manifest folder skill {entry!r} may contain only `SKILL.md` and "
            f"lowercase `.md` files directly inside it; got {path!r}."
          )
      if entry + FOLDER_SKILL_ENTRY not in declared:
        _fail(
          f"Manifest `skills[{index}]` {entry!r} must list "
          f"`{entry}{FOLDER_SKILL_ENTRY}` in `source_files`."
        )

  tools = manifest.get("tools")
  if tools is not None:
    validate_agent_tools(tools, has_service=service is not None)

  system_prompt = manifest.get("system_prompt")
  if system_prompt is not None:
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
