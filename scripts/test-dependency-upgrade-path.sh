#!/usr/bin/env bash
# Disposable hosted-runner test of an actual Python patch and platform pip-lock
# change. No published fixture SHA is invented: two local commits are bundled
# into their corresponding local-overlay images, then the existing updater
# harness drives the old image's owner HTTP API and an isolated helper stand-in.
#
#   scripts/test-dependency-upgrade-path.sh <official-base-sha> [repo]
#
# Run only on a disposable amd64 Docker/Buildx runner. This never publishes an
# image, installs a host package, or contacts a production helper.

set -euo pipefail
BASE=${1:?official base SHA}
REPO=${2:-.}
[[ $BASE =~ ^[0-9a-f]{40}$ ]] || { echo "fixture: full base SHA required" >&2; exit 2; }
[[ $(uname -m) == x86_64 ]] || { echo "fixture: amd64 runner required" >&2; exit 2; }
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
git -C "$REPO" cat-file -e "$BASE^{commit}" \
  || { echo "fixture: base commit is absent" >&2; exit 2; }
docker info >/dev/null
docker buildx version >/dev/null

work=$(mktemp -d)
id=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
old_image="mobius-dependency-fixture:old-$id"
new_image="mobius-dependency-fixture:new-$id"
old_owned=false
new_owned=false
cleanup() {
  [ "$new_owned" = true ] && docker image rm -f "$new_image" >/dev/null 2>&1 || true
  [ "$old_owned" = true ] && docker image rm -f "$old_image" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if docker image inspect "$old_image" >/dev/null 2>&1 \
   || docker image inspect "$new_image" >/dev/null 2>&1; then
  echo "fixture: generated image tag already exists; refusing to replace it" >&2
  exit 2
fi

git clone -q --no-local "$REPO" "$work/source"
git -C "$work/source" checkout -q --detach "$BASE"
source_dir=$work/source

build_image() {  # <tag> <commit SHA>
  local tag=$1 sha=$2
  mkdir -p "$work/bundle"
  # Only the two local commits travel. Dockerfile first fetches the public base
  # object, then fetches this bundle and verifies its exact SHA.
  git -C "$source_dir" bundle create "$work/bundle/platform.bundle" HEAD "^$BASE"
  docker buildx build --platform linux/amd64 --load \
    --build-context "mobius-local-platform-source=$work/bundle" \
    --build-arg "BUILD_SHA=$sha" \
    --build-arg "MOBIUS_USE_LOCAL_PLATFORM_SOURCE=1" \
    --build-arg "MOBIUS_LOCAL_PLATFORM_SHA=$sha" \
    --build-arg "MOBIUS_LOCAL_PLATFORM_BASE_SHA=$BASE" \
    -t "$tag" "$source_dir"
  rm -f "$work/bundle/platform.bundle"
}

python3 "$root/scripts/dependency-upgrade-fixture.py" old "$source_dir"
git -C "$source_dir" add Dockerfile
git -C "$source_dir" -c core.hooksPath=/dev/null \
  -c user.name=upgrade-fixture -c user.email=upgrade-fixture@localhost \
  commit -qm 'Fixture: pin Python 3.12.13'
old_sha=$(git -C "$source_dir" rev-parse HEAD)
# The unique tags were checked absent above. Own each attempted build before
# Docker can create a partial result, including an interrupted --load.
old_owned=true
build_image "$old_image" "$old_sha"

python3 "$root/scripts/dependency-upgrade-fixture.py" new "$source_dir"
git -C "$source_dir" add Dockerfile backend/requirements.txt backend/requirements.lock
git -C "$source_dir" -c core.hooksPath=/dev/null \
  -c user.name=upgrade-fixture -c user.email=upgrade-fixture@localhost \
  commit -qm 'Fixture: Python 3.12.14 and locked colorama'
new_sha=$(git -C "$source_dir" rev-parse HEAD)
new_owned=true
build_image "$new_image" "$new_sha"

echo "fixture: ${old_sha:0:12} (Python 3.12.13) -> ${new_sha:0:12} (Python 3.12.14 + colorama)"
echo "fixture: booting target image, then wrong-image rollback to verify the prepared snapshot"
UPGRADE_PYTHON_BEFORE=3.12.13 UPGRADE_PYTHON_AFTER=3.12.14 \
  UPGRADE_LOCK_PACKAGE=colorama==0.4.6 UPGRADE_FORCE_ROLLBACK=1 \
  "$root/scripts/test-upgrade-path.sh" "$old_image" "$new_image" "$source_dir"
echo "fixture: performing successful owner-reviewed replacement on a fresh volume"
UPGRADE_PYTHON_BEFORE=3.12.13 UPGRADE_PYTHON_AFTER=3.12.14 \
  UPGRADE_LOCK_PACKAGE=colorama==0.4.6 \
  "$root/scripts/test-upgrade-path.sh" "$old_image" "$new_image" "$source_dir"
