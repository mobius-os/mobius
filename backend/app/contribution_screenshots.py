"""Screenshots a prepared pull request publishes alongside its description.

GitHub has no API for description attachments, so a visible change's
before/after images are hosted on a ``mobius-screenshots`` branch of the
owner's own repository and linked from the description. The owner approves
the images together with the text: staging copies each file next to the
record under its SHA-256 and lists ``{name, sha256, size, alt}`` in
``plan.screenshots``, which the publication approval digest and the
review-verdict reset already cover. The reviewed description refers to each
image as ``(screenshot:NAME)``.

Send uploads the exact approved bytes before anything else becomes public,
proves each anonymous raw URL serves those bytes, and only then replaces each
reference with its commit-pinned URL. A failed upload or proof stops Send
before the branch is pushed.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx

from app.config import get_settings
from app.contribution_errors import ContributionSubmitError
from app.github_contribution_git import _git, _git_env, _run_cmd
from app.manifest_identity import RAW_GITHUB_HOST
from app.storage_io import atomic_write

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}\.png$")
MAX_SCREENSHOTS = 8
MAX_BYTES = 2 * 1024 * 1024
MAX_ALT_CHARS = 200
# A branch only Send writes, holding nothing but ASSETS_DIR: never a branch
# the owner might already use for something else.
ASSETS_BRANCH = "mobius-screenshots"
ASSETS_DIR = "screenshots"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# A link or image target ``](screenshot:NAME)`` outside code, so a body that
# shows the syntax in backticks is neither checked nor rewritten.
_REFERENCE = re.compile(r"\]\(screenshot:([^)\s]*)\)")
# CommonMark code: a fenced block opens on its own line and closes on a line
# holding a fence of the same character at least as long (or never closes),
# and a backtick fence's info string holds no backtick;
# an inline span is a run of backticks closed by a run of the same length,
# which may span lines but not a blank line.
_CODE = re.compile(
  r"^[ ]{0,3}(?P<tick>`{3,})[^`\n]*(?=\n|\Z).*?(?:\n[ ]{0,3}(?P=tick)`*[ \t]*(?=\n|\Z)|\Z)"
  r"|^[ ]{0,3}(?P<tilde>~{3,})[^\n]*.*?(?:\n[ ]{0,3}(?P=tilde)~*[ \t]*(?=\n|\Z)|\Z)"
  r"|(?<!`)(?P<span>`+)(?!`)(?:(?!\n[ \t]*\n).)*?(?<!`)(?P=span)(?!`)",
  re.M | re.S,
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
# raw.githubusercontent.com serves a new commit within seconds, but not always
# on the first request. Bounded, and only for a commit this Send just pushed.
_PROOF_ATTEMPTS = 6
_PROOF_BACKOFF_SECONDS = 1.0

PublicReader = Callable[[str], "bytes | None"]


def blob_dir(record_path: Path) -> Path:
  """Where one record's approved image bytes live, beside its record."""
  return record_path.with_name(f"{record_path.stem}.screenshots")


def _own_blob_dir(record_path: Path) -> Path:
  """``blob_dir``, never through a link: the folder sits in app-writable
  storage, and writing or pruning through a link would reach its target."""
  folder = blob_dir(record_path)
  if folder.is_symlink():
    folder.unlink()
  return folder


def _outside_code(body: str, rewrite: Callable[[str], str]) -> str:
  parts, start = [], 0
  for code in _CODE.finditer(body):
    parts += [rewrite(body[start:code.start()]), code.group()]
    start = code.end()
  return "".join([*parts, rewrite(body[start:])])


def references(body: str) -> list[str]:
  """Names a description refers to, in order, including malformed ones."""
  found: list[str] = []

  def collect(text: str) -> str:
    found.extend(_REFERENCE.findall(text))
    return text

  _outside_code(body or "", collect)
  return found


def _refuse(message: str, code: str = "screenshot_invalid") -> ContributionSubmitError:
  return ContributionSubmitError(message, status_code=422, code=code)


