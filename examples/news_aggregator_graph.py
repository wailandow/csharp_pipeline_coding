"""
examples/news_aggregator_graph.py

Проект "Новостной агрегатор" — ШАГ 1..4 из ТЗ, собранные ИЗ ТИПОВЫХ
УЗЛОВ. Это не production-реализация: fetch/LLM/send — заглушки,
цель примера — показать именно СБОРКУ графа поверх core/.
"""
from __future__ import annotations

import json

from langgraph.graph import END

from core import (
    NodeKind, NodeSpec, FieldSpec, NodeRegistry, build_graph, EdgeSpec,
    collector_node_factory, queue_store_node_factory, queue_preview_node_factory,
    llm_processor_node_factory, router_factory, external_action_node_factory,
)
from core.state import NewsState

DB_PATH = "news.sqlite3"
TABLE = "links_queue"


# --- проектные адаптеры: единственное, что уникально для этого проекта ---

def fetch_rss(config: dict) -> list[dict]:
    import feedparser
    entries: list[dict] = []
    for feed_url in config.get("feeds", []):
        parsed = feedparser.parse(feed_url)
        entries += [{"title": e.title, "link": e.link} for e in parsed.entries]
    return entries


def call_llm(prompt: str) -> str:
    # тут реальный вызов Anthropic API; для примера — заглушка
    return json.dumps({"news_kind": "A", "relevant": True, "summary": "..."})


def parse_llm_json(raw: str) -> dict:
    return json.loads(raw)


def send_to_telegram(state: dict) -> dict:
    # тут реальная отправка через Telegram Bot API
    item = state.get("current_item") or {}
    return {"ok": True, "sent_id": item.get("id")}


# --- регистрация типовых видов (делается один раз на весь проект) ---

registry = NodeRegistry()
registry.register_kind(NodeKind.COLLECTOR, lambda spec: collector_node_factory(
    fetch_rss, source_name="rss"))
registry.register_kind(NodeKind.QUEUE_STORE, lambda spec: queue_store_node_factory(
    DB_PATH, TABLE, operation=spec.config.get("operation", "enqueue")))
registry.register_kind(NodeKind.QUEUE_PREVIEW, lambda spec: queue_preview_node_factory(
    DB_PATH, TABLE))
registry.register_kind(NodeKind.LLM_PROCESSOR, lambda spec: llm_processor_node_factory(
    call_llm, prompt_template=spec.config["prompt_template"], output_parser=parse_llm_json))
registry.register_kind(NodeKind.EXTERNAL_ACTION, lambda spec: external_action_node_factory(
    send_to_telegram))

# --- декларация графа: ШАГ 1..4 из ТЗ ---

specs = [
    NodeSpec(node_id="collect_links", kind=NodeKind.COLLECTOR,
             description="ШАГ 1: сбор ссылок из RSS-лент"),
    NodeSpec(node_id="enqueue", kind=NodeKind.QUEUE_STORE,
             description="ШАГ 1: сохранение ссылок в очередь sqlite",
             config={"operation": "enqueue"}),
    NodeSpec(node_id="preview_queue", kind=NodeKind.QUEUE_PREVIEW,
             description="ШАГ 2: просмотр статистики очереди перед анализом"),
    NodeSpec(node_id="dequeue", kind=NodeKind.QUEUE_STORE,
             description="ШАГ 3: выборка pending-ссылок для анализа",
             config={"operation": "dequeue"}),
    NodeSpec(node_id="analyze", kind=NodeKind.LLM_PROCESSOR,
             description="ШАГ 3: анализ новости через LLM (тематика, извлечение, пересказ)",
             reads=[FieldSpec(name="current_item", type="Item")],
             writes=[FieldSpec(name="llm_result", type="dict")],
             config={"prompt_template": "Проанализируй новость: $item"}),
    # ШАГ 3.1 / 3.2: разная логика обработки — тот же вид узла (LLM_PROCESSOR),
    # отличается только prompt_template в конфигурации, а не код.
    NodeSpec(node_id="process_kind_a", kind=NodeKind.LLM_PROCESSOR,
             description="ШАГ 3.1: обработка новости вида А",
             config={"prompt_template": "Обработай новость вида А: $item"}),
    NodeSpec(node_id="process_kind_b", kind=NodeKind.LLM_PROCESSOR,
             description="ШАГ 3.2: обработка новости вида Б",
             config={"prompt_template": "Обработай новость вида Б: $item"}),
    NodeSpec(node_id="send_telegram", kind=NodeKind.EXTERNAL_ACTION,
             description="ШАГ 4: отправка выбранной новости в Telegram"),
]

route_by_kind = router_factory(
    field_path=lambda s: (s.get("llm_result") or {}).get("news_kind"),
    mapping={"A": "process_kind_a", "B": "process_kind_b"},
    default=END,
)

edges = [
    EdgeSpec(source="collect_links", target="enqueue"),
    EdgeSpec(source="enqueue", target="preview_queue"),
    EdgeSpec(source="preview_queue", target="dequeue"),
    EdgeSpec(source="dequeue", target="analyze"),
    EdgeSpec(source="analyze", condition=route_by_kind,
             condition_map={"process_kind_a": "process_kind_a",
                            "process_kind_b": "process_kind_b", END: END}),
    EdgeSpec(source="process_kind_a", target="send_telegram"),
    EdgeSpec(source="process_kind_b", target="send_telegram"),
    EdgeSpec(source="send_telegram", target=END),
]

graph = build_graph(NewsState, specs, edges, entry_point="collect_links", registry=registry)
