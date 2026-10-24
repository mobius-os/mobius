#!/usr/bin/env python3
"""Work in folders the owner shared from a browser, without uploading them.

Paths start with the folder's name: `Downloads/reports/may.pdf`. Run `folders
list` first. Every command needs Möbius open in the browser that shares the
folder; a refusal explains what the owner can do.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

from agent_api import agent_credentials, agent_request

SHOW_LIMIT_BYTES = 4 * 1024 * 1024
WRITE_LIMIT_BYTES = 4 * 1024 * 1024
# Temporary copies are for one look each: keep only the newest few, briefly,
# so even a 100-photo job never holds more than a few MB on Möbius. The hourly
# scratch sweep expires the last ones on the same clock; keep the two values
# equal (app/agent_scratch.py SHARED_FOLDER_COPY_SECONDS).
SHOW_KEEP_COPIES = 3
SHOW_KEEP_SECONDS = 10 * 60
IMAGE_MAX_EDGE = 2576
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif", ".avif", ".tif", ".tiff"})
LABEL = "folders"


class Folders:
  def __init__(self) -> None:
    self.api, self.token = agent_credentials()
    self._listing: list[dict] | None = None

  def shared(self) -> list[dict]:
    if self._listing is None:
      body = agent_request(self.api, self.token, "GET", "/api/shared-folders", label=LABEL)
      self._listing = list((body or {}).get("folders") or [])
    return self._listing

  def resolve(self, target: str, folder_id: str | None) -> tuple[dict, str]:
    """(folder, path inside it) from `Name/inner/path` or --folder ID."""
    if folder_id:
      folder = next((f for f in self.shared() if f["id"] == folder_id), None)
      if folder is None:
        raise SystemExit(f"No shared folder has id {folder_id}. Run `folders list`.")
      return folder, target.strip("/")
    name, _, inner = target.strip("/").partition("/")
    return self._by_name(name), inner

  def _by_name(self, name: str) -> dict:
    folders = self.shared()
    matches = [f for f in folders if f["id"] == name] or [f for f in folders if f["name"] == name]
    matches = matches or [f for f in folders if f["name"].casefold() == name.casefold()]
    if not matches:
      known = ", ".join(sorted({f["name"] for f in folders})) or "none"
      raise SystemExit(f'No shared folder is named "{name}". Shared folders: {known}. Run `folders list`.')
    ready = [f for f in matches if f.get("state") == "ready"]
    if len(ready) == 1 or len(matches) == 1:
      return (ready or matches)[0]
    choices = "; ".join(f'{f["id"]} ({f["device"]["label"]}, {f["state"]})' for f in matches)
    raise SystemExit(f'Several folders are named "{name}": {choices}. Use --folder ID.')

  def attempt(self, folder: dict, command: dict) -> dict:
    """The server's answer as is: {ok, result} or {ok: false, code, error}."""
    body = agent_request(
      self.api, self.token, "POST", f"/api/shared-folders/{folder['id']}/commands",
      command, label=LABEL, timeout=75,
    )
    return body if isinstance(body, dict) else {"ok": False, "error": "empty answer"}

  def run(self, folder: dict, command: dict) -> dict:
    body = self.attempt(folder, command)
    if not body.get("ok"):
      raise SystemExit(f"{folder['name']}: {body.get('error')} ({body.get('code', 'error')})")
    return body.get("result") or {}


def _size(value: int | None) -> str:
  if value is None:
    return ""
  for unit in ("B", "KB", "MB", "GB"):
    if value < 1024 or unit == "GB":
      return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
    value /= 1024
  return ""


def show_list(client: Folders, args: argparse.Namespace) -> None:
  folders = client.shared()
  if not folders:
    print("No folders are shared. The owner can share one in Settings → Shared folders.")
    return
  for folder in folders:
    rights = "read & edit" if folder["mode"] == "edit" and folder["access"] == "remembered" else "read only"
    print(f'{folder["name"]}\t{folder["device"]["label"]}\t{rights}\t{folder["state"]}\tid={folder["id"]}')


