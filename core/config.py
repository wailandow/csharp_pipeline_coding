"""
core/config.py

Загрузка конфигурации: config.yaml (структура) + .env (секреты).

Секреты в config.yaml не хранятся как значения — только как плейсхолдер
${ИМЯ_ПЕРЕМЕННОЙ}, который разворачивается в реальное значение из
переменных окружения (.env или os.environ) при загрузке.

В отличие от исходного core/config.py, здесь НЕТ жёстко прописанных
путей вида cfg["llm"]["primary"]["api_key"] — подстановка работает
рекурсивно на любой глубине и для любого количества полей/профилей,
поэтому добавление нового LLM-профиля под новый узел не требует
правок в этом файле.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_root = Path(__file__).parent.parent
load_dotenv(_root / ".env")

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _resolve(node: Any) -> Any:
    """Рекурсивно проходит по конфигу и подставляет ${VAR} из окружения."""
    if isinstance(node, dict):
        return {k: _resolve(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v) for v in node]
    if isinstance(node, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), node)
    return node


def load(path: str | None = None) -> dict:
    cfg_path = path or str(_root / "config.yaml")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return _resolve(cfg)