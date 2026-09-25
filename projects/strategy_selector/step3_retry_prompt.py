"""
projects/strategy_selector/step3_retry_prompt.py

Если компиляция не удалась (и лимит попыток не исчерпан) — собрать новый
промт с текстом ошибки и предыдущим кодом, отправить снова на ШАГ 1.
"""
from __future__ import annotations

import logging
from typing import Callable

from core.state import now_iso

log = logging.getLogger("strategy_selector.step3_retry")


def build_retry_prompt(
    strategy_description: str,
    class_template: str,
    class_addon_template: str,
    previous_code: str,
    compile_errors: str,
    attempt_number: int,
) -> str:
    lines = [
        "## Задача",
        "## Описание требуемой реализации",
        strategy_description,
        "",
        f"## Предыдущая попытка (№{attempt_number}) не прошла компиляцию",
        "### Код предыдущей попытки:",
        "```csharp",
        previous_code,
        "```",
        "### Ошибки компиляции:",
        compile_errors or "(нет текста ошибки)",
        "",
        "## Инструкция",
        "Исправь ошибки компиляции. Сохрани структуру класса и весь смысл "
        "исходной логики стратегии — правь только то, что мешает компиляции.",
        "",
        "## БЛОК ШАБЛОНА класса для изменения (структура фиксирована, изменяй только реализацию)",
        class_template,
        "## КОНЕЦ БЛОКА ШАБЛОНА класса для изменения ",
        "",
        "## Вспомогательные классы (НЕЛЬЗЯ МЕНЯТЬ И ВЫДУМЫВАТЬ новые поля)",
        class_addon_template,
        "",
        "## Обязательные требования:",
        "* Комментарии в коде — на русском, кратко",
        "* Стратегия должна компилироваться без дополнительных зависимостей",
        "* Верни ПОЛНЫЙ обновлённый код класса, а не только изменённые фрагменты",
        "* Структура класса фиксирована — менять нельзя.",
        "",
    ]
    return "\n".join(lines)


def step3_retry_node_factory(
    class_template: str,
    class_addon_template: str,
) -> Callable[[dict], dict]:
    def node(state: dict) -> dict:
        description = state.get("current_strategy_description", "")
        previous_code = (state.get("llm_result") or {}).get("cs_class_source", "")
        compile_errors = state.get("compile_errors") or ""
        attempt_number = state.get("attempt_number", 0)

        log.info("Готовим повторную попытку после неудачной №%s", attempt_number)

        prompt = build_retry_prompt(
            strategy_description=description,
            class_template=class_template,
            class_addon_template=class_addon_template,
            previous_code=previous_code,
            compile_errors=compile_errors,
            attempt_number=attempt_number,
        )

        return {
            "initial_prompt": prompt,
            "retry_reason": "compile",
            # относился к предыдущей попытке — обнуляем перед новым кругом,
            # иначе ШАГ 2 решит, что и эта попытка уже провалена заранее
            "last_error": None,
            "history": [{
                "ts": now_iso(),
                "node": "step3_retry_prompt",
                "message": f"собран промт для повторной попытки после №{attempt_number} (ошибка компиляции)",
                "data": {},
            }],
        }
    return node