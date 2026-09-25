"""
projects/strategy_selector/step4_finish.py

Маршрутизация после компиляции (успех / ретрай / сдаёмся) + узел,
закрывающий сессию в БД финальным статусом.
"""
from __future__ import annotations

import logging
from typing import Callable

from core.state import now_iso
from projects.strategy_selector import db

log = logging.getLogger("strategy_selector.step_finish")


def route_after_compile(max_attempts: int) -> Callable[[dict], str]:
    """
    Маршрутизация сразу после ШАГ 2/3 (компиляция + расчёт метрик внутри
    Glasser.exe):

    "success"       — ШАГ 3.1.2: скомпилировалось и метрики прошли порог
    "give_up"       — лимит попыток (max_attempts) исчерпан
    "retry_compile" — ШАГ 2.1: ошибка компиляции, есть ещё попытки
    "retry_metrics" — ШАГ 3.1.1: скомпилировалось, но метрики не прошли,
                       есть ещё попытки
    """
    def condition(state: dict) -> str:
        if state.get("compile_success") and state.get("metrics_passed"):
            return "success"

        attempt_number = state.get("attempt_number", 0)
        if attempt_number >= max_attempts:
            log.warning("Лимит попыток (%s) исчерпан для session_id=%s",
                        max_attempts, state.get("session_id"))
            return "give_up"

        if not state.get("compile_success"):
            return "retry_compile"
        return "retry_metrics"
    return condition


def route_after_finish(state: dict) -> str:
    """
    ДОП.: strategy_descriptions поддерживает несколько элементов — после
    закрытия сессии для текущего описания (finish_node уже увеличил
    description_index) решаем, есть ли ещё описания в очереди.
    """
    descriptions = state.get("strategy_descriptions", [])
    next_index = state.get("description_index", 0)
    if next_index < len(descriptions):
        return "next_item"
    return "done"


def finish_node_factory(db_path: str) -> Callable[[dict], dict]:
    def node(state: dict) -> dict:
        session_id = state.get("session_id")
        success = bool(state.get("compile_success") and state.get("metrics_passed"))
        status = "success" if success else "failed_max_attempts"
        model = (state.get("llm_result") or {}).get("_model_used")
        attempt_id = state.get("attempt_id")

        db.finish_session(db_path, session_id, status, model, now_iso)

        result_ids: list[int] = []
        if success:
            # ШАГ 3.1.2: маркируем стратегию успешной и запоминаем метрики
            metrics = state.get("metrics")
            result_ids = db.save_successful_metrics(db_path, session_id, attempt_id, metrics, now_iso)
            log.info("Сессия session_id=%s: метрики зафиксированы в strategy_results (%d строк)",
                      session_id, len(result_ids))

        log.info("Сессия session_id=%s завершена со статусом '%s' (попыток: %s)",
                  session_id, status, state.get("attempt_number"))

        index = state.get("description_index", 0)
        descriptions = state.get("strategy_descriptions", [])

        return {
            # переходим к следующему описанию в списке (или выходим, если
            # это было последнее) — см. route_after_finish
            "description_index": index + 1,
            "results": [{
                "session_id": session_id,
                "description_index": index,
                "status": status,
                "attempts": state.get("attempt_number"),
                "model": model,
                "metrics_passed": bool(state.get("metrics_passed")),
                "metrics": state.get("metrics"),
                "strategy_results_ids": result_ids,
            }],
            "history": [{
                "ts": now_iso(),
                "node": "step_finish",
                "message": f"сессия завершена: {status} ({index + 1}/{len(descriptions)})",
                "data": {"status": status, "attempts": state.get("attempt_number")},
            }],
        }
    return node