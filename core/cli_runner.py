"""
core/cli_runner.py

Вызов моделей через установленные CLI (claude code, codex) как источник
текстового ответа. Только генерация текста: CLI не получает права
править файлы и выполнять команды, флаги отключения защиты не используются.

Промпт передаётся через stdin (нет проблем с кавычками и длиной).
Запуск: shell=False, stdout/stderr перехватываются, CREATE_NO_WINDOW,
по таймауту убивается всё дерево процессов.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any

log = logging.getLogger("cli_runner")

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
DEFAULT_TIMEOUT = 600

_EXE_NAMES = {"claude_cli": "claude", "codex_cli": "codex"}


class CLIError(RuntimeError):
    pass


def find_executable(provider: str, override: str | None = None) -> str:
    """Ищет исполняемый файл через shutil.which (учитывает PATHEXT, в т.ч. .cmd)."""
    name = override or _EXE_NAMES[provider]
    path = shutil.which(name)
    if not path:
        raise CLIError(f"исполняемый файл '{name}' не найден в PATH")
    return path


def _wrap_cmd(argv: list[str]) -> list[str]:
    """.cmd/.bat нельзя запустить напрямую без shell — идём через cmd.exe /c."""
    if sys.platform == "win32" and argv[0].lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", *argv]
    return argv


def _kill_tree(proc: subprocess.Popen) -> None:
    """Завершает процесс и всех его потомков."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, timeout=30,
            )
        else:
            proc.kill()
    except Exception as exc:  # noqa: BLE001
        log.warning("не удалось убить дерево процессов: %s", exc)
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _run(argv: list[str], prompt: str, timeout: float) -> tuple[int, str, str]:
    proc = subprocess.Popen(
        _wrap_cmd(argv),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        shell=False, creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        out, err = proc.communicate(prompt.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        raise CLIError(f"таймаут {timeout}с: {os.path.basename(argv[0])}")
    except BaseException:
        _kill_tree(proc)
        raise
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _tail(text: str, n: int = 500) -> str:
    return text.strip()[-n:]


def _claude(ep: dict[str, Any], prompt: str, timeout: float) -> tuple[str, str]:
    argv = [find_executable("claude_cli", ep.get("executable")),
            "-p", "--output-format", "json", "--model", ep["model"],
            "--tools", ""]          # без инструментов: только текст
    code, out, err = _run(argv, prompt, timeout)
    if code != 0:
        raise CLIError(f"claude завершился с кодом {code}: {_tail(err or out)}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise CLIError(f"claude вернул не-JSON: {exc}: {_tail(out)}")
    if data.get("is_error"):
        raise CLIError(f"claude вернул ошибку: {_tail(str(data.get('result')))}")
    text = data.get("result")
    if not isinstance(text, str) or not text.strip():
        raise CLIError("claude вернул пустой ответ")
    return text, ep["model"]


def _codex(ep: dict[str, Any], prompt: str, timeout: float) -> tuple[str, str]:
    fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="codex_out_")
    os.close(fd)
    try:
        argv = [find_executable("codex_cli", ep.get("executable")),
                "exec", "--skip-git-repo-check", "--ephemeral",
                "--sandbox", "read-only",     # только чтение: правки и команды запрещены
                "-o", tmp, "--model", ep["model"], "-"]   # "-" — промпт из stdin
        code, out, err = _run(argv, prompt, timeout)
        if code != 0:
            raise CLIError(f"codex завершился с кодом {code}: {_tail(err or out)}")
        with open(tmp, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        if not text.strip():
            raise CLIError("codex вернул пустой ответ")
        return text, ep["model"]
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def run_cli(provider: str, ep: dict[str, Any], prompt: str) -> tuple[str, str]:
    """Возвращает (text, model). Бросает CLIError при любой неудаче."""
    timeout = float(ep.get("timeout", DEFAULT_TIMEOUT))
    if provider == "claude_cli":
        return _claude(ep, prompt, timeout)
    if provider == "codex_cli":
        return _codex(ep, prompt, timeout)
    raise CLIError(f"неизвестный CLI-провайдер: {provider}")
