#!/usr/bin/env python3
"""Start the update that was waiting for a newer Host replacement helper.

A release that advances ``deployment/self-hosted-helper.required`` is refused
by Settings in every older release (it reports external activation work that
only the host can do, and nothing inside the container can ever retire it).
After the owner reinstalls the helper from the release's trusted checkout,
``scripts/install-rebuild-helper.sh`` runs this file INSIDE the still-running
app, as the app user, with the app's own backend as the working directory:

    docker exec -i -u mobius -w /data/platform/backend <app> \\
      python3 -I - <target-sha> <helper-protocol> < scripts/finish-helper-update.py

It drives the running release's own updater: the same reviewed preview,
prepared source snapshot and bound replacement request that Settings uses
(``prepare_reviewed_update`` then ``request_reviewed_rebuild``, which accepts
an already-prepared exact target). It never writes the inbox itself, never
chooses an image, and refuses unless the only outstanding external work is
this helper migration. The release's own boot still decides whether storage
may be converted: its gate requires the verified ACTIVE helper.

Prints one JSON object. Exit 0 with ``state`` ``none`` when nothing waits on
the helper, or ``queued`` with the request nonce; exit 1 (nothing changed or
the prepared update left for Settings to finish) with ``state`` ``refused``.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys

ROUTINE = {"image_rebuild", "server_restart"}
MARKER = "deployment/self-hosted-helper.required"


def emit(state: str, **fields: object) -> None:
    print(json.dumps({"state": state, **fields}))


def refuse(message: str) -> None:
    emit("refused", message=message)
    raise SystemExit(1)


def main(argv: list[str]) -> None:
    if len(argv) != 3 or not re.fullmatch(r"[0-9a-f]{40}", argv[1]) \
            or not argv[2].isdigit():
        refuse("usage: finish-helper-update.py <target-sha> <helper-protocol>")
    target, protocol = argv[1], int(argv[2])
    sys.path.insert(0, ".")
    try:
        from app import auth, deployment_control, models, platform_activation
        from app import platform_update as pu
        from app.database import SessionLocal
        preview_update = pu.platform_update_preview
        prepare = pu.prepare_reviewed_update
        request = deployment_control.request_reviewed_rebuild
    except Exception as exc:  # an older release without the reviewed path
        refuse(f"This Möbius version cannot finish the update from the host ({exc}). "
               "Update it from Settings to a release from September 25, 2026 or later first.")

    try:
        preview = preview_update(target_sha=target)
    except Exception as exc:
        refuse(f"The update to {target[:12]} could not be reviewed: {exc}")
    activation = preview.get("activation") or {}
    actions = set(activation.get("required_actions") or [])
    if not preview.get("available") or "host_maintenance" not in actions:
        # Settings can install it (or it is already installed): nothing waits here.
        emit("none", target_sha=preview.get("target_sha"))
        return
    if actions - ROUTINE - {"host_maintenance"}:
        refuse("This update also needs other deployment changes ("
               + ", ".join(sorted(actions - ROUTINE - {"host_maintenance"}))
               + "). Finish those with Möbius first.")
    for reason in activation.get("reasons") or []:
        if not isinstance(reason, dict):
            continue
        impact = platform_activation.classify_activation(reason.get("paths") or [])
        if ("host_maintenance" in (impact.get("required_actions") or [])
                and reason.get("code") != "host_helper_migration"):
            refuse("This update needs host work other than the helper ("
                   f"{reason.get('code')}). Finish it with Möbius first.")
    required = subprocess.run(
        ["git", "-C", str(pu.PLATFORM_REPO), "show", f"{target}:{MARKER}"],
        capture_output=True, text=True,
    )
    if required.returncode != 0 or not required.stdout.strip().isdigit():
        refuse(f"The release {target[:12]} does not declare its helper requirement.")
    if int(required.stdout.strip()) > protocol:
        refuse(f"The release needs helper protocol {required.stdout.strip()}; "
               f"this checkout installed {protocol}. Update the host checkout first.")
    # The prepared-target path skips the older release's final review, so
    # apply its Python-package guard here: source that imports packages its
    # image lacks may run only on an image that can swap it in.
    changes_packages = getattr(pu, "activation_changes_python_dependencies", None)
    activates = getattr(pu, "image_activates_updates", None)
    incoming = preview.get("incoming_activation") or activation
    if changes_packages is None or activates is None:
        refuse("This Möbius version is too old to finish the update from the host. "
               "Move the container first as described in scripts/CONTAINER-REBUILD.md, "
               "then run the installer again.")
    if changes_packages(incoming) and not activates():
        refuse("This update changes Python packages that this version's image cannot "
               "hand over safely. Move the container first as described in "
               "scripts/CONTAINER-REBUILD.md, then run the installer again.")
    if preview.get("conflict_paths") or preview.get("blocking_paths"):
        refuse("Local changes overlap this update. Open Settings, choose the update "
               "and let Möbius resolve them; the installed helper then finishes it.")

    plan = {key: preview.get(key) for key in
            ("plan_id", "current_sha", "target_sha", "image_digest")}
    try:
        prepared = prepare(**plan)
    except Exception as exc:  # e.g. another update is still unfinished
        refuse(f"The update could not be prepared: {exc}")
    if not isinstance(prepared, dict) or prepared.get("state") != "prepared":
        refuse("Local changes overlap this update. Open Settings and let Möbius "
               "resolve them; the installed helper then finishes it.")
    if not prepared.get("requires_image"):
        refuse("This update no longer needs a new image; finish it from Settings.")

    db = SessionLocal()
    try:
        owner = db.query(models.Owner).first()
        if owner is None:
            refuse("This Möbius has no owner yet; finish setup first.")
        # The helper drains chats with the owner's service credential, minted
        # from the current epoch exactly as Settings does before this request.
        auth.write_service_token(owner.username, owner.token_epoch)
        status = asyncio.run(request(db=db, **plan))
    except deployment_control.DeploymentControlError as exc:
        refuse(f"The helper could not take the update: {exc.message}")
    except Exception as exc:
        refuse(f"The helper could not take the update: {exc}")
    finally:
        db.close()
    if not isinstance(status, dict) or status.get("state") not in {"queued", "running"}:
        refuse(f"The update was prepared but not queued: {status}")
    emit("queued", target_sha=target, request_nonce=status.get("request_nonce"))


if __name__ == "__main__":
    main(sys.argv)