def read(items: list[dict], body: str) -> tuple[list[dict], list[bytes]]:
  """Read and check the requested files; return plan entries and their bytes.

  Every screenshot must be referenced by the description and every reference
  must name a screenshot, so the owner never approves an image that is not
  shown, or a link Send cannot fill. Nothing is written: ``store`` keeps the
  bytes once the whole stage has passed its checks.
  """
  if len(items) > MAX_SCREENSHOTS:
    raise _refuse(f"A pull request carries at most {MAX_SCREENSHOTS} screenshots.")
  entries: list[dict] = []
  payloads: list[bytes] = []
  for item in items:
    name = str(item.get("name") or "")
    if not NAME.fullmatch(name):
      raise _refuse(
        f"Screenshot name {name!r} must be lowercase letters, digits and "
        "hyphens ending in .png, such as before-desktop.png."
      )
    if any(entry["name"] == name for entry in entries):
      raise _refuse(f"Screenshot name {name} is used twice.")
    alt = str(item.get("alt") or "").strip()
    if not alt or len(alt) > MAX_ALT_CHARS or "\n" in alt:
      raise _refuse(
        f"Screenshot {name} needs a one-line description of what it shows "
        f"(at most {MAX_ALT_CHARS} characters)."
      )
    data = _read_png(Path(str(item.get("path") or "")), name)
    sha = hashlib.sha256(data).hexdigest()
    entries.append({"name": name, "sha256": sha, "size": len(data), "alt": alt})
    payloads.append(data)
  check_references(entries, body)
  return entries, payloads


def store(record_path: Path, payloads: list[bytes]) -> None:
  """Keep checked image bytes beside the record, under their digests."""
  if not payloads:
    return
  folder = _own_blob_dir(record_path)
  folder.mkdir(parents=True, exist_ok=True)
  for data in payloads:
    # Rewritten every time, so restaging repairs a damaged stored copy.
    atomic_write(folder / f"{hashlib.sha256(data).hexdigest()}.png", data)


def check_references(entries: list[dict], body: str) -> None:
  """Refuse a description whose screenshot references and attachments differ."""
  if not all(
    isinstance(entry, dict) and NAME.fullmatch(str(entry.get("name") or ""))
    for entry in entries
  ):
    raise _refuse("A listed screenshot is malformed. Attach the screenshots again.")
  named = {entry["name"] for entry in entries}
  referenced = references(body)
  unknown = sorted(set(referenced) - named)
  if unknown:
    raise _refuse(
      "The description refers to screenshots that were not attached: "
      + ", ".join(unknown) + "."
    )
  unused = sorted(named - set(referenced))
  if unused:
    raise _refuse(
      "Each attached screenshot must appear in the description as "
      "![description](screenshot:NAME). Not shown: " + ", ".join(unused) + "."
    )


def _readable_source(path: Path) -> Path | None:
  """The file's real path when it is not itself a link and lies under the
  data or temporary folder, outside the stored credentials; otherwise None."""
  data_dir = Path(get_settings().data_dir).resolve()
  real = path.resolve()
  roots = (data_dir, Path(tempfile.gettempdir()).resolve())
  if (
    path.is_symlink()
    or real.is_relative_to(data_dir / "cli-auth")
    or not any(real.is_relative_to(root) for root in roots)
  ):
    return None
  return real


def _read_bounded(path: Path) -> bytes | None:
  """At most ``MAX_BYTES + 1`` bytes of a regular file that is not a link,
  or None for anything else. Non-blocking, so a named pipe is refused rather
  than waited on; bounded, so a huge file is never read whole."""
  fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
  with os.fdopen(fd, "rb") as handle:
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
      return None
    return handle.read(MAX_BYTES + 1)


def _read_png(path: Path, name: str) -> bytes:
  if not path.is_absolute():
    raise _refuse(f"Screenshot {name} needs an absolute file path.")
  try:
    real = _readable_source(path)
    if real is None:
      raise _refuse(
        f"Screenshot {name} must be a file in the data or temporary folder, "
        "not a link."
      )
    data = _read_bounded(real)
  except OSError as exc:
    raise _refuse(f"Screenshot {name} could not be read: {exc.strerror}.") from exc
  if data is None:
    raise _refuse(f"Screenshot {name} is not a file: {path}.")
  if len(data) > MAX_BYTES:
    raise _refuse(
      f"Screenshot {name} is larger than {MAX_BYTES // (1024 * 1024)} MB. "
      "Crop it to the change."
    )
  if not data.startswith(_PNG_MAGIC):
    raise _refuse(f"Screenshot {name} is not a PNG image.")
  return data


def prune(record_path: Path, keep: list[dict]) -> None:
  """Drop stored images the record no longer lists. Run after the record is
  written, so a failed stage never removes an image the old record needs."""
  folder = _own_blob_dir(record_path)
  if not folder.is_dir():
    return
  wanted = {f"{entry.get('sha256')}.png" for entry in keep}
  for child in folder.iterdir():
    # Only files staging wrote; anything else in the folder is left alone.
    if child.name not in wanted and child.is_file():
      child.unlink(missing_ok=True)
  if not wanted and not any(folder.iterdir()):
    folder.rmdir()


def plan_entries(plan: dict) -> list[dict]:
  """The record's listed screenshots, or [] when it has none."""
  value = plan.get("screenshots")
  return value if isinstance(value, list) else []


