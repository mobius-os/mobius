# App-provided models (first version)

An installed app may declare one model provider in `mobius.json`. The accepted
capability contract, rather than editable source, is projected into the same
provider registry used by chats, model pickers, app pickers, and background
agents. The provider appears shortly after install/Apply and disappears shortly
after uninstall (registry reads are throttled for one second). Existing chats
retain their saved provider/model selection but
cannot run it while the provider app is absent; Möbius does not silently send
those chats to a different company.

The first transport is **OpenAI Responses-compatible HTTPS**, driven by the
existing Codex agent runtime. A compatible endpoint must support streamed
Responses with tool calls; merely offering Chat Completions is insufficient.
The app owns its setup UI, connection instructions, and one encrypted app
secret. The owner enters the key into the app's own browser UI (which writes
`PUT /api/apps/{app_id}/secrets/{secret_name}`); it is not a manifest value and
must not pass through a chat prompt. The core only checks whether the secret
exists when displaying availability. At turn launch it decrypts the secret for
the inference transport and excludes that environment variable from Codex
shell tools. The provider app's endpoint receives the conversation and tool
requests, so installing and connecting one is a substantive trust and billing
decision. No background provider is enabled automatically by adding an app.

Example declaration, based on DeepSeek's Responses API (model names are
illustrative and should be updated by the app publisher as the API changes):

```json
{
  "model_provider": {
    "name": "DeepSeek",
    "base_url": "https://api.deepseek.com",
    "secret_name": "api_key",
    "default_model": "deepseek-flash",
    "models": [
      {"id": "deepseek-flash", "label": "DeepSeek Flash", "effort_levels": ["low", "medium", "high", "max"]},
      {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro", "effort_levels": ["low", "high", "max"]}
    ]
  }
}
```

The other required `mobius.json` fields (`id`, `name`, `version`,
`description`, `entry`) are omitted above. The app also needs an actual setup
screen to save/delete its `api_key` secret; the platform does not invent one.
Model IDs are provider wire IDs, not display labels. They must be unique across
installed providers. The provider identity is the stable local app row ID
(`app-<id>`), independent of an app rename or its repository URL. An update can
change labels, efforts, context limits, and models, but the accepted declaration
changes only after the normal reviewed update/Apply boundary.

## Boundaries and next steps

- Built-in Claude, Codex, and Möbius providers stay registered in the same
  provider map. Möbius already uses the Codex transport with its own broker,
  auth preflight, and model catalog; app providers use that transport with a
  declarative endpoint instead of adding a new chat runner.
- Möbius · You owns account linking, access, pricing, and setup presentation.
  The broker and model execution remain in the platform because they enforce
  the owner session, credential, and per-turn runtime boundary. Moving that
  privileged path into an optional app would make chats depend on its install.
- App models are currently manifest-declared, not discovered live. An app can
  publish an update when its upstream model list changes. A later version can
  add an app-owned model-discovery service without changing picker consumers.
- The first version handles one Responses provider per app and one bearer-key
  secret. OAuth, provider-specific headers, multiple endpoints per app, and
  non-Responses protocols need separate reviewed contracts, not ad-hoc fields.
- Secret isolation is the same transport-env boundary used for Codex connector
  credentials. A future inference proxy could keep the raw key outside the
  agent process entirely; that would be stronger against arbitrary code the
  agent executes. This version must not be described as that stronger boundary.
- Test the exact provider protocol against a mock streamed Responses server
  before claiming compatibility with any particular remote vendor. Live paid
  inference should be tested only after the owner explicitly authorizes it.
