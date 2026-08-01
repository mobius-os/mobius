import pytest

from app import release_channel


def test_default_channel_preserves_origin_main_behavior(monkeypatch):
  monkeypatch.delenv(release_channel.PLATFORM_RELEASE_REF_ENV, raising=False)

  channel = release_channel.platform_release_channel()

  assert channel.release_ref is None
  assert channel.target_ref == "origin/main"
  assert channel.tracking_ref == "origin/main"
  assert channel.fetch_refspec is None
  assert channel.updates_disabled is False
  assert channel.contributions_disabled is False


def test_managed_channel_targets_baked_sha_and_fetches_only_configured_ref(
  monkeypatch, tmp_path,
):
  sha = "a" * 40
  info = tmp_path / "build-info.json"
  info.write_text(f'{{"sha":"{sha}"}}\n')
  monkeypatch.setattr(release_channel, "BUILD_INFO_PATH", info)
  monkeypatch.setenv(
    release_channel.PLATFORM_RELEASE_REF_ENV,
    "refs/heads/release/external-recovery",
  )

  channel = release_channel.platform_release_channel()

  assert channel.target_ref == sha
  assert channel.tracking_ref == (
    "refs/remotes/origin/release/external-recovery"
  )
  assert channel.fetch_refspec == (
    "+refs/heads/release/external-recovery:"
    "refs/remotes/origin/release/external-recovery"
  )
  assert channel.updates_disabled is True
  assert channel.contributions_disabled is True


@pytest.mark.parametrize(
  "ref",
  [
    "release/external-recovery",
    "refs/tags/external-recovery",
    "refs/heads/release/../main",
    "refs/heads/release//external-recovery",
    "refs/heads/release/external recovery",
    "refs/heads/.hidden",
    "refs/heads/release.lock",
  ],
)
def test_invalid_release_ref_fails_closed(monkeypatch, ref):
  monkeypatch.setenv(release_channel.PLATFORM_RELEASE_REF_ENV, ref)

  with pytest.raises(
    release_channel.ReleaseChannelError,
    match="platform_release_ref_invalid",
  ):
    release_channel.platform_release_channel()
  assert release_channel.platform_contributions_disabled() is True


@pytest.mark.parametrize(
  "payload,error",
  [
    (None, "platform_baked_build_sha_missing"),
    ('{"sha":"unknown"}\n', "platform_baked_build_sha_invalid"),
    ('{"sha":"abc123"}\n', "platform_baked_build_sha_invalid"),
  ],
)
def test_missing_or_invalid_baked_sha_fails_closed(
  monkeypatch, tmp_path, payload, error,
):
  info = tmp_path / "build-info.json"
  if payload is not None:
    info.write_text(payload)
  monkeypatch.setattr(release_channel, "BUILD_INFO_PATH", info)
  monkeypatch.setenv(
    release_channel.PLATFORM_RELEASE_REF_ENV,
    "refs/heads/release/external-recovery",
  )

  with pytest.raises(release_channel.ReleaseChannelError, match=error):
    release_channel.platform_release_channel()


def test_configured_main_is_exact_but_keeps_interactive_flows(
  monkeypatch, tmp_path,
):
  sha = "b" * 40
  info = tmp_path / "build-info.json"
  info.write_text(f'{{"sha":"{sha}"}}\n')
  monkeypatch.setattr(release_channel, "BUILD_INFO_PATH", info)
  monkeypatch.setenv(
    release_channel.PLATFORM_RELEASE_REF_ENV,
    "refs/heads/main",
  )

  channel = release_channel.platform_release_channel()

  assert channel.target_ref == sha
  assert channel.updates_disabled is False
  assert channel.contributions_disabled is False
