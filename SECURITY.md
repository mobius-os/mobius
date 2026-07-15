# Security

Mobius is a **single-owner, self-hosted** application. The threat model
assumes one trusted owner on their own device, with the primary risk
being external attackers reaching the public HTTPS endpoint.

## Hardened boundaries (technical enforcement)

- **Authentication:** bcrypt-12 password hashing, HS256 JWT (30-day expiry),
  rate-limited login (5/min per IP + global backoff after 10 failures).
- **PKCE OAuth:** Server-side token exchange for CLI provider auth.
  No client-side token exposure.
- **Encryption at rest:** API keys stored with Fernet (AES-128 + HMAC),
  derived from SECRET_KEY.
- **TLS and response headers:** Caddy auto-provisions HTTPS certificates. The
  backend sets HSTS (1 year, preload), X-Frame-Options,
  X-Content-Type-Options, Referrer-Policy, and Permissions-Policy so those
  protections do not depend on the front proxy. The bundled Caddy deployment
  also sets a resource CSP and mirrors the frame policy; other operators may
  supply a different resource CSP at their proxy.
- **CSP is deployment policy, not the app authorization boundary:** the
  backend does not impose a shell-wide resource CSP. The bundled Caddyfile does
  apply one, including `frame-ancestors 'self'` on every route except the exact
  inert embedded-chat bootstrap route. Opaque frame isolation and scoped,
  server-verified principals remain the security boundary in every deployment.
- **Mini-app isolation and tokens:** shell-mounted app frames omit
  `allow-same-origin`, giving them an opaque origin. They cannot read shell
  localStorage or the owner JWT. Each receives a refreshable app JWT bound to
  the live app id, installation nonce and owner token epoch; server routes
  enforce the app's exact permissions.
- **Rate limiting:** 120 req/min global, 3-5/min on auth endpoints.
  Uses TCP peer address (not X-Forwarded-For).

## Accepted trade-offs

These are intentional design decisions appropriate for a single-owner app:

- **Owner JWT in shell localStorage:** opaque mini-app frames cannot read it,
  but script execution in the shell document itself remains equivalent to the
  owner. Moving the shell session to an HttpOnly cookie would further reduce
  that shell-XSS exposure if the threat model changes.
- **`null` CORS origin:** Required for sandboxed mini-app iframes to call
  the API. Mitigated by scoped tokens — even if a mini-app reads the
  iframe's token, it can only access storage/proxy/AI endpoints.
- **`unsafe-inline` in style-src CSP:** Required for server-injected theme
  CSS. The owner controls the theme content.
- **90-day service token:** Used by cron scripts. Stored at
  `/data/service-token.txt` (chmod 600, mobius user only). Acceptable
  because only the container's mobius user can read it.

## Agent security model

The agent runs as the `mobius` user with full write access to `/data/`, including
the live platform checkout. Security against agent mistakes is primarily
prompt- and review-based; a separate recovery service stays reachable if the
editable platform is broken. This is appropriate because:

1. The agent is a frontier AI model that follows instructions reliably.
2. The owner chose to give the agent control — restricting it defeats
   the purpose.
3. Recovery is isolated from the editable production process and provides the
   rollback boundary.

## Opaque embedded-chat contract

`window.mobius.chat` creates three documents. The outer sandbox restriction
propagates inward; adding `allow-same-origin` to the nested frame would not
restore origin privileges removed by its opaque ancestor.

| Transition | Credential | Server trust decision |
|---|---|---|
| Shell → opaque app frame | App JWT only; never owner JWT | Live owner epoch, app id and installation nonce |
| App → nested chat navigation | None; exact URL is `/shell/embed/chat` | Document stays blank/inert |
| App → capability mint | App JWT in `Authorization` | Exact app-owned chat, installation nonce, instance and role |
| Parent → child `INIT` | Random one-use grant in message memory | No trust in `null` origin, window identity, chat/instance fields, fetch metadata or handshake success |
| Child → server exchange | One-use grant in `Authorization` | Atomic consume; exact owner epoch, app nonce, chat ownership, instance, role, operations and expiry |
| Authorized ChatView → APIs | 15-minute `chat_embed` JWT in memory only | Signature plus live grant/app/chat checks on every request |

The first role is `participant`: exact-chat read/send/stream/stop, chat runtime
settings, attachments/media, and read-only model/provider metadata. It cannot
list owner chats or open owner chat-summary/agent-context surfaces. Source
window, protocol namespace and instance correlation remain useful routing
guards, but are explicitly not authorization.

Bootstrap grants are single-use. A successful session refresh atomically
revokes older sessions for that embed instance only after the replacement
exchange succeeds. The old UI/session stays in memory across a transient or
ambiguous refresh failure; every retry mints a fresh one-use grant with bounded
backoff, and frame replacement/destruction cancels retries. Media tokens are
keyed to the exact in-memory session, so the successful swap also discards an
otherwise-cached token chained to the revoked grant. Iframe destruction
explicitly revokes the instance. Owner epoch changes, app uninstall/nonce
rotation, chat ownership changes and expiry also revoke access. Long-lived SSE
responses recheck session liveness at event/keepalive boundaries. Revocation
blocks later API/SSE use but does not itself cancel an agent process already
started for the chat; use the scoped stop operation when cancellation is needed.
An exactly stolen session bearer remains a conventional bearer limitation;
memory-only handling, short expiry, exact scoping and server revocation bound
its usefulness.

Only the inert bootstrap route omits `X-Frame-Options: SAMEORIGIN`, because an
opaque ancestor cannot satisfy SAMEORIGIN (and `frame-ancestors 'self'` has the
same ancestor problem). All other routes retain the global frame denial.

Opaque frames remain the safe default. A separate future design may give
reviewed high-capability apps real per-app origins for IndexedDB/OPFS, robust
offline outboxes and APIs such as `getUserMedia`; that must not restore
`allow-same-origin` on the shell origin.

## Reporting vulnerabilities

If you find a security issue, please open a GitHub issue or contact
the maintainer directly. This is a hobby project — there is no bug
bounty, but reports are appreciated and will be addressed promptly.