def approved_bytes(record_path: Path, entry: dict) -> bytes:
  """The stored image, only if it still has the approved digest.

  The record is app-writable, so Send re-checks what staging enforced rather
  than trusting the listed name.
  """
  sha = str(entry.get("sha256") or "") if isinstance(entry, dict) else ""
  if not _SHA256.fullmatch(sha) or not NAME.fullmatch(str(entry.get("name") or "")):
    raise ContributionSubmitError(
      "A screenshot in this contribution has no valid fingerprint. Ask the "
      "agent to prepare it again. Nothing was sent.",
      code="screenshot_changed",
    )
  path = blob_dir(record_path) / f"{sha}.png"
  try:
    data = _read_bounded(path) or b""
  except OSError:
    data = b""
  if (
    len(data) > MAX_BYTES
    or hashlib.sha256(data).hexdigest() != sha
    or not data.startswith(_PNG_MAGIC)
  ):
    raise ContributionSubmitError(
      f"Screenshot {entry.get('name')} is missing or changed since it was "
      "prepared. Ask the agent to prepare it again. Nothing was sent.",
      code="screenshot_changed",
    )
  return data


def _asset_path(branch: str, name: str) -> str:
  return f"{ASSETS_DIR}/{branch}/{name}"


def raw_url(assets_repo: str, commit: str, branch: str, name: str) -> str:
  path = quote(_asset_path(branch, name), safe="/")
  return f"https://{RAW_GITHUB_HOST}/{assets_repo}/{commit}/{path}"


def resolve_body(
  body: str, entries: list[dict], *, assets_repo: str, commit: str, branch: str,
) -> str:
  """The reviewed description with each reference pinned to its upload."""
  names = {entry["name"] for entry in entries}

  def pin(match: re.Match) -> str:
    name = match.group(1)
    if name not in names:
      raise ContributionSubmitError(
        f"The description refers to an unattached screenshot {name}. "
        "Nothing was sent.",
        code="screenshot_changed",
      )
    return f"]({raw_url(assets_repo, commit, branch, name)})"

  return _outside_code(body, lambda text: _REFERENCE.sub(pin, text))


def verify(record_path: Path, entries: list[dict]) -> None:
  """Refuse before any public write when a listed image is not as approved."""
  for entry in entries:
    approved_bytes(record_path, entry)


def _read_public(url: str) -> bytes | None:
  try:
    response = httpx.get(url, timeout=15.0, follow_redirects=False)
  except httpx.HTTPError:
    return None
  return response.content if response.status_code == 200 else None


def publish(
  record_path: Path,
  entries: list[dict],
  *,
  assets_repo: str,
  branch: str,
  author: tuple[str, str],
  reuse_commit: str | None = None,
  remote_url: str | None = None,
  read_public: PublicReader = _read_public,
) -> str:
  """Upload the approved images to ``assets_repo`` and return the commit
  whose raw URLs serve them.

  ``reuse_commit`` is the commit an earlier attempt for this record already
  proved; it is used again when it still serves the same bytes, so a retried
  Send links the same URLs. Otherwise the images are added on top of the
  current ``ASSETS_BRANCH`` tip (a commit is made only when a file is new or
  different) and pushed behind that tip.
  """
  payloads = [(entry, approved_bytes(record_path, entry)) for entry in entries]
  # The reused commit comes from the app-writable record: only a bare commit
  # id may reach a public link.
  reuse_commit = reuse_commit if _COMMIT.fullmatch(str(reuse_commit or "")) else None
  try:
    with tempfile.TemporaryDirectory() as scratch:
      return _publish(
        Path(scratch), payloads, assets_repo=assets_repo, branch=branch,
        author=author, reuse_commit=reuse_commit,
        remote=remote_url or f"https://github.com/{assets_repo}.git",
        read_public=read_public,
      )
  except (subprocess.TimeoutExpired, OSError) as exc:
    raise ContributionSubmitError(
      f"Uploading the screenshots to {assets_repo} timed out or could not "
      "start. Nothing else was sent; try Send again.",
      code="screenshot_upload_failed",
    ) from exc


