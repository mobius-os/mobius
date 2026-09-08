# Project apps

A **Project app** is an ordinary installable Möbius app that contributes one or
more `project_templates`. It is not a second app runtime or a separate project
database. Installation registers its templates in **Projects → +**. The shell owns files, chats, builds and Creations; the app supplies the
domain-specific starting point, guidance and build tools.

See [Game Studio](examples/project-apps/game-studio/) for a complete small
example: editable JSON scenes become a playable canvas game. Nothing in the
shell knows about its game format. The example is not installed automatically.

## Declare a template

In `mobius.json`:

```json
{
  "project_templates": [{
    "id": "game",
    "name": "Canvas game",
    "kind": "game",
    "description": "An editable scene with a playable viewer.",
    "files": {"scene.game.json": "templates/scene.game.json"},
    "guidance": "Edit the scene, then build the Game Creation.",
    "previews": [{"id": "game", "name": "Game", "kind": "html", "path": "scene.game.json"}],
    "artifact_types": [{
      "id": "game", "name": "Playable game", "extensions": ["json"],
      "preview": "html", "script": "build.sh", "output": "index.html"
    }],
    "actions": [{"id": "level", "name": "Design a level", "prompt": "Help me design a level."}]
  }]
}
```

List starter files, scripts, sibling build dependencies and guidance files in
`source_files`, just like other packaged app source. Template `files` maps
project-relative destinations to packaged app-relative sources. Creation copies
these files once; existing projects are never reset to the current starter.
Optional template `skills` names packaged guidance files.

`id` is local to the app. The API returns the installed template `key`, combining
the actual installed slug and local ID. Never assume the slug equals the package
ID: installations may rename it to avoid a collision.

`kind` is optional presentation metadata, not a capability or executable selector.
Known shell glyphs include `blank`, `mini-app`, `web`, `latex`, `document`,
`sheet`, `slides`, `visualization` and `github`. Other strings remain discoverable
and use a neutral folder glyph. Core Möbius contributes **Blank project**, **App
project**, and the **Import from GitHub** action. Installed providers append their
active templates directly to the same picker: LaTeX contributes LaTeX documents,
Web Studio contributes Websites, and other Project apps can contribute new types.
No provider names, template names or known-kind allowlist control discovery.
Multiple providers of the same kind remain separate, attributed choices.

Set `retired: true` on a template to stop offering it for new project creation.
Its declaration remains available for source-format imports; existing project
snapshots and builder scripts still work. Keep the corresponding build scripts
packaged if you intend to retain rebuild support for those formats. Web Studio
is website-only: it does not ship retired templates or non-website builders.
Previously created project files and built outputs remain owned by Projects.

Core App projects use the platform's ordinary app compiler, not Web Studio's
builder. Their sandboxed Creation is a source preview, not an installed app:
app-scoped data and permissions require a separately installed app. Neither
creating nor building a project installs or publishes it. Core starter files are
copied independently for every new project.

Template actions open a new chat with an editable draft. They do not silently
send a message, start an agent, or authorize spending.

## Optional launcher

An app can be a compact launcher rather than a duplicate editor:

```js
const projects = window.mobius.projects
const templates = await projects.templates()
const game = templates.find(template => template.id === 'game')
if (!game) throw new Error('This project type is unavailable')
await projects.create({templateId: game.key, name: 'My game'})
```

`templates()` returns only this app's templates and narrow fields:
`key`, `id`, `name`, `description`, `kind`. Private file maps, scripts and guidance
are not exposed to the frame. `list()` returns this app's ordinary projects;
`open(projectId)` opens one it owns. `browse()` opens all shell Projects.
`migrate()` imports this app's supported legacy projects and returns its list.
Creation owns navigation: do not also call `open()` after `create()`.
Handle loading, unavailable runtime, request failure and empty lists separately.
The shell broker checks ownership; an app cannot select a foreign template to
bypass that boundary. Project-owned imports are not exposed as app-owned work.

## Build contract and custom display

The shell invokes the reviewed `script` with Bash from the project root. It
supplies `PROJECT_ROOT`, `PROJECT_SOURCE` (project-relative),
`PROJECT_OUTPUT_DIR` (an isolated staging directory), and `PROJECT_ARTIFACT_ID`.
Write generated files only beneath `PROJECT_OUTPUT_DIR`. A zero exit and the
presence of the declared output entry are required for publication. stdout and
stderr go to the bounded build log. `output` supports `{source}` and `{stem}`.
Timeout and cancellation terminate the child build. Projects serialize builds.
Do not depend on the staging directory name or read another project's data.

Creations support `html`, `pdf` and `image` preview kinds. An HTML Creation can
supply its own game player, canvas/WebGL renderer, interactive diagram or
specialized read-only viewer. Use a self-contained HTML output, or package local
assets supported by the existing preview assembler; do not rely on remote
scripts, fonts or runtime fetches. HTML runs in an opaque sandbox without access
to the shell's credentials. A custom viewer does not automatically get project
write access, shell navigation, or native device capabilities. Editing remains
in the project workspace unless a separately designed capability provides it.

The example demonstrates a **custom output format and player**, not a built-in
native game engine, arbitrary iframe permissions, or a generic custom editor
SDK. Those are separate extension decisions if a real use case requires them.

## Ownership, updates and trust

Each project snapshots its template declaration and retains its own editable
source and built output. Builder scripts are resolved from the installed
provider, so updating an app updates its build implementation for existing
projects. Authors must preserve their own source-format compatibility or provide
an explicit migration; do not overwrite owner files as an implicit migration.
Removing a provider removes its templates from discovery but keeps project files
and completed Creations. Provider-specific new builds require that provider.

Builds now stage replacements before publication. A failed or cancelled build
leaves the last successful Creation available. Publication uses same-filesystem
directory renames and restores the prior output on a rename error; it is not a
transactional filesystem or a promise of uninterrupted reads across a machine
crash. A subsequent publication recovers an interrupted prior rename.

**Provider scripts are trusted installed code, not an untrusted plugin sandbox.**
Their environment excludes agent/API credentials but they run in the container
with ordinary filesystem/process privileges. Review scripts and dependencies
before installation. Do not put credentials in templates, starter files or
output. Existing manifest review, app permissions, output confinement and frame
isolation remain in force; kind metadata grants none of these privileges.

## Verification

`backend/tests/test_project_apps.py` exercises the packaged game example through
real template discovery, creation, build and output routes using an isolated test
database. It also checks independent starter copies, provider removal and
preservation of a successful output after a failed rebuild. Frontend broker tests
cover the app-scoped template view, and picker tests cover unfamiliar formats
and competing providers. Run backend checks with `scripts/wt-pytest.sh` and the
focused frontend Node tests; no live owner projects are needed for these tests.
