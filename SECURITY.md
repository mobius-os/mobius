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
- **TLS:** Caddy auto-provisions HTTPS certificates. HSTS enabled
  (1 year, preload).
- **CSP:** Strict Content-Security-Policy via Caddy — scripts limited
  to self + esm.sh CDN, no external connect-src.
- **Protected files:** Credential-handling components (login form, setup
  wizard, provider auth) are root-owned and read-only (chmod 444),
  re-enforced on every container boot.
- **Mini-app isolation:** Iframes receive a scoped JWT that cannot access
  auth, settings, or chat endpoints. The full owner token never enters
  the iframe context.
- **Rate limiting:** 120 req/min global, 3-5/min on auth endpoints.
  Uses TCP peer address (not X-Forwarded-For).

## Accepted trade-offs

These are intentional design decisions appropriate for a single-owner app:

- **JWT in localStorage:** The owner is the only user on their own device.
  XSS is mitigated by CSP.
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