def show_entries(client: Folders, args: argparse.Namespace) -> None:
  folder, inner = client.resolve(args.target, args.folder)
  command = {"op": "list", "path": inner, "cursor": args.cursor}
  if args.limit:
    command["limit"] = args.limit
  result = client.run(folder, command)
  for entry in result.get("entries", []):
    suffix = "/" if entry["kind"] == "folder" else f'\t{_size(entry.get("size"))}'
    print(f'{entry["name"]}{suffix}')
  if result.get("nextCursor") is not None:
    print(f'… {result["total"]} entries. Next page: --cursor {result["nextCursor"]}')


def show_info(client: Folders, args: argparse.Namespace) -> None:
  folder, inner = client.resolve(args.target, args.folder)
  print(json.dumps(client.run(folder, {"op": "info", "path": inner}), indent=2, ensure_ascii=False))


def read_file(client: Folders, args: argparse.Namespace) -> None:
  folder, inner = client.resolve(args.target, args.folder)
  command: dict = {"op": "read", "path": inner, "length": args.tail or args.length}
  if args.tail:
    command["fromEnd"] = True
  elif args.offset:
    command["offset"] = args.offset
  if args.base64:
    command["encoding"] = "base64"
  result = client.run(folder, command)
  print(result.get("base64") if args.base64 else result.get("text", ""), end="", flush=True)
  if not result.get("eof"):
    end = result["offset"] + result["length"]
    print(f'\n[{_size(result["length"])} of {_size(result["size"])} shown. Continue with --offset {end}]', file=sys.stderr)


def show_file(client: Folders, args: argparse.Namespace) -> None:
  """Copy one image or PDF to this chat's scratch folder so the agent can open it."""
  folder, inner = client.resolve(args.target, args.folder)
  image = None
  if Path(inner).suffix.lower() in IMAGE_SUFFIXES:
    image = client.attempt(folder, {"op": "image", "path": inner, "maxEdge": IMAGE_MAX_EDGE})
  # A tab that loaded Möbius before photo shrinking answers invalid_request:
  # fall back to the plain copy, which still works for files under 4 MB.
  if image and image.get("code") != "invalid_request":
    if not image.get("ok"):
      raise SystemExit(f"{folder['name']}: {image.get('error')} ({image.get('code', 'error')})")
    result = image.get("result") or {}
    name = Path(inner).stem + (".jpg" if result.get("shrunk") else Path(inner).suffix)
    note = _shrink_note(result)
  else:
    size = client.run(folder, {"op": "info", "path": inner}).get("size")
    if size is None or size > SHOW_LIMIT_BYTES:
      raise SystemExit(f'{inner} is not a file under {_size(SHOW_LIMIT_BYTES)}; read a piece with `folders read`.')
    result = client.run(folder, {"op": "read", "path": inner, "length": SHOW_LIMIT_BYTES, "encoding": "base64"})
    name = Path(inner).name
    note = " (not shrunk: reload the Möbius tab on that device to shrink photos)" if image else ""
  output = _scratch_dir() / f"{time.time_ns()}-{name}"
  output.write_bytes(base64.b64decode(result["base64"]))
  print(f"Saved a temporary copy to {output}{note}. Look at it now: only the {SHOW_KEEP_COPIES} newest copies are kept.")


def _shrink_note(result: dict) -> str:
  if not result.get("shrunk"):
    return ""
  return (f' (shrunk on the device from {result["originalWidth"]}×{result["originalHeight"]}, '
          f'{_size(result["size"])}, to {result["width"]}×{result["height"]}, {_size(result["bytes"])})')


