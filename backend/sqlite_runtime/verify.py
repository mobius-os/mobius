"""Image-build contracts for the SQLite library Python loads and the CLI."""
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

VERSION = "3.53.4"
SOURCE_ID = "2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc"


class SQLiteImageContract(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)

    def test_python_and_cli_load_the_pinned_fixed_release(self):
        self.assertEqual(sqlite3.sqlite_version, VERSION)
        self.assertEqual(self.db.execute("SELECT sqlite_source_id()").fetchone()[0], SOURCE_ID)
        output = subprocess.check_output(
            ["sqlite3", ":memory:", "SELECT sqlite_version(); SELECT sqlite_source_id();"],
            text=True,
        )
        self.assertEqual(output.splitlines(), [VERSION, SOURCE_ID])

    def test_python_maps_the_installed_shared_library(self):
        # This verifier runs inside a Linux image, after ldconfig. Private
        # build tests use a child-only loader override to the staging directory.
        libraries = {line.split()[-1] for line in Path("/proc/self/maps").read_text().splitlines()
                     if "libsqlite3.so" in line}
        expected = Path(os.environ.get("LD_LIBRARY_PATH", "/usr/local/lib")) / "libsqlite3.so.0"
        self.assertEqual(libraries, {str(expected.resolve())})

    def test_debian_library_features_and_limits_are_preserved(self):
        options = {row[0] for row in self.db.execute("PRAGMA compile_options")}
        required = {
            "ALLOW_ROWID_IN_VIEW", "ENABLE_COLUMN_METADATA", "ENABLE_DBPAGE_VTAB",
            "ENABLE_DBSTAT_VTAB", "ENABLE_FTS3", "ENABLE_FTS3_PARENTHESIS",
            "ENABLE_FTS3_TOKENIZER", "ENABLE_FTS4", "ENABLE_FTS5",
            "ENABLE_LOAD_EXTENSION", "ENABLE_MATH_FUNCTIONS", "ENABLE_PREUPDATE_HOOK",
            "ENABLE_RTREE", "ENABLE_SESSION", "ENABLE_STMTVTAB", "ENABLE_UNLOCK_NOTIFY",
            "ENABLE_UPDATE_DELETE_LIMIT", "HAVE_ISNAN", "LIKE_DOESNT_MATCH_BLOBS",
            "MAX_SCHEMA_RETRY=25", "SECURE_DELETE", "SOUNDEX", "THREADSAFE=1", "USE_URI",
            "MAX_DEFAULT_PAGE_SIZE=32768", "MAX_FUNCTION_ARG=127",
            "MAX_VARIABLE_NUMBER=250000", "DEFAULT_SYNCHRONOUS=2",
            "DEFAULT_WAL_SYNCHRONOUS=2", "TEMP_STORE=1",
        }
        self.assertFalse(required - options, f"Missing SQLite options: {required - options}")
        cli_options = set(subprocess.check_output(
            ["sqlite3", ":memory:", "PRAGMA compile_options;"], text=True,
        ).splitlines())
        self.assertFalse(required - cli_options, f"Missing CLI options: {required - cli_options}")
        self.assertEqual(self.db.execute("PRAGMA secure_delete").fetchone()[0], 1)
        self.assertEqual(self.db.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER), 250000)
        self.assertEqual(self.db.getlimit(sqlite3.SQLITE_LIMIT_FUNCTION_ARG), 127)

    def test_cli_keeps_interactive_editing_and_compressed_archive_support(self):
        linked = subprocess.check_output(["ldd", shutil.which("sqlite3")], text=True)
        self.assertIn("libreadline.so.8", linked)
        output = subprocess.check_output([
            "sqlite3", ":memory:",
            "WITH payload(v) AS (SELECT CAST(printf('%01024d', 1) AS BLOB)) "
            "SELECT length(sqlar_compress(v)) < length(v) AND "
            "sqlar_uncompress(sqlar_compress(v), length(v)) = v FROM payload;",
        ], text=True)
        self.assertEqual(output.strip(), "1")

    def test_generated_parser_supports_ordered_update_and_delete_limits(self):
        self.db.executescript("""
            CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT);
            INSERT INTO items VALUES(1, 'a'), (2, 'b'), (3, 'c');
            UPDATE items SET value='changed' ORDER BY id DESC LIMIT 1;
            DELETE FROM items ORDER BY id LIMIT 1;
        """)
        self.assertEqual(self.db.execute("SELECT * FROM items ORDER BY id").fetchall(),
                         [(2, "b"), (3, "changed")])
        output = subprocess.check_output([
            "sqlite3", ":memory:",
            "CREATE TABLE t(n); INSERT INTO t VALUES(1),(2); "
            "DELETE FROM t ORDER BY n LIMIT 1; SELECT n FROM t;",
        ], text=True)
        self.assertEqual(output.strip(), "2")

    def test_legacy_view_rowid_is_still_accepted(self):
        self.db.executescript("""
            CREATE TABLE t(value);
            INSERT INTO t VALUES('item');
            CREATE VIEW v AS SELECT * FROM t;
        """)
        self.assertEqual(self.db.execute("SELECT rowid FROM v").fetchall(), [(None,)])

    def test_json_and_full_text_queries_preserve_unicode(self):
        self.db.executescript("CREATE VIRTUAL TABLE search USING fts5(body);")
        self.db.execute("INSERT INTO search VALUES(?)", ("café transcript",))
        self.assertEqual(self.db.execute(
            "SELECT body FROM search WHERE search MATCH 'transcript'"
        ).fetchone()[0], "café transcript")
        self.assertEqual(self.db.execute(
            "SELECT json_extract(?, '$.text')", ('{"text":"café"}',)
        ).fetchone()[0], "café")

    def test_wal_database_checkpoint_and_online_backup_remain_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.db"
            with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(":memory:")) as backup:
                self.assertEqual(source.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                source.executescript("CREATE TABLE items(n); INSERT INTO items VALUES(7);")
                source.backup(backup)
                self.assertEqual(backup.execute("SELECT n FROM items").fetchone()[0], 7)
                self.assertEqual(source.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0], 0)
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as reader:
                self.assertEqual(reader.execute("SELECT n FROM items").fetchone()[0], 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
