#!/usr/bin/env python3
"""Bootstraps the agent-editable skills layer at /data/shared/skills/ on boot.

The system prompt (skill/core.md, baked + owner-curated) is the stable
"constitution"; the detailed how-to skills live here, under /data, so the agent
(and the nightly Reflection agent) can IMPROVE them and write new ones. Like the
knowledge graph, this is CREATE-IF-ABSENT — reseeding would clobber the agent's
own skill edits.

Propagation policy, precisely (the code below is the contract): on first boot
the whole seed tree is copied. On every later boot we add missing seed skills
and apply only explicit, hash-gated migrations:
an existing file is replaced when it is byte-for-byte a known baked predecessor,
while every owner/agent-edited copy is preserved. A normal baked-seed edit does
not propagate until its predecessor hash is deliberately registered below; this
keeps urgent fix-forward migrations possible without blind overwrites.

Retired flat seed skills use a separate one-way contract. Every known baked
generation is removed from the active skills directory. A customized copy is
not discarded: its exact bytes are moved to `/data/shared/retired-skills/`,
outside runtime discovery, before the active legacy path is removed. This lets
one overloaded skill id disappear without erasing owner notes or leaving stale
instructions active forever.

One narrow exception, and it is deliberate: a registered digest may name a
known-bad OWNER-CURATED generation rather than a baked one, when that exact
content is unsafe to leave in place (today: a `cron.md` copy that tells app
jobs to read the owner service token). Such an entry is annotated with its
provenance, because it cannot be reproduced from this repository's history and
a reviewer would otherwise have no way to audit what is being overwritten. It
remains an exact-hash match -- an owner edit that differs by one byte is still
preserved. App-owned
skills are not part of this seed tree; they arrive through manifests and their
generic ownership sidecar.
`.seed-version` remains a reserved internal name for instances that carry the
retired marker; bootstrap neither reads nor writes it.

Seed source: /app/scripts/seed-skills/ (baked), falling back to the in-repo
backend/scripts/seed-skills/ for dev. Run from entrypoint after
init_chat_summaries.py.
"""

import hashlib
import os
import pwd
import shutil
import sys
from pathlib import Path

# Entrypoint executes this file by absolute path, which puts /app/scripts rather
# than its sibling /app package on sys.path. Resolve the trusted runtime root
# from this script so both boot reconciliation imports work standalone.
_APP_IMPORT_ROOT = str(Path(__file__).resolve().parent.parent)
if _APP_IMPORT_ROOT not in sys.path:
  sys.path.insert(0, _APP_IMPORT_ROOT)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SKILLS = DATA_DIR / "shared" / "skills"
