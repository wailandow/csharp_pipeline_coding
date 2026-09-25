"""
core/llm_client.py

Универсальный LLM-клиент с fallback по списку эндпойнтов.
Работает с любым OpenAI-совместимым API (/chat/completions).

Намеренно НЕ содержит промтов и парсинга ответа — это дело конкретного
узла. Один и тот же класс используют разные узлы разных проектов:
каждый создаёт свой LLMClient со своим списком эндпойнтов (модель,
base_url, ключ, fallback-цепочка) при сборке графа.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .cli_runner import run_cli

log = logging.getLogger("llm_client")

CLI_PROVIDERS = ("claude_cli", "codex_cli")


def has_usable_endpoint(endpoints: list[dict[str, Any]] | None) -> bool:
    """Есть ли хотя бы один эндпойнт, который LLMClient не пропустит."""
    return any(
        ep.get("provider", "api") in CLI_PROVIDERS or ep.get("api_key")
        for ep in endpoints or []
    )


def messages_to_prompt(messages: list[dict[str, str]]) -> str:
    """CLI принимает один текст: склеиваем сообщения, если их больше одного."""
    if len(messages) == 1:
        return messages[0]["content"]
    return "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in messages)


class LLMClient:
    def __init__(self, endpoints: list[dict[str, Any]]):
        """
        endpoints — список конфигураций в порядке приоритета:
            [{"label": "primary",  "base_url": "...", "model": "...",
              "api_key": "...", "max_tokens": 1200, "temperature": 0.2},
             {"label": "fallback", ...}, ...]

        Эндпойнт без api_key пропускается — так же, как в исходном
        core/llm.py: можно держать конфиг fallback-цепочки в коде,
        а ключи подключать только через переменные окружения.
        """
        if not endpoints:
            raise ValueError("нужен хотя бы один LLM-эндпойнт")
        self._endpoints = endpoints

    def complete(self, messages: list[dict[str, str]]) -> tuple[str, str, str]:
        """Возвращает (raw_text, model_name, finish_reason)."""
        last_exc = None
        for ep in self._endpoints:
            label = ep.get("label", ep.get("model", "?"))
            provider = ep.get("provider", "api")
            if provider == "api" and not ep.get("api_key"):
                log.debug("Пропуск %s LLM — нет api_key", label)
                continue
            try:
                log.info("-> %s LLM [%s, %s]", label, provider, ep["model"])
                if provider == "api":
                    raw, model, finish_reason = self._call(ep, messages)
                elif provider in CLI_PROVIDERS:
                    raw, model = run_cli(provider, ep, messages_to_prompt(messages))
                    finish_reason = "stop"
                else:
                    raise ValueError(f"неизвестный provider: {provider}")
                if finish_reason == "length":
                    log.warning(
                        "%s LLM вернул обрезанный ответ (finish_reason=length, max_tokens=%s)",
                        label, ep.get("max_tokens", 4000),
                    )
                else:
                    log.info("OK ответ получен от %s (finish_reason=%s)", label, finish_reason)
                return raw, model, finish_reason
            except Exception as exc:
                log.warning("FAIL %s LLM ошибка: %s", label, exc)
                last_exc = exc
        raise RuntimeError(f"Все LLM-эндпойнты недоступны: {last_exc}")

    @staticmethod
    def _call(ep, messages):
        url = ep["base_url"].rstrip("/") + "/chat/completions"
        payload = {"model": ep["model"], "messages": messages,
                   "max_tokens": ep.get("max_tokens", 4000), "temperature": ep.get("temperature", 0.2)}
        headers = {"Authorization": f"Bearer {ep['api_key']}", "Content-Type": "application/json"}
        with httpx.Client(timeout=ep.get("timeout", 90)) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        choice = data["choices"][0]
        content = choice["message"]["content"]
        finish_reason = choice.get("finish_reason", "unknown")
        return content, data.get("model", ep["model"]), finish_reason