import json

import httpx
import pytest

from app.community_broker import CommunityBrokerClient, CommunityBrokerError


@pytest.mark.asyncio
async def test_mutation_binds_exact_body_path_and_idempotency_key():
  seen = {}

  async def handler(request: httpx.Request) -> httpx.Response:
    seen["method"] = request.method
    seen["url"] = str(request.url)
    seen["body"] = await request.aread()
    seen["headers"] = dict(request.headers)
    return httpx.Response(201, json={"ok": True})

  client = CommunityBrokerClient(transport=httpx.MockTransport(handler))
  payload, status, _ = await client.request(
    "POST",
    "/v1/community/apps",
    body={"z": 1, "a": "é"},
    idempotency_key="publish-request-0001",
  )

  assert status == 201
  assert payload == {"ok": True}
  assert seen["method"] == "POST"
  assert seen["url"].endswith("/v1/community/apps")
  assert seen["body"] == '{"a":"é","z":1}'.encode()
  assert seen["headers"]["idempotency-key"] == "publish-request-0001"
  assert seen["headers"]["x-mobius-request-id"].startswith("community:")


@pytest.mark.asyncio
async def test_rating_put_is_forwarded_with_exact_idempotent_body():
  seen = {}

  async def handler(request: httpx.Request) -> httpx.Response:
    seen["method"] = request.method
    seen["url"] = str(request.url)
    seen["body"] = await request.aread()
    seen["headers"] = dict(request.headers)
    return httpx.Response(200, json={"rating_average": 5, "rating_count": 1})

  client = CommunityBrokerClient(transport=httpx.MockTransport(handler))
  payload, status, _ = await client.request(
    "PUT",
    "/v1/community/apps/app_12345678/rating",
    body={"revision_id": "rev_12345678", "value": 5},
    idempotency_key="rating-request-0001",
  )

  assert status == 200
  assert payload == {"rating_average": 5, "rating_count": 1}
  assert seen["method"] == "PUT"
  assert seen["url"].endswith("/v1/community/apps/app_12345678/rating")
  assert seen["body"] == b'{"revision_id":"rev_12345678","value":5}'
  assert seen["headers"]["idempotency-key"] == "rating-request-0001"


@pytest.mark.asyncio
async def test_read_preserves_bounded_query_for_broker_binding():
  seen = {}

  async def handler(request: httpx.Request) -> httpx.Response:
    seen["url"] = str(request.url)
    return httpx.Response(200, json={"apps": []})

  client = CommunityBrokerClient(transport=httpx.MockTransport(handler))
  await client.request(
    "GET",
    "/v1/community/apps",
    params={"q": "notes & lists", "limit": 20, "empty": None},
  )

  assert seen["url"].endswith(
    "/v1/community/apps?limit=20&q=notes+%26+lists"
  )


@pytest.mark.asyncio
async def test_mutation_requires_idempotency_before_socket_call():
  called = False

  async def handler(_request: httpx.Request) -> httpx.Response:
    nonlocal called
    called = True
    return httpx.Response(200, json={})

  client = CommunityBrokerClient(transport=httpx.MockTransport(handler))
  with pytest.raises(CommunityBrokerError) as raised:
    await client.request("POST", "/v1/community/apps", body={"github": {}})

  assert raised.value.code == "invalid_idempotency_key"
  assert called is False


@pytest.mark.asyncio
async def test_central_error_envelope_is_preserved_without_token_details():
  async def handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
      409,
      headers={"Retry-After": "2"},
      json={
        "error": {
          "code": "request_in_progress",
          "message": "The same request is still running.",
        },
      },
    )

  client = CommunityBrokerClient(transport=httpx.MockTransport(handler))
  with pytest.raises(CommunityBrokerError) as raised:
    await client.request(
      "POST",
      "/v1/community/apps",
      body={"github": {}},
      idempotency_key="publish-request-0002",
    )

  assert raised.value.status_code == 409
  assert raised.value.code == "request_in_progress"
  assert raised.value.retry_after == 2
