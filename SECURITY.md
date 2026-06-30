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
- **TLS:** Caddy auto-provisions HTTPS certificates. HSTS (1 year,
  preload) plus X-Frame-Options, X-Content-Type-Options, Referrer-Policy,
  and Permissions-Policy are set by a backend middleware (`main.py`), so
  they hold regardless of which proxy fronts the app (the external
  production Caddy does not set them).
- **No Content-Security-Policy on the app:** the bundled Caddyfile carries
  a strict CSP, but it is NOT enforced on the external-Caddy production
  edge — and a CSP would not close the real risk anyway, because a
  same-origin mini-app reads the owner JWT and can exfiltrate it via
  `/api/proxy` regardless of any CSP. See the accepted trade-off below and
  `.pm/172` for the real (HttpOnly-cookie session) fix.
- **Protected files:** Credential-handling components (login form, setup
  wizard, provider auth) are root-owned and read-only (chmod 444),
  re-enforced on every container boot.
- **Mini-app tokens are scoped (least privilege, not an isolation wall):**
  mini-apps are given an app-scoped JWT that can't reach auth/settings/chat
  endpoints. But because mini-app iframes run same-origin
  (`allow-same-origin`), their JavaScript CAN read the owner JWT from the
  shell's localStorage — so the scoped token is a sensible default, not a
  boundary. See the accepted same-origin trade-off below and `.pm/172`.
- **Rate limiting:** 120 req/min global, 3-5/min on auth endpoints.
  Uses TCP peer address (not X-Forwarded-For).

## Accepted trade-offs

These are intentional design decisions appropriate for a single-owner app:

- **JWT in localStorage, readable by mini-apps:** the owner is the only
  user on their own device and every mini-app is authored by the owner's
  own agent. A malicious or compromised app could read the owner JWT and
  exfiltrate it (same-origin localStorage + `/api/proxy`); the accepted bet
  is that the agent-authored, single-owner model keeps that low-risk. The
  real fix, if the threat model changes, is an HttpOnly-cookie session
  (`.pm/172`).
- **`null` CORS origin:** Required for sandboxed mini-app iframes to call
  the API. Mitigated by scoped tokens — even if a mini-app reads the
  iframe's token, it can only access storage/proxy/AI endpoints.
- **`unsafe-inline` in style-src CSP:** Required for server-injected theme
  CSS. The owner controls the theme content.
- **90-day service token:** Used by cron scripts. Stored at
  `/data/service-token.txt` (chmod 600, mobius user only). Acceptable
  because only the container's mobius user can read it.

## Agent security model

The agent (Claude CLI) runs as the `mobius` user with full write access
to `/data/`. Security against agent mistakes is **prompt-based** — the
agent skill file instructs it on what to protect and how to recover.
This is appropriate because:

1. The agent is a frontier AI model that follows instructions reliably.
2. The owner chose to give the agent control — restricting it defeats
   the purpose.
3. Critical files (auth components, backend code) are technically
   protected anyway (root-owned, outside `/data/`).

## Reporting vulnerabilities

If you find a security issue, please open a GitHub issue or contact
the maintainer directly. This is a hobby project — there is no bug
bounty, but reports are appreciated and will be addressed promptly.
