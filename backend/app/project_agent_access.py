"""How far a project's AI agents reach: app agent access (``agent_roles``)
for Projects, decided by whether the project is shared.

  private ... the owner's agent has full access, as in any of their chats
  shared .... (anyone else is a member) every agent chat runs locked down: an
              Edit room on the project's files, with the web and questions
              to the owner

Members' edits are files the owner's agent then reads, so in a shared
project a hidden instruction in one could otherwise steer an agent that
holds the owner's whole Möbius. A locked-down chat runs each turn in the app
agent's room (``agent_roles.Room``) with the project's folder as its files,
the Agent Gateway as its only way to its model, and a key
(``agent_roles.AgentKey`` with ``project``) that is the owner's run-bound
agent bearer narrowed to the room's allow-list. A project has no app
credential, so the key names the project, and it stops working the moment
the project's access differs from its grant (``deps``). When a project
becomes shared or private, its running agent turns that started under the
other access are stopped.

The room can write the project's ``.git`` folder, which members cannot reach
through the file API. Möbius's own Git runs in that folder, so in a shared
project it never starts a program the repository's settings name
(``project_git``).

Not covered yet: the project's builds, and an app Apply from a linked app's
project, still run as the backend user on files the agent (or a member)
wrote. Running them, and app Git, in rooms follows app jobs and services, the
next step named in #2154; until then an agent in a shared project on an app's
source folder can read it but not change it.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agent_roles import AgentRole, Room, tools_prompt, web_sentence

log = logging.getLogger(__name__)

# Every agent chat of a shared project: the project's files, the web and
# questions to the owner; nothing else.
SHARED_ROLE = AgentRole("edit", ("ask", "web"), "")
# A shared project on an installed app's source folder reads it only: Möbius's
# app Git (Apply, updates, rebuild checks; ``app_git``) runs in that folder as
# the backend user with the repository's own settings, so an agent that could
# write its ``.git`` could start a program there. Members edit it as usual.
SHARED_APP_SOURCE_ROLE = AgentRole("read", ("ask", "web"), "")


def is_shared(db: Any, project_id: str) -> bool:
  """Whether anyone besides the owner is a member of the project now."""
  from app import models
  return db.query(models.ProjectMember.id).filter(
    models.ProjectMember.project_id == str(project_id),
    models.ProjectMember.revoked_at.is_(None),
  ).first() is not None


def role_for(db: Any, project: Any) -> AgentRole | None:
  """The role a project's agents run confined by: None (full access) while
  the project is private."""
  if project is None or not is_shared(db, project.id):
    return None
  return SHARED_APP_SOURCE_ROLE if _on_app_source(db, project) else SHARED_ROLE


def _on_app_source(db: Any, project: Any) -> bool:
  from pathlib import Path
  from app import models
  from app.routes.projects import _project_root
  root = _project_root(project).resolve()
  return any(
    Path(source).resolve() == root
    for (source,) in db.query(models.App.source_dir).filter(
      models.App.deleted_at.is_(None), models.App.source_dir.isnot(None),
    )
  )


def grant(db: Any, project: Any) -> str | None:
  """What a project's agents may run with now (``agent_roles._run_grants``),
  or None for a deleted project."""
  if project is None or getattr(project, "deleted_at", None) is not None:
    return None
  role = role_for(db, project)
  return f"project:{role.grant if role else 'admin'}"


async def stop_runs_outside_grant(db: Any, project_id: str) -> bool:
  """Stop the project's agent turns that started under the other access.
  Returns False when a turn could not be stopped."""
  from app import models
  from app.agent_roles import stop_runs_not_under

  def current(state_db: Any) -> str | None:
    return grant(state_db, state_db.get(models.Project, project_id))

  return await stop_runs_not_under(db, models.Chat.project_id == project_id, current)


def sharing_changed(project_id: str) -> None:
  """For a synchronous route that just made a project shared or private:
  stop its turns running under the other access. A turn that cannot be
  stopped is still held to the new access by its key from its next request
  (a locked-down one), or ends with its turn (a full-access one)."""
  from anyio import from_thread
  from app.database import SessionLocal

  async def stop() -> bool:
    with SessionLocal() as db:
      return await stop_runs_outside_grant(db, project_id)

  try:
    if not from_thread.run(stop):
      log.warning("a running chat of project %s could not be stopped", project_id)
  except Exception:
    log.exception("stopping project %s's chats after a sharing change failed", project_id)


def system_prompt(
  role: AgentRole, *, project_name: str, room: Room, context: str, custom: str | None
) -> str:
  """The whole system prompt of a confined project agent. Like an app's room
  agent it gets no constitution, recent chats or Memory, only its reach and
  the project's own context (instructions, knowledge)."""
  reach = {
    "read": f"You can read this project's files in {room.data}, but not change them.",
    "write": f"You can read and change this project's files in {room.data}.",
    None: "You work in an empty folder and have no access to the owner's data.",
  }[role.spec.data]
  prompt = (
    f'You are an AI agent working in the Möbius project "{project_name}", with '
    f"{role.spec.label} access. Other people work on this project too. {reach} "
    f"{web_sentence(room)} The owner's other projects, apps, accounts and "
    "everything else are blocked by the system. You cannot install packages. "
    + tools_prompt(role, room)
  )
  if context and role.spec.data is not None:
    prompt += "\n\n" + context.strip()
  if custom:
    prompt += "\n\nThis chat's own instructions follow.\n\n" + custom.strip()
  return prompt
