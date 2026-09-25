"""
projects/strategy_selector/step3b_metrics_retry_prompt.py

ШАГ 3.1.1: компиляция прошла, но метрики НЕ прошли критерии проверки
(metrics_passed=false). Если общий лимит попыток (max_attempts, по
умолчанию 7 — см. config.yaml) не исчерпан, собираем новый промт: он
содержит реализованный класс, посчитанные метрики по инструментам и текст
причины провала (metrics_check_result), и просит доработать стратегию,
чтобы улучшить метрики (а не "почини компиляцию", как в step3_retry_prompt).

Отправка этого промта в LLM идёт по той же схеме, что и ШАГ 2 (через
step1_llm_generate -> step2_compile_and_evaluate), но с возможностью
использовать отдельные модели primary/fallback — см.
config.yaml:llm.strategy_metrics_retry и step1_node_factory(metrics_retry_llm_client=...).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from core.state import now_iso

log = logging.getLogger("strategy_selector.step3b_metrics_retry")


def _format_metrics(metrics: Any) -> str:
    if not metrics:
        return "(метрики отсутствуют)"
    try:
        return json.dumps(metrics, ensure_ascii=False, indent=2)
    except TypeError:
        return str(metrics)


def build_metrics_retry_prompt(
    strategy_description: str,
    class_template: str,
    class_addon_template: str,
    previous_code: str,
    metrics: Any,
    metrics_check_result: str,
    attempt_number: int,
) -> str:
    lines = [
        "## Задача",
        "## Описание требуемой реализации",
        strategy_description,
        "",
        f"## Предыдущая попытка (№{attempt_number}) скомпилировалась, "
        "но НЕ прошла проверку метрик качества стратегии",
        "### Код предыдущей попытки:",
        "```csharp",
        previous_code,
        "```",
        "### Метрики расчёта предыдущей попытки (по инструментам):",
        "```json",
        _format_metrics(metrics),
        "```",
        "### Почему проверка метрик не пройдена:",
        metrics_check_result or "(текст причины не передан раннером)",
        "",
        "## Инструкция",
        "Код компилируется корректно, менять это не нужно. Доработай ЛОГИКУ "
        "стратегии так, чтобы улучшить метрики и устранить причины, "
        "перечисленные выше (например: изменить условия входа/выхода, "
        "фильтры сигналов, риск-менеджмент). Сохрани структуру класса.",
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
        "* Верни ПОЛНЫЙ обновлённый код класса, а не только изменённые фрагменты",
        "* Структура класса фиксирована — менять нельзя.",
        "",
    ]
    return "\n".join(lines)


def step3b_metrics_retry_node_factory(
    class_template: str,
    class_addon_template: str,
) -> Callable[[dict], dict]:
    def node(state: dict) -> dict:
        description = state.get("current_strategy_description", "")
        previous_code = (state.get("llm_result") or {}).get("cs_class_source", "")
        metrics = state.get("metrics")
        metrics_check_result = state.get("metrics_check_result") or ""
        attempt_number = state.get("attempt_number", 0)

        log.info("Готовим доработку по метрикам после попытки №%s: %s",
                  attempt_number, metrics_check_result)

        prompt = build_metrics_retry_prompt(
            strategy_description=description,
            class_template=class_template,
            class_addon_template=class_addon_template,
            previous_code=previous_code,
            metrics=metrics,
            metrics_check_result=metrics_check_result,
            attempt_number=attempt_number,
        )

        return {
            "initial_prompt": prompt,
            "retry_reason": "metrics",
            # относилось к предыдущей попытке — обнуляем перед новым кругом
            "last_error": None,
            "history": [{
                "ts": now_iso(),
                "node": "step3b_metrics_retry_prompt",
                "message": f"собран промт для доработки метрик после №{attempt_number}",
                "data": {"metrics_check_result": metrics_check_result},
            }],
        }
    return node
