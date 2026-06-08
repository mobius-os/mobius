# `mobius.json` — the app manifest

Every Möbius mini-app ships a `mobius.json` manifest. This is the single
canonical field reference.

**The enforcing source of truth is `install._validate_manifest` in
`backend/app/install.py`.** If this doc and that function disagree, the
function wins — fix the doc. Citations below name the function or symbol that
enforces or applies each rule (e.g. `install._validate_manifest`,
`install._manifest_color`) rather than a line number, which drifts whenever
`install.py` changes; the named symbols are authoritative — grep for them.

Install runs through `POST /api/apps/install` with a `manifest_url` (or an
inline `manifest` + `raw_base`). The installer fetches the manifest, validates
it (`install._validate_manifest`), then fetches `entry`/`icon`/string
seeds/static assets relative to the manifest's directory.

## Required fields

All five must be present and truthy or install 400s
(`install._REQUIRED_FIELDS`, enforced in `install._validate_manifest`).

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | App identity. Charset + reservation rules below. |
| `name` | string | Display name. Must be a string (`install._validate_manifest`). |
| `version` | string | Installed version, stamped onto the App row and surfaced by `GET /api/apps/` (stamped in `install.install_from_manifest`). |
| `description` | string | Stored on the App row (`install.install_from_manifest`). |
| `entry` | string | Repo-relative path to the JSX entry (e.g. `index.jsx`), validated as a repo-relative path (`install._validate_manifest` via `install._validate_repo_relative_path`). |

### `id` rules (enforced in `install._validate_manifest`)

- Charset: `a-z`, `0-9`, `-`, `_` only (`install._SLUG_OK`). No uppercase, no spaces, no dots.
- Must **not** start with `-` or `_` — a leading dash could be smuggled as an argv flag into the cron scaffold.
- Must **not** be purely numeric — bare integers are reserved for the per-app storage path `/data/apps/<int app id>`; a numeric slug would collide with that tree.

The `id` is load-bearing in three places: it is the **cron job identifier**, the **source-directory slug** (`/data/apps/<id>/`), and the **install-identity key** that discriminates update-vs-fresh-install. Changing it across versions creates a new app rather than updating the old one — unless you declare `previous_id` (below).

### `previous_id` rules (enforced in `install._validate_manifest`)

Optional string: the app's prior `id` from before a rename. On install, if no app matches the current `id`, the installer adopts the app previously installed under `previous_id` (same canonical base) and migrates it in place — preserving its data — instead of creating a duplicate. Same slug rules as `id` (charset, no leading `-`/`_`, not purely numeric); must differ from `id`. `install._validate_manifest` is authoritative.

## Optional fields

| Field | Type | Validated / stored? |
|-------|------|---------------------|
| `icon` | string | Repo-relative path; validated as such when present (`install._validate_manifest`). Capped at 12 MB on fetch. |
| `previous_id` | string | The app's prior `id` from before a rename; lets the installer migrate in place instead of duplicating. See below (`install._validate_manifest`). |
| `author` | string | **Decorative.** Not validated, not stored. For humans reading the repo. |
| `license` | string | **Decorative.** Not validated, not stored. |
| `homepage` | string | **Decorative.** Not validated, not stored. |
| `theme_color` | hex string | Coerced to `#RRGGBB` via `install._manifest_color` (applied in `install.install_from_manifest`); a non-hex value is silently dropped to `None`. |
| `background_color` | hex string | Same coercion; falls back to `theme_color` when absent (`install.install_from_manifest`). |
| `offline_capable` | bool | Opts the app into SW caching for offline open (stored in `install.install_from_manifest`). Defaults `false`. |
| `embeds_agent` | bool | Marks an app that embeds the Möbius chat agent (badge + behavior). Stored in `install.install_from_manifest`. Defaults `false`. |
| `permissions` | object | See below. Must be an object (`install._validate_manifest`). |
| `storage_seeds` | object | See below. Must be an object (`install._validate_manifest`). |
| `static_assets` | object or array | See below (`install._validate_manifest`). |
| `runtime` | object | **Informational only** — the installer never reads it (see below). |
| `schedule` | object | See below. Must be an object (`install._validate_manifest`). |

### `permissions` (enforced in `install._validate_manifest`)

| Key | Values | Default |
|-----|--------|---------|
| `cross_app_access` | `none` \| `read` \| `write` | `none` |
| `share_with_apps` | `none` \| `read` \| `write` | `none` |
| `chat_log_access` | `none` \| `summary` \| `full` | `none` |
| `manage_apps` | bool | `false` |