RETIRED_SKILLS = DATA_DIR / "shared" / "retired-skills"
# Update only byte-for-byte baked copies; an owner/agent-edited file is never
# touched. A set preserves every known unmodified predecessor when one skill
# needs more than one fix-forward migration over its lifetime.
_UNMODIFIED_MIGRATIONS = {
  "agent-coaching.md": {
    # First shared coaching seed, before Codex gained an exact thread-fork API
    # and transcript reseeding was removed from the coaching contract.
    "1730bcf614f0689f2c6459396c342f4090c1374eeb62450e21a81463fe0098bd",
  },
  "platform-maintenance.md": {
    # Baked copies before the container-boundary guidance. Both hashes are
    # released, untouched generations; owner-edited copies remain protected.
    "bcc617354747c49ddad7fa1f419cf921fd7280909358096323cdbc427ad063c3",
    "b591d15e335c72c0acf394ca7ce4b0daa633e124a487df7a713847cafc13ab6d",
  },
  "goal-planning.md": {
    # First dependency-aware Goal-plan seed. Replace only the untouched copy
    # so existing instances learn the completion preflight without clobbering
    # any owner-authored planning guidance.
    "2adb39457e0ec2ee9d9a3596cb96e5a6240bae23d725e6459ba0f6e77d5474c4",
    # Dependency-aware plan seed before provider-neutral automatic promotion.
    "a3edc5fcc453a5305102e144c2d58ae16b828612ac93a6a7442be7e267779f59",
    # Hierarchical Goal seed before provider-neutral automatic promotion.
    "bca228c745881bfffdad5d7adaab3c62871e6f62801252784fb0522787cfb850",
    # Provider-neutral promotion seed before phase-transition rechecks and the
    # first-class platform promote_goal affordance.
    "0c2b88ff8a79ff05f75ebaa60af2899f0b9ed27d0a23bfff83b54f4e2a1de97a",
    # Phase-transition seed before the mandatory turn-local routing decision.
    "7a80e90870f75be7c8802f421e76ac21790f1ef812d4a4d0d923d474e5dadd2d",
    # Mandatory turn-local routing seed before every unfinished Goal wait had
    # to declare a durable monitor or present an explicit owner action.
    "07ac534c61899fc1154dc4ba99a4eda0f648b2f33c839dc65879d12952e09533",
  },
  "reflection.md": {
    "c0f57c227f61cd8539a56b70eadfbbe2212125c23b7137472dd173a578baacd8",
    # Resource-stewardship predecessor: propagate the adaptive analytics
    # and self-throttling contract only to untouched copies.
    "865dd241a99668b026cd9be90c472cfde562210df51f729b2c25929f6b3bd60a",
    # v15 baked copy: route app work through the base + matching extensions.
    "cba6c0c7dd97384bbe3bfa19e78707bfa272085843bab5102279a937467e5d17",
    # Pre-reconciliation seed: still scanned provider credential caches and
    # lagged the checkpoint-ordered live Reflection procedure.
    "1086688efd4dede48ebc95b12b92fb958280e67896a53e52e02cd5def3aa265f",
    # Pre-Agent-Coaching seed: kept a separate Reflection interview method and
    # still referenced the retired manager-session evidence helper.
    "daf11f9e65e347334b57b5f5607a7a3fe4135349b4cbe20d94e53444f37e9535",
    # Current upstream predecessor, including the learning-loop and receipt
    # refinements that Agent Coaching must preserve during migration.
    "3b9af10ffe3db873df8ba7fd9719c126e1de2951c10c7b85cac9f47f27c82217",
    # Exact upstream seed immediately before coaching became
    # exact-session-only. Only untouched Reflection copies may migrate.
    "e5099eee9c5479312a0565c95477d59bab78da325c1622efc7b24d2a81459f34",
  },
  "cron.md": {
    "289336d78ad4268110360f12faac5512d5a53b66aa31c2a6ddd1a44f538f2559",
    "ed100cb496b887a7951adc967e92cda1449c4f8594f7859fbd32762221d24914",
    # Every remaining baked generation that still shows the owner service token
    # as the example an app job should read.  An instance sitting on one of
    # these keeps the pre-app-token guidance until it is registered here, so
    # the migration is only as complete as this set.
    "76ab03fd128157715b388b16146239217f57bba62c5248b8192a39639d0200b1",
    "e4539739815b80b4c52ca2c56f2a4055e7a4a12cd1843c0cb5077a149547acd1",
    # Pre-#612 baked copy: remove guidance for the retired cron-emit wrapper.
    "55a350281a94ecabf35297d4aef019eedaed28378ac0a2a440815120c6219ba7",
    # Locally curated pre-app-token copy. It tells scheduled app jobs to read
    # the owner service token, contradicting supervised $APP_TOKEN authority.
    # Not a baked generation: it exists only on instances whose owner edited
    # this skill before the app-token migration, which is why the digest
    # cannot be reproduced from this repository's history.  Registered
    # deliberately -- see the propagation policy note in the module docstring.
    "16055ea6ba6e4663636f87fde9868aa98d49ab39c5037ff90fa673d96c259cd9",
  },
  "embedded-app-agent.md": {
    # Pre-#612 baked copy: clarify that an accepted overlapping run may skip.
    "e58970bb7357030b9ac9c72e3b547d3bc93cdb75a1442dc5bb92db6174beebad",
  },
  "theming.md": {
    # v22 baked copy: point shell-break guidance at the operator reset path.
    "7fb5ed4c1e29e6822b56394c089984a1a7e5da1bdf552a21ff0cbdc6413bd998",
    # Owner-curated copy that still sends agents to the removed /recover UI.
    # The current seed retains its useful shell-scale invariant and removes the
    # dead route, so this exact known-bad generation is safe to reconcile.
    "1d655ec09d7f25c831e105411d59b8428b07defc6f58416f8290a8c1b08ca594",
    # Untouched baked and locally curated mapi generations that still wrapped
    # whole CSS documents inside hand-escaped JSON. Migrate both exact copies
    # to the raw text boundary; any owner-authored variation remains protected.
    "7994321819a43708debb77104edea12f785beb46d0933c2b94f93cf418f510ee",
    "cd4d6f03f6ba87d8b3d1799aa81c3ab5444900362e56edc3e48803fa1f1fee4b",
  },
  "workflows-app.md": {
    # Resolved the app by slug=="workflows". An install whose preferred slug
    # is taken gets a fallback (this instance's row is 'workflows-2'), so the
    # lookup silently found nothing and the skill's own "skip silently" branch
    # hid the failure. Now keyed on the manifest id, as bootstrap already is.
    "895dfa031e1a633ceac9a1f16895d43e5084d052c0616bd79a7a4005d06ba324",
  },
  "images.md": {
    "248ea31e13d2d2d84a5acfca13526aa8ebfa3d90e9ee4bf55cfb72d47937f7d1",
    # v20 baked copy: publish the exact generated image, never newest-by-time.
    "29039a6fc5c9281794247eda5d0bbf66e969a1a260e9ed56c69ee6e1cd175f7c",
    # Pre-direct-result seed: sent Codex through a backing file under the
    # protected credentials tree instead of returning the tool result.
    "75271f2a704a6db349e2529d76ddfa505f0ceb1a7f33894a6d4bba23dbd317bb",
  },
  "notifications.md": {
    # Pre-slimming seed: open_item mechanics still lived in core.md, leaving
    # this policy owner without the executable recipe.
    "309e5969df6f589cc82c17b450e7596a00bae87ef77ab2923a9b0de061ed146e",
    # Untouched baked and locally curated mapi generations that interpolated
    # ids through nested shell quotes. Replace only those exact copies with
    # stdin-delimited JSON guidance.
    "6fa9c177db508ef05dfc73de224cd3f33350c79d8f807ac9909942d761f21103",
    "db0c1138ffd0890936ccdeba6ced4ccde867ba3044eeef0a5c87cdf2f279eaaa",
  },
  "building-apps.md": {
    "4126b40d209c422184e0135f611bb9f4197ea280fa27e63cd71c806f8b5ebd79",
    "91b655952d55b37fda0be82e3914c3b09e67ca7c5f5a575d315fb2ca75ef08f1",
    "563dcd7bfa1ff7cbad074d98462eb9755a010a15bf340c7f594fc7f6825a6a86",
    # v17 baked copy: replace watcher publication with explicit apply.
    "a8591f03bd5fb6eb0cfcd811d6d6d4309657f2f4e9e8e11ded4cbefbd77facfd",
    # v20 baked copy: delete by exact id and retain its recovery receipt.
    "5a6bafaa654071c4af5a5c7a201e23e4b0294c392ccb2b9afd7c2b18e17ff3fe",
    # Pre-slimming seed: duplicated the component catalog's full UI skeleton
    # inside an advanced runtime guide that is always read with quickstart.
    "294a4a207a2528245b006877ff486aa79fdf401b738afbf43aaf2b67b3e7eead",
  },
  "building-apps-quickstart.md": {
    "7d8af2664b37a69b88e48c2a28140c15556202c3c7ce30d77816c203d1959fcb",
    # v16 baked copy: replace the unreliable CSS iframe selector.
    "4c2b080bcc91626f761c5823ea00d324667b9710f6757931823e22e9c8b5c2b1",
    # v17 baked copy: teach the coherent apply/retry lifecycle.
    "85a4b5ce5b47c81fa53bec90d530adfe433c0d2f7f31363427b6c792bd332e05",
    # v18 baked copy: co-read completion policy and use compact app discovery.
    "02fda2ea04f3c0ce808ef0db4b1fe4e893924bd019a5bf102a46749ef9142510",
    # v20 baked copy: retain the apply receipt's numeric id across the flow.
    "68c84158a9255ab53686968ed4ec8f594c460483bec0e90dcfa472682c1d9b70",
    # Owner-curated pre-preview-helper copy: valid local prose, but stale
    # capture and apply receipts now bypass readiness and relist app state.
    "c8d1dada4ba2a4ad29da159edf654cf99175a372569f753100398a8a307bc7d6",
  },
  "resolving-app-git.md": {
    # v17 baked copy: resolution is an explicit installer replay.
    "6d462f1711891a182c26e212a1ec8fc922eeb02faee45e70ab9b2becfba24f5a",
    # Pre-v17 baked copy still describing the retired source watcher
    # ("finish by saving — the watcher does the rest"). It contradicts the
    # resolver prompt in routes/apps.py, which sends the agent here and then
    # tells it to run resolve_app_update.py. Unmodified copies migrate.
    "4911c6db2d3d47eb7c3c206b53ca9be9459619f149a78c06c02711422b941127",
    # Owner-curated resolver guide predating the mandatory policy/review/finalize
    # modes. Its bare command now fails argparse before doing useful work.
    "6bbfe07a575734c4bc0b1d84e40dbaab9d9689671825e7a42383b4f2e673112a",
  },
  "app-component-shapes.md": {
    "0320609ff924a0954c20d5e5db91ed3681d421d76f6804b24552eb6e8fa5eb31",
    # v16 baked copy: keep routine app builds from loading the catalog.
    "91243377242700acb5093165af58c372bed0005f358d3a4b26774aeb2ef8a365",
  },
  "visual-testing.md": {
    "9525b36b945c2a0b4cb02806081bb674f38e865b6e1c3961226112e1dbbc16ec",
    # v16 baked copy: use iframe refs and preserve React's inert cleanup.
    "a0648921b9c9ea2423e8abd52aa57e71e7bebfa1736073fcf3bfcaec3749ad19",
    # v18 baked copy: avoid measured textbox and opaque-frame wait failures.
    "5db160b2d796d54ec320119cbdbbb2860a78cfd703cfe37667626d23abc8e4d9",
    # v19 baked copy: replace speculative selector examples with grounding.
    "bf58243aeb1779eb0a94d5404a99c2132e55d60542cbb555fc50bc5cf65349fe",
    # Owner-curated copy using the retired opaque-frame selector path. The
    # merged seed preserves its media-order and browser-cleanup safeguards.
    "2b14caf13f4cc7c76868f9566f2c0789f6e9b8c0fefac897e1d9ebda11dff8bf",
  },
}

