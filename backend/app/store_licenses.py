"""The free licenses an app may be published to the Store under.

Publishing makes an app's source public, so the Store requires the owner to
say how others may use it. The choice lives where every app tool already
looks for it: an SPDX identifier in ``mobius.json``'s ``license`` and the
full text in a root ``LICENSE`` file. Saving the listing writes both, but
replaces only license text it wrote itself; the publish gate refuses a
snapshot missing either. An app the Store already lists may keep publishing
under another free license it declares (``LISTED_FREE_SPDX``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


LICENSE_FILE = "LICENSE"
# Root files that count as the license text, compared case-insensitively.
LICENSE_FILE_NAMES = frozenset({"license", "license.md", "license.txt", "copying"})
_TEXTS = Path(__file__).resolve().parent / "license_texts"

_MIT = """MIT License

Copyright (c) {holder}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


@dataclass(frozen=True)
class StoreLicense:
  spdx: str
  name: str


# Order is the order the Store offers them; the first is its recommendation.
OFFERED_LICENSES = (
  StoreLicense("MIT", "MIT License"),
  StoreLicense("Apache-2.0", "Apache License 2.0"),
  StoreLicense("GPL-3.0-or-later", "GNU GPL v3.0 or later"),
)
OFFERED_SPDX = frozenset(item.spdx for item in OFFERED_LICENSES)
# Other free (OSI-approved) SPDX ids an app the Store already lists may keep
# publishing updates under. The Host's registry keeps the same set
# (LISTED_FREE_LICENSES) and admits such an update only when the listed
# revision declares that same id. New listings need an offered license.
LISTED_FREE_SPDX = frozenset({
  "0BSD", "AGPL-3.0-only", "AGPL-3.0-or-later", "Artistic-2.0",
  "BSD-2-Clause", "BSD-3-Clause", "BSL-1.0", "EPL-2.0", "GPL-2.0-only",
  "GPL-2.0-or-later", "GPL-3.0-only", "ISC", "LGPL-2.1-only",
  "LGPL-2.1-or-later", "LGPL-3.0-only", "LGPL-3.0-or-later", "MPL-2.0",
  "Unlicense", "Zlib",
})


def license_options() -> list[dict[str, str]]:
  """The offered licenses as the Store's picker shows them, recommended first."""
  return [{"spdx": item.spdx, "name": item.name} for item in OFFERED_LICENSES]


def declared_license(manifest: dict) -> str:
  """The manifest's license as written, offered or not; "" when it has none."""
  value = manifest.get("license")
  return value.strip() if isinstance(value, str) else ""


def offered_license(manifest: dict) -> str:
  """The manifest's license when it is one the Store offers, else ""."""
  value = declared_license(manifest)
  return value if value in OFFERED_SPDX else ""


def publishable_license(manifest: dict, *, listed: bool) -> str:
  """The manifest's license when publishing accepts it, else "".

  That is an offered license, or for an app the Store already lists
  (``listed``), one of ``LISTED_FREE_SPDX``.
  """
  value = declared_license(manifest)
  if value in OFFERED_SPDX or (listed and value in LISTED_FREE_SPDX):
    return value
  return ""


def copyright_holder(manifest: dict) -> str:
  """Who the MIT notice names: the declared author, else the app's authors."""
  author = manifest.get("author")
  if isinstance(author, dict):
    author = author.get("name")
  author = author.strip() if isinstance(author, str) else ""
  if author:
    return author
  name = manifest.get("name")
  name = name.strip() if isinstance(name, str) else ""
  return f"The {name} authors" if name else "The app's authors"


@lru_cache(maxsize=None)
def _shipped_text(spdx: str) -> str:
  return (_TEXTS / f"{spdx}.txt").read_text(encoding="utf-8")


def license_text(spdx: str, manifest: dict) -> bytes:
  """The exact LICENSE bytes a listing save writes for ``spdx``."""
  if spdx not in OFFERED_SPDX:
    raise ValueError(f"{spdx!r} is not a license the Store offers.")
  if spdx == "MIT":
    return _MIT.format(holder=copyright_holder(manifest)).encode("utf-8")
  return _shipped_text(spdx).encode("utf-8")


def is_license_file(path: str) -> bool:
  """Whether the repository-relative ``path`` is a root license text."""
  return "/" not in path and path.casefold() in LICENSE_FILE_NAMES


def has_license_file(paths: Iterable[str]) -> bool:
  """Whether a root license text is among the repository-relative ``paths``."""
  return any(is_license_file(path) for path in paths)
