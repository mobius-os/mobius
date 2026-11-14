# App-provided models (first version)

An installed app may declare one model provider in `mobius.json`. The accepted
capability contract, rather than editable source, is projected into the same
provider registry used by chats, model pickers, app pickers, and background
agents. That includes owner-built local apps: their `mobius.json` declaration
is accepted with each source Apply (and kept across owner permission edits),
limited to the HTTPS app-secret transport below; the broker transport stays
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

The Möbius subscription models are not an app declaration. The built-in
`mobius` provider reads `GET /v1/models` through the local identity broker;
model changes do not require a client release. The provider identity and
transport remain platform-owned at `http://127.0.0.1:8765/v1`; service metadata
cannot change the endpoint, credentials, or runtime. The broker keeps account
credentials and signs inference requests.

The version-1 service contract supplies `contract_version`, `default_model`,
and `data`: each model has a stable `id`, `name`, `owned_by: "mobius"`, context
window, input modalities, effort levels/default, and runtime capabilities.
An optional `replacements` map declares retired ID → active ID directly.
Replacement IDs never appear as picker rows; saved selections resolve at use,
without rewriting chat history, delegations or preferences. Missing successors
remain unavailable rather than silently choosing the default. Services must
publish explicit replacements before retiring saved IDs. Existing services
without this optional field continue to work unchanged.

The existing five-minute model-registry refresh owns discovery; failures retry
after thirty seconds and preserve the complete last-good snapshot. A private,
identity-scoped disk cache survives restarts, but cannot cross account changes.
With no valid snapshot the list is empty: the client does not invent models.
Defaults and runtime capabilities come from the same snapshot. Each agent run
owns a temporary frozen SDK catalogue, so another refresh cannot change its
startup metadata. The SDK template's usable-window ratio is applied equally
to picker context display; the service may also publish an explicit
`auto_compact_token_limit`. New wire protocols still require runtime updates.

`GET /api/models` retains `providers` and adds provider-keyed `defaults` and
`replacements` for picker/background selection. Claude and Codex discovery is
unchanged. The `identity_broker` manifest transport remains accepted only for
the reviewed `identity` app but inert; uninstalling that app cannot remove the
provider. Other connector apps use HTTPS and their own encrypted keys.
The default-on `GET/PATCH /api/auth/providers/{provider_id}/enabled` switch
removes a provider from pickers and blocks new turns without deleting chats.

## Boundaries and next steps

- Claude, Codex and Möbius are built in. The Möbius provider keeps the stable
  `mobius` identity for existing chats and offers its service-discovered models
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
