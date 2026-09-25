"""
examples/sales_assistant_graph.py

Проект "Помощник по продажам" — ШАГ 1..5 из ТЗ. Показывает HUMAN_APPROVAL
и RATE_LIMITER — виды узлов, не встречавшиеся в первых двух примерах,
но подчиняющиеся тому же контракту NodeSpec/NodeRegistry.
"""
from __future__ import annotations

import json

from langgraph.graph import END

from core import (
    NodeKind, NodeSpec, NodeRegistry, build_graph, EdgeSpec,
    collector_node_factory, llm_processor_node_factory,
    human_approval_node_factory, external_action_node_factory,
    rate_limiter_router_factory,
)
from core.state import SalesState

MIN_INTERVAL_SEC = 3600  # anti-spam: не чаще раза в час на одного адресата


# --- проектные адаптеры ---

def fetch_contacts(config: dict) -> list[dict]:
    # ШАГ 2: 2GIS / Telegram / сайты с адресами компаний — заглушка
    return [{"company": "ООО Ромашка", "phone": "+7..."}]


def call_llm(prompt: str) -> str:
    return json.dumps({"proposal_text": "Здравствуйте! Предлагаем..."})


def parse_proposal(raw: str) -> dict:
    return json.loads(raw)


def ask_entrepreneur(state: dict) -> None:
    pass  # ШАГ 4: отправка карточки предложения предпринимателю в Telegram


def poll_entrepreneur_decision(state: dict) -> str | None:
    return "approved"  # в реальности — чтение ответа из Telegram


def send_proposal(state: dict) -> dict:
    # ШАГ 5: первичная рассылка с follow-up — заглушка
    return {"ok": True}


# --- регистрация типовых видов ---

registry = NodeRegistry()
registry.register_kind(NodeKind.COLLECTOR, lambda spec: collector_node_factory(
    fetch_contacts, source_name="contacts"))
registry.register_kind(NodeKind.LLM_PROCESSOR, lambda spec: llm_processor_node_factory(
    call_llm, prompt_template=spec.config["prompt_template"], output_parser=parse_proposal))
registry.register_kind(NodeKind.HUMAN_APPROVAL, lambda spec: human_approval_node_factory(
    ask_entrepreneur, poll_entrepreneur_decision))
registry.register_kind(NodeKind.EXTERNAL_ACTION, lambda spec: external_action_node_factory(
    send_proposal))

# --- декларация графа ---

specs = [
    NodeSpec(node_id="collect_contacts", kind=NodeKind.COLLECTOR,
             description="ШАГ 2: мониторинг источников контактов для холодных продаж"),
    NodeSpec(node_id="draft_proposal", kind=NodeKind.LLM_PROCESSOR,
             description="ШАГ 3: формулировка индивидуального предложения",
             config={"prompt_template": "Составь предложение для $item"}),
    NodeSpec(node_id="approve", kind=NodeKind.HUMAN_APPROVAL,
             description="ШАГ 4: согласование с предпринимателем в Telegram"),
    NodeSpec(node_id="send", kind=NodeKind.EXTERNAL_ACTION,
             description="ШАГ 5: отправка предложения с follow-up"),
]

rate_limit_router = rate_limiter_router_factory(min_interval_sec=MIN_INTERVAL_SEC)


def post_approval_router(state: dict) -> str:
    """Объединяет решение человека (ШАГ 4) и anti-spam проверку (ШАГ 5)
    в одно условие на ребре — LangGraph разрешает только одну условную
    функцию на исходящий узел."""
    if state.get("human_decision") != "approved":
        return END
    return rate_limit_router(state)  # "send" или "wait"


edges = [
    EdgeSpec(source="collect_contacts", target="draft_proposal"),
    EdgeSpec(source="draft_proposal", target="approve"),
    EdgeSpec(source="approve", condition=post_approval_router,
             condition_map={"send": "send", "wait": END, END: END}),
    EdgeSpec(source="send", target=END),
]

graph = build_graph(SalesState, specs, edges, entry_point="collect_contacts", registry=registry)
