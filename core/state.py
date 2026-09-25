"""
core/state.py

Универсальная схема состояния (State) для всех pipeline на LangGraph.

Идея: все три проекта расширяют BaseNodeState своими полями, но общие
поля (очередь, статус, попытки, история, human-in-the-loop) остаются
одинаковыми. Именно это позволяет типовым узлам (см. node_kinds.py)
работать одинаково в любом проекте — они читают/пишут поля
BaseNodeState, не зная ничего о конкретном проекте.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Optional, TypedDict


class ItemStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    ERROR = "error"
    SENT = "sent"
    REJECTED = "rejected"


class Item(TypedDict, total=False):
    """Единица работы, с которой имеет дело pipeline: ссылка на новость,
    сгенерированный класс стратегии, контакт клиента и т.п."""
    id: str
    source: str                 # rss / 2gis / telegram / template ...
    payload: dict[str, Any]     # сырые данные конкретного проекта
    status: str
    created_at: str
    updated_at: str


class Event(TypedDict):
    """Запись в истории выполнения графа — для логирования и отладки."""
    ts: str
    node: str
    message: str
    data: dict[str, Any]


def _append(existing: list, new: list) -> list:
    """Reducer для Annotated-полей: history не перезаписывается, а растёт."""
    return (existing or []) + (new or [])


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BaseNodeState(TypedDict, total=False):
    """
    Общий "контракт" состояния, который понимают ВСЕ типовые узлы.
    Проектные состояния наследуют его и добавляют свои поля поверх.
    """
    # текущий обрабатываемый элемент / пакет элементов из очереди
    current_item: Optional[Item]
    items_batch: list[Item]

    # лимит выборки при dequeue (сама операция задаётся конфигурацией узла,
    # см. core/node_kinds.py:queue_store_node_factory)
    queue_limit: int

    # управление повторами (используется RetryLoopController)
    attempt_count: int
    max_attempts: int
    last_error: Optional[str]

    # результат последнего LLM-вызова (используется LLMProcessorNode / Router)
    llm_result: Optional[dict[str, Any]]

    # результат последнего внешнего действия (используется ExternalActionNode)
    action_status: Optional[Literal["success", "error"]]
    action_result: Optional[dict[str, Any]]

    # human-in-the-loop
    pending_approval: Optional[dict[str, Any]]
    human_decision: Optional[Literal["approved", "rejected", "edited"]]

    # anti-spam / rate limiting
    last_sent_at: Optional[str]

    # журнал событий (append-only, см. reducer _append)
    history: Annotated[list[Event], _append]


# ---------------------------------------------------------------------------
# Проектные расширения — единственное, что уникально для каждого проекта.
# Логика узлов при этом не меняется, меняются только эти дополнительные поля.
# ---------------------------------------------------------------------------

class NewsState(BaseNodeState, total=False):
    news_kind: Optional[Literal["A", "B"]]


# В StrategyState/SalesState нет своих доп. полей: специфика проекта
# (сгенерированный C#-класс, метрики, текст предложения) естественно
# лежит внутри generic-полей llm_result / action_result — их не нужно
# дублировать наверх. Добавляйте top-level поле только когда его должны
# читать несколько разных узлов напрямую (пример — action_status в
# BaseNodeState, который читает Router сразу после ExternalActionNode).
class StrategyState(BaseNodeState, total=False):
    pass


class SalesState(BaseNodeState, total=False):
    entrepreneur_profile: dict[str, Any]