# Every distinct recovery.md shipped on main. This legacy file mixed external
# emergency access with ordinary maintenance, Git undo, and soft-delete APIs.
# The focused replacement skills are platform-maintenance.md and
# undo-and-restore.md. Current-seed hashes belong here by design: retirement,
# unlike a fix-forward replacement, must also remove the latest untouched copy.
_RETIRED_UNMODIFIED_SKILLS = {
  # Agent Coaching subsumes the former on-demand manager ritual with a neutral
  # feedback-first method that Reflection can also use for self-improvement.
  # Preserve customized copies in retired-skills, but keep no parallel active
  # skill whose framing or procedure can drift from Agent Coaching.
  "manager-session.md": {
    "3a3535b7bfa5d8214a5559567c1e7fb4b7218f404e8a8cf1426455cf46af075d",
    "8375041d3b37cd3f97d8a3d554c85485a35dc9c8b1466abab2c5f52ff44e1c18",
    "f1157721e9c874cd69c961bd018d13d0233bac1e3720b1d5655e06033bc20aea",
  },
  "recovery.md": {
    "0a028cfea8427d9c7b7cd9522da64caf196554f268957e305dc521bb7d6faa3d",
    "0e68863722e977c2ca78754fb2699ac0c19906062bbc63acc3a1aab41b4ea260",
    "0f58a4b5d83dfab083549cc1209d3f7835c973b61928752543be286fad360017",
    "0fbd53e4ac9d67ed7c2731271f5f4ccf5a74bb8348bd5885605bc3c2b5a2b7f4",
    "109cc54c47595a2b9f7d09bbfbfc0f7b6be919ef3ca2a2c16cd2d8fc5d6533e7",
    "157700e43f17cf81ef0cb993c8bbc887ae5dc1303508778382f136e7f80c3d8c",
    "3c4db1828fd893738f0e241bcecb7da218159656ea3a2b63c35c16d4d771febe",
    "467542740b110e6fbd21e86bfbd247551c676385a46d64a5130a2a648c47346a",
    "4805da9cc334d5a1c0e0d5e30d0b2655bd4a4de3e1d1f470b73a01092d8d18cb",
    "5fbf576db34553b5552e83383590435a8e96dbfcdf71837dbe3de4f4ca1c1d45",
    "59af11e6f1313f1e0df4fc7905cf018786eb648116aaf7e8bcafea7aa7a4c9fe",
    "6e6e82e02287e8bb38195fb021ea25cee2dc4e27da1a6ce1e2a0143fb1d82d87",
    "79d4a1ff10cf2a28d8e74123aa95e1ba006f1ede299d64c619b2b15d0c89ce57",
    "8cda43c1637cdb66702a70c53d1682629e6923ccf157676faf09582109b8e570",
    "a27e02e948b417dddecf0f7d81c6d00e3c7a044e7901cd3c026031a2f05eb978",
    "b4b634e93d43b635cf46ed37a12f3f419d3bee4d926cb912e42f9ddb1098dd94",
    "c679f6e1f1cee15f18704e21b88c6ef1acdb67ca10ca0e80757987a1d935465b",
    "cb283d498f55a188f9e8bed0664afb0472ec76f2ddfd421a007f844b720679f5",
    "e4b2866319e5aa59f688e32f2e5ff3ddf262f339c1404ce6e451fa0857c3f995",
    "e648e1d45b43c3a0360a244521f1387f52ee5c5e48eb7d5d2db9bddcdf86ae0e",
    "ef62abb0d03d740f99add1b6f3938f780b34439cb0025616cb9dc5f74f779633",
    "f72a51b41f1cde7ca9b7bf00029a33bc90203a5f252d274381fccddf2040a4a0",
  },
}

