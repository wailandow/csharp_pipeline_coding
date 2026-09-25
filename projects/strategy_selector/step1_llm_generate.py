"""
projects/strategy_selector/step1_llm_generate.py

ШАГ 1: вызов LLM (с fallback между эндпойнтами) с промтом из ШАГ 0,
разбор ответа в C#-класс, сохранение попытки в БД и .cs-файла на диск.
"""
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Callable

from core.llm_client import LLMClient
from core.state import now_iso
from projects.strategy_selector import db

log = logging.getLogger("strategy_selector.step1")

_OPEN_FENCE_RE = re.compile(r"```(?:csharp|cs)?\s*\n")


def parse_cs_response(raw: str) -> dict:
    """
    Модели просят вернуть код БЕЗ markdown-fence (см. системный промт), но
    некоторые всё равно оборачивают ответ в ```csharp ... ``` — если fence
    есть, аккуратно вырезаем код из неё, если нет — берём весь ответ как есть.

    ВАЖНО: отсутствие закрывающей ``` (или fence вообще) — это НЕ признак
    обрезанного ответа. Модель может как раз следовать инструкции "без
    markdown-блоков" и просто не ставить fence. Единственный надёжный
    источник истины про обрезание — finish_reason от API (см. step1_node_factory).
    """
    open_match = _OPEN_FENCE_RE.search(raw)
    if open_match:
        tail = raw[open_match.end():]
        close_idx = tail.find("```")
        code = tail[:close_idx] if close_idx != -1 else tail
    else:
        code = raw

    code = code.strip()
    if "class" not in code:
        raise ValueError("в ответе LLM не найден C#-класс")
    return {"cs_class_source": code}


def _unique_cs_filename(session_id: int, attempt_number: int) -> str:
    return f"strategy_s{session_id}_a{attempt_number}_{uuid.uuid4().hex[:8]}.cs"


def step1_node_factory(
    llm_client: LLMClient,
    db_path: str,
    generated_dir: str,
    compile_retry_llm_client: LLMClient | None = None,   # НОВОЕ
    metrics_retry_llm_client: LLMClient | None = None,
) -> Callable[[dict], dict]:
    """
    llm_client                — модели (primary/fallback) для первичной
                                 генерации и для доработки после ошибки
                                 компиляции (retry_reason == "compile"/None).
    metrics_retry_llm_client  — отдельные модели (primary/fallback) для
                                 ШАГ 3.1.1.2: доработка стратегии, которая
                                 скомпилировалась, но не прошла метрики
                                 (retry_reason == "metrics"). Если не задан,
                                 используется тот же llm_client.
    """
    Path(generated_dir).mkdir(parents=True, exist_ok=True)

    def node(state: dict) -> dict:
        prompt = state.get("initial_prompt")
        session_id = state.get("session_id")
        if not prompt:
            raise ValueError("initial_prompt пуст — сначала должен отработать ШАГ 0")
        if session_id is None:
            raise ValueError("session_id отсутствует в state — сначала должен отработать ШАГ 0")

        attempt_number = db.next_attempt_number(db_path, session_id)
        retry_reason = state.get("retry_reason")
        # active_client = metrics_retry_llm_client if (retry_reason == "metrics" and metrics_retry_llm_client) else llm_client
        if retry_reason == "metrics" and metrics_retry_llm_client:
            active_client = metrics_retry_llm_client
        elif retry_reason == "compile" and compile_retry_llm_client:
            active_client = compile_retry_llm_client
        else:
            active_client = llm_client
        log.info("ШАГ 1: session_id=%s, attempt_number=%s, retry_reason=%s — вызов LLM",
                 session_id, attempt_number, retry_reason)

        sys_promt =  "Ты — Senior Quant Developer. Твоя задача — реализовать торговую стратегию в виде C# класса, наследующего BaseSubStrategy. Ты пишешь ТОЛЬКО компилируемый код, без объяснений, если не попросили иначе. Верни ТОЛЬКО полный компилируемый C# код класса. Без markdown-блоков, без пояснений."

        messages = [{"role": "system", "content": sys_promt}, {"role": "user", "content": prompt}]
        code_file_path: str | None = None
        model_used: str | None = None
        try:
            raw, model_used, finish_reason = active_client.complete(messages)
            # некоторые провайдеры при finish_reason=length (и иногда при
            # отказах) возвращают content: null вместо пустой/обрезанной
            # строки — не даём это уронить len()/regex ниже
            raw = raw or ""
            log.info("Ответ получен от модели %s (%d симв., finish_reason=%s)",
                      model_used, len(raw), finish_reason)
            log.info("Ответ модели %s", raw)

            if not raw.strip():
                raise ValueError(
                    f"LLM ({model_used}) вернул пустой ответ (finish_reason={finish_reason}) — "
                    "скорее всего max_tokens исчерпан на служебных/reasoning-токенах "
                    "раньше, чем начался код класса; увеличьте max_tokens в config.yaml"
                )

            parsed = parse_cs_response(raw)
            code = parsed["cs_class_source"]

            filename = _unique_cs_filename(session_id, attempt_number)
            code_file_path = str(Path(generated_dir) / filename)
            Path(code_file_path).write_text(code, encoding="utf-8")

            # Единственный надёжный признак обрезанного ответа — finish_reason
            # от самого API (см. докстринг parse_cs_response выше). Наличие/
            # отсутствие закрывающей ``` fence ничего не говорит об обрезании:
            # модель могла просто не поставить fence, при этом ответ полный.
            is_truncated = finish_reason == "length"
            if is_truncated:
                log.warning(
                    "Ответ обрезан (finish_reason=%s) — класс сохранён НЕПОЛНОСТЬЮ: %s. "
                    "Увеличьте max_tokens в config.yaml или уменьшите объём промта.",
                    finish_reason, code_file_path,
                )
                error = f"ответ LLM обрезан (finish_reason={finish_reason}), класс неполный"
            else:
                log.info("C#-класс сохранён полностью: %s", code_file_path)
                error = None

            parsed["_model_used"] = model_used
            parsed["_code_file_path"] = code_file_path
        except Exception as exc:
            log.exception("ШАГ 1: ошибка при генерации/сохранении класса")
            parsed, error = {}, str(exc)
            # model_used мог успеть заполниться из active_client.complete()
            # до сбоя (например, ответ пуст, но модель известна) — не теряем его
            
        attempt_id = db.save_attempt(
            db_path=db_path,
            session_id=session_id,
            attempt_number=attempt_number,
            prompt=prompt,
            generated_code=parsed.get("cs_class_source"),
            code_file_path=code_file_path,
            model=model_used,
            now_iso_fn=now_iso,
        )
        log.info("Попытка сохранена в БД: strategy_attempts.id=%s", attempt_id)

        return {
            "llm_result": parsed,
            "last_error": error,
            "attempt_id": attempt_id,
            "attempt_number": attempt_number,
            "history": [{
                "ts": now_iso(),
                "node": "step1_generate_class",
                "message": "C#-класс сгенерирован" if not error else f"ошибка: {error}",
                "data": {"model": model_used, "attempt_id": attempt_id},
            }],
        }
    return node