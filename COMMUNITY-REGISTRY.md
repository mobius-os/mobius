# Community registry and GitHub synchronization

## Decision

The Host is the source of truth for discovery, publisher identity, social
signals, moderation, install receipts, and notification state. GitHub is the
source of truth for source history, releases, issues, forks, and pull requests.

Store browsing must never enumerate or search GitHub repositories on the
request path. The Host serves a denormalized registry projection that includes
both official and community apps. GitHub events update that projection.

The baked `catalog.json` remains only an offline/bootstrap snapshot. It is not
a second live catalog and must not accumulate Host-only state.

## Why the split exists

Repository hosting and marketplace discovery have different access patterns.
GitHub is optimized for repository collaboration. Store browse needs low-cost
text search, stable pagination, category and trust filters, aggregate ratings,
install eligibility, and one compact response for many apps. A Host-owned read
model avoids GitHub API fan-out, rate-limit coupling, and inconsistent results
while preserving GitHub's familiar contribution workflow.

## Host-owned records

The minimum durable model is:

- `publishers`: Host identity, linked GitHub identity, display profile, trust
  state, and moderation state.
- `apps`: stable app identity, owner, slug, channel (`official` or
  `community`), visibility, listing copy, categories, current revision, and
  lifecycle state.
- `revisions`: immutable version, source repository and commit, manifest,
  compatibility contract, check result, publication time, and supersession.
- `artifacts`: content digest, media type, byte size, source commit, runtime ABI,
  object key, signature/attestation, and quarantine state.
- `repositories`: GitHub repository identity, installation id, default branch,
  last observed commit, and reconciliation cursor.
- `lineage`: parent app/revision and child app for every remix.
- `ratings` and `reviews`: Host identity plus target revision. Verified-install
  status is derived from an install receipt for that exact revision.
- `install_receipts`: instance identity, app, revision, artifact digest,
  install/update time, and outcome. Public aggregates must not reveal an
  instance's history.
- `webhook_deliveries`: GitHub delivery id, event/action, repository, receipt
  time, processing state, attempts, and last error. Delivery id is unique.
- `events`: durable app/revision/contribution changes from which per-user and
  per-instance notifications are projected.

The searchable listing document is a projection, not another authority. It
contains only card/detail fields and aggregate counters. Rebuilding it from the
records above must be safe.

## Read contract

### Public catalog

`GET /v1/community/catalog`

Query fields:

- `q`, `category`, `channel`, `publisher`, `sort`
- opaque `cursor` and bounded `limit`
- optional `updated_since` for lightweight refresh

Response:

```json
{
  "items": [
    {
      "id": "app_...",
      "slug": "example",
      "name": "Example",
      "summary": "...",
      "channel": "community",
      "categories": ["Productivity"],
      "publisher": {"id": "pub_...", "name": "...", "verified": false},
      "rating": {"average": 4.7, "count": 41},
      "installs": 1200,
      "latest_revision": {
        "id": "rev_...",
        "version": "1.4.0",
        "source_commit": "...",
        "manifest_url": "...",
        "artifact": {
          "media_type": "application/vnd.mobius.app.module.v1",
          "sha256": "...",
          "bytes": 184221,
          "runtime_abi": "mobius-module-v1",
          "download_url": "..."
        }
      }
    }
  ],
  "next_cursor": "...",
  "catalog_revision": "cat_..."
}
```

This response is anonymous and cacheable at the CDN. Use an ETag derived from
`catalog_revision`, short freshness, and `stale-while-revalidate`. Search and
filter parameters are part of the cache key. Do not mix user rating or install
eligibility into it.

### Identity overlay

`POST /v1/community/catalog-overlay`

The authenticated instance submits the visible app/revision ids. The Host
returns only viewer-specific fields such as the viewer's rating, verified
review eligibility, ownership, followed state, and update notification state.
This response is private and `no-store`.

Separating these responses lets the large discovery payload be shared while
keeping identity state correct. The local BFF may merge them before handing
them to an app, but must preserve the public ETag independently.

### Detail and mutable operations

Detail, publication lifecycle, rating, review, report, remix, and install
receipt operations remain under `/v1/community/apps`,
`/v1/community/publications`, and `/v1/community/installs`. Every mutation uses
an idempotency key. Revisions are immutable; publishing an update creates a new
revision and advances the app only after checks pass.

## Publish and update flow

1. The instance sends a source snapshot or an existing GitHub repository plus
   exact commit. If it sends a compiled module, the module declares the same
   source commit and includes its digest.
2. Host authenticates publisher and repository ownership, creates the pending
   revision, and stores source/artifact inputs outside the catalog projection.