_SEED_CANDIDATES = [
  Path("/app/scripts/seed-skills"),
  Path(__file__).resolve().parent / "seed-skills",
]


def _seed_dir() -> Path | None:
  return next((p for p in _SEED_CANDIDATES if p.is_dir()), None)


def _chown_mobius(path: Path) -> None:
  """Make the tree mobius-owned + writable so the agent can edit skills."""
  try:
    m = pwd.getpwnam("mobius")
  except KeyError:
    return
  for p in [path, *path.rglob("*")]:
    try:
      os.chown(p, m.pw_uid, m.pw_gid)
      os.chmod(p, 0o775 if p.is_dir() else 0o664)
    except (PermissionError, OSError):
      pass


def _write_index() -> None:
  """Regenerates skills-index.md via app.skills; best-effort.

  This script runs standalone from the entrypoint (before uvicorn), so the
  app package import can fail on a badly broken tree — the index is a
  convenience surface, never worth failing boot over. The server-side install
  paths regenerate it too, so a skipped boot write self-heals on the next
  install.
  """
  try:
    from app.skills import reconcile_installed, write_index

    # Startup half of the installer's crash-recovery contract: repair any
    # install a crash interrupted (finalize published, discard staged) before
    # the index snapshots the tree.
    repaired = reconcile_installed(SKILLS)
    if repaired:
      print(f"init_skills: reconciled interrupted install(s): {repaired}")
    write_index(SKILLS)
    print("init_skills: skills-index.md regenerated")
  except Exception as exc:  # noqa: BLE001 - boot must not fail on the index
    print(f"init_skills: index generation skipped ({exc})")


