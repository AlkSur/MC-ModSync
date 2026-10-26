"""Menu dispatch tests: no remote operations are executed."""
import subprocess
from pathlib import Path

import pytest

from mcmodsync import cli
from mcmodsync.op_menu import run_menu


def play(answers, results=()):
    answers, results = iter(answers), iter(results)
    calls, output = [], []

    def dispatch(args):
        calls.append(args)
        return next(results, 0)

    def read(prompt):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError

    assert run_menu('test.json', read=read, write=output.append, dispatch=dispatch) == 0
    return calls, output


def test_publish_previews_same_arguments_before_confirmed_write():
    calls, _ = play(['4', '1', '1.2.0', 'new mods', '1', '0'])
    command = ['publish-client', '--notes', 'new mods', '--version', '1.2.0']
    assert calls == [command + ['--dry-run', '--config', 'test.json'],
                     command + ['--config', 'test.json']]


@pytest.mark.parametrize('choice,answers,command', [
    ('3', [], ['push-server']),
    ('6', ['new package'], ['publish-updater', '--package', 'new package']),
    ('8', [], ['rebuild-sources']),
])
def test_mutations_require_successful_preview_and_confirmation(choice, answers, command):
    calls, output = play([choice] + answers + ['0'], results=[1])
    assert calls == [command + ['--dry-run', '--config', 'test.json']]
    assert any('预览失败' in line for line in output)
    calls, _ = play([choice] + answers + ['0', 'exit'])
    assert len(calls) == 1
    calls, _ = play([choice] + answers + ['1', 'exit'])
    assert calls[-1] == command + ['--config', 'test.json']


def test_unverified_server_cannot_publish():
    calls, _ = play(['4', '0', 'exit'])
    assert not calls


def test_invalid_input_config_change_and_eof():
    calls, output = play(['nonsense', '10', 'other config.json', '1', '2'])
    assert calls == [['doctor', '--offline', '--config', 'other config.json']]
    assert any('无效选择' in line for line in output)
    assert any('已退出' in line for line in output)


def test_fetch_upgrade_and_package_defaults():
    calls, _ = play(['2', '2', '5', '', '0'])
    assert calls == [['fetch-mods', '--upgrade', '--config', 'test.json'],
                     ['package-client', '--out', 'dist/client-package', '--config', 'test.json']]


def test_exception_and_interrupt_return_to_menu():
    answers = iter(['1', '1', '1', '2', '0'])
    attempts, output = [], []

    def dispatch(args):
        attempts.append(args)
        if len(attempts) == 1:
            raise RuntimeError('secret credential must not be printed')
        raise KeyboardInterrupt

    assert run_menu(read=lambda _: next(answers), write=output.append, dispatch=dispatch) == 0
    assert len(attempts) == 2
    assert 'secret credential' not in '\n'.join(output)
    assert any('已中断' in line for line in output)


@pytest.mark.parametrize('args,config', [([], './pack.local.json'),
    (['menu'], './pack.local.json'), (['-c', 'custom.json', 'menu'], 'custom.json'),
    (['menu', '-c', 'custom.json'], 'custom.json')])
def test_cli_menu_entry(monkeypatch, args, config):
    from mcmodsync import op_menu
    seen = []
    monkeypatch.setattr(op_menu, 'run_menu', lambda cfg: seen.append(cfg) or 0)
    assert cli.main(args) == 0
    assert seen == [config]


def test_windows_launcher_opens_real_menu_and_exits_without_action():
    import os
    if os.name != 'nt':
        pytest.skip('Windows launcher')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['cmd', '/d', '/c', str(root / '启动OP端.bat')],
                            cwd=root.parent, input='0\n', capture_output=True,
                            encoding='utf-8', errors='replace', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'OP 管理菜单' in result.stdout
    assert '0、退出' in result.stdout
