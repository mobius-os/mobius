"""Outside channels (Slack, Discord, ntfy, webhooks): notification copies and posts."""

import pytest

from app import models, notification_channels, presence
from app.database import SessionLocal
from test_app_fixtures import create_local_app

SLACK = "https://hooks.slack.com/services/T000/B000/secretpart"
DISCORD_BAD = "https://discord.com/api/webhooks/1"
DISCORD = "https://discord.com/api/webhooks/1/secrettoken"


@pytest.fixture
def sent(monkeypatch):
  """Capture deliveries synchronously instead of posting to the internet."""
  posts = []
  monkeypatch.setattr(
    notification_channels, "validate_url_safe", lambda url: (url, "host", "host"),
  )
  monkeypatch.setattr(notification_channels, "_post", lambda url, payload: posts.append((url, payload)))
  monkeypatch.setattr(
    notification_channels, "send_later",
    lambda targets, message: notification_channels._deliver_all(targets, message),
  )
  return posts


def _app(db, slug):
  app = models.App(
    slug=slug, source_dir=f"/tmp/mobius-tests/{slug}", name=slug, description="",
    jsx_source="export default function App(){}", compiled_path="/tmp/app.js",
  )
  db.add(app)
  db.commit()
  return app


def _add(client, auth, **body):
  response = client.post("/api/channels", headers=auth, json=body)
  assert response.status_code == 201, response.text
  return response.json()


def _notify(client, auth, app, title="Message from @sam", body="See you at <!channel> 5"):
  response = client.post("/api/notifications/send", headers=auth, json={
    "title": title, "body": body, "source_type": "app", "source_id": str(app.id),
    "target": f"/shell/?app={app.id}&intent=dm:x",
  })
  assert response.status_code == 200, response.text


def test_link_is_recognised_kept_encrypted_and_never_returned(client, auth, db, sent):
  channel = _add(client, auth, url=SLACK)
  assert channel["kind"] == "slack" and channel["label"] == "Slack"
  assert "secretpart" not in str(client.get("/api/channels", headers=auth).json())
  row = db.query(models.NotificationChannel).one()
  assert SLACK not in row.url_encrypted
  assert notification_channels.decrypt_url(row.url_encrypted) == SLACK


def test_only_public_https_links_are_accepted(client, auth):
  assert client.post(
    "/api/channels", headers=auth, json={"url": "http://hooks.slack.com/services/x"},
  ).status_code == 400
  # The real SSRF check refuses an address inside the deployment.
  assert client.post(
    "/api/channels", headers=auth, json={"url": "https://127.0.0.1/hook"},
  ).status_code == 400


def test_channels_receive_only_the_apps_they_follow_and_title_only_by_default(
  client, auth, db, sent,
):
  social, news = _app(db, "channel-social"), _app(db, "channel-news")
  _add(client, auth, url=SLACK, app_ids=[social.id])
  _add(client, auth, url=DISCORD, app_ids=[news.id], include_body=True)
  _notify(client, auth, social)

  assert len(sent) == 1
  url, payload = sent[0]
  assert url == SLACK
  assert "Message from @sam" in payload["text"]
  assert "See you" not in payload["text"]
  assert f"/shell/?app={social.id}&intent=dm:x|Open in Möbius>" in payload["text"]
  assert db.query(models.NotificationChannel).filter_by(kind="slack").one().last_error is None


def test_message_text_cannot_ping_a_channel(client, auth, db, sent):
  social = _app(db, "channel-social-2")
  _add(client, auth, url=SLACK, include_body=True)
  _add(client, auth, url=DISCORD, include_body=True)
  _notify(client, auth, social, body="hi @everyone <!channel>")

  slack, discord = (payload for _, payload in sent)
  assert "<!channel>" not in slack["text"] and "&lt;!channel&gt;" in slack["text"]
  assert discord["allowed_mentions"] == {"parse": []}
  assert "@everyone" in discord["content"]


def test_nothing_is_copied_while_the_owner_is_watching(client, auth, db, sent, monkeypatch):
  social = _app(db, "channel-social-3")
  _add(client, auth, url=SLACK)
  monkeypatch.setattr(presence, "has_app_watchers", lambda _app_id: True)
  _notify(client, auth, social)
  assert sent == []


