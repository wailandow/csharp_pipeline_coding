"""
projects/strategy_selector/graph.py

ШАГ 0-1 проекта "Автоматический подбор стратегий C#".
"""
from __future__ import annotations

from pathlib import Path

from langgraph.graph import END

from core import NodeKind, NodeSpec, NodeRegistry, build_graph, EdgeSpec
from core.config import load as load_config
from core.logging_setup import setup_logging
from core.llm_client import LLMClient, has_usable_endpoint
from projects.strategy_selector.state import StrategySelectorState
from projects.strategy_selector.step0_prompt_builder import step0_node_factory
from projects.strategy_selector.step1_llm_generate import step1_node_factory
from projects.strategy_selector.step2_compile_and_evaluate import step2_node_factory
from projects.strategy_selector.step3_retry_prompt import step3_retry_node_factory
from projects.strategy_selector.step3b_metrics_retry_prompt import step3b_metrics_retry_node_factory
from projects.strategy_selector.step4_finish import (
    route_after_compile, route_after_finish, finish_node_factory,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
CLASS_TEMPLATE = (TEMPLATES_DIR / "class_template.cs").read_text(encoding="utf-8")
CLASS_ADDON_TEMPLATE = (TEMPLATES_DIR / "class_addon_template.cs").read_text(encoding="utf-8")

cfg = load_config()
setup_logging(cfg.get("logging"))

paths = cfg["paths"]["strategy_selector"]
DB_PATH = paths["db_path"]
GENERATED_DIR = paths["generated_dir"]

registry = NodeRegistry()

# --- ШАГ 0 ---
step0_spec = NodeSpec(
    node_id="build_initial_prompt",
    kind=NodeKind.CUSTOM,
    description="ШАГ 0: взять первую стратегию из списка, создать сессию, собрать промт",
)
registry.register_compiled(
    step0_spec,
    step0_node_factory(
        class_template=CLASS_TEMPLATE,
        class_addon_template=CLASS_ADDON_TEMPLATE,
        db_path=DB_PATH,
    ),
)

# --- ШАГ 1 (+ ШАГ 3.1.1.2: отдельный LLM-клиент для доработки по метрикам) ---
_metrics_retry_endpoints = (cfg["llm"] or {}).get("strategy_metrics_retry")
_metrics_retry_llm_client = (
    LLMClient(_metrics_retry_endpoints)
    if has_usable_endpoint(_metrics_retry_endpoints)
    else None
)

_compile_retry_endpoints = (cfg["llm"] or {}).get("strategy_compile_retry")
_compile_retry_llm_client = (
    LLMClient(_compile_retry_endpoints)
    if has_usable_endpoint(_compile_retry_endpoints)
    else None
)

step1_spec = NodeSpec(
    node_id="generate_class",
    kind=NodeKind.CUSTOM,
    description="ШАГ 1 / ШАГ 3.1.1.2: вызов LLM (с fallback), сохранение попытки в БД и .cs-файла",
)
registry.register_compiled(
    step1_spec,
    step1_node_factory(
        LLMClient(cfg["llm"]["strategy_step1"]),
        db_path=DB_PATH,
        generated_dir=GENERATED_DIR,
        compile_retry_llm_client=_compile_retry_llm_client,   # НОВОЕ
        metrics_retry_llm_client=_metrics_retry_llm_client,
    ),
)

# --- ШАГ 2 ---
step2_spec = NodeSpec(
    node_id="compile_and_evaluate",
    kind=NodeKind.CUSTOM,
    description="ШАГ 2: компиляция через Roslyn-раннер + расчёт метрик",
)
registry.register_compiled(
    step2_spec,
    step2_node_factory(
        command=paths["compiler_command"],
        result_dir=paths["compile_results_dir"],
        db_path=DB_PATH,
        timeout_sec=paths.get("compile_timeout_sec", 120),
    ),
)

# ШАГ 3.1.1: общий бюджет попыток — считает и ошибки компиляции (ШАГ 2.1),
# и неудачные метрики (ШАГ 3.1.1) в один и тот же attempt_number
MAX_ATTEMPTS = paths.get("max_attempts", 7)

# --- ШАГ 2.1: ретрай после ошибки компиляции ---
step3_spec = NodeSpec(
    node_id="build_retry_prompt",
    kind=NodeKind.CUSTOM,
    description="ШАГ 2.1: собрать промт для повторной попытки после неудачной компиляции",
)
registry.register_compiled(
    step3_spec,
    step3_retry_node_factory(CLASS_TEMPLATE, CLASS_ADDON_TEMPLATE),
)

# --- ШАГ 3.1.1: ретрай после непройденных метрик (компиляция была успешной) ---
step3b_spec = NodeSpec(
    node_id="build_metrics_retry_prompt",
    kind=NodeKind.CUSTOM,
    description="ШАГ 3.1.1: собрать промт для доработки стратегии по метрикам",
)
registry.register_compiled(
    step3b_spec,
    step3b_metrics_retry_node_factory(CLASS_TEMPLATE, CLASS_ADDON_TEMPLATE),
)

step4_spec = NodeSpec(
    node_id="finish_session",
    kind=NodeKind.CUSTOM,
    description="Закрыть сессию в БД финальным статусом, зафиксировать успешные метрики (ШАГ 3.1.2)",
)
registry.register_compiled(step4_spec, finish_node_factory(DB_PATH))

specs = [step0_spec, step1_spec, step2_spec, step3_spec, step3b_spec, step4_spec]
edges = [
    EdgeSpec(source="build_initial_prompt", target="generate_class"),
    EdgeSpec(source="generate_class", target="compile_and_evaluate"),
    EdgeSpec(
        source="compile_and_evaluate",
        condition=route_after_compile(MAX_ATTEMPTS),
        condition_map={
            "success": "finish_session",
            "give_up": "finish_session",
            "retry_compile": "build_retry_prompt",
            "retry_metrics": "build_metrics_retry_prompt",
        },
    ),
    EdgeSpec(source="build_retry_prompt", target="generate_class"),
    EdgeSpec(source="build_metrics_retry_prompt", target="generate_class"),
    # ДОП.: strategy_descriptions — список; после закрытия сессии либо
    # переходим к следующему описанию (снова на ШАГ 0), либо завершаем граф
    EdgeSpec(
        source="finish_session",
        condition=route_after_finish,
        condition_map={"next_item": "build_initial_prompt", "done": END},
    ),
]

graph = build_graph(
    StrategySelectorState,
    specs,
    edges,
    entry_point="build_initial_prompt",
    registry=registry,
)