import json
import os
import sqlite3
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "backend" / "scripts" / "fork-chat.sh"


def _seed_chat(data_dir: Path, *, provider="codex", chat_id="chat-1") -> None:
  db_dir = data_dir / "db"
  db_dir.mkdir(parents=True)
  con = sqlite3.connect(db_dir / "ultimate.db")
  con.execute(
    "create table chats (id text primary key, provider text, "
    "session_id text, messages text)"
  )
  messages = [
    {"role": "user", "content": "please fix the thing"},
    {"role": "assistant", "content": "I fixed the thing"},
  ]
  con.execute(
    "insert into chats (id, provider, session_id, messages) values (?, ?, ?, ?)",
    (chat_id, provider, "codex-session-id", json.dumps(messages)),
  )
  con.commit()
  con.close()


def _write_fake_codex(bin_dir: Path, *, exit_code=0) -> None:
  bin_dir.mkdir()
  fake = bin_dir / "codex"
  fake.write_text(
    f"""#!/usr/bin/env bash
set -euo pipefail
out=""
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--output-last-message)
      out="$2"; shift 2 ;;
    *)
      args+=("$1"); shift ;;
  esac
done
if [[ {exit_code} -ne 0 ]]; then
  echo "fake codex trust failure"
  exit {exit_code}
fi
{{
  printf 'pwd=%s\\n' "$PWD"
  printf 'CODEX_HOME=%s\\n' "${{CODEX_HOME:-}}"
  printf 'args=%s\\n' "${{args[*]}}"
  printf 'prompt=%s\\n' "${{args[-1]}}"
}} > "$out"
""",
    encoding="utf-8",
  )
  fake.chmod(0o755)


def test_codex_fork_uses_prod_auth_home_and_non_git_data_dir(tmp_path):
  _seed_chat(tmp_path)
  bin_dir = tmp_path / "bin"
  _write_fake_codex(bin_dir)

  env = {
    **os.environ,
    "DATA_DIR": str(tmp_path),
    "PATH": f"{bin_dir}:{os.environ['PATH']}",
  }
  env.pop("CODEX_HOME", None)

  proc = subprocess.run(
    ["bash", str(SCRIPT), "chat-1", "what should Reflection learn?"],
    env=env,
    text=True,
    capture_output=True,
    check=True,
  )

  assert f"pwd={tmp_path}" in proc.stdout
  assert f"CODEX_HOME={tmp_path / 'cli-auth' / 'codex'}" in proc.stdout
  assert "exec --skip-git-repo-check --ephemeral --sandbox read-only" in proc.stdout
  assert "You previously worked on this" in proc.stdout
  assert "what should Reflection learn?" in proc.stdout


def test_codex_fork_reports_cli_failure(tmp_path):
  _seed_chat(tmp_path)
  bin_dir = tmp_path / "bin"
  _write_fake_codex(bin_dir, exit_code=7)

  env = {
    **os.environ,
    "DATA_DIR": str(tmp_path),
    "PATH": f"{bin_dir}:{os.environ['PATH']}",
  }

  proc = subprocess.run(
    ["bash", str(SCRIPT), "chat-1", "interview"],
    env=env,
    text=True,
    capture_output=True,
  )

  assert proc.returncode == 7
  assert "fake codex trust failure" in proc.stderr
  assert "fork-chat: codex interview failed for chat-1 (rc=7)" in proc.stderr