def test_ntfy_publishes_json_to_the_server_root():
  message = notification_channels.Message(
    title="Möbius", body="hello", url="https://me.example/shell/",
    notification_id="n1", source_type="app", source_id="7",
  )
  url, payload = notification_channels.build_request(
    "ntfy", "https://ntfy.sh/my-topic", message, include_body=True,
  )
  assert url == "https://ntfy.sh/"
  assert payload == {
    "topic": "my-topic", "title": "Möbius", "message": "hello",
    "click": "https://me.example/shell/",
  }


def test_send_test_reports_and_records_a_broken_link(client, auth, db, sent, monkeypatch):
  channel = _add(client, auth, url=DISCORD)

  def refuse(url, payload):
    raise notification_channels._DeliveryError("The service answered 404.")
  monkeypatch.setattr(notification_channels, "_post", refuse)
  result = client.post(f"/api/channels/{channel['id']}/test", headers=auth).json()
  assert result == {"ok": False, "error": "The service answered 404."}
  listed = client.get("/api/channels", headers=auth).json()[0]
  assert listed["lastError"] == "The service answered 404."


def test_update_and_remove(client, auth, db, sent):
  social = _app(db, "channel-social-4")
  channel = _add(client, auth, url=SLACK)
  patched = client.patch(
    f"/api/channels/{channel['id']}", headers=auth,
    json={"app_ids": [social.id], "include_body": True, "label": "Team"},
  ).json()
  assert patched["appIds"] == [social.id] and patched["includeBody"] and patched["label"] == "Team"
  assert client.patch(
    f"/api/channels/{channel['id']}", headers=auth, json={"all_notifications": True},
  ).json()["appIds"] is None
  assert client.delete(f"/api/channels/{channel['id']}", headers=auth).status_code == 204
  assert client.get("/api/channels", headers=auth).json() == []


def test_cut_off_slack_and_discord_links_are_refused_when_saved(client, auth, sent):
  for url in ("https://hooks.slack.com/services", "https://hooks.slack.com/services/T000/B000", DISCORD_BAD):
    response = client.post("/api/channels", headers=auth, json={"url": url})
    assert response.status_code == 400, url
    assert "cut off" in response.json()["detail"]


def test_replacing_a_link_keeps_the_channel_settings(client, auth, db, sent):
  social = _app(db, "channel-social-5")
  channel = _add(client, auth, url=SLACK, app_ids=[social.id], include_body=True)
  db.query(models.NotificationChannel).update({"last_error": "The service answered 404."})
  db.commit()
  new_slack = "https://hooks.slack.com/services/T111/B111/newsecret"
  patched = client.patch(f"/api/channels/{channel['id']}", headers=auth, json={"url": new_slack})
  assert patched.status_code == 200, patched.text
  body = patched.json()
  assert body["id"] == channel["id"] and body["appIds"] == [social.id] and body["includeBody"]
  assert body["lastError"] is None and body["urlHint"].endswith("…cret")
  assert "newsecret" not in patched.text
  row = db.query(models.NotificationChannel).one()
  db.refresh(row)
  assert notification_channels.decrypt_url(row.url_encrypted) == new_slack

  # A new service's link follows that service, including its default name.
  moved = client.patch(f"/api/channels/{channel['id']}", headers=auth, json={"url": DISCORD}).json()
  assert moved["kind"] == "discord" and moved["label"] == "Discord"
  # A cut-off link is refused and the working one is kept.
  refused = client.patch(f"/api/channels/{channel['id']}", headers=auth, json={"url": DISCORD_BAD})
  assert refused.status_code == 400
  db.refresh(row)
  assert notification_channels.decrypt_url(row.url_encrypted) == DISCORD


