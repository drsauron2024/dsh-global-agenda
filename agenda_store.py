"""Small, atomic JSON store for the global agenda overlay."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any


APP_DIR_NAME = "DshGlobalAgenda"
VALID_SEVERITIES = ("high", "medium", "low")


def app_data_dir() -> Path:
    root = os.environ.get("APPDATA")
    base = Path(root) if root else Path.home() / "AppData" / "Roaming"
    return base / APP_DIR_NAME


DATA_FILE = app_data_dir() / "agenda.json"
LOG_FILE = app_data_dir() / "overlay.log"


def _new_id() -> str:
    import time
    import uuid

    return f"task-{int(time.time() * 1000):x}-{uuid.uuid4().hex[:8]}"


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())[:240]


def _clean_due(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if len(candidate) != 10:
        return None
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def default_state() -> dict[str, Any]:
    return {
        "version": 2,
        "tasks": [],
        "window": {"x": None, "y": None, "collapsed": False, "mode": "dock"},
    }


def _normalise_task(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    text = _clean_text(raw.get("text"))
    if not text:
        return None
    task_id = raw.get("id")
    if not isinstance(task_id, str) or not task_id:
        task_id = _new_id()
    done = raw.get("done") is True
    created = raw.get("created_at")
    if not isinstance(created, (int, float)):
        import time

        created = time.time()
    severity = raw.get("severity")
    if severity not in VALID_SEVERITIES:
        severity = "medium"
    return {
        "id": task_id,
        "text": text,
        "done": done,
        "created_at": float(created),
        "severity": severity,
        "due": _clean_due(raw.get("due")),
    }


def load_state() -> dict[str, Any]:
    state = default_state()
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if not isinstance(raw, dict):
        return state
    tasks = raw.get("tasks")
    if isinstance(tasks, list):
        seen: set[str] = set()
        for candidate in tasks:
            task = _normalise_task(candidate)
            if task is None or task["id"] in seen:
                continue
            seen.add(task["id"])
            state["tasks"].append(task)
            if len(state["tasks"]) >= 500:
                break
    window = raw.get("window")
    if isinstance(window, dict):
        for key in ("x", "y"):
            value = window.get(key)
            if isinstance(value, (int, float)) and abs(value) < 100000:
                state["window"][key] = int(value)
        state["window"]["collapsed"] = window.get("collapsed") is True
        mode = window.get("mode")
        state["window"]["mode"] = mode if mode in ("dock", "floating") else "dock"
    return state


def save_state(tasks: list[dict[str, Any]], window: dict[str, Any]) -> None:
    directory = app_data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "tasks": tasks,
        "window": {
            "x": window.get("x"),
            "y": window.get("y"),
            "collapsed": bool(window.get("collapsed")),
            "mode": window.get("mode") if window.get("mode") in ("dock", "floating") else "dock",
        },
    }
    fd, temporary = tempfile.mkstemp(prefix=".agenda-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, DATA_FILE)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
