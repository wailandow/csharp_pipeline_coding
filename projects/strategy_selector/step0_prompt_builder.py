"""
projects/strategy_selector/step0_prompt_builder.py

ШАГ 0: берёт первую стратегию из strategy_descriptions, создаёт сессию
в БД (strategy_sessions) и собирает первый промт из
(strategyDescription, ClassTemplate, ClassAddonTemplate).
"""
from __future__ import annotations

import logging
from typing import Callable

from core.state import now_iso
from projects.strategy_selector import db

log = logging.getLogger("strategy_selector.step0")


def build_initial_prompt(
    strategy_description: str,
    class_template: str,
    class_addon_template: str,
) -> str:
    lines = [
        "## Задача",
        "## Описание требуемой реализации",
        strategy_description,
        "",
        "## БЛОК ШАБЛОНА класса для изменения (структура фиксирована, изменяй только реализацию)",
        class_template,
        "## КОНЕЦ БЛОКА ШАБЛОНА класса для изменения ",
        "",
        "## Вспомогательные классы (НЕЛЬЗЯ МЕНЯТЬ И ВЫДУМЫВАТЬ новые поля)",
        class_addon_template,
        "",
        "## Обязательные требования:",
        "* Комментарии в коде — на русском, кратко",
        "* Стратегия должна компилироваться без дополнительных зависимостей",
        "* Заполни название стратегии StrategyTemplateDeals осмысленным названием.",
        "* Структура класса фиксирована — менять нельзя.",
        "",
    ]
    return "\n".join(lines)


def step0_node_factory(
    class_template: str,
    class_addon_template: str,
    db_path: str,
) -> Callable[[dict], dict]:
    def node(state: dict) -> dict:
        descriptions = state.get("strategy_descriptions", [])
        index = state.get("description_index", 0)
        log.info("ШАГ 0: получено %d описаний стратегий, текущий индекс=%d", len(descriptions), index)
        if not descriptions:
            log.error("strategy_descriptions пуст — нечего обрабатывать")
            raise ValueError("strategy_descriptions пуст — нечего обрабатывать на ШАГ 0")
        if index >= len(descriptions):
            log.error("description_index=%d вне диапазона (всего описаний: %d)", index, len(descriptions))
            raise ValueError("description_index вне диапазона strategy_descriptions")

        current_description = descriptions[index]
        log.debug("Выбрано описание #%d (первые 80 симв.): %s...", index, current_description[:80])

        session_id = db.create_session(db_path, current_description, now_iso)
        log.info("Создана сессия strategy_sessions.id=%s (описание %d/%d)",
                 session_id, index + 1, len(descriptions))

        prompt = build_initial_prompt(
            strategy_description=current_description,
            class_template=class_template,
            class_addon_template=class_addon_template,
        )
        log.debug("Промт собран, длина=%d символов", len(prompt))

        return {
            "description_index": index,
            "current_strategy_description": current_description,
            "initial_prompt": prompt,
            "session_id": session_id,
            # сброс полей предыдущего описания (если это не первый проход
            # по списку) — иначе на роутинг могли бы повлиять данные
            # прошлой сессии, ещё не перезаписанные ШАГ 1/2 нового прохода
            "attempt_id": None,
            "attempt_number": 0,
            "compile_success": None,
            "compile_errors": None,
            "metrics": None,
            "metrics_passed": None,
            "metrics_check_result": None,
            "retry_reason": None,
            "llm_result": None,
            "last_error": None,
            "history": [{
                "ts": now_iso(),
                "node": "step0_prompt_builder",
                "message": f"промт для ШАГ 1 собран, session_id={session_id} ({index + 1}/{len(descriptions)})",
                "data": {"strategy_len": len(current_description)},
            }],
        }
    return node