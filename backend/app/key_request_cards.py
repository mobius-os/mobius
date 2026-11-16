"""An agent's key request as a saved owner card.

When a top-level agent needs a key the owner has not given it (a new key, or
sites a key does not list yet), it asks with the ``request_key`` control. The
vault records the request; if the owner must answer, the ask is saved as an
ordinary owner card, so the chat waits for the owner like every other card and
the answer resumes the agent.

The card only records the owner's answer. The grant itself is the owner's own
input: the owner's browser answers the vault request through the owner-only
key routes (a pasted value goes only there, never into the card), then submits
the card. ``check_answer`` admits an answer only when the vault already agrees
with it, so no other participant can record an "Allow" the owner never gave.
"""

from __future__ import annotations

from fastapi import HTTPException

from app import key_vault

QUESTION_ID = "key"
ALLOW = "allow"
DENY = "deny"


def _host_list(sites: list[dict]) -> str:
  hosts = list(dict.fromkeys(site.get("host") for site in sites if site.get("host")))
  if not hosts:
    return ""
  if len(hosts) == 1:
    return hosts[0]
  return f"{', '.join(hosts[:-1])} and {hosts[-1]}"


def card_payload(answer: dict, reason: str, sites: list[dict]) -> dict:
  """The saved card for the vault request ``answer`` names.

  Its text reads well in the transcript and to the resumed agent; the card
  shows the live request (reason, sites, how a new key is sent) from the vault.
  """
  key = answer["key"]
  hosts = _host_list(sites)
  question = (
    f"Let your agent send **{key}** to **{hosts}**?" if hosts
    else f"Let your agent use **{key}**?"
  )
  return {
    "questions": [{
      "id": QUESTION_ID,
      "header": "Key",
      "question": question,
      "options": [
        {
          "id": ALLOW, "label": "Allow",
          "description": f"Your agents may read {key} and send it to {hosts}." if hosts
          else f"Your agents may read and use {key}.",
        },
        {
          "id": DENY, "label": "Not now",
          "description": "Nothing is saved or changed; your agent carries on without it.",
        },
      ],
    }],
    "key_request": {"id": answer["id"], "key": key, "reason": reason},
  }


def needs_owner(answer: dict) -> bool:
  """Whether the vault's answer waits on the owner."""
  return answer.get("status") == "pending" or answer.get("sites_request") == "pending"


async def check_answer(card: dict, selections: dict | None) -> None:
  """Admit an answer to a key card only when the vault already agrees.

  Allow: the request has left the vault's pending list (an allowed request
  is removed). Not now: the vault records it as denied. Anything else, a
  typed reply included, is refused: the owner answers on the card itself.
  """
  request_id = (card.get("key_request") or {}).get("id")
  chosen = (selections or {}).get(QUESTION_ID)
  if not isinstance(request_id, str) or chosen not in ([ALLOW], [DENY]):
    raise HTTPException(409, detail="Answer this key request with Allow or Not now on its card.")
  try:
    requests = (await key_vault.vault_call("GET", "/requests"))["requests"]
  except (key_vault.VaultUnavailable, key_vault.VaultTimeout) as exc:
    raise HTTPException(503, detail="The key vault isn't running. Try again in a moment.") from exc
  request = next((item for item in requests if item.get("id") == request_id), None)
  status = request.get("status") if request else None
  agrees = status == "denied" if chosen == [DENY] else status != "pending" and status != "denied"
  if not agrees:
    raise HTTPException(409, detail="Your key vault hasn't recorded this answer. Reload Möbius and answer the card again.")