def test_replacing_a_self_hosted_ntfy_link_keeps_it_ntfy(client, auth, db, sent):
  app = _app(db, "channel-social-ntfy")
  channel = _add(client, auth, url="https://ntfy.example.org/alerts", kind="ntfy")
  patched = client.patch(
    f"/api/channels/{channel['id']}", headers=auth,
    json={"url": "https://ntfy.example.org/alerts-v2"},
  ).json()
  assert patched["kind"] == "ntfy" and patched["label"] == "ntfy"
  _notify(client, auth, app)
  url, payload = sent[-1]
  assert url == "https://ntfy.example.org/" and payload["topic"] == "alerts-v2"
  # A link a known service claims still moves the channel to that service.
  moved = client.patch(f"/api/channels/{channel['id']}", headers=auth, json={"url": SLACK}).json()
  assert moved["kind"] == "slack" and moved["label"] == "Slack"


def test_an_explicit_kind_switches_between_ntfy_and_webhook_keeping_settings(client, auth, db, sent):
  app = _app(db, "channel-social-switch")
  channel = _add(
    client, auth, url="https://ntfy.example.org/alerts", kind="ntfy", app_ids=[app.id],
  )
  # Unclaimed links look alike, so the caller names the kind; the channel and
  # its app filter survive instead of having to be deleted and added again.
  hook = "https://hooks.zapier.com/hooks/catch/123/abc"
  as_webhook = client.patch(
    f"/api/channels/{channel['id']}", headers=auth, json={"url": hook, "kind": "webhook"},
  ).json()
  assert as_webhook["kind"] == "webhook" and as_webhook["label"] == "Webhook"
  assert as_webhook["id"] == channel["id"] and as_webhook["appIds"] == [app.id]
  _notify(client, auth, app)
  url, payload = sent[-1]
  assert url == hook and "topic" not in payload
  # A webhook without a path is a webhook, not an ntfy link missing its topic.
  bare = client.patch(
    f"/api/channels/{channel['id']}", headers=auth,
    json={"url": "https://webhook.example.org/", "kind": "webhook"},
  )
  assert bare.status_code == 200, bare.text

  as_ntfy = client.patch(
    f"/api/channels/{channel['id']}", headers=auth,
    json={"url": "https://ntfy.example.org/alerts", "kind": "ntfy"},
  ).json()
  assert as_ntfy["kind"] == "ntfy" and as_ntfy["label"] == "ntfy"
  _notify(client, auth, app)
  url, payload = sent[-1]
  assert url == "https://ntfy.example.org/" and payload["topic"] == "alerts"


def test_changing_the_kind_without_the_link_is_refused(client, auth, db, sent):
  channel = _add(client, auth, url="https://ntfy.example.org/alerts", kind="ntfy")
  refused = client.patch(f"/api/channels/{channel['id']}", headers=auth, json={"kind": "webhook"})
  assert refused.status_code == 400
  assert client.get("/api/channels", headers=auth).json()[0]["kind"] == "ntfy"
  # Naming the kind it already has changes nothing and is accepted.
  same = client.patch(
    f"/api/channels/{channel['id']}", headers=auth, json={"kind": "ntfy", "label": "Alerts"},
  )
  assert same.status_code == 200 and same.json()["label"] == "Alerts"


def test_a_crafted_link_back_cannot_add_a_mention_or_another_link(client, auth, db, sent):
  social = _app(db, "channel-social-6")
  _add(client, auth, url=SLACK)
  _add(client, auth, url=DISCORD)
  response = client.post("/api/notifications/send", headers=auth, json={
    "title": "Hi", "body": None, "source_type": "app", "source_id": str(social.id),
    "target": f"/shell/?app={social.id}&intent=x> <!channel> <https://example.com|Open in Möbius>)",
  })
  assert response.status_code == 200, response.text

  slack, discord = (payload for _, payload in sent)
  link = slack["text"].splitlines()[-1]
  assert link.startswith("<") and link.endswith("|Open in Möbius>")
  assert link.count("<") == 1 and link.count(">") == 1 and link.count("|") == 1
  assert "<!channel>" not in slack["text"]
  assert f"/shell/?app={social.id}&intent=x%3E%20%3C!channel%3E" in link

  link = discord["content"].splitlines()[-1]
  assert link.startswith("[Open in Möbius](<") and link.endswith(">)")
  assert link.count("<") == 1 and link.count(">") == 1


