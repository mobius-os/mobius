# `mobius.json` — the app manifest

Every Möbius mini-app ships a `mobius.json` manifest. This is the single
canonical field reference.

**The enforcing source of truth is `install._validate_manifest` in
`backend/app/install.py`.** If this doc and that function disagree, the
function wins — fix the doc. Citations below point at the line that enforces
or applies each rule (`install.py:N`); they were accurate at the time of
writing but the function is authoritative.

Install runs through `POST /api/apps/install` with a `manifest_url` (or an
inline `manifest` + `raw_base`). The installer fetches the manifest, validates
it (`_validate_manifest`, `install.py:140`), then fetches `entry`/`icon`/string
seeds/static assets relative to the manifest's directory.

## Required fields

All five must be present and truthy or install 400s
(`_REQUIRED_FIELDS`, `install.py:133`; missing-check at `install.py:142`).

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | App identity. Charset + reservation rules below. |
| `name` | string | Display name. Must be a string (`install.py:175`). |
| `version` | string | Installed version, stamped onto the App row and surfaced by `GET /api/apps/` (`install.py:1200`). |
| `description` | string | Stored on the App row (`install.py:1033`). |
| `entry` | string | Repo-relative path to the JSX entry (e.g. `index.jsx`), validated as a repo-relative path (`install.py:178`). |

### `id` rules (`install.py:163-173`)

- Charset: `a-z`, `0-9`, `-`, `_` only (`_SLUG_OK`, `install.py:137`; checked at `install.py:163`). No uppercase, no spaces, no dots.
- Must **not** start with `-` or `_` (`install.py:165`) — a leading dash could be smuggled as an argv flag into the cron scaffold.
- Must **not** be purely numeric (`install.py:168`) — bare integers are reserved for the per-app storage path `/data/apps/<int app id>`; a numeric slug would collide with that tree.

The `id` is load-bearing in three places: it is the **cron job identifier**, the **source-directory slug** (`/data/apps/<id>/`), and the **install-identity key** that discriminates update-vs-fresh-install. Changing it across versions creates a new app rather than updating the old one.

## Optional fields

| Field | Type | Validated / stored? |
|-------|------|---------------------|
| `icon` | string | Repo-relative path; validated as such when present (`install.py:177`). Capped at 12 MB on fetch. |
| `author` | string | **Decorative.** Not validated, not stored. For humans reading the repo. |
| `license` | string | **Decorative.** Not validated, not stored. |
| `homepage` | string | **Decorative.** Not validated, not stored. |
| `theme_color` | hex string | Coerced to `#RRGGBB` via `_manifest_color` (`install.py:87`, applied `install.py:1201`); a non-hex value is silently dropped to `None`. |
| `background_color` | hex string | Same coercion; falls back to `theme_color` when absent (`install.py:1202`). |
| `offline_capable` | bool | Opts the app into SW caching for offline open (`install.py:1048`). Defaults `false`. |
| `embeds_agent` | bool | Marks an app that embeds the Möbius chat agent (badge + behavior). Stored (`install.py:1049`). Defaults `false`. |
| `permissions` | object | See below. Must be an object (`install.py:183`). |
| `storage_seeds` | object | See below. Must be an object (`install.py:210`). |
| `static_assets` | object or array | See below (`install.py:217`). |
| `runtime` | object | **Informational only** — the installer never reads it (see below). |
| `schedule` | object | See below. Must be an object (`install.py:226`). |

### `permissions` (`install.py:182-206`)

| Key | Values | Default |
|-----|--------|---------|
| `cross_app_access` | `none` \| `read` \| `write` | `none` |
| `share_with_apps` | `none` \| `read` \| `write` | `none` |
| `chat_log_access` | `none` \| `summary` \| `full` | `none` |
| `manage_apps` | bool | `false` |

`cross_app_access` and `share_with_apps` share the storage read/write/none
ladder (`install.py:184-190`). `chat_log_access` has a **different** value
space — the redaction tiers `none`/`summary`/`full` (`install.py:196`); `full`
is accepted into the manifest so the column round-trips, but the read API
defers it until a concrete consumer lands. `manage_apps`, if present, must be a
boolean (`install.py:203`).

### `storage_seeds` (`install.py:209-216`) — the string-vs-non-string distinction

`storage_seeds` maps a storage sub-path to its seed value. The value's **type**
selects two completely different behaviors:

- **String value → a repo-relative path the installer FETCHES.** The string is
  validated as a repo-relative path (`install.py:215`, via
  `_validate_repo_relative_path`) and its file contents are fetched and seeded.
- **Non-string value (object/array/number/bool/null) → stored INLINE** as a
  JSON literal. No fetch happens; the literal becomes the seed.

**Common mistake:** authors reach for a string to inline literal content
(HTML/CSS/JS/markdown). That trips the repo-relative-path check on the first
`://` or `#` in the markup and surfaces as a confusing "must be a relative
path" 400 (the validator's docstring at `install.py:246` calls this out and
hints the fix). To seed literal **text**, put it in a repo file and point the
key at that path. To store an inline **JSON** value, use a non-string.

```json
"storage_seeds": {
  "system-prompt.md": "system-prompt.md",
  "schedule.json": { "hour": 10, "minute": 0 }
}
```

The first key is a string (fetch the repo file `system-prompt.md`); the second
is an object (store `{"hour":10,"minute":0}` inline).

### `static_assets` (`install.py:217-224`, `_static_asset_entries` at `install.py:473`)

A dest→source map of prebuilt files written under `/data/apps/<slug>/static`
and served at `/app-assets/...`. May be an object (`{ "dest": "source" }`) or a
bare array (each entry is both dest and source). Every dest and source is
validated as a repo-relative path. Caps (`install.py:483-486`): 256 files,
16 MB per file, 64 MB total per manifest.

### `runtime` (informational only)

`{ "imports": [...], "esm_deps": [...] }`. **The installer never reads this
field** — it does not drive dependency resolution. Module resolution is
governed by `backend/app/runtime_libs.py` (the esbuild externals) plus the
two importmaps (`app-frame.html` for in-shell, `routes/standalone.py` for the
PWA). Keep `runtime` accurate for human readers if you like, but it has no
runtime effect; a dependency only resolves if it is wired into `runtime_libs.py`
and both importmaps.

### `schedule` (`install.py:225-242`)

An object that registers a scheduled task and/or names a bundled job script.

| Key | Type | Notes |
|-----|------|-------|
| `default` | cron string | A 5-field cron expression, validated by `_validate_cron_expr` (`install.py:229`, def at `install.py:293`). Only when present is a recurring crontab entry installed. |
| `user_configurable` | bool | Whether the owner may edit the schedule from the app. |
| `job` | bare filename | The job script (e.g. `fetch.sh`). Must be a bare filename — no `/` or `..` (`install.py:230`) — because cron registration and the run-job endpoint both use only the basename. |

**Dual semantics of `job`** (`install.py:1374-1408`): a bundled `job` script is
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
- `docs/public-apps-spec.md` — the public catalog-app spec.
