# Live recovery capabilities

Normal Möbius containers start the private recovery target on port `18002`
when `MOBIUS_RECOVERY_CAPABILITY_PUBLIC_KEY` is configured. The value is the
unpadded base64url encoding of a raw 32-byte Ed25519 public key. The normal app
keeps running; live recovery does not change `MOBIUS_BOOT_MODE`.

The launcher supplies the worker with a bearer token no longer than 512 ASCII
bytes in this exact form:

```
mrc1.<payload>.<signature>
```

- `payload` is unpadded base64url of UTF-8 JSON produced with sorted keys and
  compact separators (`json.dumps(claims, sort_keys=True,
  separators=(",", ":"), ensure_ascii=True)`).
- `signature` is unpadded base64url of the 64-byte Ed25519 signature over the
  ASCII bytes `mrc1.<payload>`.
- Every capability has exactly `v=1`, `iss="mobius.you"`,
  `aud="mobius-recovery-target"`, `sub` (Möbius instance id), `dep` (Railway
  deployment id), `scp`, and integer epoch seconds `iat`, `nbf`, and `exp`.
- A probe has `scp="probe"`, no `sid` or `bid`, a maximum 60-second lifetime,
  and authorizes only `GET /v1/health`.
- A repair session has `scp="session"`, `sid` (recovery session id), and `bid`
  (the exact target boot id). It has a maximum 3,600-second lifetime and
  authorizes health, exec, filesystem, and self-revoke operations.
- Identifiers contain 1–128 printable ASCII bytes, and the complete signed
  token must still fit the 512-byte wire limit. The launcher rejects oversized
  aggregate claims before issuance; its compact managed instance, deployment,
  and session identifiers fit this bound. Unknown or missing claims are
  rejected. `iat <= nbf < exp`; expiry is enforced without clock skew. A
  30-second positive skew is accepted only for `iat` and `nbf`.

`MOBIUS_INSTANCE_ID` and `RAILWAY_DEPLOYMENT_ID` are required target
configuration. `sub` and `dep` must match them. The entrypoint also generates a
fresh, unpredictable 32-character base64url `MOBIUS_RECOVERY_BOOT_ID` for every
container start. Session `bid` must match that exact boot, so a restart rejects
all tokens from the previous process even if Railway retains its deployment id.
Missing or invalid local identity disables the live target without blocking the
normal Möbius app.

Existing managed services receive the instance id and verification key only
through a digest-bound core rollout. Keep live-session admission disabled until
every eligible service has restarted on that rollout and proved its exact
deployment and boot identity; quarantined services remain fail-closed.

The entrypoint spawns the target early, best-effort, and never probes or waits
for it, so target initialization or bind failure cannot delay the normal app.
A unique root-owned `mktemp` file keeps normal target endpoints at
`503 attach_not_ready` while entrypoint performs boot-time mutations. Immediately
before handing off to uvicorn, entrypoint writes `ready:<boot-id>`. The target
validates the exact boot-bound marker as a regular root-owned `0600` `/tmp`
file, caches readiness, and unlinks it. This is an attachment gate, not an
application-health gate: repair remains available when uvicorn is degraded or
fails its health endpoint, while it cannot race initialization or reuse a stale
marker from another boot.

Every endpoint, including `/v1/health`, verifies the signed bearer. Successful
live health responses include `deployment_id` and `boot_id`; the launcher can
exchange a short read-only probe for a session bound to that boot. The server
admits at most 16 concurrent request handlers, gives the complete request line
and headers five seconds, and gives an authenticated body at most 30 seconds or
the time remaining until `exp`, whichever is shorter. An exec still running at
`exp` has its complete supervised process tree killed before the target returns
`auth_expired`.

`POST /v1/revoke` accepts exactly `{}` and is intentionally available before
attachment readiness for session capabilities only. It derives `sid`, `dep`,
and `exp` from the verified token and retains the released worker's exact
response contract:

```
{"status":"revoked","deployment_id":"<dep>","session_id":"<sid>"}
```

The same still-valid token may retry its own revoke. A successful response
guarantees subsequent normal endpoints reject that `sid` until `exp` and that
only that session's active exec supervisors have been killed. Revoked entries
are pruned at expiry and held in a bounded denylist. A new boot has a new `bid`,
so tokens from an earlier boot are rejected independently of this process-local
denylist.

The legacy `MOBIUS_BOOT_MODE=recovery` opaque-token path remains available for
rollout compatibility; it does not use this capability format.
