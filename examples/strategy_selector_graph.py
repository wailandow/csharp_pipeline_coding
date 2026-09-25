"""
examples/strategy_selector_graph.py

Проект "Автоматический подбор стратегий C#" — ШАГ 1..8 из ТЗ. Показывает
переиспользование тех же видов узлов (LLM_PROCESSOR, EXTERNAL_ACTION,
RETRY_CONTROLLER), что и в news_aggregator_graph.py, но для совершенно
другой предметной области — меняется только конфигурация.
"""
from __future__ import annotations

from langgraph.graph import END

from core import (
    NodeKind, NodeSpec, NodeRegistry, build_graph, EdgeSpec,
    llm_processor_node_factory, external_action_node_factory,
    retry_controller_node_factory, retry_router_factory,
)
from core.state import StrategyState

TEMPLATE_CLASS = "public class StrategyBase { /* фиксированный шаблон */ }"
MAX_ATTEMPTS = 5


# --- проектные адаптеры ---

def call_llm(prompt: str) -> str:
    # реальный вызов LLM API; для примера — заглушка
    return "public class Strategy001 : StrategyBase { /* сгенерированный код */ }"


def parse_cs_output(raw: str) -> dict:
    return {"cs_class_source": raw}


def save_and_compile(state: dict) -> dict:
    # ШАГ 3: запись .cs файла + компиляция приложения (App-in-Loop) — заглушка
    return {"ok": True, "compile_log": "build succeeded"}


def run_strategy(state: dict) -> dict:
    # ШАГ 4-5: запуск расчёта стратегии, получение метрик — заглушка
    metrics = {"sharpe": 1.4, "drawdown": 0.12}
    return {"ok": True, "metrics": metrics, "metrics_ok": metrics["sharpe"] > 1.0}


ACTIONS = {"compile": save_and_compile, "run_calc": run_strategy}

# --- регистрация типовых видов ---

registry = NodeRegistry()
registry.register_kind(NodeKind.LLM_PROCESSOR, lambda spec: llm_processor_node_factory(
    call_llm, prompt_template=spec.config["prompt_template"], output_parser=parse_cs_output))
registry.register_kind(NodeKind.EXTERNAL_ACTION, lambda spec: external_action_node_factory(
    ACTIONS[spec.config["action"]]))
registry.register_kind(NodeKind.RETRY_CONTROLLER, lambda spec: retry_controller_node_factory(
    max_attempts=spec.config.get("max_attempts", MAX_ATTEMPTS)))

# --- декларация графа ---

specs = [
    NodeSpec(node_id="generate_class", kind=NodeKind.LLM_PROCESSOR,
             description="ШАГ 2/7-8: генерация (или доработка) C#-класса стратегии",
             config={"prompt_template": f"Шаблон: {TEMPLATE_CLASS}\nЗадача: $item"}),
    NodeSpec(node_id="compile", kind=NodeKind.EXTERNAL_ACTION,
             description="ШАГ 3: сохранение .cs и компиляция",
             config={"action": "compile"}),
    NodeSpec(node_id="run_calc", kind=NodeKind.EXTERNAL_ACTION,
             description="ШАГ 4-5: запуск расчёта стратегии, получение метрик",
             config={"action": "run_calc"}),
    NodeSpec(node_id="retry_ctl", kind=NodeKind.RETRY_CONTROLLER,
             description="ШАГ 6: учёт числа попыток улучшения стратегии",
             config={"max_attempts": MAX_ATTEMPTS}),
]

# ШАГ 3.1/3.2: ветвление по результату компиляции — тот же ROUTER-паттерн,
# что и в проекте 1 (news_aggregator_graph.py), просто на других полях.
compile_router = lambda s: "run_calc" if s.get("action_status") == "success" else "generate_class"

# ШАГ 6: если метрики плохие и попытки не исчерпаны — назад на генерацию промпта (ШАГ 7-8).
# metrics_ok лежит внутри action_result (см. run_strategy выше), а не на
# верхнем уровне state — это следствие общего контракта ExternalActionNode.
metrics_router = retry_router_factory(
    ok_check=lambda s: (s.get("action_result") or {}).get("metrics_ok"),
    retry_target="generate_class", ok_target=END, giveup_target=END,
)

edges = [
    EdgeSpec(source="generate_class", target="compile"),
    EdgeSpec(source="compile", condition=compile_router,
             condition_map={"run_calc": "run_calc", "generate_class": "generate_class"}),
    EdgeSpec(source="run_calc", target="retry_ctl"),
    EdgeSpec(source="retry_ctl", condition=metrics_router,
             condition_map={END: END, "generate_class": "generate_class"}),
]

graph = build_graph(StrategyState, specs, edges, entry_point="generate_class", registry=registry)
