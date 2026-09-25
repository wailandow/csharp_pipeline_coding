"""
projects/strategy_selector/step2_compile_and_evaluate.py

ШАГ 2: вызов внешнего Roslyn-раннера (C#) для компиляции сгенерированного
.cs-файла и расчёта метрик, разбор JSON-результата, обновление попытки в БД.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Callable

from core.state import now_iso
from projects.strategy_selector import db

log = logging.getLogger("strategy_selector.step2")


def _normalize_compile_errors(raw) -> str | None:
    """
    Glasser.exe может вернуть ошибки компиляции и списком строк, и одной
    строкой, и вовсе не вернуть поле (null) — приводим к единому текстовому
    виду либо None, если ошибок нет.
    """
    if not raw:
        return None
    if isinstance(raw, list):
        return "\n".join(str(x) for x in raw) or None
    return str(raw)


def run_compiler(
    command: list[str],
    cs_file_path: str,
    result_dir: str,
    timeout_sec: int,
) -> dict:
    """
    command — шаблон команды со слотами {cs_file} и {result_json}, например:
        ["dotnet", "StrategyRunner.dll", "--file", "{cs_file}", "--output", "{result_json}"]

    Возвращает распарсенный JSON-результат раннера. Бросает исключение,
    если сам процесс не отработал (инфраструктурная ошибка) — это
    отличается от "compile_success=false" внутри JSON.
    """
    result_json_path = str(Path(result_dir) / (Path(cs_file_path).stem + ".result.json"))
    resolved = [part.format(cs_file=cs_file_path, result_json=result_json_path) for part in command]

    log.info("Запуск раннера: %s", " ".join(resolved))
    proc = subprocess.run(resolved, capture_output=True, text=True, timeout=timeout_sec, creationflags=subprocess.CREATE_NO_WINDOW)

    if proc.returncode != 0:
        log.error("Раннер завершился с кодом %s. stderr: %s", proc.returncode, proc.stderr[:2000])
        raise RuntimeError(f"раннер упал с кодом {proc.returncode}: {proc.stderr[:500]}")

    if not Path(result_json_path).exists():
        log.error("Раннер отработал (код 0), но файл результата не найден: %s", result_json_path)
        raise RuntimeError(f"файл результата не создан: {result_json_path}")

    # Glasser.exe (.NET) по умолчанию пишет JSON в UTF-8 С BOM — обычный
    # "utf-8" на этом падает с JSONDecodeError, поэтому читаем как utf-8-sig
    # (он одинаково корректно понимает файлы и с BOM, и без него)
    with open(result_json_path, encoding="utf-8-sig") as f:
        return json.load(f)


def step2_node_factory(
    command: list[str],
    result_dir: str,
    db_path: str,
    timeout_sec: int = 120,
) -> Callable[[dict], dict]:
    Path(result_dir).mkdir(parents=True, exist_ok=True)

    def node(state: dict) -> dict:
        attempt_id = state.get("attempt_id")
        code_file_path = (state.get("llm_result") or {}).get("_code_file_path")

        # attempt_id обязателен всегда — это уже инфраструктурная ошибка
        # (например, сбой БД на ШАГ 1). А вот code_file_path может законно
        # отсутствовать, если ШАГ 1 сам поймал ошибку генерации (пустой/
        # обрезанный ответ LLM и т.п.) — это НЕ повод падать здесь, компиляцию
        # просто нужно пропустить и уйти на ретрай (ветка ниже уже это делает)
        if attempt_id is None:
            raise ValueError("нет attempt_id — сначала должен отработать ШАГ 1")

        # если ШАГ 1 уже пометил ответ как обрезанный/ошибочный (в т.ч. если
        # из-за этого .cs-файл вообще не был создан) — компилировать
        # заведомо неполный/отсутствующий код бессмысленно, сразу фиксируем это в БД
        if state.get("last_error") or not code_file_path:
            reason = state.get("last_error") or "ШАГ 1 не создал .cs-файл"
            log.warning("Пропуск компиляции: ШАГ 1 завершился с ошибкой (%s)", reason)
            db.update_attempt(
                db_path, attempt_id, now_iso,
                compile_success=False,
                compile_errors=f"компиляция пропущена: {reason}",
                metrics_passed=False,
            )
            return {
                "compile_success": False,
                "compile_errors": f"компиляция пропущена: {reason}",
                "metrics": None,
                "metrics_passed": False,
                "metrics_check_result": None,
                "history": [{
                    "ts": now_iso(), "node": "step2_compile_and_evaluate",
                    "message": "компиляция пропущена из-за ошибки на ШАГ 1", "data": {},
                }],
            }

        try:
            result = run_compiler(command, code_file_path, result_dir, timeout_sec)
            compile_success = bool(result.get("compile_success"))
            compile_errors = _normalize_compile_errors(result.get("compile_errors"))
            # ШАГ 3: метрики считаются внутри Glasser.exe только если компиляция
            # прошла (ШАГ 2.2) — если compile_success=false, metrics будет пуст
            metrics = result.get("metrics") or []
            metrics_passed = bool(result.get("metrics_passed"))
            metrics_check_result = result.get("metrics_check_result")

            if compile_success:
                log.info("Компиляция успешна (ШАГ 2.2). metrics_passed=%s (ШАГ 3.1): %s",
                          metrics_passed, metrics_check_result)
            else:
                log.warning("Компиляция НЕ удалась (ШАГ 2.1): %s", compile_errors)

            error = None if compile_success else f"ошибка компиляции: {compile_errors}"
        except subprocess.TimeoutExpired:
            log.error("ШАГ 2: раннер не уложился в timeout_sec=%s", timeout_sec)
            compile_success, metrics = False, []
            compile_errors = (
                "Стратегия слишком долго выполняет расчет по тестовым данным, "
                "требуется оптимизация, чтобы ускорить ее работу"
            )
            metrics_passed, metrics_check_result = False, None
            error = f"раннер не отработал: таймаут ({timeout_sec} сек)"
        except Exception as exc:
            log.exception("ШАГ 2: инфраструктурная ошибка при вызове раннера")
            compile_success, compile_errors, metrics = False, str(exc), []
            metrics_passed, metrics_check_result = False, None
            error = f"раннер не отработал: {exc}"

        db.update_attempt(
            db_path, attempt_id, now_iso,
            compile_success=compile_success,
            compile_errors=compile_errors,
            metrics=metrics,
            metrics_passed=metrics_passed,
            metrics_check_result=metrics_check_result,
        )

        return {
            "compile_success": compile_success,
            "compile_errors": compile_errors,
            "metrics": metrics,
            "metrics_passed": metrics_passed,
            "metrics_check_result": metrics_check_result,
            "last_error": error,
            "history": [{
                "ts": now_iso(),
                "node": "step2_compile_and_evaluate",
                "message": (
                    "метрики прошли проверку" if compile_success and metrics_passed
                    else "метрики НЕ прошли проверку" if compile_success
                    else f"ошибка компиляции: {compile_errors}"
                ),
                "data": {"compile_success": compile_success, "metrics_passed": metrics_passed},
            }],
        }
    return node