def _retire_legacy_skills() -> tuple[int, int]:
  """Remove baked legacy seeds and archive customized flat copies exactly."""
  removed = 0
  archived = 0
  for name, baked_digests in _RETIRED_UNMODIFIED_SKILLS.items():
    path = SKILLS / name
    if not path.is_file():
      continue
    try:
      content = path.read_bytes()
    except OSError as exc:
      print(f"init_skills: could not inspect retired {name} ({exc})")
      continue
    digest = hashlib.sha256(content).hexdigest()
    if digest in baked_digests:
      try:
        path.unlink()
      except OSError as exc:
        print(f"init_skills: could not remove retired {name} ({exc})")
        continue
      removed += 1
      continue

    # A content-addressed archive is idempotent across a crash between writing
    # the archive and unlinking the active path. Never overwrite different
    # owner bytes, even under an astronomically unlikely digest collision.
    archive = RETIRED_SKILLS / f"{path.stem}-{digest}.md"
    try:
      RETIRED_SKILLS.mkdir(parents=True, exist_ok=True)
      if archive.exists():
        if archive.read_bytes() != content:
          raise OSError("archive digest collision")
      else:
        archive.write_bytes(content)
        if archive.read_bytes() != content:
          raise OSError("archive verification failed")
      path.unlink()
    except OSError as exc:
      print(f"init_skills: could not archive customized {name} ({exc})")
      continue
    archived += 1
  return removed, archived


