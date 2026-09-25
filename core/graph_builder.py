"""
core/graph_builder.py

Собирает LangGraph StateGraph из декларативного списка узлов и рёбер.
Так реализуется "универсальная основа": описание графа — это данные
(NodeSpec + EdgeSpec), а не жёстко зашитый код, и добавление нового узла
в цепочку — это добавление записи в список, а не правка логики фреймворка.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Type

from langgraph.graph import StateGraph, END

from .node_kinds import NodeSpec
from .registry import NodeRegistry


@dataclass
class EdgeSpec:
    source: str
    target: Optional[str] = None                        # простое ребро
    condition: Optional[Callable[[dict], str]] = None    # условное ребро
    condition_map: Optional[dict[str, str]] = None       # {"approved": "send", END: END}


def build_graph(
    state_schema: Type[dict],
    node_specs: list[NodeSpec],
    edges: list[EdgeSpec],
    entry_point: str,
    registry: NodeRegistry,
) -> Any:
    graph = StateGraph(state_schema)

    for spec in node_specs:
        graph.add_node(spec.node_id, registry.build(spec))

    graph.set_entry_point(entry_point)

    for edge in edges:
        if edge.condition is not None:
            graph.add_conditional_edges(edge.source, edge.condition, edge.condition_map)
        else:
            graph.add_edge(edge.source, edge.target if edge.target is not None else END)

    return graph.compile()