@pytest.fixture
def queued(monkeypatch, sent):
  """Hold sends in the queue until the test releases them."""
  waiting = []
  monkeypatch.setattr(
    notification_channels, "send_later", lambda ids, message: waiting.append((ids, message)),
  )

  def release():
    sent.clear()
    for ids, message in waiting:
      notification_channels._deliver_all(ids, message)
    return sent
  return release


def test_a_queued_send_follows_the_channels_settings_when_it_goes_out(
  client, auth, db, queued, sent,
):
  social = _app(db, "channel-social-7")
  removed = _add(client, auth, url=SLACK, include_body=True)
  narrowed = _add(client, auth, url=DISCORD, include_body=True)
  quieted = _add(client, auth, url="https://hooks.slack.com/services/T222/B222/quietsecret", include_body=True)
  _notify(client, auth, social)

  client.delete(f"/api/channels/{removed['id']}", headers=auth)
  client.patch(f"/api/channels/{narrowed['id']}", headers=auth, json={"app_ids": []})
  client.patch(f"/api/channels/{quieted['id']}", headers=auth, json={"include_body": False})

  delivered = queued()
  assert len(delivered) == 1
  url, payload = delivered[0]
  assert url.endswith("quietsecret") and "See you" not in payload["text"]


def test_a_queued_send_goes_to_the_replacement_link(client, auth, db, queued, sent):
  social = _app(db, "channel-social-8")
  channel = _add(client, auth, url=SLACK)
  _notify(client, auth, social)
  new_slack = "https://hooks.slack.com/services/T111/B111/newsecret"
  client.patch(f"/api/channels/{channel['id']}", headers=auth, json={"url": new_slack})

  assert [url for url, _ in queued()] == [new_slack]


def test_a_send_to_a_replaced_link_does_not_report_on_the_new_one(client, auth, db, sent):
  channel = _add(client, auth, url=SLACK)
  row = db.query(models.NotificationChannel).one()
  old = notification_channels.Target(
    id=row.id, kind=row.kind, url_encrypted=row.url_encrypted, include_body=False,
  )
  client.patch(
    f"/api/channels/{channel['id']}", headers=auth,
    json={"url": "https://hooks.slack.com/services/T111/B111/newsecret"},
  )
  message = notification_channels.Message(
    title="Hi", body=None, url=None, notification_id="n", source_type="system", source_id=None,
  )
  notification_channels.record_outcome(old, message, "The service answered 404.")
  assert client.get("/api/channels", headers=auth).json()[0]["lastError"] is None


def test_a_channel_can_stop_copying_notifications(client, auth, db, sent):
  social = _app(db, "channel-social-9")
  channel = _add(client, auth, url=SLACK, copy_notifications=False, accept_posts=True)
  assert channel["copyNotifications"] is False and channel["acceptPosts"] is True
  _notify(client, auth, social)
  assert sent == []
  client.patch(f"/api/channels/{channel['id']}", headers=auth, json={"copy_notifications": True})
  _notify(client, auth, social)
  assert len(sent) == 1


def test_posts_need_the_channel_to_accept_them(client, auth, sent):
  channel = _add(client, auth, url=SLACK)
  refused = client.post(
    f"/api/channels/{channel['id']}/send", headers=auth, json={"title": "Released v1"},
  )
  assert refused.status_code == 409 and "Allow posts" in refused.json()["detail"]
  assert sent == []


def test_a_post_keeps_its_formatting_but_cannot_ping(client, auth, sent):
  slack = _add(client, auth, url=SLACK, accept_posts=True, copy_notifications=False)
  discord = _add(client, auth, url=DISCORD, accept_posts=True, copy_notifications=False)
  text = "*Version 1.2* is out\n• #12 faster search\n<!channel> @everyone"
  for channel in (slack, discord):
    result = client.post(
      f"/api/channels/{channel['id']}/send", headers=auth,
      json={"title": "Released v1.2", "text": text},
    ).json()
    assert result == {"ok": True, "error": None}

  (_, slack_payload), (_, discord_payload) = sent
  assert "*Version 1.2* is out" in slack_payload["text"]
  assert "&lt;!channel&gt;" in slack_payload["text"] and "<!channel>" not in slack_payload["text"]
  assert "Open in Möbius" not in slack_payload["text"]
  assert "*Version 1.2*" in discord_payload["content"] and "#12" in discord_payload["content"]
  assert discord_payload["allowed_mentions"] == {"parse": []}