def init() -> None:
  seed = _seed_dir()
  if seed is None:
    print("init_skills: no seed-skills dir found; skipping")
    return
  SKILLS.parent.mkdir(parents=True, exist_ok=True)
  if not SKILLS.exists():
    SKILLS.mkdir(parents=True)
    for src in seed.glob("*.md"):
      shutil.copy2(src, SKILLS / src.name)
    n = len(list(SKILLS.glob("*.md")))
    print(f"init_skills: seeded {n} skills")
    _chown_mobius(SKILLS)
    _write_index()
    return
  # Present already — preserve the agent's edits. Only add NEW seed skills the
  # instance doesn't have yet (never overwrite an existing one).
  # Resolve any crash-interrupted /api/skills install first, so a pending
  # directory skill is either published (and seen as a collision below) or
  # gone — the same reconciliation the runtime skills mutations run.
  try:
    from app.skills import reconcile_installed

    reconcile_installed(SKILLS)
  except Exception as exc:  # pragma: no cover - best-effort at boot
    print(f"init_skills: reconcile skipped ({exc})")
  added = 0
  migrated = 0
  skipped = 0
  for src in seed.glob("*.md"):
    dst = SKILLS / src.name
    old_digests = _UNMODIFIED_MIGRATIONS.get(src.name)
    if dst.is_file() and old_digests:
      try:
        digest = hashlib.sha256(dst.read_bytes()).hexdigest()
      except OSError:
        digest = ""
      if digest in old_digests:
        shutil.copy2(src, dst)
        migrated += 1
        continue
    # Both on-disk shapes share one logical id: never add a flat `foo.md` seed
    # when an install-provenance directory skill `foo/` already holds `foo`
    # (the runtime install path enforces the same both-shape rule).
    if (SKILLS / src.stem).is_dir():
      skipped += 1
      continue
    if not dst.exists():
      shutil.copy2(src, dst)
      added += 1
  retired, archived = _retire_legacy_skills()
  if skipped:
    print(f"init_skills: skipped {skipped} seed skill(s) colliding with an "
          "installed directory skill of the same id")
  if added:
    print(f"init_skills: added {added} new seed skill(s) (existing kept)")
  if migrated:
    print(f"init_skills: migrated {migrated} unmodified base skill(s)")
  if retired:
    print(f"init_skills: removed {retired} retired base skill(s)")
  if archived:
    print(f"init_skills: archived {archived} customized retired skill(s)")
  _chown_mobius(SKILLS)
  if RETIRED_SKILLS.exists():
    _chown_mobius(RETIRED_SKILLS)
  _write_index()


if __name__ == "__main__":
  init()
