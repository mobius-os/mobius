# App-provided models (first version)

An installed app may declare one model provider in `mobius.json`. The accepted
capability contract, rather than editable source, is projected into the same
provider registry used by chats, model pickers, app pickers, and background
agents. That includes owner-built local apps: their `mobius.json` declaration
is accepted with each source Apply (and kept across owner permission edits),
limited to the HTTPS vault-key transport below; the broker transport stays
reserved for the reviewed Möbius · You package, whose declaration the registry
ignores (see below). The provider appears shortly
after install/Apply and disappears shortly after uninstall (registry reads are
throttled for one second). Existing chats
retain their saved provider/model selection but
cannot run it while the provider app is absent; Möbius does not silently send
those chats to a different company.

The shared transport is **OpenAI Responses-compatible HTTPS**, driven by the
existing Codex agent runtime. A compatible endpoint must support streamed
Responses with tool calls; merely offering Chat Completions is insufficient.
The app owns its setup UI and connection instructions; its key lives in the
owner's key vault. The app asks for the key named by `secret_name`
(`POST /api/keys/requests`) and the owner enters and approves it in
**Settings › Keys**; it is not a manifest value and never passes through the
app or a chat prompt. The platform looks for the app's own key created for
that name (for example by the upgrade import, which keeps names like `api.key`
distinct even when stored as `api-key` or `app<id>-api-key`), and otherwise a
key with exactly that name. The models are available while the owner's grant
to the app stands. At turn launch the platform reads the key for the inference
transport (logged as `platform:app-<id>`) and excludes that environment
variable from Codex shell tools. The provider app's endpoint receives the
conversation and tool requests, so installing and connecting one is a
substantive trust and billing decision. No background provider is enabled automatically by adding an app.

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
screen that asks for its `api_key` key and shows whether it is approved; the
platform does not invent one.
Model IDs are provider wire IDs, not display labels. They must be unique across
installed providers. The provider identity is the stable local app row ID
(`app-<id>`), independent of an app rename or its repository URL. An update can
change labels, efforts, context limits, and models, but the accepted declaration
changes only after the normal reviewed update/Apply boundary.

The Möbius subscription models are not an app declaration. The platform owns
their catalogue (`backend/app/mobius_models.json`: model names, effort levels,
context ceilings), and it changes only with a platform release. The built-in
`mobius` provider serves it through the image-owned identity broker at a fixed
`http://127.0.0.1:8765/v1` endpoint with no vault key; the broker keeps
account credentials and signs inference requests. The `identity_broker`
transport stays valid in a manifest (reserved to the reviewed `identity` app),
but the registry ignores an installed app's declaration of it. Other connector
apps use HTTPS and their own encrypted keys. Any model connection can use the
default-on `GET/PATCH /api/auth/providers/{provider_id}/enabled` switch; for
an app provider only its declaring app or the owner may change it. Turning one
off removes it from pickers and blocks new turns without deleting saved chats.

## Boundaries and next steps

- Claude, Codex and Möbius are built in. The Möbius provider keeps the stable
  `mobius` identity for existing chats and offers its platform-owned models
  with no app installed. Both broker and external app connections use the
  Responses harness and one picker/agent registry.
- The Möbius account (sign-in, profile, deployments) and its **Möbius models**
  switch live in Settings. New deployments no longer install Möbius · You; an
  existing copy is an ordinary app and is not a runtime dependency for any
  provider. The switch is on by default and can be changed while signed out.
  Turning it off removes only Möbius from model pickers and prevents new turns
  in chats still set to a Möbius model; those chats remain saved. Signing in
  never turns Möbius on as a background-task provider; the owner chooses that.
  The preference lives in shared agent settings, so it survives any app
  install or uninstall.
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