def _scratch_dir() -> Path:
  directory = Path(os.environ.get("TMPDIR") or "/tmp") / "shared-folders"
  directory.mkdir(parents=True, exist_ok=True)
  copies = sorted((p for p in directory.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
  cutoff = time.time() - SHOW_KEEP_SECONDS
  for index, old in enumerate(copies):
    if index >= SHOW_KEEP_COPIES - 1 or old.stat().st_mtime < cutoff:
      old.unlink(missing_ok=True)
  return directory


def write_file(client: Folders, args: argparse.Namespace) -> None:
  folder, inner = client.resolve(args.target, args.folder)
  if args.text is not None:
    data = args.text.encode()
    command = {"op": "write", "path": inner, "content": args.text}
  else:
    # stdin only when asked for with "-": an agent's shell has no input, and
    # reading it by default would save an empty file.
    data = sys.stdin.buffer.read() if args.from_file == "-" else Path(args.from_file).read_bytes()
    command = {"op": "write", "path": inner, "content": base64.b64encode(data).decode(), "encoding": "base64"}
  if not data and not args.allow_empty:
    raise SystemExit("Nothing to write: the content is empty. Add --allow-empty if an empty file is intended.")
  if len(data) > WRITE_LIMIT_BYTES:
    raise SystemExit(f"Write at most {_size(WRITE_LIMIT_BYTES)} at a time.")
  if args.overwrite:
    command["overwrite"] = True
  result = client.run(folder, command)
  if not result.get("created"):
    kept = f' The old version is in {folder["name"]}/{result["binned"]}.' if result.get("binned") else ""
    print(f'Replaced {folder["name"]}/{result["path"]} ({_size(result["size"])}).{kept}')
    return
  print(f'Created {folder["name"]}/{result["path"]} ({_size(result["size"])}).')


def move_file(client: Folders, args: argparse.Namespace) -> None:
  folder, inner = client.resolve(args.target, args.folder)
  destination = args.destination.strip("/")
  if not args.folder:
    # Both paths start with the folder's name, so a move can't silently
    # land somewhere else; moving between shared folders is not possible.
    name, _, destination = destination.partition("/")
    same = name == folder["id"] or name.casefold() == folder["name"].casefold()
    if not same or not destination:
      raise SystemExit(
        f'Moves stay inside one shared folder. Start the new path with "{folder["name"]}/", '
        f'for example "{folder["name"]}/Sorted/{Path(inner).name}".'
      )
  result = client.run(folder, {"op": "move", "path": inner, "to": destination})
  print(f'Moved {result["path"]} → {result["to"]} in {folder["name"]}.')


def make_folder(client: Folders, args: argparse.Namespace) -> None:
  folder, inner = client.resolve(args.target, args.folder)
  result = client.run(folder, {"op": "mkdir", "path": inner})
  print(f'{"Created" if result.get("created") else "Already there:"} {folder["name"]}/{result["path"]}')


def bin_file(client: Folders, args: argparse.Namespace) -> None:
  folder, inner = client.resolve(args.target, args.folder)
  result = client.run(folder, {"op": "bin", "path": inner})
  print(f'Moved {result["path"]} to {folder["name"]}/{result["to"]}. The owner empties the bin.')


def _parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  sub = parser.add_subparsers(dest="action", required=True)
  sub.add_parser("list", help="shared folders, their device, rights and state").set_defaults(run=show_list)

  def target(name: str, run, help_text: str) -> argparse.ArgumentParser:
    command = sub.add_parser(name, help=help_text)
    command.add_argument("target", help="FolderName/path/inside")
    command.add_argument("--folder", help="folder id, when two folders share a name")
    command.set_defaults(run=run)
    return command

  ls = target("ls", show_entries, "list a folder's contents")
  ls.add_argument("--cursor", type=int, default=0)
  ls.add_argument("--limit", type=int)
  target("info", show_info, "size, type and dates")
  read = target("read", read_file, "read a piece of a file (default 256 KB)")
  read.add_argument("--offset", type=int, default=0)
  read.add_argument("--length", type=int, default=256 * 1024)
  read.add_argument("--tail", type=int, help="read this many bytes from the end")
  read.add_argument("--base64", action="store_true")
  target("show", show_file, "a temporary copy of an image (shrunk on the device) or PDF, to look at")
  write = target("write", write_file, "save a file (Read & edit folders)")
  source = write.add_mutually_exclusive_group(required=True)
  source.add_argument("--text")
  source.add_argument("--from-file", metavar="PATH", help='a local file, or "-" for standard input')
  write.add_argument("--overwrite", action="store_true", help="replace an existing file; its old version goes to the bin")
  write.add_argument("--allow-empty", action="store_true", help="save an empty file on purpose")
  move = target("move", move_file, "rename or move a file (Read & edit folders)")
  move.add_argument("destination", help="new path, starting with the same folder's name")
  target("mkdir", make_folder, "create a folder (Read & edit folders)")
  target("bin", bin_file, "move a file to the Möbius bin; nothing is ever deleted")
  return parser


def main() -> None:
  args = _parser().parse_args()
  args.run(Folders(), args)


if __name__ == "__main__":
  main()
