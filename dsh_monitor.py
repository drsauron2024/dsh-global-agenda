"""Read-only DSH session-log monitor for the global agenda overlay.

DSH keeps one compressed JSONL log per session under ``%USERPROFILE%\\.dsh\\
sessions``. A turn is live while the newest ``turn/start`` has no matching
``turn/end``. This module reconstructs that small amount of state without
touching the Harness process or its authenticated Web API.
"""

from __future__ import annotations

import ctypes
import json
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:  # Python 3.14+
    from compression import zstd
except Exception:  # pragma: no cover - only relevant on older interpreters
    zstd = None


MAX_PATH = 260
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_NAMES = {
    "deepseek harness.exe",
    "deepseek-harness.exe",
    "dsh.exe",
}
SCAN_INTERVAL_SECONDS = 6.0
MAX_SESSION_FILES = 240
STALE_TURN_SECONDS = 12 * 60 * 60


@dataclass(frozen=True, slots=True)
class RunningTask:
    session_id: str
    title: str
    cwd: str
    activity: str
    turn: int
    step: int
    started_at: float
    last_event_at: float
    needs_attention: bool = False


@dataclass(frozen=True, slots=True)
class HarnessSnapshot:
    process_running: bool
    window_present: bool
    sessions: tuple[RunningTask, ...]
    error: Optional[str] = None


@dataclass(slots=True)
class _ParsedSession:
    session_id: str
    title: str
    cwd: str
    running: bool
    turn: int
    step: int
    started_at: float
    last_event_at: float
    last_event_type: str
    open_tools: tuple[str, ...]
    approval_pending: bool


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _user32():
    return ctypes.WinDLL("user32", use_last_error=True)


