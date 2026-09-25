"""
core/logging_setup.py

Единая настройка логирования для всех проектов/узлов каркаса.
Вызывается один раз при старте (в graph.py или в скрипте запуска).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any


def setup_logging(cfg: dict[str, Any] | None = None) -> None:
    """
    cfg — секция logging из config.yaml, например:
        {"level": "INFO", "file": "logs/pipeline.log"}
    Если file не задан — пишем только в консоль.
    """
    cfg = cfg or {}
    level_name = str(cfg.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = cfg.get("file")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()  # чтобы при повторном вызове не плодились дубли

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    # HTTP-библиотека логирует каждый запрос на INFO — приглушаем, иначе
    # забьёт лог сотнями строк про соединение
    logging.getLogger("httpx").setLevel(logging.WARNING)