"""
core/registry.py

Реестр узлов: связывает NodeSpec (декларацию) с реальной функцией-узлом.

Типовые виды — берутся из каталога (node_kinds.py): разработчик один раз
регистрирует, как из NodeSpec.config собрать нужную фабрику (см. примеры
в examples/*.py). Custom-виды — генерируются через LLM
(llm_node_factory.py) и кэшируются по хэшу спецификации, чтобы код не
пересоздавался заново при каждой сборке графа.
"""
from __future__ import annotations

import hashlib
from typing import Callable

from .node_kinds import NodeKind, NodeSpec


class NodeRegistry:
    def __init__(self) -> None:
        self._builders: dict[NodeKind, Callable[[NodeSpec], Callable[[dict], dict]]] = {}
        self._compiled_cache: dict[str, Callable[[dict], dict]] = {}

    def register_kind(
        self,
        kind: NodeKind,
        builder: Callable[[NodeSpec], Callable[[dict], dict]],
    ) -> None:
        """Регистрирует, как из NodeSpec данного kind собрать функцию-узел."""
        self._builders[kind] = builder

    def build(self, spec: NodeSpec) -> Callable[[dict], dict]:
        cache_key = self._hash(spec)
        if cache_key in self._compiled_cache:
            return self._compiled_cache[cache_key]

        if spec.kind not in self._builders:
            raise ValueError(
                f"Нет builder-а для вида узла '{spec.kind}'. "
                f"Зарегистрируйте его через register_kind(), либо используйте "
                f"kind=NodeKind.CUSTOM и llm_node_factory.generate_custom_node()."
            )

        node_fn = self._builders[spec.kind](spec)
        self._compiled_cache[cache_key] = node_fn
        return node_fn

    def register_compiled(self, spec: NodeSpec, node_fn: Callable[[dict], dict]) -> None:
        """Кладёт в кэш уже готовую функцию — используется после LLM-генерации,
        чтобы не запускать generate_custom_node() повторно для того же NodeSpec."""
        self._compiled_cache[self._hash(spec)] = node_fn

    @staticmethod
    def _hash(spec: NodeSpec) -> str:
        payload = spec.model_dump_json(exclude_none=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