def process_running() -> bool:
    """Return whether a DSH/Electron process exists in this user session."""

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * MAX_PATH),
        ]

    kernel = _kernel32()
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid = ctypes.c_void_p(-1).value
    if not snapshot or snapshot == invalid:
        return False
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        present = bool(kernel.Process32FirstW(snapshot, ctypes.byref(entry)))
        while present:
            name = str(entry.szExeFile).strip().lower()
            if name in PROCESS_NAMES:
                return True
            present = bool(kernel.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel.CloseHandle(snapshot)
    return False


_WINDOW_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def find_harness_windows() -> list[tuple[int, str]]:
    """Return visible top-level windows that belong to the Harness desktop app."""

    user = _user32()
    user.EnumWindows.argtypes = [_WINDOW_PROC, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextLengthW.restype = ctypes.c_int
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowTextW.restype = ctypes.c_int
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetWindowRect.restype = wintypes.BOOL

    found: list[tuple[int, int, str]] = []

    def callback(hwnd, _lparam):
        if not user.IsWindowVisible(hwnd):
            return True
        length = int(user.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if "deepseek harness" not in title.lower():
            return True
        rect = wintypes.RECT()
        if not user.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        found.append((int(hwnd), area, title))
        return True

    user.EnumWindows(_WINDOW_PROC(callback), 0)
    found.sort(key=lambda item: item[1], reverse=True)
    return [(hwnd, title) for hwnd, _area, title in found]


def focus_harness_window() -> bool:
    """Bring the main Harness desktop window to the foreground."""

    windows = find_harness_windows()
    if not windows:
        return False
    hwnd = windows[0][0]
    user = _user32()
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.ShowWindow.restype = wintypes.BOOL
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.SetForegroundWindow.restype = wintypes.BOOL
    user.BringWindowToTop.argtypes = [wintypes.HWND]
    user.BringWindowToTop.restype = wintypes.BOOL
    user.ShowWindow(hwnd, 9)  # SW_RESTORE
    user.BringWindowToTop(hwnd)
    return bool(user.SetForegroundWindow(hwnd))


def _event_time(item: dict) -> float:
    value = item.get("time")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return 0.0


def _user_text(message: dict) -> str:
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def _compact(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _friendly_tool(name: str) -> str:
    lowered = name.lower()
    if lowered in {"pwsh", "bash", "shell", "command"}:
        return "命令"
    if lowered == "read":
        return "读取文件"
    if lowered in {"write", "edit"}:
        return "修改文件"
    if lowered in {"glob", "grep"}:
        return "搜索文件"
    if lowered == "web_search":
        return "联网搜索"
    if lowered == "web_fetch":
        return "读取网页"
    if lowered in {"subagent", "subagent_fork"}:
        return "子代理"
    if lowered == "workflow":
        return "工作流"
    if lowered == "ask_user_question":
        return "等待你的回答"
    if lowered == "present":
        return "交付文件"
    return name


def _activity(parsed: _ParsedSession, now_ms: float) -> str:
    if parsed.approval_pending:
        return "等待批准"
    if parsed.open_tools:
        names = parsed.open_tools
        if "ask_user_question" in names:
            return "等待你的回答"
        first = _friendly_tool(names[0])
        if len(names) > 1:
            return f"正在执行{first}等 {len(names)} 项"
        return f"正在执行{first}"
    idle_ms = now_ms - parsed.last_event_at
    if idle_ms > 120_000:
        return "等待中"
    mapping = {
        "turn/start": "开始处理",
        "step/start": "思考中",
        "assistant/message": "生成回复",
        "assistant/attempt": "生成回复",
        "tool/result": "继续处理",
        "job/registered": "运行命令",
        "job/settled": "处理结果",
        "command/run": "执行命令",
        "user/message": "接收指令",
        "session-title": "整理标题",
    }
    return mapping.get(parsed.last_event_type, "处理中")


class DshMonitor:
    """Poll DSH session logs and report sessions with an open turn."""

    def __init__(
        self,
        dsh_home: Optional[Path] = None,
        *,
        max_age_seconds: float = 7 * 24 * 60 * 60,
        stale_turn_seconds: float = STALE_TURN_SECONDS,
    ) -> None:
        home = dsh_home
        if home is None:
            configured = os.environ.get("DSH_HOME")
            home = Path(configured) if configured else Path.home() / ".dsh"
        self.sessions_root = Path(home) / "sessions"
        self.max_age_seconds = max_age_seconds
        self.stale_turn_seconds = stale_turn_seconds
        self._cache: dict[str, tuple[tuple[int, int], _ParsedSession]] = {}
        self._files: list[Path] = []
        self._last_scan = 0.0
        self._process_known: Optional[bool] = None
        self._process_checked = 0.0

    def _detect_process(self) -> bool:
        now = time.monotonic()
        if self._process_known is not None and now - self._process_checked < 4.0:
            return self._process_known
        try:
            present = process_running()
        except Exception:
            present = True
        self._process_known = present
        self._process_checked = now
        return present

    def _scan(self) -> list[Path]:
        now_mono = time.monotonic()
        if self._files and now_mono - self._last_scan < SCAN_INTERVAL_SECONDS:
            return self._files
        self._last_scan = now_mono
        if not self.sessions_root.is_dir():
            self._files = []
            return self._files
        cutoff = time.time() - self.max_age_seconds
        candidates: list[tuple[float, Path]] = []
        try:
            for path in self.sessions_root.rglob("session*.jsonl*"):
                if not path.is_file():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime < cutoff:
                    continue
                if "zstd" not in path.name and "jsonl" not in path.name:
                    continue
                candidates.append((stat.st_mtime, path))
        except OSError:
            self._files = []
            return self._files
        candidates.sort(key=lambda item: item[0], reverse=True)
        self._files = [path for _mtime, path in candidates[:MAX_SESSION_FILES]]
        return self._files

    def _load(self, path: Path) -> Optional[_ParsedSession]:
        try:
            stat = path.stat()
        except OSError:
            return None
        signature = (int(stat.st_mtime_ns), int(stat.st_size))
        cached = self._cache.get(str(path))
        if cached is not None and cached[0] == signature:
            return cached[1]
        try:
            data = path.read_bytes()
            if "zstd" in path.name or path.suffix == ".zst":
                if zstd is None:
                    raise RuntimeError("Python 3.14+ is required to read compressed DSH session logs")
                data = zstd.decompress(data)
        except Exception:
            return cached[1] if cached is not None else None

        session_id = path.parent.name
        title = ""
        fallback_title = ""
        cwd = ""
        running = False
        turn = 0
        step = 0
        started_at = 0.0
        last_event_at = 0.0
        last_event_type = ""
        open_tools: dict[str, str] = {}
        approval_pending = False

        for raw_line in data.splitlines():
            if not raw_line.strip():
                continue
            try:
                item = json.loads(raw_line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if not isinstance(kind, str):
                continue
            payload = item.get("data")
            when = _event_time(item)
            if when <= 0 and isinstance(payload, dict):
                for key in ("time", "startedAt", "finishedAt", "at"):
                    value = payload.get(key)
                    if isinstance(value, (int, float)) and value > 0:
                        when = float(value)
                        break
            if when > last_event_at:
                last_event_at = when
            if kind != "session":
                last_event_type = kind

            if kind == "session":
                header_id = item.get("id")
                if isinstance(header_id, str) and header_id:
                    session_id = header_id
                header_cwd = item.get("cwd")
                if isinstance(header_cwd, str):
                    cwd = header_cwd
            elif kind == "session/title":
                if isinstance(payload, dict) and isinstance(payload.get("title"), str):
                    title = payload["title"].strip()
            elif kind == "user/message":
                if isinstance(payload, dict) and not fallback_title:
                    text = _user_text(payload)
                    if text:
                        first_line = text.splitlines()[0].strip()
                        fallback_title = _compact(first_line, 56)
            elif kind == "turn/start":
                running = True
                if isinstance(payload, dict) and isinstance(payload.get("turn"), int):
                    turn = payload["turn"]
                step = 0
                started_at = when
                open_tools.clear()
                approval_pending = False
            elif kind == "turn/end":
                running = False
                open_tools.clear()
                approval_pending = False
            elif kind == "approval/asked":
                approval_pending = True
            elif kind == "approval/decided":
                approval_pending = False
            elif kind == "step/start":
                if isinstance(payload, dict) and isinstance(payload.get("step"), int):
                    step = payload["step"]
            elif kind == "tool/call":
                if isinstance(payload, dict):
                    call_id = payload.get("callId")
                    name = payload.get("name")
                    if isinstance(call_id, str) and isinstance(name, str):
                        open_tools[call_id] = name
                    if isinstance(payload.get("step"), int):
                        step = payload["step"]
            elif kind == "tool/result":
                if isinstance(payload, dict):
                    message = payload.get("message")
                    call_id = payload.get("callId")
                    if not isinstance(call_id, str) and isinstance(message, dict):
                        call_id = message.get("toolCallId")
                    if isinstance(call_id, str):
                        open_tools.pop(call_id, None)
                    if isinstance(payload.get("step"), int):
                        step = payload["step"]

        if not last_event_at:
            last_event_at = stat.st_mtime * 1000.0
        if not title:
            if fallback_title:
                title = fallback_title
            elif cwd:
                title = Path(cwd).name
            else:
                title = session_id
        parsed = _ParsedSession(
            session_id=session_id,
            title=_compact(title, 72),
            cwd=cwd,
            running=running,
            turn=turn,
            step=step,
            started_at=started_at,
            last_event_at=last_event_at,
            last_event_type=last_event_type,
            open_tools=tuple(open_tools.values()),
            approval_pending=approval_pending,
        )
        self._cache[str(path)] = (signature, parsed)
        return parsed

    def snapshot(self) -> HarnessSnapshot:
        try:
            if not self._detect_process():
                return HarnessSnapshot(False, False, ())
            windows = find_harness_windows()
            now_ms = time.time() * 1000.0
            sessions: list[RunningTask] = []
            seen: set[str] = set()
            for path in self._scan():
                parsed = self._load(path)
                if parsed is None or not parsed.running:
                    continue
                if parsed.session_id in seen:
                    continue
                if now_ms - parsed.last_event_at > self.stale_turn_seconds * 1000.0:
                    continue
                seen.add(parsed.session_id)
                sessions.append(
                    RunningTask(
                        session_id=parsed.session_id,
                        title=parsed.title,
                        cwd=parsed.cwd,
                        activity=_activity(parsed, now_ms),
                        turn=parsed.turn,
                        step=parsed.step,
                        started_at=parsed.started_at,
                        last_event_at=parsed.last_event_at,
                        needs_attention=parsed.approval_pending,
                    )
                )
            sessions.sort(key=lambda item: item.last_event_at, reverse=True)
            return HarnessSnapshot(True, bool(windows), tuple(sessions))
        except Exception as exc:  # Keep the overlay alive if a log is unreadable.
            return HarnessSnapshot(False, False, (), str(exc))
