"""The Wait CLI receipt describes the actual schedule and its admission barriers."""

import importlib.util
from pathlib import Path

import pytest


def test_command_file_is_saved_literally_not_nested_in_shell_quotes(monkeypatch, tmp_path):
  path = Path(__file__).parents[1] / 'scripts' / 'chat_wait.py'
  spec = importlib.util.spec_from_file_location('chat_wait_helper', path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  command = "set -o pipefail\nvalue='a \"quoted\" value'\n[[ $value == *quoted* ]]\n"
  check = tmp_path / 'check.sh'
  check.write_text(command)
  captured = []
  monkeypatch.setattr(module, '_settings', lambda: ('http://example.invalid', 'test-only', 'chat'))
  monkeypatch.setattr(module, '_call', lambda *args: captured.append(args) or {'kind': 'command'})
  monkeypatch.setattr('sys.argv', ['chat_wait.py', 'declare', 'Ready',
    '--command-file', str(check), '--owner', 'test executor', '--deadline', '600'])
  module.main()
  assert captured[0][2]['command'] == command


def test_command_file_and_timer_are_rejected_before_any_request(monkeypatch, tmp_path):
  path = Path(__file__).parents[1] / 'scripts' / 'chat_wait.py'
  spec = importlib.util.spec_from_file_location('chat_wait_helper', path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  check = tmp_path / 'check.sh'
  check.write_text('true\n')
  monkeypatch.setattr(module, '_settings', lambda: pytest.fail('must not connect'))
  monkeypatch.setattr('sys.argv', [
    'chat_wait.py', 'declare', 'Ready', '--command-file', str(check), '--in', '60',
  ])
  with pytest.raises(SystemExit) as exc:
    module.main()
  assert exc.value.code == 2


@pytest.mark.parametrize('kind', ['timer', 'command'])
def test_declaration_receipt_distinguishes_timers_and_command_checks(monkeypatch, capsys, kind):
  path = Path(__file__).parents[1] / 'scripts' / 'chat_wait.py'
  spec = importlib.util.spec_from_file_location('chat_wait_helper', path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  monkeypatch.setattr(module, '_settings', lambda: ('http://example.invalid', 'test-only', 'chat'))
  monkeypatch.setattr(module, 'declare_wait', lambda *args, **kwargs: {
    'kind': kind, 'interval_secs': 300,
    'due_at': '2026-09-08T12:00:00', 'deadline_at': '2026-09-08T13:00:00',
  })
  arguments = ['--in', '60'] if kind == 'timer' else [
    '--owner', 'test executor', '--command', 'true', '--deadline', '3600',
  ]
  monkeypatch.setattr('sys.argv', ['chat_wait.py', 'declare', 'Resume when ready', *arguments])
  module.main()
  receipt = capsys.readouterr().err
  assert 'Open owner-input cards and recovery holds still take precedence' in receipt
  if kind == 'timer':
    assert 'timer becomes due at 2026-09-08T12:00:00' in receipt
    assert 'probed now' not in receipt
    assert 'every 300s' not in receipt
  else:
    assert 'condition is met, its check fails, or its deadline is reached' in receipt
    assert 'probed now, then every 300s' in receipt
