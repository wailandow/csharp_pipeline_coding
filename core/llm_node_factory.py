"""
core/llm_node_factory.py

Генерация НОВОГО вида узла с помощью LLM: разработчик описывает вход,
выход и роль узла в NodeSpec(kind=CUSTOM, ...), а эта функция:

  1. Формирует промпт с контрактом (сигнатура функции, доступные поля
     state, примеры уже существующих узлов, ограничения).
  2. Запрашивает Claude через Anthropic API.
  3. Валидирует полученный код (синтаксис, сигнатура, запрещённые импорты).
  4. Прогоняет "сухой" тест на минимальном state.
  5. При ошибке — формирует новый промпт с текстом ошибки и повторяет
     (тот же паттерн retry-петли, что в проекте "Автоподбор стратегий",
     шаги 6-8 из ТЗ).
  6. Возвращает готовую функцию узла + исходный код (для code review).

ВАЖНО про безопасность: exec() чужого/сгенерированного кода в этом файле
выполняется в том же процессе для простоты примера. В боевом использовании
сгенерированный код нужно запускать в изолированном окружении (subprocess
с ограниченными правами, контейнер, gVisor и т.п.), а не в основном
процессе pipeline.
"""
from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .node_kinds import NodeSpec

MODEL = "claude-sonnet-4-6"

FORBIDDEN_IMPORTS = {"os", "subprocess", "sys", "socket", "shutil"}

SYSTEM_PROMPT = textwrap.dedent(f"""\
    Ты пишешь ОДИН python-узел для графа LangGraph.
    Требования:
    - функция называется `node`, сигнатура: def node(state: dict) -> dict
    - функция ЧИТАЕТ только поля, перечисленные в разделе READS
    - функция ВОЗВРАЩАЕТ dict, содержащий ТОЛЬКО поля из раздела WRITES
      (частичное обновление состояния, как того требует LangGraph)
    - никаких side-effects (сеть, файлы, subprocess) — если они нужны,
      опиши в комментарии, что функция должна принимать готовый callable
      через замыкание, но сам вызов не реализуй
    - не используй импорты из списка: {", ".join(sorted(FORBIDDEN_IMPORTS))}
    - в ответе — ТОЛЬКО python-код, без markdown-разметки и пояснений
""")


@dataclass
class GeneratedNode:
    spec: NodeSpec
    source_code: str
    fn: Callable[[dict], dict]
    attempts_used: int


def _build_prompt(spec: NodeSpec, state_fields: list[str], examples: str) -> str:
    reads = "\n".join(f"- {f.name}: {f.type} — {f.description}" for f in spec.reads)
    writes = "\n".join(f"- {f.name}: {f.type} — {f.description}" for f in spec.writes)
    return textwrap.dedent(f"""\
        РОЛЬ УЗЛА: {spec.description}

        READS (поля state, которые можно читать):
        {reads or "(нет)"}

        WRITES (поля, которые нужно вернуть в dict):
        {writes or "(нет)"}

        Доступные поля общего состояния (BaseNodeState): {", ".join(state_fields)}

        Примеры уже существующих узлов для стиля (не копировать буквально):
        {examples or "(нет примеров)"}
    """)


def _validate_source(code: str) -> Optional[str]:
    """Возвращает текст ошибки или None, если код прошёл проверку."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"SyntaxError: {exc}"

    has_node_fn = any(
        isinstance(n, ast.FunctionDef) and n.name == "node" for n in ast.walk(tree)
    )
    if not has_node_fn:
        return "В коде не найдена функция `def node(state: dict) -> dict`"

    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names = [a.name.split(".")[0] for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            names = [(n.module or "").split(".")[0]]
        else:
            continue
        bad = FORBIDDEN_IMPORTS.intersection(names)
        if bad:
            return f"Запрещённые импорты: {bad}"
    return None


def _compile_node(code: str) -> Callable[[dict], dict]:
    namespace: dict[str, Any] = {}
    exec(compile(code, "<generated_node>", "exec"), namespace)  # noqa: S102 — см. docstring модуля
    return namespace["node"]


def generate_custom_node(
    spec: NodeSpec,
    state_fields: list[str],
    examples: str = "",
    max_attempts: int = 3,
    client: Optional[Any] = None,
) -> GeneratedNode:
    """
    client — экземпляр anthropic.Anthropic(). Передаётся параметром, а не
    импортируется на верхнем уровне модуля, чтобы core/ можно было
    использовать (тестировать сборку графа) без установленного SDK,
    если custom-узлы в конкретном графе не нужны.
    """
    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    prompt = _build_prompt(spec, state_fields, examples)
    last_error: Optional[str] = None

    for attempt in range(1, max_attempts + 1):
        full_prompt = prompt if last_error is None else (
            prompt + f"\n\nПредыдущая попытка не прошла проверку:\n{last_error}\n"
            f"Пришли исправленный код."
        )
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": full_prompt}],
        )
        code = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        ).strip()

        error = _validate_source(code)
        if error is None:
            try:
                fn = _compile_node(code)
                fn({f: None for f in state_fields})  # сухой прогон
                return GeneratedNode(spec=spec, source_code=code, fn=fn, attempts_used=attempt)
            except Exception as exc:
                error = f"Ошибка при выполнении: {exc}"
        last_error = error

    raise RuntimeError(
        f"Не удалось сгенерировать рабочий узел '{spec.node_id}' за {max_attempts} "
        f"попыток. Последняя ошибка: {last_error}"
    )
