"""Тесты CLI-провайдеров и каскада fallback. Реальные CLI и сеть не вызываются."""
import json
import os
import subprocess
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import cli_runner
from core.cli_runner import CLIError
from core.llm_client import LLMClient, has_usable_endpoint, messages_to_prompt

MSGS = [{"role": "user", "content": "привет \"кавычки\""}]


class FakeProc:
    def __init__(self, out=b"", err=b"", code=0, timeout=False):
        self.pid, self.returncode = 4242, code
        self._out, self._err, self._timeout = out, err, timeout
        self.stdin_data = None
        self.calls = 0

    def communicate(self, data=None, timeout=None):
        self.calls += 1
        if self._timeout and self.calls == 1:
            raise subprocess.TimeoutExpired("cmd", timeout)
        self.stdin_data = data
        return self._out, self._err

    def kill(self):
        pass


@pytest.fixture(autouse=True)
def fake_which(monkeypatch):
    monkeypatch.setattr(cli_runner.shutil, "which", lambda n: f"C:\\bin\\{n}.exe")


def test_claude_success_prompt_via_stdin_and_safe_flags():
    proc = FakeProc(out=json.dumps({"result": "ответ", "is_error": False}).encode())
    with mock.patch.object(cli_runner.subprocess, "Popen", return_value=proc) as popen:
        text, model = cli_runner.run_cli("claude_cli", {"model": "sonnet"}, "промпт")
    argv = popen.call_args.args[0]
    kw = popen.call_args.kwargs
    assert (text, model) == ("ответ", "sonnet")
    assert argv[1:6] == ["-p", "--output-format", "json", "--model", "sonnet"]
    assert "--dangerously-skip-permissions" not in argv
    assert proc.stdin_data == "промпт".encode("utf-8")
    assert kw["shell"] is False
    assert kw["stdin"] == subprocess.PIPE
    if sys.platform == "win32":
        assert kw["creationflags"] & cli_runner.CREATE_NO_WINDOW


def test_claude_is_error_and_nonzero_and_bad_json():
    for proc in (
        FakeProc(out=json.dumps({"result": "boom", "is_error": True}).encode()),
        FakeProc(err=b"fail", code=1),
        FakeProc(out=b"not json"),
    ):
        with mock.patch.object(cli_runner.subprocess, "Popen", return_value=proc):
            with pytest.raises(CLIError):
                cli_runner.run_cli("claude_cli", {"model": "m"}, "p")


def test_codex_reads_output_file_and_cleans_up():
    created = {}

    def fake_popen(argv, **kw):
        path = argv[argv.index("-o") + 1]
        created["path"] = path
        with open(path, "w", encoding="utf-8") as f:
            f.write("codex-ответ")
        created["argv"] = argv
        return FakeProc()

    with mock.patch.object(cli_runner.subprocess, "Popen", side_effect=fake_popen):
        text, model = cli_runner.run_cli("codex_cli", {"model": "gpt-x"}, "p")
    argv = created["argv"]
    assert text == "codex-ответ" and model == "gpt-x"
    assert argv[1:4] == ["exec", "--skip-git-repo-check", "--ephemeral"]
    assert "read-only" in argv and "--model" in argv
    assert not os.path.exists(created["path"])


def test_codex_empty_output_is_error():
    with mock.patch.object(cli_runner.subprocess, "Popen", return_value=FakeProc()):
        with pytest.raises(CLIError):
            cli_runner.run_cli("codex_cli", {"model": "m"}, "p")


def test_timeout_kills_process_tree():
    proc = FakeProc(timeout=True)
    with mock.patch.object(cli_runner.subprocess, "Popen", return_value=proc), \
         mock.patch.object(cli_runner, "_kill_tree") as kill:
        with pytest.raises(CLIError, match="таймаут"):
            cli_runner.run_cli("claude_cli", {"model": "m", "timeout": 1}, "p")
    kill.assert_called_once_with(proc)


def test_kill_tree_uses_taskkill_on_windows(monkeypatch):
    monkeypatch.setattr(cli_runner.sys, "platform", "win32")
    with mock.patch.object(cli_runner.subprocess, "run") as run:
        cli_runner._kill_tree(FakeProc())
    assert run.call_args.args[0][:2] == ["taskkill", "/PID"]
    assert "/T" in run.call_args.args[0]


def test_missing_executable(monkeypatch):
    monkeypatch.setattr(cli_runner.shutil, "which", lambda n: None)
    with pytest.raises(CLIError, match="не найден"):
        cli_runner.run_cli("claude_cli", {"model": "m"}, "p")


def test_cmd_wrapper_on_windows(monkeypatch):
    monkeypatch.setattr(cli_runner.sys, "platform", "win32")
    argv = cli_runner._wrap_cmd(["C:\\npm\\codex.cmd", "exec"])
    assert argv[1] == "/c" and argv[2].endswith("codex.cmd")
    assert cli_runner._wrap_cmd(["C:\\bin\\claude.exe", "-p"]) == ["C:\\bin\\claude.exe", "-p"]


# ---------- каскад LLMClient ----------

API = {"label": "primary", "base_url": "http://x", "model": "m1", "api_key": "k"}
API2 = {"label": "fallback", "base_url": "http://y", "model": "m2", "api_key": "k2"}
CLI = {"label": "fallback2", "provider": "claude_cli", "model": "sonnet"}


def test_cascade_reaches_fallback2_cli():
    client = LLMClient([API, API2, CLI])
    with mock.patch.object(LLMClient, "_call", side_effect=RuntimeError("down")) as call, \
         mock.patch("core.llm_client.run_cli", return_value=("cli-text", "sonnet")) as cli:
        assert client.complete(MSGS) == ("cli-text", "sonnet", "stop")
    assert call.call_count == 2 and cli.call_count == 1


def test_without_fallback2_behaviour_unchanged():
    client = LLMClient([API, API2])
    with mock.patch.object(LLMClient, "_call", side_effect=RuntimeError("down")):
        with pytest.raises(RuntimeError, match="Все LLM-эндпойнты недоступны"):
            client.complete(MSGS)


def test_api_without_key_skipped_cli_without_key_used():
    client = LLMClient([{**API, "api_key": ""}, CLI])
    with mock.patch.object(LLMClient, "_call") as call, \
         mock.patch("core.llm_client.run_cli", return_value=("t", "m")):
        client.complete(MSGS)
    call.assert_not_called()


def test_cli_as_primary_and_error_falls_through():
    client = LLMClient([CLI, API])
    with mock.patch("core.llm_client.run_cli", side_effect=CLIError("x")), \
         mock.patch.object(LLMClient, "_call", return_value=("api", "m1", "stop")):
        assert client.complete(MSGS)[0] == "api"


def test_unknown_provider_is_skipped_as_failure():
    client = LLMClient([{"provider": "zzz", "model": "m"}, API])
    with mock.patch.object(LLMClient, "_call", return_value=("api", "m1", "stop")):
        assert client.complete(MSGS)[0] == "api"


def test_helpers():
    assert has_usable_endpoint([CLI]) and has_usable_endpoint([API])
    assert not has_usable_endpoint([{**API, "api_key": ""}]) and not has_usable_endpoint(None)
    assert messages_to_prompt(MSGS) == MSGS[0]["content"]
    both = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    assert messages_to_prompt(both) == "[system]\nS\n\n[user]\nU"
