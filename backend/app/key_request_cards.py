"""An agent's key request as a saved owner card.

When a top-level agent needs a key the owner has not given it (a new key, or
sites a key does not list yet), it asks with the ``request_key`` control. The
vault records the request; if the owner must answer, the ask is saved as a
barring owner card that ends the turn (routes/key_requests.py), so the chat
waits for the owner and the answer resumes the agent. It is the one owner ask
outside the `owner_input` request list.

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


def card_payload(answer: dict) -> dict:
  """The saved card for the vault request ``answer`` names.

  Every word comes from the request as the vault stored it (``answer``'s
  ``sites`` and ``reason``), never from the ask that led here: a request
  already waiting may be one another chat made, and the card must name exactly
  what its Allow grants. Its text reads well in the transcript and to the
  resumed agent; the card shows the live request from the vault and keeps
  the sites it names, against which its answer is checked.
  """
  key = answer["key"]
  sites = answer.get("sites") or []
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
          "description": "Your agent carries on without it, and can't ask for this key again "
          "until you let it in Settings › Keys.",
        },
      ],
    }],
    "key_request": {"id": answer["id"], "key": key, "reason": answer.get("reason") or "", "sites": sites},
  }


async def stored_request(answer: dict) -> dict | None:
  """The request ``answer`` names, as the vault stores it (with its chat and ticket)."""
  try:
    requests = (await key_vault.vault_call("GET", "/requests"))["requests"]
  except (key_vault.VaultUnavailable, key_vault.VaultTimeout) as exc:
    raise HTTPException(503, detail="The key vault isn't running. Try again in a moment.") from exc
  return next((item for item in requests if item.get("id") == answer.get("id")), None)


async def waiting_elsewhere(answer: dict, chat_id: str) -> dict | None:
  """The stored request ``answer`` names, when it was made outside ``chat_id``.

  A request already waiting answers for every later ask for its key, so a
  card for it belongs only in the chat whose agent asked: shown here, another
  chat's (or a chatless agent's) ask would read as this agent's own.
  """
  request = await stored_request(answer)
  return request if request is not None and request.get("chat_id") != chat_id else None


def holds_open_card(db, chat_id: str, request_id: str) -> bool:
  """Whether ``chat_id`` shows an unanswered card for vault request
  ``request_id``. A deleted chat, or one whose turn stopped before the owner
  answered, holds none; its request then waits in Settings › Keys alone."""
  from app import models, questions
  chat = db.get(models.Chat, chat_id, populate_existing=True)
  if chat is None or chat.deleted_at is not None:
    return False
  card = questions.open_continuation_question(chat, chat.pending_question_id)
  return card is not None and (card.get("key_request") or {}).get("id") == request_id


def elsewhere_note(title: str | None) -> str:
  """What an agent hears when its ask joins a request already waiting.

  The owner answers it in Settings › Keys, which lists every waiting
  request, and on the card in the chat that asked (``title``) while that
  chat still shows one.
  """
  where = f"on a card in the chat \"{title}\" (waiting_chat_id) and in Settings › Keys" if title else "in Settings › Keys"
  return (f"A request for this key is already waiting for the owner {where}, so no card was saved "
          "here; answer.sites is what it asks. Tell the owner to answer it there, and ask again after they do.")


def needs_owner(answer: dict) -> bool:
  """Whether the vault's answer waits on the owner."""
  return answer.get("status") == "pending" or answer.get("sites_request") == "pending"


async def check_answer(card: dict, selections: dict | None) -> None:
  """Admit an answer to a key card only when the vault already agrees.

  The vault is read for exactly what the card asked (its ``sites``):
  Allow needs the key to exist with your agents allowed on every one of them.
  Not now needs the card's request refused, or no longer waiting without the
  key being allowed there (removed in Settings, say), so a card always has an
  honest answer and the agent is never told Allow for nothing. Anything
  else, a typed reply included, is refused: the owner answers on the card.
  """
  key_request = card.get("key_request") or {}
  request_id = key_request.get("id")
  chosen = (selections or {}).get(QUESTION_ID)
  sites = key_request.get("sites")
  if not isinstance(request_id, str) or not isinstance(sites, list) or chosen not in ([ALLOW], [DENY]):
    raise HTTPException(409, detail="Answer this key request with Allow or Not now on its card.")
  try:
    requests = (await key_vault.vault_call("GET", "/requests"))["requests"]
    keys = (await key_vault.vault_call("GET", "/keys"))["keys"]
  except (key_vault.VaultUnavailable, key_vault.VaultTimeout) as exc:
    raise HTTPException(503, detail="The key vault isn't running. Try again in a moment.") from exc
  request = next((item for item in requests if item.get("id") == request_id), None)
  # The request this card shows, if it still waits or was refused as asked.
  own = request if request is not None and request.get("sites") == sites else None
  record = next((item for item in keys if item.get("name") == key_request.get("key")), None)
  covered = record is not None and bool(record["grants"].get("agents")) and not (
    key_vault.sites_outside(record["grants"].get("sites") or [], sites)
  )
  if own is not None and own.get("status") == "pending":
    raise HTTPException(409, detail="Your key vault hasn't recorded this answer. Reload Möbius and answer the card again.")
  if chosen == [ALLOW] and not covered:
    raise HTTPException(409, detail="Nothing was allowed for this request. Choose Not now, or allow the key in Settings › Keys.")
  if chosen == [DENY] and covered and not (own is not None and own.get("status") == "denied"):
    raise HTTPException(409, detail="Your agents may already use this key there. Choose Allow.")
