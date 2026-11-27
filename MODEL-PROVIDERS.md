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

A vault-key declaration names its wire protocol with optional `protocol`:

- `responses` (the default) — **OpenAI Responses-compatible HTTPS**, driven by
  the Codex agent runtime; `/responses` is appended to `base_url` (commonly
  ending in `/v1`). A compatible endpoint must support streamed Responses with
  tool calls; merely offering Chat Completions is insufficient.
- `anthropic_messages` — **Anthropic Messages-compatible HTTPS**, driven by
  the Claude agent runtime. `base_url` is the API root the Anthropic SDKs use
  (`/v1/messages` is appended). Each such provider gets its own Claude config
  directory with Möbius's Claude settings defaults, and every Claude model
  alias resolves to its `default_model`.
- `gemini` — **Google's Gemini API**, driven by the Codex agent runtime.
  `base_url` is the Gemini API root (for Google AI Studio keys,
  `https://generativelanguage.googleapis.com/v1beta`). The engine still
  speaks Responses to the relay; the relay translates each request to
  `POST <base_url>/models/<model>:streamGenerateContent?alt=sse` with the key
  in `x-goog-api-key`, and translates the streamed answer back. The model must
  be one the app declared. Gemini's thought signatures travel in a reasoning
  item's `encrypted_content`, which the engine replays, so the relay keeps no
  conversation state. Namespaced engine tools are declared to Gemini as
  `<namespace>.<name>`; other non-function tool kinds are not offered.
  Google's consumer subscriptions (AI Pro/Ultra sign-in) are not a supported
  credential for third-party agents; use an AI Studio or Gemini Enterprise key.

Each provider's engine home (Codex threads, Claude config and transcripts)
lives with the other engine homes under `cli-auth/app-<id>/`, outside the
app's own storage. The app's storage API and its browser code cannot reach
it, so they cannot read its transcripts or plant engine configuration. This
is not a boundary against an app service, which runs with file access to the
data volume. Deleting the app deletes that home. Older versions kept it in
the app's storage; a one-time startup sweep deletes those copies for apps
that declare a model provider rather than moving them, since the app could
have written to them, so chats on those providers start a new engine thread
reseeded from the chat.

Both runtimes talk to the loopback model relay
(`backend/app/routes/model_relay.py`), never to the provider. The agent
process holds only a server-derived relay token; the relay checks it, attaches
the key it reads from the vault, repairs request shapes compatible providers reject, and
streams the response back unchanged. Today's repairs: Messages requests fold
mid-conversation `system` turns into user turns (the Claude runtime emits them;
many compatible providers accept only user/assistant), and Responses requests
omit `content: null` on replayed reasoning items (some providers emit that and
then reject it as input). The relay answers only direct loopback connections
without proxy forwarding headers.

The app owns its setup UI and connection instructions; its key lives in the
owner's key vault. The app asks for the key named by `secret_name`
(`POST /api/keys/requests`) and the owner enters and approves it in
**Settings › Keys**; it is not a manifest value and never passes through the
app, the agent process or a chat prompt. The models are available while the
owner's grant to the app stands and that grant's sites allow every path the
relay calls for the declared protocol (`POST <base_url>/responses`,
`POST <base_url>/v1/messages` and `/v1/messages/count_tokens` for
`anthropic_messages`, or `POST <base_url>/models/*:streamGenerateContent` for
`gemini`); availability never reads the value. Only the
relay reads the key (logged as `platform:app-<id>`), and it never leaves the
server. The provider
app's endpoint receives the conversation and tool requests, so installing and
connecting one is a substantive trust and billing decision. No background
provider is enabled automatically by adding an app. Runs record only the usage
a provider actually reports: the engines' own prices do not apply to another
company's models, so no cost is recorded, and all-zero usage is recorded as
unknown.

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
When a saved effort is absent from the resolved model's nonempty scale, the
runtime uses that model's service-declared `default_effort`; an empty scale
omits the effort override. The saved choice and per-provider effort memory
remain unchanged, so a later catalogue can restore the owner's preference.

The existing five-minute model-registry refresh owns discovery; failures retry
after thirty seconds and preserve the complete last-good snapshot. A private,
identity-scoped disk cache survives restarts, but cannot cross account changes.
With no valid snapshot the list is empty: the client does not invent models.
Defaults and runtime capabilities come from the same snapshot. Each agent run
owns a temporary frozen SDK catalogue, so another refresh cannot change its
startup metadata. Disposable Codex compaction uses the same discovery,
retirement, effort, and frozen-catalogue lifecycle as an ordinary turn. The
SDK template's usable-window ratio is applied equally
to picker context display; the service may also publish an explicit
`auto_compact_token_limit`. New wire protocols still require runtime updates.

`GET /api/models` retains `providers` and adds provider-keyed `defaults` and
`replacements` for picker/background selection. Claude and Codex discovery is
unchanged. The `identity_broker` manifest transport remains accepted only for
the reviewed `identity` app but inert; uninstalling that app cannot remove the
provider. Other connector apps use HTTPS and their own vault keys.
The default-on `GET/PATCH /api/auth/providers/{provider_id}/enabled` switch
removes a provider from pickers and blocks new turns without deleting chats.

## Boundaries and next steps

- Google Gemini is built in on the same `gemini` relay path. Settings ›
  AI providers › Google Gemini › Connection offers two keys, one in use at a
  time: an AI Studio key (vault key `gemini`, Google's Gemini API) or a Gemini
  Enterprise Agent Platform express-mode key (vault key
  `gemini-agent-platform`, `aiplatform.googleapis.com/v1/publishers/google`).
  Both are kept from agents; the platform fixes each key's destination
  (`providers.GEMINI_CONNECTIONS`), the status reports which `connection` is
  in use, and Disconnect deletes both keys. A third way is a Google Cloud
  sign-in (`gemini_google_cloud`) built on the connectors' Google-account
  sign-in (`connector_oauth`): the Cloud SDK's published client and Google's
  "copy this code" page, then a project picked from the owner's Cloud
  projects. Möbius ships no gcloud and registers no Google app; it seals the
  tokens, refreshes them before expiry, and sends Bearer plus
  `x-goog-user-project` to that project's Agent Platform. Its model list is the
  platform's declaration in `providers.GEMINI_DECLARATION`, with each model's
  documented thinking levels and default as its effort scale.
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
- One provider per app, one protocol per provider, and one vault key.
  OAuth, provider-specific headers, multiple endpoints per app, and further
  protocols (such as Chat Completions, which Codex no longer speaks) need
  separate reviewed contracts, not ad-hoc fields. `gemini` is the first
  translating protocol: the relay rewrites requests and streamed responses for
  it, while the forwarding protocols keep the rule below.
- The raw key stays outside the agent process for both protocols; only the
  relay token is there. Codex shell tools, including helpers in a shared
  host, do not receive it; the Claude engine reads it from
  `ANTHROPIC_API_KEY`, which its shell tools inherit. The token lets code the
  agent runs spend on that provider through the local relay, so it bounds
  disclosure of the key, not spending.
- The relay repairs request shapes only. It does not rewrite streamed
  responses: a provider whose stream is malformed (for example, reused content
  block indices) is incompatible until the provider fixes it.
- Test the exact provider protocol against a matching mock streamed server
  before claiming compatibility with any particular remote vendor. Live paid
  inference should be tested only after the owner explicitly authorizes it.