def _publish(
  scratch: Path,
  payloads: list[tuple[dict, bytes]],
  *,
  assets_repo: str,
  branch: str,
  author: tuple[str, str],
  reuse_commit: str | None,
  remote: str,
  read_public: PublicReader,
) -> str:
  if reuse_commit and _serves(assets_repo, reuse_commit, branch, payloads, read_public, attempts=1):
    return reuse_commit
  # A throwaway repository, so the review checkout never collects the
  # branch's history or other PRs' images.
  repo = scratch / "assets.git"
  _run_cmd(["git", "init", "-q", "--bare", str(repo)], cwd=scratch)
  for _ in range(2):
    tip = _remote_tip(repo, remote, assets_repo)
    commit = _assets_commit(
      repo, scratch, remote, tip, payloads,
      assets_repo=assets_repo, branch=branch, author=author,
    )
    if commit == tip:
      break
    pushed = _git(
      repo, "push", f"--force-with-lease=refs/heads/{ASSETS_BRANCH}:{tip or ''}",
      remote, f"{commit}:refs/heads/{ASSETS_BRANCH}", check=False,
    )
    if pushed.returncode == 0:
      break
    # Another Send moved the branch between our read and push: rebuild on it.
  else:
    raise ContributionSubmitError(
      f"Could not upload the screenshots to {assets_repo}. Nothing else was "
      "sent; try Send again.",
      code="screenshot_upload_failed",
      detail=(pushed.stderr or pushed.stdout or "").strip()[:400],
    )
  if not _serves(assets_repo, commit, branch, payloads, read_public):
    raise ContributionSubmitError(
      f"The screenshots were uploaded to {assets_repo}, but GitHub does not "
      "serve them publicly, which happens when the repository is private. "
      "Screenshots can only be linked from a public repository: ask the agent "
      "to prepare this PR without them. The code was not sent.",
      code="screenshot_not_public",
    )
  return commit


def _serves(
  assets_repo: str, commit: str, branch: str,
  payloads: list[tuple[dict, bytes]], read_public: PublicReader, *,
  attempts: int = _PROOF_ATTEMPTS,
) -> bool:
  for entry, data in payloads:
    url = raw_url(assets_repo, commit, branch, entry["name"])
    for attempt in range(attempts):
      if read_public(url) == data:
        break
      if attempt + 1 < attempts:
        time.sleep(_PROOF_BACKOFF_SECONDS * (attempt + 1))
    else:
      return False
  return True


def _remote_tip(repo: Path, remote: str, assets_repo: str) -> str | None:
  listed = _git(repo, "ls-remote", remote, f"refs/heads/{ASSETS_BRANCH}", check=False)
  if listed.returncode != 0:
    raise ContributionSubmitError(
      f"Could not read the screenshot branch of {assets_repo}. Nothing was "
      "sent.",
      code="screenshot_upload_failed",
      detail=(listed.stderr or "").strip()[:400],
    )
  line = listed.stdout.strip().split("\n", 1)[0]
  return line.split()[0] if line else None


def _assets_commit(
  repo: Path, scratch: Path, remote: str, tip: str | None,
  payloads: list[tuple[dict, bytes]], *,
  assets_repo: str, branch: str, author: tuple[str, str],
) -> str:
  if tip:
    # Only the tip's trees are needed to add files on top of it.
    _git(
      repo, "fetch", "--no-tags", "--quiet", "--depth=1",
      "--filter=blob:none", remote, tip,
    )
    held = _git(repo, "ls-tree", "--name-only", tip).stdout.splitlines()
    if set(held) - {ASSETS_DIR}:
      raise ContributionSubmitError(
        f"The {ASSETS_BRANCH} branch of {assets_repo} holds files "
        "Send did not put there, so screenshots were not added to it. "
        "Nothing was sent.",
        code="screenshot_upload_failed",
      )
  # Hash the verified bytes, never the app-writable file again.
  blobs: dict[str, str] = {}
  for index, (entry, data) in enumerate(payloads):
    copy = scratch / f"{index}.png"
    copy.write_bytes(data)
    blobs[_asset_path(branch, entry["name"])] = (
      _git(repo, "hash-object", "-w", str(copy)).stdout.strip()
    )
  if tip and all(_blob_at(repo, tip, path) == blob for path, blob in blobs.items()):
    return tip
  env = {
    **_git_env(repo),
    "GIT_INDEX_FILE": str(scratch / "index"),
    "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
    "GIT_COMMITTER_NAME": author[0], "GIT_COMMITTER_EMAIL": author[1],
  }

  def git(*args: str) -> str:
    return _run_cmd(["git", "-C", str(repo), *args], cwd=repo, env=env).stdout.strip()

  if tip:
    git("read-tree", tip)
  for path, blob in blobs.items():
    git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
  # The tip's other images were not fetched; they stay on the remote.
  tree = git("write-tree", "--missing-ok")
  parents = ["-p", tip] if tip else []
  return git("commit-tree", tree, *parents, "-m", f"Add screenshots for {branch}")


def _blob_at(repo: Path, commit: str, path: str) -> str | None:
  found = _git(repo, "rev-parse", "--verify", "--quiet", f"{commit}:{path}", check=False)
  return found.stdout.strip() if found.returncode == 0 else None
