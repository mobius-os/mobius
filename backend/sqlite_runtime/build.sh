#!/usr/bin/env bash
# Build a pinned SQLite into a staging root; never change the host loader cache.
set -euo pipefail
DESTDIR="$(realpath -m "${1:?usage: build.sh STAGING_ROOT}")"
BUILD="$(mktemp -d)"
trap 'rm -rf -- "$BUILD"' EXIT
cd "$BUILD"

curl --fail --silent --show-error --location \
  https://sqlite.org/2026/sqlite-src-3530400.zip -o source.zip
python3 - <<'PY'
import hashlib
from pathlib import Path

expected = "b834d474b9b393d85a9e3ee4cc11f1329e007e9376a424ee740796f5c4bda3a8"
actual = hashlib.sha3_256(Path("source.zip").read_bytes()).hexdigest()
if actual != expected:
    raise SystemExit(f"SQLite source checksum mismatch: {actual}")
PY
unzip -q source.zip
cd sqlite-src-3530400

# Match Debian trixie's library features and limits. UPDATE/DELETE LIMIT must
# also reach the parser generator, so build from canonical source, not the
# pre-generated download. See https://sqlite.org/howtocompile.html.
./configure --prefix=/usr/local --soname=legacy --disable-static \
  --disable-tcl --fts3 --fts4 --fts5 --rtree --session --update-limit \
  CFLAGS="-O2 -DSQLITE_ALLOW_ROWID_IN_VIEW -DSQLITE_ENABLE_COLUMN_METADATA \
  -DSQLITE_ENABLE_DBSTAT_VTAB -DSQLITE_ENABLE_DBPAGE_VTAB -DSQLITE_ENABLE_PREUPDATE_HOOK \
  -DSQLITE_ENABLE_FTS3_PARENTHESIS -DSQLITE_ENABLE_FTS3_TOKENIZER -DSQLITE_ENABLE_LOAD_EXTENSION \
  -DSQLITE_ENABLE_STMTVTAB -DSQLITE_ENABLE_UNLOCK_NOTIFY -DHAVE_ISNAN \
  -DSQLITE_LIKE_DOESNT_MATCH_BLOBS -DSQLITE_MAX_SCHEMA_RETRY=25 -DSQLITE_SECURE_DELETE \
  -DSQLITE_SOUNDEX -DSQLITE_USE_URI -DSQLITE_MAX_VARIABLE_NUMBER=250000 \
  -DSQLITE_MAX_DEFAULT_PAGE_SIZE=32768 -DSQLITE_MAX_FUNCTION_ARG=127"
make -j2
make install DESTDIR="$DESTDIR"
readelf -d "$DESTDIR/usr/local/lib/libsqlite3.so" | grep -F 'Library soname: [libsqlite3.so.0]'
