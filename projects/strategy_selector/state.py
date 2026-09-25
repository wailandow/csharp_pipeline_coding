"""
projects/strategy_selector/state.py

Состояние для проекта "Автоматический подбор стратегий C#".
Расширяет BaseNodeState полями, специфичными именно для этого проекта.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from core.state import BaseNodeState, Event, _append


class StrategySelectorState(BaseNodeState, total=False):
    # ШАГ 0: входной список описаний стратегий (то, что раньше было
    # `List<string> strategyDescription` в C#). Поддерживается несколько
    # элементов — граф последовательно проходит их все, одну сессию на
    # каждое описание (см. description_index/route_after_finish).
    strategy_descriptions: list[str]

    # ШАГ 0: индекс описания, обрабатываемого сейчас в strategy_descriptions
    description_index: int

    # ШАГ 0: описание, выбранное для текущего прохода
    current_strategy_description: str

    # ШАГ 0: итоговый промт для ШАГ 1 (LLM-генерация C#-класса)
    initial_prompt: str

    # ШАГ 1: отправка в LLM
    session_id: int        # strategy_sessions.id, создаётся в ШАГ 0
    attempt_id: int        # strategy_attempts.id, создаётся в ШАГ 1
    attempt_number: int    # № попытки внутри сессии (общий бюджет на компиляцию И метрики)

    # ШАГ 2/3: результат компиляции и расчёта метрик от Glasser.exe (App-in-Loop)
    compile_success: Optional[bool]
    compile_errors: Optional[str]
    metrics: Optional[Any]              # список метрик по инструментам (см. примеры JSON)
    metrics_passed: Optional[bool]
    metrics_check_result: Optional[str]  # текстовое пояснение, какой критерий не прошёл

    # ШАГ 2.1 / ШАГ 3.1.1: какой из двух ретрай-промтов собирать дальше
    retry_reason: Optional[Literal["compile", "metrics"]]

    # накопитель итогов по каждой обработанной стратегии (session_id,
    # статус, метрики) — растёт по мере прохода strategy_descriptions
    results: Annotated[list[dict[str, Any]], _append]