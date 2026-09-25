"""
projects/strategy_selector/db.py

История запросов/ответов и попыток генерации стратегии.
Схема — расширенная версия вашей C#-версии (см. пояснения ниже).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'running',
    created_at  TEXT    NOT NULL,
    finished_at TEXT,
    model       TEXT
);

CREATE TABLE IF NOT EXISTS strategy_attempts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id           INTEGER NOT NULL,
    attempt_number       INTEGER NOT NULL,
    prompt               TEXT    NOT NULL,
    generated_code       TEXT,
    code_file_path       TEXT,
    model                TEXT,
    compile_success      INTEGER NOT NULL DEFAULT 0,
    compile_errors       TEXT,
    metrics_json         TEXT,
    metrics_passed       INTEGER NOT NULL DEFAULT 0,
    metrics_check_result TEXT,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT,
    FOREIGN KEY (session_id) REFERENCES strategy_sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_attempts_session
    ON strategy_attempts(session_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_session_number
    ON strategy_attempts(session_id, attempt_number);

-- ШАГ 3.1.2: успешные метрики по инструментам для стратегий, прошедших
-- проверку (metrics_passed=true) — фиксация "в статистиках", отдельно от
-- сырой истории попыток в strategy_attempts.
CREATE TABLE IF NOT EXISTS strategy_results (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id            INTEGER NOT NULL,
    attempt_id            INTEGER,
    security_name         TEXT,
    strategy_name         TEXT,
    total_trades          INTEGER,
    win_rate              REAL,
    profit_factor         REAL,
    total_return_percent  REAL,
    max_drawdown_percent  REAL,
    metrics_json          TEXT    NOT NULL,
    created_at            TEXT    NOT NULL,
    FOREIGN KEY (session_id) REFERENCES strategy_sessions(id),
    FOREIGN KEY (attempt_id) REFERENCES strategy_attempts(id)
);

CREATE INDEX IF NOT EXISTS idx_results_session
    ON strategy_results(session_id);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def create_session(db_path: str, description: str, now_iso_fn) -> int:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO strategy_sessions (description, status, created_at) VALUES (?,?,?)",
            (description, "running", now_iso_fn()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def finish_session(db_path: str, session_id: int, status: str, model: Optional[str], now_iso_fn) -> None:
    """Вызывается на последних шагах (успех/исчерпаны попытки), не в ШАГ 0-1."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE strategy_sessions SET status = ?, finished_at = ?, model = ? WHERE id = ?",
            (status, now_iso_fn(), model, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def next_attempt_number(db_path: str, session_id: int) -> int:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt_number), 0) AS m FROM strategy_attempts WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row["m"] + 1
    finally:
        conn.close()


def save_attempt(
    db_path: str,
    session_id: int,
    attempt_number: int,
    prompt: str,
    generated_code: Optional[str],
    code_file_path: Optional[str],
    model: Optional[str],
    now_iso_fn,
    compile_success: bool = False,
    compile_errors: Optional[str] = None,
    metrics: Optional[Any] = None,
    metrics_passed: bool = False,
    metrics_check_result: Optional[str] = None,
) -> int:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO strategy_attempts
               (session_id, attempt_number, prompt, generated_code, code_file_path,
                model, compile_success, compile_errors, metrics_json, metrics_passed,
                metrics_check_result, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id, attempt_number, prompt, generated_code, code_file_path,
                model, int(compile_success), compile_errors,
                json.dumps(metrics, ensure_ascii=False) if metrics is not None else None,
                int(metrics_passed), metrics_check_result, now_iso_fn(),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_attempt(
    db_path: str,
    attempt_id: int,
    now_iso_fn,
    compile_success: Optional[bool] = None,
    compile_errors: Optional[str] = None,
    metrics: Optional[Any] = None,
    metrics_passed: Optional[bool] = None,
    metrics_check_result: Optional[str] = None,
) -> None:
    """Дозаполнение попытки на ШАГ 2/3 (компиляция, метрики) по attempt_id из ШАГ 1."""
    fields, values = [], []
    if compile_success is not None:
        fields.append("compile_success = ?"); values.append(int(compile_success))
    if compile_errors is not None:
        fields.append("compile_errors = ?"); values.append(compile_errors)
    if metrics is not None:
        fields.append("metrics_json = ?"); values.append(json.dumps(metrics, ensure_ascii=False))
    if metrics_passed is not None:
        fields.append("metrics_passed = ?"); values.append(int(metrics_passed))
    if metrics_check_result is not None:
        fields.append("metrics_check_result = ?"); values.append(metrics_check_result)
    if not fields:
        return
    fields.append("updated_at = ?"); values.append(now_iso_fn())
    values.append(attempt_id)

    conn = get_connection(db_path)
    try:
        conn.execute(f"UPDATE strategy_attempts SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


def save_successful_metrics(
    db_path: str,
    session_id: int,
    attempt_id: Optional[int],
    metrics: Any,
    now_iso_fn,
) -> list[int]:
    """
    ШАГ 3.1.2: стратегия прошла проверку метрик — фиксируем метрики "в
    статистиках". `metrics` — то, что вернул раннер: список словарей по
    инструментам (см. примеры JSON) либо один словарь (на всякий случай
    поддерживаем оба варианта).
    """
    rows = metrics if isinstance(metrics, list) else [metrics] if metrics else []
    conn = get_connection(db_path)
    inserted_ids = []
    try:
        for row in rows:
            row = row or {}
            cur = conn.execute(
                """INSERT INTO strategy_results
                   (session_id, attempt_id, security_name, strategy_name, total_trades,
                    win_rate, profit_factor, total_return_percent, max_drawdown_percent,
                    metrics_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    session_id, attempt_id,
                    row.get("SecurityName"), row.get("StrategyName"), row.get("TotalTrades"),
                    row.get("WinRate"), row.get("ProfitFactor"),
                    row.get("TotalReturnPercent"), row.get("MaxDrawdownPercent"),
                    json.dumps(row, ensure_ascii=False), now_iso_fn(),
                ),
            )
            inserted_ids.append(cur.lastrowid)
        conn.commit()
        return inserted_ids
    finally:
        conn.close()