"""
core/node_kinds.py

Каталог типовых узлов ("node kinds"). Для каждого вида — factory-функция,
которая по конфигурации проекта строит конкретный узел: обычную python
функцию `def node(state: dict) -> dict`, совместимую с LangGraph
(нода принимает state и возвращает частичное обновление).

Ключевая идея: одинаковые виды узлов в разных проектах используют одну
и ту же factory — меняется только конфигурация (SQL-таблица, промпт,
клиент внешнего API), а не логика самого узла.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from string import Template
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from .state import now_iso


class NodeKind(str, Enum):
    COLLECTOR = "collector"
    QUEUE_STORE = "queue_store"
    QUEUE_PREVIEW = "queue_preview"
    LLM_PROCESSOR = "llm_processor"
    ROUTER = "router"
    EXTERNAL_ACTION = "external_action"
    HUMAN_APPROVAL = "human_approval"
    RETRY_CONTROLLER = "retry_controller"
    RATE_LIMITER = "rate_limiter"
    CUSTOM = "custom"


class FieldSpec(BaseModel):
    name: str
    type: str            # "str" | "int" | "float" | "bool" | "dict" | "list" ...
    description: str = ""


class NodeSpec(BaseModel):
    """
    Декларативное описание узла — универсальный язык, на котором можно
    либо (а) собрать типовой узел из каталога ниже, либо (б) попросить
    LLM сгенерировать код нового вида узла (см. llm_node_factory.py).
    """
    node_id: str
    kind: NodeKind
    description: str
    reads: list[FieldSpec] = Field(default_factory=list)
    writes: list[FieldSpec] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 1. Collector — тянет сырые кандидаты из внешнего источника
# ---------------------------------------------------------------------------

def collector_node_factory(
    fetch_fn: Callable[[dict], list[dict]],
    source_name: str,
) -> Callable[[dict], dict]:
    """
    fetch_fn(config) -> список сырых записей (dict).
    Пример для новостей: обход RSS через feedparser.
    Пример для продаж: выгрузка карточек компаний из 2GIS/Telegram.
    """
    def node(state: dict) -> dict:
        raw_items = fetch_fn(state.get("collector_config", {}))
        items = [
            {
                "id": f"{source_name}:{i}",
                "source": source_name,
                "payload": raw,
                "status": "pending",
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
            for i, raw in enumerate(raw_items)
        ]
        return {
            "items_batch": items,
            "history": [{"ts": now_iso(), "node": f"collector:{source_name}",
                         "message": f"собрано {len(items)} записей", "data": {}}],
        }
    return node


# ---------------------------------------------------------------------------
# 2. QueueStore — персистентная очередь на sqlite (enqueue / dequeue)
# ---------------------------------------------------------------------------

def _ensure_table(conn: sqlite3.Connection, table: str) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id TEXT PRIMARY KEY,
            source TEXT,
            payload TEXT,
            status TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()


def queue_store_node_factory(
    db_path: str,
    table: str,
    operation: str = "enqueue",
) -> Callable[[dict], dict]:
    """
    operation="enqueue" — кладёт items_batch в sqlite (upsert).
    operation="dequeue" — достаёт batch pending-записей.

    operation задаётся один раз при СБОРКЕ узла (через NodeSpec.config),
    а не читается из состояния во время выполнения: enqueue-узел и
    dequeue-узел — это два РАЗНЫХ узла в графе (см. examples/), которые
    используют одну и ту же фабрику с разной конфигурацией.

    Один и тот же узел используется как очередь ссылок (новости),
    очередь контактов (продажи) и лог попыток (стратегии) — меняется
    только db_path/table/operation в конфигурации, не код.
    """
    def node(state: dict) -> dict:
        conn = sqlite3.connect(db_path)
        _ensure_table(conn, table)

        if operation == "enqueue":
            for item in state.get("items_batch", []):
                conn.execute(
                    f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?)",
                    (item["id"], item["source"], json.dumps(item["payload"]),
                     item["status"], item["created_at"], item["updated_at"]),
                )
            conn.commit()
            conn.close()
            return {"history": [{"ts": now_iso(), "node": "queue_store",
                                  "message": "enqueue выполнен", "data": {}}]}

        limit = state.get("queue_limit", 20)
        rows = conn.execute(
            f"SELECT id, source, payload, status, created_at, updated_at "
            f"FROM {table} WHERE status = 'pending' LIMIT ?", (limit,),
        ).fetchall()
        conn.close()
        items = [
            {"id": r[0], "source": r[1], "payload": json.loads(r[2]),
             "status": r[3], "created_at": r[4], "updated_at": r[5]}
            for r in rows
        ]
        return {
            "items_batch": items,
            "history": [{"ts": now_iso(), "node": "queue_store",
                         "message": f"dequeue: {len(items)} записей", "data": {}}],
        }
    return node


# ---------------------------------------------------------------------------
# 3. QueuePreview — сводная статистика по очереди (read-only checkpoint)
# ---------------------------------------------------------------------------

def queue_preview_node_factory(db_path: str, table: str) -> Callable[[dict], dict]:
    def node(state: dict) -> dict:
        conn = sqlite3.connect(db_path)
        _ensure_table(conn, table)
        rows = conn.execute(
            f"SELECT status, COUNT(*) FROM {table} GROUP BY status"
        ).fetchall()
        conn.close()
        summary = {status: count for status, count in rows}
        return {
            "action_result": {"queue_summary": summary},
            "history": [{"ts": now_iso(), "node": "queue_preview",
                         "message": f"статистика очереди: {summary}", "data": summary}],
        }
    return node


# ---------------------------------------------------------------------------
# 4. LLMProcessor — вызов LLM по шаблону + разбор структурированного ответа
# ---------------------------------------------------------------------------

def llm_processor_node_factory(
    llm_call_fn: Callable[[str], str],
    prompt_template: str,
    output_parser: Callable[[str], dict],
) -> Callable[[dict], dict]:
    """
    llm_call_fn(prompt) -> сырой текст ответа модели (обёртка над Anthropic API).
    prompt_template — шаблон с плейсхолдерами $item / $state (string.Template,
                    НЕ str.format!). Это важно: сгенерированный код (C#, JSON)
                    почти всегда содержит фигурные скобки, и .format() падал
                    бы на них с KeyError — $-синтаксис Template с ними не
                    конфликтует.
    output_parser — превращает текст ответа в dict (обычно json.loads +
                    pydantic-валидация под конкретную задачу).

    Один и тот же узел используется для классификации/пересказа новости,
    генерации C#-класса стратегии и формулировки предложения клиенту.
    Разница — только в prompt_template и output_parser.
    """
    def node(state: dict) -> dict:
        item = state.get("current_item") or {}
        prompt = Template(prompt_template).safe_substitute(
            item=json.dumps(item, ensure_ascii=False),
            state=json.dumps({k: v for k, v in state.items() if k != "history"},
                              ensure_ascii=False, default=str),
        )
        raw = llm_call_fn(prompt)
        try:
            parsed = output_parser(raw)
            error = None
        except Exception as exc:  # ошибка парсинга уходит в last_error для retry-петли
            parsed, error = {}, str(exc)
        return {
            "llm_result": parsed,
            "last_error": error,
            "history": [{"ts": now_iso(), "node": "llm_processor",
                         "message": "LLM вызван", "data": {"error": error}}],
        }
    return node


# ---------------------------------------------------------------------------
# 5. Router — условная маршрутизация (используется в add_conditional_edges)
# ---------------------------------------------------------------------------

def router_factory(
    field_path: Callable[[dict], Any],
    mapping: dict[Any, str],
    default: str,
) -> Callable[[dict], str]:
    """
    field_path(state) -> значение; mapping -> имя следующего узла.
    Примеры: news_kind A/Б, compile_ok True/False, metrics_ok True/False.
    """
    def route(state: dict) -> str:
        value = field_path(state)
        return mapping.get(value, default)
    return route


# ---------------------------------------------------------------------------
# 6. ExternalAction — эффектный шаг во внешнюю систему
# ---------------------------------------------------------------------------

def external_action_node_factory(action_fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
    """
    action_fn(state) -> {"ok": bool, ...}.
    Примеры action_fn: отправка в Telegram, запись .cs файла + компиляция,
    запуск расчёта стратегии.
    """
    def node(state: dict) -> dict:
        try:
            result = action_fn(state)
            status = "success" if result.get("ok") else "error"
        except Exception as exc:
            result, status = {"error": str(exc)}, "error"
        return {
            "action_status": status,
            "action_result": result,
            "history": [{"ts": now_iso(), "node": "external_action",
                         "message": status, "data": result}],
        }
    return node


# ---------------------------------------------------------------------------
# 7. HumanApproval — согласование с человеком (напр. через Telegram)
# ---------------------------------------------------------------------------

def human_approval_node_factory(
    request_fn: Callable[[dict], None],
    poll_fn: Callable[[dict], Optional[str]],
) -> Callable[[dict], dict]:
    """
    request_fn(state) — отправляет человеку карточку на согласование.
    poll_fn(state) -> "approved" | "rejected" | "edited" | None (ждём ответа).

    В боевом графе такой узел обычно реализуется через LangGraph interrupt()
    и checkpointer, а не polling — но контракт (что узел читает/пишет)
    от этого не меняется.
    """
    def node(state: dict) -> dict:
        if state.get("human_decision") is None:
            request_fn(state)
        decision = poll_fn(state)
        return {
            "human_decision": decision,
            "history": [{"ts": now_iso(), "node": "human_approval",
                         "message": f"решение: {decision}", "data": {}}],
        }
    return node


# ---------------------------------------------------------------------------
# 8. RetryLoopController — счётчик попыток + решение "повторить / выйти"
# ---------------------------------------------------------------------------

def retry_controller_node_factory(max_attempts: int) -> Callable[[dict], dict]:
    def node(state: dict) -> dict:
        attempts = state.get("attempt_count", 0) + 1
        return {
            "attempt_count": attempts,
            "max_attempts": max_attempts,
            "history": [{"ts": now_iso(), "node": "retry_controller",
                         "message": f"попытка {attempts}/{max_attempts}", "data": {}}],
        }
    return node


def retry_router_factory(
    ok_check: Callable[[dict], bool],
    retry_target: str,
    ok_target: str,
    giveup_target: str,
) -> Callable[[dict], str]:
    """
    Условие для add_conditional_edges сразу после retry_controller-узла.

    ok_check — callable(state) -> bool, а не имя плоского поля: результат
    предыдущего ExternalActionNode лежит в state["action_result"] (см.
    external_action_node_factory), а не разливается по верхнему уровню
    состояния, поэтому проверка "успеха" почти всегда читает вложенный
    путь, например: lambda s: (s.get("action_result") or {}).get("metrics_ok").
    Тот же паттерн, что и field_path в router_factory.
    """
    def route(state: dict) -> str:
        if ok_check(state):
            return ok_target
        if state.get("attempt_count", 0) < state.get("max_attempts", 1):
            return retry_target
        return giveup_target
    return route


# ---------------------------------------------------------------------------
# 9. RateLimiter — anti-spam проверка перед ExternalAction
# ---------------------------------------------------------------------------

def rate_limiter_router_factory(
    min_interval_sec: int,
    ts_field: str = "last_sent_at",
) -> Callable[[dict], str]:
    """Возвращает имя следующего узла: "send" либо "wait"."""
    def route(state: dict) -> str:
        last = state.get(ts_field)
        if not last:
            return "send"
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        return "send" if elapsed >= min_interval_sec else "wait"
    return route
