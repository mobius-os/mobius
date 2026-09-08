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
async def test_unlinked_public_app_reads_fall_back_without_authorization():
  seen = []

  async def handler(request: httpx.Request) -> httpx.Response:
    seen.append((request.method, str(request.url), dict(request.headers)))
    if request.url.host == "mobius-identity-broker":
      return httpx.Response(
        401, json={"error": "a mobius.you account must be linked"},
      )
    return httpx.Response(
      200,
      headers={"ETag": '"catalog-1"'},
      json={"apps": [{"id": "app_12345678"}]},
    )

  client = CommunityBrokerClient(transport=httpx.MockTransport(handler))
  payload, status, headers = await client.request(
    "GET", "/v1/community/apps",
    params={"limit": 25, "offset": 0, "q": "social"},
  )

  assert status == 200
  assert payload == {"apps": [{"id": "app_12345678"}]}
  assert headers == {"etag": '"catalog-1"'}
  assert [item[1] for item in seen] == [
    "http://mobius-identity-broker/v1/community/apps?limit=25&offset=0&q=social",
    "https://www.mobius.you/api/store/v1/apps?limit=25&offset=0&q=social",
  ]
  assert "authorization" not in seen[-1][2]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
  "/v1/community/apps/app_12345678",
  "/v1/community/apps/app_12345678/revisions/rev_12345678",
  "/v1/community/editorial/spotlight",
])
async def test_unlinked_public_detail_reads_use_the_matching_public_path(path):
  seen = []

  async def handler(request: httpx.Request) -> httpx.Response:
    seen.append(str(request.url))
    if request.url.host == "mobius-identity-broker":
      return httpx.Response(
        401, json={"error": "a mobius.you account must be linked"},
      )
    return httpx.Response(200, json={"ok": True})

  client = CommunityBrokerClient(transport=httpx.MockTransport(handler))
  await client.request("GET", path)

  assert seen[-1] == "https://www.mobius.you/api/store/v1" + path.removeprefix(
    "/v1/community"
  )


@pytest.mark.asyncio
async def test_unlinked_private_reads_stay_account_gated():
  seen = []

  async def handler(request: httpx.Request) -> httpx.Response:
    seen.append(str(request.url))
    return httpx.Response(
      401, json={"error": "a mobius.you account must be linked"},
    )

  client = CommunityBrokerClient(transport=httpx.MockTransport(handler))
  with pytest.raises(CommunityBrokerError) as raised:
    await client.request("GET", "/v1/community/publications")

  assert raised.value.status_code == 401
  assert len(seen) == 1


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
