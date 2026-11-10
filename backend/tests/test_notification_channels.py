"""Owner notifications reach outside channels (Slack, Discord, ntfy, webhooks)."""

import pytest

from app import models, notification_channels, presence

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
  response = client.post("/api/push/channels", headers=auth, json=body)
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
  assert "secretpart" not in str(client.get("/api/push/channels", headers=auth).json())
  row = db.query(models.NotificationChannel).one()
  assert SLACK not in row.url_encrypted
  assert notification_channels.decrypt_url(row.url_encrypted) == SLACK


def test_only_public_https_links_are_accepted(client, auth):
  assert client.post(
    "/api/push/channels", headers=auth, json={"url": "http://hooks.slack.com/services/x"},
  ).status_code == 400
  # The real SSRF check refuses an address inside the deployment.
  assert client.post(
    "/api/push/channels", headers=auth, json={"url": "https://127.0.0.1/hook"},
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
  result = client.post(f"/api/push/channels/{channel['id']}/test", headers=auth).json()
  assert result == {"ok": False, "error": "The service answered 404."}
  listed = client.get("/api/push/channels", headers=auth).json()[0]
  assert listed["lastError"] == "The service answered 404."


def test_update_and_remove(client, auth, db, sent):
  social = _app(db, "channel-social-4")
  channel = _add(client, auth, url=SLACK)
  patched = client.patch(
    f"/api/push/channels/{channel['id']}", headers=auth,
    json={"app_ids": [social.id], "include_body": True, "label": "Team"},
  ).json()
  assert patched["appIds"] == [social.id] and patched["includeBody"] and patched["label"] == "Team"
  assert client.patch(
    f"/api/push/channels/{channel['id']}", headers=auth, json={"all_notifications": True},
  ).json()["appIds"] is None
  assert client.delete(f"/api/push/channels/{channel['id']}", headers=auth).status_code == 204
  assert client.get("/api/push/channels", headers=auth).json() == []


def test_cut_off_slack_and_discord_links_are_refused_when_saved(client, auth, sent):
  for url in ("https://hooks.slack.com/services", "https://hooks.slack.com/services/T000/B000", DISCORD_BAD):
    response = client.post("/api/push/channels", headers=auth, json={"url": url})
    assert response.status_code == 400, url
    assert "cut off" in response.json()["detail"]


def test_replacing_a_link_keeps_the_channel_settings(client, auth, db, sent):
  social = _app(db, "channel-social-5")
  channel = _add(client, auth, url=SLACK, app_ids=[social.id], include_body=True)
  db.query(models.NotificationChannel).update({"last_error": "The service answered 404."})
  db.commit()
  new_slack = "https://hooks.slack.com/services/T111/B111/newsecret"
  patched = client.patch(f"/api/push/channels/{channel['id']}", headers=auth, json={"url": new_slack})
  assert patched.status_code == 200, patched.text
  body = patched.json()
  assert body["id"] == channel["id"] and body["appIds"] == [social.id] and body["includeBody"]
  assert body["lastError"] is None and body["urlHint"].endswith("…cret")
  assert "newsecret" not in patched.text
  row = db.query(models.NotificationChannel).one()
  db.refresh(row)
  assert notification_channels.decrypt_url(row.url_encrypted) == new_slack

  # A new service's link follows that service, including its default name.
  moved = client.patch(f"/api/push/channels/{channel['id']}", headers=auth, json={"url": DISCORD}).json()
  assert moved["kind"] == "discord" and moved["label"] == "Discord"
  # A cut-off link is refused and the working one is kept.
  refused = client.patch(f"/api/push/channels/{channel['id']}", headers=auth, json={"url": DISCORD_BAD})
  assert refused.status_code == 400
  db.refresh(row)
  assert notification_channels.decrypt_url(row.url_encrypted) == DISCORD