`cross_app_access` and `share_with_apps` share the storage read/write/none
ladder. `chat_log_access` has a **different** value space — the redaction tiers
`none`/`summary`/`full`; `full` is accepted into the manifest so the column
round-trips, but the read API defers it until a concrete consumer lands.
`manage_apps`, if present, must be a boolean. All enforced in
`install._validate_manifest`.

### `storage_seeds` (enforced in `install._validate_manifest`) — the string-vs-non-string distinction

`storage_seeds` maps a storage sub-path to its seed value. The value's **type**
selects two completely different behaviors:

- **String value → a repo-relative path the installer FETCHES.** The string is
  validated as a repo-relative path (via `install._validate_repo_relative_path`)
  and its file contents are fetched and seeded.
- **Non-string value (object/array/number/bool/null) → stored INLINE** as a
  JSON literal. No fetch happens; the literal becomes the seed.

**Common mistake:** authors reach for a string to inline literal content
(HTML/CSS/JS/markdown). That trips the repo-relative-path check on the first
`://` or `#` in the markup and surfaces as a confusing "must be a relative
path" 400 (the `install._validate_repo_relative_path` docstring calls this out
and hints the fix). To seed literal **text**, put it in a repo file and point the
key at that path. To store an inline **JSON** value, use a non-string.

```json
"storage_seeds": {
  "system-prompt.md": "system-prompt.md",
  "schedule.json": { "hour": 10, "minute": 0 }
}
```

The first key is a string (fetch the repo file `system-prompt.md`); the second
is an object (store `{"hour":10,"minute":0}` inline).

### `static_assets` (validated in `install._validate_manifest`; normalized by `install._static_asset_entries`)

A dest→source map of prebuilt files written under `/data/apps/<slug>/static`
and served at `/app-assets/...`. May be an object (`{ "dest": "source" }`) or a
bare array (each entry is both dest and source). Every dest and source is
validated as a repo-relative path. Caps (in `install._static_asset_entries`):
256 files, 16 MB per file, 64 MB total per manifest.

### `runtime` (informational only)

`{ "imports": [...], "esm_deps": [...] }`. **The installer never reads this
field** — it does not drive dependency resolution. Module resolution is
governed by `backend/app/runtime_libs.py` (the esbuild externals) plus the
two importmaps (`app-frame.html` for in-shell, `routes/standalone.py` for the
PWA). Keep `runtime` accurate for human readers if you like, but it has no
runtime effect; a dependency only resolves if it is wired into `runtime_libs.py`
and both importmaps.

### `schedule` (enforced in `install._validate_manifest`)

An object that registers a scheduled task and/or names a bundled job script.

| Key | Type | Notes |
|-----|------|-------|
| `default` | cron string | A 5-field cron expression, validated by `install._validate_cron_expr`. Only when present is a recurring crontab entry installed. |
| `user_configurable` | bool | Whether the owner may edit the schedule from the app. |
| `job` | bare filename | The job script (e.g. `fetch.sh`). Must be a bare filename — no `/` or `..` (`install._validate_manifest`) — because cron registration and the run-job endpoint both use only the basename. |

**Dual semantics of `job`** (handled in `install.install_from_manifest`): a bundled `job` script is
written to the source dir whenever one is fetched, **independent** of whether
`schedule.default` is also declared.

- With `schedule.default` → a **recurring cron job** runs the script on the
  cron expression.
- Without `schedule.default` → an **on-demand build hook** (e.g. the LaTeX
  app's `build.sh`, compiled on a Build click) that runs only through the
  run-job endpoint, never on a timer. The script still lands on disk so run-job
  can find it.

## Minimal skeleton

```json
{
  "id": "my-app",
  "name": "My App",
  "version": "1.0.0",
  "description": "What this app does.",
  "entry": "index.jsx",
  "icon": "icon.png",
  "offline_capable": true,
  "theme_color": "#0c0f14",
  "background_color": "#0c0f14",
  "permissions": {
    "cross_app_access": "none",
    "share_with_apps": "none"
  },
  "storage_seeds": {},
  "schedule": {
    "default": "0 10 * * *",
    "user_configurable": true,
    "job": "fetch.sh"
  }
}
```

## See also

- `install._validate_manifest` (`backend/app/install.py`) — the enforcing validator; authoritative.
- `backend/app/runtime_libs.py` + the two importmaps (`app-frame.html`, `routes/standalone.py`) — what actually governs dependency resolution.
- `backend/scripts/seed-skills/building-apps.md` (live: `/data/shared/skills/building-apps.md`) — how to build a mini-app.
- the published public spec at mobius-os.github.io/spec/manifest.md + spec/mobius.schema.json (mirror of install._validate_manifest, which stays authoritative).
- `docs/public-apps-spec.md` — the UNIMPLEMENTED `/p/<slug>` public-sharing brainstorm (Status: brainstorming), NOT the manifest spec.
