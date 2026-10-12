# App-provided models (first version)

An installed app may declare one model provider in `mobius.json`. The accepted
capability contract, rather than editable source, is projected into the same
provider registry used by chats, model pickers, app pickers, and background
agents. That includes owner-built local apps: their `mobius.json` declaration
is accepted with each source Apply (and kept across owner permission edits),
limited to the HTTPS app-secret transport below; the broker transport stays
reserved for the reviewed Möbius · You package. The provider appears shortly
after install/Apply and disappears shortly after uninstall (registry reads are
throttled for one second). Existing chats
retain their saved provider/model selection but
cannot run it while the provider app is absent; Möbius does not silently send
those chats to a different company.

An app-secret declaration names its wire protocol with optional `protocol`:

- `responses` (the default) — **OpenAI Responses-compatible HTTPS**, driven by
  the Codex agent runtime; `/responses` is appended to `base_url` (commonly
  ending in `/v1`). A compatible endpoint must support streamed Responses with
  tool calls; merely offering Chat Completions is insufficient.
- `anthropic_messages` — **Anthropic Messages-compatible HTTPS**, driven by
  the Claude agent runtime. `base_url` is the API root the Anthropic SDKs use
  (`/v1/messages` is appended). Each such provider gets its own Claude config
  directory, and every Claude model alias resolves to its `default_model`.

Both runtimes talk to the loopback model relay
(`backend/app/routes/model_relay.py`), never to the provider. The agent
process holds only a server-derived relay token; the relay checks it, attaches
the decrypted key, repairs request shapes compatible providers reject, and
streams the response back unchanged. Today's repairs: Messages requests fold
mid-conversation `system` turns into user turns (the Claude runtime emits them;
many compatible providers accept only user/assistant), and Responses requests
omit `content: null` on replayed reasoning items (some providers emit that and
then reject it as input). The relay answers only direct loopback connections
without proxy forwarding headers.

The app owns its setup UI, connection instructions, and one encrypted app
secret. The owner enters the key into the app's own browser UI (which writes
`PUT /api/apps/{app_id}/secrets/{secret_name}`); it is not a manifest value and
must not pass through a chat prompt. The core only checks whether the secret
exists when displaying availability; only the relay decrypts it. The provider
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
screen to save/delete its `api_key` secret; the platform does not invent one.
Model IDs are provider wire IDs, not display labels. They must be unique across
installed providers. The provider identity is the stable local app row ID
(`app-<id>`), independent of an app rename or its repository URL. An update can
change labels, efforts, context limits, and models, but the accepted declaration
changes only after the normal reviewed update/Apply boundary.

Möbius · You uses an accepted declaration for its model names, effort levels,
context ceilings, and endpoint too. Its `identity_broker` transport has a
fixed `http://127.0.0.1:8765/v1` endpoint and no app secret; only the
`identity` app with the reviewed `identity_manage` grant may declare it. The
image-owned broker keeps account credentials and signs inference requests.
Other connector apps use HTTPS and their own encrypted keys. Any app model
connection can use the default-on
`GET/PATCH /api/auth/providers/{provider_id}/enabled` switch; only its declaring app or
the owner may change it. Turning one off removes it from pickers and blocks
new turns without deleting saved chats.

## Boundaries and next steps

- Claude and Codex remain built in. The image-owned Möbius broker adapter
  keeps the stable `mobius` provider identity for existing chats but offers no
  models until Möbius · You supplies its declaration. Both broker and external
  app connections use the Responses harness and one picker/agent registry.
- Möbius · You is an optional account/setup app, not a runtime dependency for
  Claude, Codex, or app-provided providers. Its **Möbius models** switch is on
  by default and can be changed while signed out. Turning it off removes only
  Möbius from model pickers and prevents new turns in chats still set to a
  Möbius model; those chats remain saved. The preference lives in shared agent
  settings, so uninstalling and reinstalling the app does not silently reset it.
- App models are currently manifest-declared, not discovered live. An app can
  publish an update when its upstream model list changes. A later version can
  add an app-owned model-discovery service without changing picker consumers.
- One provider per app, one protocol per provider, and one bearer-key secret.
  OAuth, provider-specific headers, multiple endpoints per app, and further
  protocols (such as Chat Completions, which Codex no longer speaks) need
  separate reviewed contracts, not ad-hoc fields.
- The raw key stays outside the agent process for both protocols (only the
  relay token is there, and Codex shell tools exclude it). The token still lets
  code the agent runs spend on that provider through the local relay, so it
  bounds disclosure of the key, not spending.
- The relay repairs request shapes only. It does not rewrite streamed
  responses: a provider whose stream is malformed (for example, reused content
  block indices) is incompatible until the provider fixes it.
- Test the exact provider protocol against a mock streamed Responses server
  before claiming compatibility with any particular remote vendor. Live paid
  inference should be tested only after the owner explicitly authorizes it.