3. Checks validate the manifest, capability declaration, source/artifact
   correspondence, malware policy, size, runtime ABI, and listing content.
4. Accepted artifacts are stored by digest in object storage and served through
   a CDN. Metadata and blobs do not live in the primary relational database.
5. In one transaction, Host marks the revision live, advances the app's current
   revision, updates the search projection, and appends a durable event.
6. Cache invalidation/purge follows the commit. Clients may continue to receive
   the prior catalog revision briefly; both revisions remain internally
   consistent.

Official and community apps use this same path and schema. `channel` and trust
policy change ranking, badges, and review requirements; they do not create a
parallel catalog implementation.

## GitHub App

Use a GitHub App rather than a broad personal token. Install it only on
repositories a publisher chooses. Begin with the minimum repository
permissions needed for the product:

- metadata: read
- contents: read (write only if Host provisions repositories or accepted
  incorporation commits)
- pull requests: read; write only when Möbius must create or update PRs
- issues: read if issue state appears in the Store/Contribute projection

Subscribe only to events used by the projection:

- `installation`, `installation_repositories`, `repository`
- `push` for the tracked default branch or published tag
- `release`
- `pull_request` and, if needed, `pull_request_review`
- `issues` only when issue status is surfaced

Webhook receipt rules:

1. Require HTTPS and verify the signature against the exact raw request body.
2. Reject events for unknown installations/repositories.
3. Insert `X-GitHub-Delivery` under a unique constraint before enqueueing.
   A repeated delivery returns success without repeating work.
4. Validate event and action, enqueue the payload/reference, and acknowledge
   quickly. No GitHub fetch, build, notification, or search update runs in the
   HTTP receipt transaction.
5. A worker fetches authoritative GitHub state with an installation token,
   applies monotonic state transitions, refreshes the projection, and appends
   Host events in the same transaction where possible.
6. Failed work retries with bounded exponential backoff and lands in an
   operator-visible dead-letter state. Processing is safe after restart.

Webhook events are prompts to reconcile, not absolute truth. A scheduled
reconciler compares tracked repositories, open pull requests, releases, and
default-branch commits with GitHub. It repairs missed deliveries and records a
cursor/checkpoint. Manual publication and contribution views may request a
bounded immediate refresh, but ordinary Store browsing never does.

## Contributions and notifications

GitHub remains the place where code review and merge happen. Host stores a
small contribution projection: app, repository, pull request number, author,
head/base commits, review/merge state, check summary, and last observed time.

`pull_request`, review, push, and release processing appends normalized events
such as:

- `contribution.opened`, `contribution.review_requested`,
  `contribution.merged`
- `revision.checks_passed`, `revision.published`, `revision.rejected`
- `app.update_available`, `app.withdrawn`

Recipients and delivery preferences are resolved by Host. Instances consume a
cursor-based event feed (and may receive a push wake-up), acknowledge their
cursor, then refresh only affected records. Notification delivery must be
deduplicated by `(recipient, event, channel)`; a webhook retry cannot alert the
owner twice.

## Prebuilt distribution

Prebuilt modules can materially reduce work on each self-hosted instance:
download and digest verification replace repeated compilation, and identical
versions share CDN/object-cache hits. They do not reduce the app's runtime
memory or CPU once loaded.

The Store uses a prebuilt module only when all are true:

- revision checks are accepted;
- artifact source commit equals the revision source commit;
- runtime ABI is supported;
- downloaded bytes match the declared digest and size;
- signature/attestation policy passes.

Otherwise installation stops with a clear compatibility/integrity outcome.
Source compilation is a deliberate developer/import path, not a silent
fallback for a public Store install. This keeps “Install” deterministic and
makes failures observable instead of quietly consuming unexpected resources.

## Rollout

1. **Host registry minimum:** database records, object storage, catalog/detail
   reads, publication lifecycle, and official-catalog import. Backfill current
   official entries into the same schema.
2. **Fast discovery:** CDN-cacheable `/catalog`, cursor pagination, search
   projection, ETags, and private overlay. Point Store browse to this endpoint;
   retain the baked snapshot only for offline startup.
3. **GitHub synchronization:** register the GitHub App, webhook receiver,
   durable queue/delivery log, worker, reconciliation job, and contribution
   projection.
4. **Efficient installs:** serve digest-addressed modules, teach Store to verify
   and install them, then measure transferred bytes, compile time avoided,
   cache hit rate, and integrity failures.
5. **Notifications and social state:** cursor event feed, deduplicated delivery,
   verified-install reviews, ratings, remix lineage, and moderation queues.

The first two phases are the discoverability prerequisite. GitHub-only browsing
should not be shipped as an interim marketplace because it establishes the
wrong performance and consistency contract.