def test_a_post_too_long_for_discord_is_refused_not_cut(client, auth, sent):
  channel = _add(client, auth, url=DISCORD, accept_posts=True, copy_notifications=False)
  url = f"/api/channels/{channel['id']}/send"
  text = "x" * 2500 + "\nFinal step: run the migration."
  result = client.post(url, headers=auth, json={
    "title": "Released", "text": text, "link": "https://github.com/x/y/releases",
  }).json()
  assert result["ok"] is False and "too long for Discord" in result["error"]
  assert sent == []

  fits = client.post(url, headers=auth, json={
    "title": "Released", "text": "x" * 1900 + "\nFinal step.",
    "link": "https://github.com/x/y/releases",
  }).json()
  assert fits == {"ok": True, "error": None}
  (_, payload), = sent
  assert payload["content"].endswith("(<https://github.com/x/y/releases>)")


def test_a_post_links_to_a_page_or_an_https_address(client, auth, sent):
  channel = _add(client, auth, url=SLACK, accept_posts=True)
  url = f"/api/channels/{channel['id']}/send"
  client.post(url, headers=auth, json={"title": "A", "link": "/shell/?app=12"})
  client.post(url, headers=auth, json={"title": "B", "link": "https://github.com/x/y/releases"})
  inside, outside = (payload["text"].splitlines()[-1] for _, payload in sent)
  assert inside.endswith("/shell/?app=12|Open in Möbius>")
  assert outside == "<https://github.com/x/y/releases|Open link>"
  for bad in ("http://example.com", "javascript:alert(1)", "//evil.example"):
    assert client.post(url, headers=auth, json={"title": "C", "link": bad}).status_code == 400


def test_activity_keeps_recent_titles_and_outcomes_only(client, auth, db, sent, monkeypatch):
  social = _app(db, "channel-social-10")
  channel = _add(client, auth, url=SLACK, accept_posts=True, include_body=True)
  _notify(client, auth, social)
  client.post(f"/api/channels/{channel['id']}/test", headers=auth)
  client.post(
    f"/api/channels/{channel['id']}/send", headers=auth,
    json={"title": "Released", "text": "private details"},
  )
  activity = client.get(f"/api/channels/{channel['id']}/activity", headers=auth).json()
  assert [entry["source"] for entry in activity] == ["post", "test", "notification"]
  assert activity[0]["title"] == "Released" and activity[0]["error"] is None
  assert "private details" not in str(activity)

  monkeypatch.setattr(notification_channels, "ACTIVITY_KEPT", 2)
  client.post(f"/api/channels/{channel['id']}/test", headers=auth)
  assert db.query(models.ChannelDelivery).count() == 2
  client.delete(f"/api/channels/{channel['id']}", headers=auth)
  assert db.query(models.ChannelDelivery).count() == 0


def _app_auth(client, auth, *, granted: bool):
  app_id = create_local_app(client, auth, name=f"channels-{granted}", description="t")["id"]
  with SessionLocal() as session:
    app = session.query(models.App).filter(models.App.id == app_id).one()
    app.capability_contract = {"schema": 1, "data": {"channels_manage": granted}}
    session.commit()
  minted = client.post("/api/auth/app-token", json={"app_id": app_id}, headers=auth)
  assert minted.status_code == 200, minted.text
  return {"Authorization": f"Bearer {minted.json()['token']}"}


def test_only_an_app_granted_channels_manage_reaches_channels(client, auth, sent):
  channel = _add(client, auth, url=SLACK, accept_posts=True)
  denied = _app_auth(client, auth, granted=False)
  assert client.get("/api/channels", headers=denied).status_code == 403
  assert client.post(
    f"/api/channels/{channel['id']}/send", headers=denied, json={"title": "x"},
  ).status_code == 403

  granted = _app_auth(client, auth, granted=True)
  listed = client.get("/api/channels", headers=granted)
  assert listed.status_code == 200 and listed.json()[0]["id"] == channel["id"]
  assert "secretpart" not in listed.text
