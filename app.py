from __future__ import annotations

import ctypes
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from datetime import date, timedelta
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox
from typing import Optional

from agenda_store import LOG_FILE, app_data_dir, load_state, save_state
from dsh_monitor import DshMonitor, HarnessSnapshot, focus_harness_window

APP_TITLE = "悬浮日程"
MUTEX_NAME = "Local\\DshGlobalAgendaOverlayV1"

BG = "#0d131b"
CARD = "#141d28"
CARD_HOVER = "#1b2635"
ROW_HOVER = "#182230"
BORDER = "#263345"
BORDER_SOFT = "#1c2735"
TEXT = "#eef4ff"
MUTED = "#8e9db3"
DIM = "#5e6d82"
ACCENT = "#5d9cff"
ACCENT_HOVER = "#78adff"
ON_ACCENT = "#07111f"
SUCCESS = "#46d39b"
WARNING = "#f0b35b"
DANGER = "#ff7182"
DANGER_HOVER = "#ff8d9b"
INPUT_BG = "#090f17"

SEVERITY_ORDER = ("high", "medium", "low")
SEVERITY_LABELS = {"high": "高", "medium": "中", "low": "低"}
SEVERITY_COLORS = {"high": "#ff7182", "medium": "#f0b35b", "low": "#5d9cff"}
WEEKDAY_LABELS = ("一", "二", "三", "四", "五", "六", "日")


def enable_dpi_awareness() -> None:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.WinDLL("user32").SetProcessDPIAware()
    except Exception:
        pass


def work_area() -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SystemParametersInfoW.argtypes = [
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(wintypes.RECT),
        wintypes.UINT,
    ]
    user32.SystemParametersInfoW.restype = wintypes.BOOL
    if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
        return (rect.left, rect.top, rect.right, rect.bottom)
    width = user32.GetSystemMetrics(0)
    height = user32.GetSystemMetrics(1)
    return (0, 0, width, height - 48)


def acquire_single_instance() -> Optional[int]:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return 0
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel.CloseHandle(handle)
        return None
    return int(handle)


def focus_existing_instance() -> None:
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowW.restype = wintypes.HWND
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.ShowWindow.restype = wintypes.BOOL
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.SetForegroundWindow.restype = wintypes.BOOL
    hwnd = user.FindWindowW(None, APP_TITLE)
    if hwnd:
        user.ShowWindow(hwnd, 9)
        user.SetForegroundWindow(hwnd)


def apply_window_rounding(root: tk.Misc, rounded: bool = True) -> None:
    try:
        root.update_idletasks()
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetParent.argtypes = [wintypes.HWND]
        user.GetParent.restype = wintypes.HWND
        hwnd = int(root.winfo_id())
        parent = int(user.GetParent(hwnd) or 0)
        targets = [hwnd] + ([parent] if parent and parent != hwnd else [])
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwm.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_int),
            wintypes.DWORD,
        ]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        corner = ctypes.c_int(2 if rounded else 1)  # DWMWCP_ROUND / DWMWCP_DONOTROUND
        dark = ctypes.c_int(1)
        for target in targets:
            dwm.DwmSetWindowAttribute(target, 33, ctypes.byref(corner), ctypes.sizeof(corner))
            dwm.DwmSetWindowAttribute(target, 20, ctypes.byref(dark), ctypes.sizeof(dark))
    except Exception:
        logging.getLogger("agenda").debug("DWM rounding unavailable", exc_info=True)


def setup_logging(debug: bool) -> None:
    app_data_dir().mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = []
    try:
        from logging.handlers import RotatingFileHandler

        handlers.append(RotatingFileHandler(LOG_FILE, maxBytes=512_000, backupCount=2, encoding="utf-8"))
    except OSError:
        pass
    if debug and sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers or [logging.NullHandler()],
    )


def install_exception_hook() -> None:
    def handle(exc_type, exc, tb):
        logging.getLogger("agenda").critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        try:
            messagebox.showerror(APP_TITLE, f"悬浮日程遇到错误：\n{exc}\n\n日志：{LOG_FILE}")
        except Exception:
            pass

    sys.excepthook = handle


def ellipsize(text: str, limit: int) -> str:
    value = " ".join(text.split())
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"


def format_elapsed(milliseconds: float) -> str:
    seconds = max(0, int(milliseconds / 1000))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def rounded_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, radius: float, **kwargs):
    points = [
        x1 + radius,
        y1,
        x2 - radius,
        y1,
        x2,
        y1,
        x2,
        y1 + radius,
        x2,
        y2 - radius,
        x2,
        y2,
        x2 - radius,
        y2,
        x1 + radius,
        y2,
        x1,
        y2,
        x1,
        y2 - radius,
        x1,
        y1 + radius,
        x1,
        y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


class AgendaApp:
    def __init__(self) -> None:
        self.log = logging.getLogger("agenda")
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_TITLE)
        self.root.overrideredirect(True)
        self.root.configure(bg=BORDER)
        self.root.resizable(False, False)
        self.root.attributes("-alpha", 0.985)
        icon_path = Path(__file__).resolve().parent / "assets" / "agenda.ico"
        if icon_path.is_file():
            try:
                self.root.iconbitmap(default=str(icon_path))
            except tk.TclError:
                self.log.debug("Window icon could not be applied", exc_info=True)

        self.dpi = float(self.root.winfo_fpixels("1i"))
        self.scale = max(1.0, self.dpi / 96.0)
        self.root.tk.call("tk", "scaling", self.dpi / 72.0)
        self.family = self._pick_font_family()
        self._make_fonts()

        self.W = self.px(340)
        self.PILL_W = self.px(216)
        self.PILL_H = self.px(46)

        state = load_state()
        self.tasks: list[dict] = list(state["tasks"])
        saved_window = state["window"]
        self.mode: str = saved_window.get("mode", "dock")
        self.collapsed = bool(saved_window.get("collapsed"))
        self._x: Optional[int] = saved_window.get("x")
        self._y: Optional[int] = saved_window.get("y")

        today = date.today()
        self.selected_date: Optional[date] = None
        self.compose_severity = "medium"
        self.calendar_open = False
        self.calendar_year = today.year
        self.calendar_month = today.month
        self.editing_task_id: Optional[str] = None
        self._edit_due: Optional[date] = None
        self._edit_previous: Optional[dict] = None

        self._drag_origin: Optional[tuple[int, int]] = None
        self._pill_press_pos: Optional[tuple[int, int]] = None
        self._pill_drag_origin: Optional[tuple[int, int]] = None
        self._pill_dragging = False
        self._resize_pending = False
        self._last_geometry = ""
        self._last_snapshot_signature = None
        self._latest_snapshot: Optional[HarnessSnapshot] = None
        self._save_error = ""
        self._closing = False

        self.monitor = DshMonitor()
        self.snapshots: "queue.Queue[HarnessSnapshot]" = queue.Queue(maxsize=2)
        self._stop_monitor = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

        self._build_ui()
        self._configure_mode_layout()
        self._refresh_task_ui(scroll_top=True)
        self._render_snapshot(None)
        self._apply_startup_geometry()
        self._start_monitor()
        self.root.after(350, self._poll_snapshots)
        self.root.after(50, lambda: apply_window_rounding(self.root, rounded=self._rounded_mode()))
        self.root.bind("<Escape>", self._on_escape)
        self.root.bind("<Control-q>", lambda _event: self.close())
        self.root.bind("<Control-n>", self._focus_entry)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    # ------------------------------------------------------------------ setup
    def px(self, value: float) -> int:
        return int(round(value * self.scale))

    def _pick_font_family(self) -> str:
        available = set(tkfont.families(self.root))
        for family in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "Arial"):
            if family in available:
                return family
        return "TkDefaultFont"

    def _make_fonts(self) -> None:
        family = self.family

        def font(size: int, weight: str = "normal", overstrike: bool = False):
            return tkfont.Font(self.root, family=family, size=size, weight=weight, overstrike=overstrike)

        self.f_kicker = font(8, "bold")
        self.f_title = font(12, "bold")
        self.f_body = font(10)
        self.f_body_bold = font(10, "bold")
        self.f_task = font(10)
        self.f_task_done = font(10, overstrike=True)
        self.f_small = font(9)
        self.f_small_bold = font(9, "bold")
        self.f_tiny = font(8)
        self.f_tiny_bold = font(8, "bold")
        self.f_icon = font(12)
        self.f_plus = font(15, "bold")
        self.f_logo = font(8, "bold")

    def _flat_button(
        self,
        parent: tk.Misc,
        text: str,
        command,
        *,
        font=None,
        fg: str = MUTED,
        hover_fg: str = TEXT,
        bg: str = BG,
        hover_bg: str = CARD_HOVER,
        active_bg: Optional[str] = None,
        width: Optional[int] = None,
        padx: int = 0,
        pady: int = 0,
        cursor: str = "hand2",
    ) -> tk.Button:
        button = tk.Button(
            parent,
            text=text,
            command=command,
            font=font or self.f_body,
            fg=fg,
            bg=bg,
            activeforeground=hover_fg,
            activebackground=active_bg or hover_bg,
            disabledforeground=DIM,
            bd=0,
            relief="flat",
            overrelief="flat",
            highlightthickness=0,
            padx=padx,
            pady=pady,
            width=width or 0,
            cursor=cursor,
            takefocus=0,
        )
        button.bind("<Enter>", lambda _event: button.configure(bg=hover_bg, fg=hover_fg))
        button.bind("<Leave>", lambda _event: button.configure(bg=bg, fg=fg))
        return button

    def _bind_drag(self, widgets) -> None:
        for widget in widgets:
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._end_drag)

    def _configure_mode_layout(self) -> None:
        if hasattr(self, "header"):
            self.header.configure(cursor="arrow" if self.mode == "dock" else "fleur")
        if not hasattr(self, "list_wrap") or not self.list_wrap.winfo_manager():
            return
        if self.mode == "dock":
            self.list_wrap.pack_configure(fill="both", expand=True)
            self.task_canvas.pack_configure(fill="both", expand=True)
        else:
            self.list_wrap.pack_configure(fill="x", expand=False)
            self.task_canvas.pack_configure(fill="x", expand=False)

    def _rounded_mode(self) -> bool:
        return not (self.mode == "dock" and not self.collapsed)

    def _draw_brand_icon(self, canvas: tk.Canvas, size: int) -> None:
        s = size
        rounded_rect(canvas, s * 0.04, s * 0.12, s * 0.96, s * 0.9, s * 0.24, fill=ACCENT, outline="")
        canvas.create_line(s * 0.22, s * 0.34, s * 0.78, s * 0.34, fill="#a9ccff", width=max(1, s * 0.055))
        canvas.create_line(s * 0.32, s * 0.05, s * 0.32, s * 0.2, fill="#dceaff", width=max(1, s * 0.075), capstyle=tk.ROUND)
        canvas.create_line(s * 0.68, s * 0.05, s * 0.68, s * 0.2, fill="#dceaff", width=max(1, s * 0.075), capstyle=tk.ROUND)
        canvas.create_line(
            s * 0.27,
            s * 0.59,
            s * 0.43,
            s * 0.74,
            s * 0.74,
            s * 0.43,
            fill="#f7fbff",
            width=max(1, s * 0.075),
            capstyle=tk.ROUND,
            joinstyle=tk.ROUND,
        )

    def _build_ui(self) -> None:
        self.shell = tk.Frame(self.root, bg=BG, highlightthickness=1, highlightbackground=BORDER)
        self.shell.pack(fill="both", expand=True)
        self.main = tk.Frame(self.shell, bg=BG)
        self.pill = tk.Frame(self.shell, bg=BG)
        self._build_main()
        self._build_pill()
        if self.collapsed:
            self.pill.pack(fill="both", expand=True)
        else:
            self.main.pack(fill="both", expand=True)

    def _build_main(self) -> None:
        header = tk.Frame(self.main, bg=BG, padx=self.px(13), pady=self.px(10))
        header.pack(fill="x")
        self.header = header

        brand = tk.Frame(header, bg=BG)
        brand.pack(side="left", fill="x", expand=True)
        icon = tk.Canvas(brand, width=self.px(24), height=self.px(24), bg=BG, highlightthickness=0, bd=0)
        icon.pack(side="left", padx=(0, self.px(9)))
        self._draw_brand_icon(icon, self.px(24))

        heading = tk.Frame(brand, bg=BG)
        heading.pack(side="left", anchor="w")
        kicker = tk.Label(
            heading,
            text="GLOBAL AGENDA",
            bg=BG,
            fg=MUTED,
            font=self.f_kicker,
            anchor="w",
            bd=0,
        )
        kicker.pack(anchor="w")
        title = tk.Label(
            heading,
            text=APP_TITLE,
            bg=BG,
            fg=TEXT,
            font=self.f_title,
            anchor="w",
            bd=0,
        )
        title.pack(anchor="w")

        actions = tk.Frame(header, bg=BG)
        actions.pack(side="right", anchor="n", pady=(self.px(1), 0))
        close_btn = self._flat_button(
            actions,
            "×",
            self.close,
            font=self.f_icon,
            fg=MUTED,
            hover_fg=DANGER,
            hover_bg=CARD_HOVER,
            width=2,
        )
        close_btn.pack(side="right", padx=(self.px(1), 0))
        collapse_btn = self._flat_button(
            actions,
            "–",
            self.collapse,
            font=self.f_icon,
            fg=MUTED,
            hover_fg=TEXT,
            hover_bg=CARD_HOVER,
            width=2,
        )
        collapse_btn.pack(side="right")
        self.mode_btn = self._flat_button(
            actions,
            "浮窗" if self.mode == "dock" else "侧栏",
            self.toggle_mode,
            font=self.f_tiny,
            fg=MUTED,
            hover_fg=TEXT,
            bg=CARD,
            hover_bg=CARD_HOVER,
            padx=self.px(5),
            pady=self.px(1),
        )
        self.mode_btn.pack(side="right", padx=(self.px(4), 0), pady=(self.px(3), 0))
        self.badge = tk.Label(
            actions,
            text="0",
            bg=CARD,
            fg=TEXT,
            font=self.f_tiny_bold,
            padx=self.px(7),
            pady=self.px(2),
            bd=0,
        )
        self.badge.pack(side="right", padx=(0, self.px(3)), pady=(self.px(4), 0))
        self.calendar_btn = self._flat_button(
            actions,
            "日历",
            self.toggle_calendar,
            font=self.f_tiny,
            fg=MUTED,
            hover_fg=TEXT,
            bg=CARD,
            hover_bg=CARD_HOVER,
            padx=self.px(5),
            pady=self.px(1),
        )
        self.calendar_btn.pack(side="right", padx=(self.px(3), 0), pady=(self.px(3), 0))

        self._bind_drag([header, brand, icon, heading, kicker, title, self.badge])
        for widget in (header, brand, icon, heading, kicker, title, self.badge):
            widget.bind("<Button-3>", self._show_menu)

        tk.Frame(self.main, bg=BORDER_SOFT, height=1).pack(fill="x", padx=self.px(13))

        status_section = tk.Frame(self.main, bg=BG, padx=self.px(12))
        status_section.pack(fill="x", pady=(self.px(9), self.px(3)))
        status_outer = tk.Frame(status_section, bg=BORDER_SOFT)
        status_outer.pack(fill="x")
        self.status_card = tk.Frame(status_outer, bg=CARD)
        self.status_card.pack(fill="both", expand=True, padx=1, pady=1)

        status_head = tk.Frame(self.status_card, bg=CARD)
        status_head.pack(fill="x", padx=self.px(10), pady=(self.px(8), self.px(2)))
        self.status_dot = tk.Canvas(
            status_head,
            width=self.px(10),
            height=self.px(10),
            bg=CARD,
            highlightthickness=0,
            bd=0,
        )
        self.status_dot.pack(side="left", padx=(0, self.px(6)))
        harness_label = tk.Label(
            status_head,
            text="HARNESS",
            bg=CARD,
            fg=MUTED,
            font=self.f_kicker,
            bd=0,
        )
        harness_label.pack(side="left")
        self.open_harness_btn = self._flat_button(
            status_head,
            "打开",
            self.focus_harness,
            font=self.f_tiny,
            fg=ACCENT,
            hover_fg=ACCENT_HOVER,
            bg=CARD,
            hover_bg=CARD_HOVER,
            padx=self.px(6),
            pady=self.px(1),
        )
        self.open_harness_btn.pack(side="right")
        self.status_summary = tk.Label(
            status_head,
            text="读取中",
            bg=CARD,
            fg=MUTED,
            font=self.f_small_bold,
            bd=0,
        )
        self.status_summary.pack(side="right", padx=(0, self.px(6)))

        self.running_box = tk.Frame(self.status_card, bg=CARD)
        self.running_box.pack(fill="x", padx=self.px(6), pady=(self.px(1), 0))
        self.status_note = tk.Label(
            self.status_card,
            text="",
            bg=CARD,
            fg=MUTED,
            font=self.f_tiny,
            anchor="w",
            bd=0,
        )
        self.status_note.pack(fill="x", padx=self.px(11), pady=(self.px(1), self.px(8)))

        input_section = tk.Frame(self.main, bg=BG, padx=self.px(12))
        input_section.pack(fill="x", pady=(self.px(8), self.px(4)))
        self.input_border = tk.Frame(input_section, bg=BORDER)
        self.input_border.pack(side="left", fill="x", expand=True)
        self.input_inner = tk.Frame(self.input_border, bg=INPUT_BG)
        self.input_inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.draft_var = tk.StringVar()
        vcmd = (self.root.register(self._validate_length), "%P")
        self.entry = tk.Entry(
            self.input_inner,
            textvariable=self.draft_var,
            font=self.f_body,
            fg=TEXT,
            bg=INPUT_BG,
            insertbackground=TEXT,
            selectbackground=ACCENT,
            selectforeground=ON_ACCENT,
            relief="flat",
            bd=0,
            highlightthickness=0,
            validate="key",
            validatecommand=vcmd,
        )
        self.entry.pack(fill="both", expand=True, padx=self.px(9), pady=self.px(8))
        self.placeholder = tk.Label(
            self.input_inner,
            text="添加日程，按 Enter 保存",
            bg=INPUT_BG,
            fg=DIM,
            font=self.f_body,
            bd=0,
            cursor="xterm",
        )
        self.placeholder.place(x=self.px(10), rely=0.5, anchor="w")
        self.placeholder.bind("<Button-1>", lambda _event: self.entry.focus_set())
        self.entry.bind("<Return>", self._add_task)
        self.entry.bind("<FocusIn>", self._on_entry_focus)
        self.entry.bind("<FocusOut>", self._on_entry_blur)
        self.draft_var.trace_add("write", lambda *_args: self._sync_placeholder())
        self.add_btn = self._flat_button(
            input_section,
            "+",
            self._add_task,
            font=self.f_plus,
            fg=ON_ACCENT,
            hover_fg=ON_ACCENT,
            bg=ACCENT,
            hover_bg=ACCENT_HOVER,
            padx=self.px(8),
            pady=self.px(1),
        )
        self.add_btn.pack(side="right", fill="y", padx=(self.px(8), 0))
        self.cancel_edit_btn = self._flat_button(
            input_section,
            "取消",
            self._cancel_edit,
            font=self.f_tiny,
            fg=MUTED,
            hover_fg=TEXT,
            bg=BG,
            hover_bg=CARD_HOVER,
            padx=self.px(5),
            pady=self.px(2),
        )

        compose_row = tk.Frame(self.main, bg=BG, padx=self.px(12))
        compose_row.pack(fill="x", pady=(0, self.px(8)))
        severity_box = tk.Frame(compose_row, bg=BG)
        severity_box.pack(side="left")
        tk.Label(
            severity_box,
            text="优先级",
            bg=BG,
            fg=DIM,
            font=self.f_tiny,
            bd=0,
        ).pack(side="left", padx=(0, self.px(4)))
        self.severity_buttons: dict[str, tk.Label] = {}
        for key in SEVERITY_ORDER:
            button = tk.Label(
                severity_box,
                text=SEVERITY_LABELS[key],
                font=self.f_tiny_bold,
                padx=self.px(6),
                pady=self.px(1),
                bd=0,
                cursor="hand2",
            )
            button.pack(side="left", padx=(0, self.px(3)))
            button.bind("<Button-1>", lambda _event, level=key: self._set_compose_severity(level))
            button.bind("<Enter>", lambda _event, level=key: self._hover_severity(level))
            button.bind("<Leave>", lambda _event: self._render_compose_controls())
            self.severity_buttons[key] = button
        self.date_chip = tk.Label(
            compose_row,
            text="",
            font=self.f_tiny_bold,
            fg=MUTED,
            bg=CARD,
            padx=self.px(7),
            pady=self.px(2),
            bd=0,
            cursor="hand2",
        )
        self.date_chip.pack(side="right")
        self.date_chip.bind("<Button-1>", lambda _event: self.toggle_calendar())
        self.date_chip.bind("<Enter>", lambda _event: self.date_chip.configure(bg=CARD_HOVER))
        self.date_chip.bind("<Leave>", lambda _event: self._render_compose_controls())
        self._render_compose_controls()

        tk.Frame(self.main, bg=BORDER_SOFT, height=1).pack(fill="x", padx=self.px(13))

        self.content_wrap = tk.Frame(self.main, bg=BG)
        self.content_wrap.pack(fill="both", expand=True)

        self.calendar_frame = tk.Frame(self.content_wrap, bg=BG, padx=self.px(10), pady=self.px(2))
        self._build_calendar()

        self.view_bar = tk.Frame(self.content_wrap, bg=BG, padx=self.px(14))
        self.view_label = tk.Label(
            self.view_bar,
            text="全部日程",
            bg=BG,
            fg=MUTED,
            font=self.f_tiny_bold,
            bd=0,
        )
        self.view_label.pack(side="left")
        self.all_btn = self._flat_button(
            self.view_bar,
            "全部",
            self.show_all_tasks,
            font=self.f_tiny,
            fg=ACCENT,
            hover_fg=ACCENT_HOVER,
            bg=BG,
            hover_bg=CARD_HOVER,
            padx=self.px(4),
            pady=0,
        )
        self.view_bar.pack(fill="x", pady=(self.px(5), 0))

        self.list_wrap = tk.Frame(self.content_wrap, bg=BG)
        self.list_wrap.pack(fill="x")
        self.task_canvas = tk.Canvas(
            self.list_wrap,
            bg=BG,
            highlightthickness=0,
            bd=0,
            height=self.px(68),
            yscrollincrement=self.px(20),
        )
        self.task_canvas.pack(fill="x", padx=self.px(8), pady=self.px(4))
        self.task_inner = tk.Frame(self.task_canvas, bg=BG)
        self._task_window = self.task_canvas.create_window((0, 0), window=self.task_inner, anchor="nw")
        self.task_inner.bind("<Configure>", self._on_task_inner_configure)
        self.task_canvas.bind("<Configure>", self._on_task_canvas_configure)
        self.root.bind("<MouseWheel>", self._on_mousewheel)

        tk.Frame(self.main, bg=BORDER_SOFT, height=1).pack(fill="x", padx=self.px(13))

        footer = tk.Frame(self.main, bg=BG, padx=self.px(14), pady=self.px(8))
        footer.pack(fill="x")
        self.footer_count = tk.Label(
            footer,
            text="0 项待办",
            bg=BG,
            fg=MUTED,
            font=self.f_tiny,
            bd=0,
        )
        self.footer_count.pack(side="left")
        self.clear_btn = self._flat_button(
            footer,
            "清除已完成",
            self._clear_done,
            font=self.f_tiny,
            fg=MUTED,
            hover_fg=TEXT,
            bg=BG,
            hover_bg=CARD_HOVER,
            padx=self.px(5),
            pady=self.px(1),
        )
        self.clear_btn.pack(side="right")

        self.menu = tk.Menu(
            self.root,
            tearoff=0,
            bg=CARD,
            fg=TEXT,
            activebackground=ACCENT,
            activeforeground=ON_ACCENT,
            bd=0,
            relief="flat",
            font=self.f_body,
            activeborderwidth=0,
        )
        self.menu.add_command(label="打开 Harness", command=self.focus_harness)
        self.menu.add_command(label="恢复悬浮窗", command=self.toggle_mode)
        self.menu.add_command(label="收起为胶囊", command=self.collapse)
        self.menu.add_command(label="打开日历", command=self.toggle_calendar)
        self.menu.add_separator()
        self.menu.add_command(label="退出悬浮日程", command=self.close)
        self.priority_menu = tk.Menu(
            self.root,
            tearoff=0,
            bg=CARD,
            fg=TEXT,
            activebackground=ACCENT,
            activeforeground=ON_ACCENT,
            bd=0,
            relief="flat",
            font=self.f_body,
            activeborderwidth=0,
        )

    def _build_pill(self) -> None:
        self.pill_inner = tk.Frame(self.pill, bg=BG, padx=self.px(12), pady=self.px(8))
        self.pill_inner.pack(fill="both", expand=True)
        icon = tk.Canvas(
            self.pill_inner,
            width=self.px(18),
            height=self.px(18),
            bg=BG,
            highlightthickness=0,
            bd=0,
        )
        icon.pack(side="left", padx=(0, self.px(7)))
        self._draw_brand_icon(icon, self.px(18))
        label = tk.Label(
            self.pill_inner,
            text=APP_TITLE,
            bg=BG,
            fg=TEXT,
            font=self.f_small_bold,
            bd=0,
        )
        label.pack(side="left")
        self.pill_count = tk.Label(
            self.pill_inner,
            text="0",
            bg=CARD,
            fg=MUTED,
            font=self.f_tiny_bold,
            padx=self.px(6),
            pady=self.px(1),
            bd=0,
        )
        self.pill_count.pack(side="left", padx=(self.px(6), 0))
        self.pill_live = tk.Frame(self.pill_inner, bg=BG)
        self.pill_live.pack(side="right")
        self.pill_live_dot = tk.Canvas(
            self.pill_live,
            width=self.px(8),
            height=self.px(8),
            bg=BG,
            highlightthickness=0,
            bd=0,
        )
        self.pill_live_dot.pack(side="left")
        self.pill_live_label = tk.Label(
            self.pill_live,
            text="",
            bg=BG,
            fg=SUCCESS,
            font=self.f_tiny_bold,
            bd=0,
        )
        self.pill_live_label.pack(side="left", padx=(self.px(4), 0))
        chevron = tk.Label(
            self.pill_inner,
            text="›",
            bg=BG,
            fg=MUTED,
            font=self.f_icon,
            bd=0,
        )
        chevron.pack(side="right", padx=(self.px(6), 0))
        self.pill_chevron = chevron

        for widget in (self.pill, self.pill_inner, icon, label, self.pill_count, self.pill_live, self.pill_live_dot, self.pill_live_label, chevron):
            widget.bind("<ButtonPress-1>", self._pill_press)
            widget.bind("<B1-Motion>", self._pill_drag)
            widget.bind("<ButtonRelease-1>", self._pill_release)
            widget.bind("<Button-3>", self._show_menu)
            if "cursor" in widget.keys():
                widget.configure(cursor="hand2")
        self._draw_pill_status()

    # ---------------------------------------------------------- severity/date
    @staticmethod
    def _task_date(task: dict) -> Optional[date]:
        value = task.get("due")
        if not isinstance(value, str) or len(value) != 10:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    def _short_date(self, value: date) -> str:
        today = date.today()
        delta = (value - today).days
        if delta == 0:
            return "今天"
        if delta == 1:
            return "明天"
        if delta == -1:
            return "昨天"
        if value.year == today.year:
            return f"{value.month}月{value.day}日"
        return f"{value.year}年{value.month}月{value.day}日"

    def _long_date(self, value: date) -> str:
        return f"{self._short_date(value)} · 周{WEEKDAY_LABELS[value.weekday()]}"

    def _compose_date(self) -> Optional[date]:
        if self.editing_task_id is not None:
            return self._edit_due
        return self.selected_date or date.today()

    def _due_text(self, value: Optional[date], done: bool) -> str:
        if value is None:
            return "未安排"
        text = self._short_date(value)
        if not done and value < date.today():
            return f"逾期 · {text}"
        return text

    @staticmethod
    def _severity_of(task: dict) -> str:
        value = task.get("severity")
        return value if value in SEVERITY_LABELS else "medium"

    def _hover_severity(self, level: str) -> None:
        if level == self.compose_severity:
            return
        button = self.severity_buttons.get(level)
        if button is not None:
            button.configure(bg=CARD_HOVER, fg=TEXT)

    def _set_compose_severity(self, level: str) -> None:
        if level not in SEVERITY_COLORS:
            return
        self.compose_severity = level
        self._render_compose_controls()
        self.entry.focus_set()

    def _render_compose_controls(self) -> None:
        if not hasattr(self, "severity_buttons"):
            return
        for level, button in self.severity_buttons.items():
            if level == self.compose_severity:
                button.configure(bg=SEVERITY_COLORS[level], fg=ON_ACCENT)
            else:
                button.configure(bg=CARD, fg=MUTED)
        if not hasattr(self, "date_chip"):
            return
        if self.calendar_open:
            target = self._compose_date()
            text = f"{self._short_date(target) if target else '未安排'} · 收起"
            fg = ACCENT
            bg = CARD_HOVER
        elif self.editing_task_id is not None:
            target = self._edit_due
            text = f"日历 · {self._short_date(target) if target else '未安排'}"
            fg = ACCENT
            bg = CARD
        else:
            target = self.selected_date or date.today()
            text = f"日历 · {self._short_date(target)}"
            fg = ACCENT if self.selected_date else MUTED
            bg = CARD
        self.date_chip.configure(text=text, fg=fg, bg=bg)
        self._sync_placeholder()

    # --------------------------------------------------------------- calendar
    def _build_calendar(self) -> None:
        head = tk.Frame(self.calendar_frame, bg=BG)
        head.pack(fill="x", pady=(self.px(1), self.px(4)))
        prev_btn = self._flat_button(
            head,
            "‹",
            lambda: self._shift_calendar_month(-1),
            font=self.f_icon,
            fg=MUTED,
            hover_fg=TEXT,
            bg=BG,
            hover_bg=CARD_HOVER,
            width=2,
        )
        prev_btn.pack(side="left")
        self.calendar_month_label = tk.Label(
            head,
            text="",
            bg=BG,
            fg=TEXT,
            font=self.f_body_bold,
            bd=0,
        )
        self.calendar_month_label.pack(side="left", fill="x", expand=True, padx=(self.px(3), 0))
        next_btn = self._flat_button(
            head,
            "›",
            lambda: self._shift_calendar_month(1),
            font=self.f_icon,
            fg=MUTED,
            hover_fg=TEXT,
            bg=BG,
            hover_bg=CARD_HOVER,
            width=2,
        )
        next_btn.pack(side="right")
        self.calendar_all_btn = self._flat_button(
            head,
            "全部",
            self.show_all_tasks,
            font=self.f_tiny,
            fg=ACCENT,
            hover_fg=ACCENT_HOVER,
            bg=BG,
            hover_bg=CARD_HOVER,
            padx=self.px(4),
            pady=0,
        )
        self.calendar_all_btn.pack(side="right", padx=(0, self.px(2)))
        self.calendar_today_btn = self._flat_button(
            head,
            "今天",
            self._calendar_today,
            font=self.f_tiny,
            fg=MUTED,
            hover_fg=TEXT,
            bg=BG,
            hover_bg=CARD_HOVER,
            padx=self.px(4),
            pady=0,
        )
        self.calendar_today_btn.pack(side="right", padx=(0, self.px(1)))

        weekdays = tk.Frame(self.calendar_frame, bg=BG)
        weekdays.pack(fill="x", pady=(0, self.px(2)))
        for column, label in enumerate(WEEKDAY_LABELS):
            weekdays.columnconfigure(column, weight=1, uniform="weekday")
            tk.Label(
                weekdays,
                text=label,
                bg=BG,
                fg=DIM,
                font=self.f_tiny,
                bd=0,
            ).grid(row=0, column=column, sticky="ew")

        self.calendar_grid = tk.Frame(self.calendar_frame, bg=BG)
        self.calendar_grid.pack(fill="x")
        for column in range(7):
            self.calendar_grid.columnconfigure(column, weight=1, uniform="calendar")

    def _render_calendar(self) -> None:
        for child in self.calendar_grid.winfo_children():
            child.destroy()
        self.calendar_month_label.configure(text=f"{self.calendar_year}年{self.calendar_month}月")
        first = date(self.calendar_year, self.calendar_month, 1)
        start = first - timedelta(days=first.weekday())
        today = date.today()
        for week in range(6):
            for column in range(7):
                day = start + timedelta(days=week * 7 + column)
                in_month = day.year == self.calendar_year and day.month == self.calendar_month
                marked_date = self._edit_due if self.editing_task_id is not None else self.selected_date
                selected = marked_date == day
                is_today = day == today
                if selected:
                    bg = ACCENT
                    fg = ON_ACCENT
                elif is_today:
                    bg = CARD
                    fg = ACCENT
                else:
                    bg = BG
                    fg = TEXT if in_month else DIM
                cell = tk.Frame(
                    self.calendar_grid,
                    bg=bg,
                    width=self.px(42),
                    height=self.px(33),
                    highlightthickness=1 if is_today and not selected else 0,
                    highlightbackground=ACCENT,
                    bd=0,
                    cursor="hand2",
                )
                cell.grid(row=week, column=column, padx=1, pady=1, sticky="nsew")
                cell.grid_propagate(False)
                number = tk.Label(
                    cell,
                    text=str(day.day),
                    bg=bg,
                    fg=fg,
                    font=self.f_small_bold if (selected or is_today) else self.f_small,
                    bd=0,
                    cursor="hand2",
                )
                number.place(relx=0.5, rely=0.36, anchor="center")
                dot = tk.Canvas(
                    cell,
                    width=self.px(20),
                    height=self.px(6),
                    bg=bg,
                    highlightthickness=0,
                    bd=0,
                    cursor="hand2",
                )
                dot.place(relx=0.5, rely=0.76, anchor="center")
                tasks = [task for task in self.tasks if task.get("due") == day.isoformat()]
                dot_size = self.px(5)
                gap = self.px(2)
                visible = tasks[:3]
                total_width = len(visible) * dot_size + max(0, len(visible) - 1) * gap
                x = (self.px(20) - total_width) / 2
                for task in visible:
                    color = ON_ACCENT if selected else SEVERITY_COLORS[self._severity_of(task)]
                    if task.get("done"):
                        color = DIM
                    dot.create_oval(x, 1, x + dot_size, 1 + dot_size, fill=color, outline="")
                    x += dot_size + gap
                for widget in (cell, number, dot):
                    widget.bind("<Button-1>", lambda _event, value=day: self._select_calendar_date(value))
                if not selected:
                    def enter(_event, c=cell, n=number, d=dot):
                        c.configure(bg=CARD_HOVER)
                        n.configure(bg=CARD_HOVER)
                        d.configure(bg=CARD_HOVER)

                    def leave(_event, c=cell, n=number, d=dot, base=bg):
                        c.configure(bg=base)
                        n.configure(bg=base)
                        d.configure(bg=base)

                    for widget in (cell, number, dot):
                        widget.bind("<Enter>", enter)
                        widget.bind("<Leave>", leave)

    def _shift_calendar_month(self, delta: int) -> None:
        month = self.calendar_month + delta
        year = self.calendar_year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        self.calendar_year, self.calendar_month = year, month
        self._render_calendar()

    def _calendar_today(self) -> None:
        self._select_calendar_date(date.today())

    def _select_calendar_date(self, value: date) -> None:
        if self.editing_task_id is not None:
            self._edit_due = value
            if self.calendar_open:
                self._hide_calendar()
            else:
                self._render_compose_controls()
            self.entry.focus_set()
            return
        self.selected_date = value
        if self.calendar_open:
            self._hide_calendar(resize=False)
        self._render_compose_controls()
        self._refresh_task_ui(scroll_top=True)
        self.entry.focus_set()

    def show_all_tasks(self) -> None:
        self.selected_date = None
        if self.calendar_open:
            self._hide_calendar(resize=False)
        self._render_compose_controls()
        self._refresh_task_ui(scroll_top=True)
        self.entry.focus_set()

    def toggle_calendar(self) -> None:
        if self.calendar_open:
            self._hide_calendar()
        else:
            self._show_calendar()

    def _show_calendar(self) -> None:
        if self.calendar_open:
            return
        if self.collapsed:
            self.expand()
        target = self._compose_date() or date.today()
        self.calendar_year, self.calendar_month = target.year, target.month
        self.calendar_open = True
        self.view_bar.pack_forget()
        self.list_wrap.pack_forget()
        self.calendar_frame.pack(fill="x")
        self.calendar_btn.configure(text="收起")
        self._render_calendar()
        self._render_compose_controls()
        self.root.update_idletasks()
        self._resize_to_fit(force=True)

    def _hide_calendar(self, resize: bool = True) -> None:
        if not self.calendar_open:
            return
        self.calendar_frame.pack_forget()
        self.view_bar.pack(fill="x", pady=(self.px(5), 0))
        self.list_wrap.pack(fill="x")
        self.calendar_open = False
        self.calendar_btn.configure(text="日历")
        self._render_compose_controls()
        self._update_view_bar()
        self._configure_mode_layout()
        if resize:
            self.root.update_idletasks()
            self._resize_to_fit(force=True)

    # ------------------------------------------------------------------- fonts
    def _validate_length(self, proposed: str) -> bool:
        return len(proposed) <= 240

    # ------------------------------------------------------------------ status
    def _set_status_dot(self, color: str) -> None:
        self.status_dot.delete("all")
        size = self.px(10)
        self.status_dot.create_oval(0, 0, size, size, fill=color, outline="")

    def _draw_pill_status(self) -> None:
        self.pill_live_dot.delete("all")
        sessions = self._latest_snapshot.sessions if self._latest_snapshot else ()
        waiting = sum(1 for task in sessions if task.needs_attention)
        if sessions:
            color = WARNING if waiting else SUCCESS
            self.pill_live_label.configure(text=str(len(sessions)), fg=color)
            self.pill_live.pack(side="right", padx=(0, self.px(4)))
        else:
            color = DIM
            self.pill_live_label.configure(text="")
            self.pill_live.pack_forget()
        if not self.pill_chevron.winfo_manager():
            self.pill_chevron.pack(side="right", padx=(self.px(6), 0))
        size = self.px(8)
        self.pill_live_dot.create_oval(0, 0, size, size, fill=color, outline="")

    def _render_snapshot(self, snapshot: Optional[HarnessSnapshot]) -> None:
        if self._closing:
            return
        now_ms = time.time() * 1000.0
        if snapshot is None:
            signature = "initial"
        else:
            signature = (
                snapshot.process_running,
                snapshot.window_present,
                snapshot.error,
                tuple(
                    (
                        task.session_id,
                        task.title,
                        task.activity,
                        task.turn,
                        task.step,
                        int(max(0.0, now_ms - task.started_at) / 5000) if task.started_at else 0,
                        task.needs_attention,
                    )
                    for task in snapshot.sessions
                ),
            )
        if snapshot is None:
            for child in self.running_box.winfo_children():
                child.destroy()
            self.status_summary.configure(text="读取中", fg=MUTED)
            self._set_status_dot(MUTED)
            self.open_harness_btn.configure(state="disabled")
            self.status_note.configure(text="正在读取 Harness 状态", fg=MUTED)
            if not self.status_note.winfo_manager():
                self.status_note.pack(fill="x", padx=self.px(11), pady=(self.px(1), self.px(8)))
            self._draw_pill_status()
            return

        if signature == self._last_snapshot_signature:
            return
        self._last_snapshot_signature = signature
        self._latest_snapshot = snapshot

        for child in self.running_box.winfo_children():
            child.destroy()

        sessions = snapshot.sessions
        waiting = [task for task in sessions if task.needs_attention]
        process_running = bool(snapshot.process_running)
        window_present = bool(snapshot.window_present)
        error = snapshot.error

        if error:
            summary = "读取失败"
            summary_color = DANGER
            dot_color = DANGER
            note = "无法读取 DSH 会话日志"
        elif not process_running:
            summary = "未启动"
            summary_color = MUTED
            dot_color = DIM
            note = "DeepSeek Harness 未启动"
        elif sessions:
            if waiting:
                summary = f"{len(waiting)} 个待授权"
                summary_color = WARNING
                dot_color = WARNING
            else:
                summary = f"{len(sessions)} 个运行中"
                summary_color = SUCCESS
                dot_color = SUCCESS
            note = ""
        else:
            summary = "空闲"
            summary_color = MUTED
            dot_color = MUTED
            note = "当前没有运行中的任务"

        self.status_summary.configure(text=summary, fg=summary_color)
        self._set_status_dot(dot_color)
        if window_present:
            self.open_harness_btn.configure(state="normal", fg=ACCENT)
        else:
            self.open_harness_btn.configure(state="disabled")

        if sessions:
            ordered = sorted(sessions, key=lambda task: (not task.needs_attention, -task.last_event_at))
            max_rows = 3
            for task in ordered[:max_rows]:
                self._build_running_row(task, now_ms, window_present)
            remaining = len(ordered) - max_rows
            if remaining > 0:
                more = tk.Label(
                    self.running_box,
                    text=f"+{remaining} 个运行中的任务",
                    bg=CARD,
                    fg=MUTED,
                    font=self.f_tiny,
                    anchor="w",
                    bd=0,
                )
                more.pack(fill="x", padx=self.px(5), pady=(self.px(1), self.px(3)))
            self.status_note.pack_forget()
        else:
            self.status_note.pack(fill="x", padx=self.px(11), pady=(self.px(1), self.px(8)))
            self.status_note.configure(text=note, fg=DANGER if error else MUTED)

        self._update_counts()
        self._draw_pill_status()
        if self.collapsed:
            self._resize_to_fit()
        else:
            self._schedule_resize()

    def _build_running_row(self, task, now_ms: float, window_present: bool) -> None:
        row = tk.Frame(self.running_box, bg=CARD, cursor="hand2" if window_present else "arrow")
        row.pack(fill="x", padx=self.px(4), pady=self.px(2))
        inner = tk.Frame(row, bg=CARD, padx=self.px(6), pady=self.px(5))
        inner.pack(fill="x")
        top = tk.Frame(inner, bg=CARD)
        top.pack(fill="x")
        needs_attention = bool(task.needs_attention)
        row_dot = tk.Canvas(
            top,
            width=self.px(7),
            height=self.px(7),
            bg=CARD,
            highlightthickness=0,
            bd=0,
        )
        row_dot.pack(side="left", padx=(0, self.px(6)), pady=(self.px(4), 0))
        dot_size = self.px(7)
        row_dot.create_oval(
            0,
            0,
            dot_size,
            dot_size,
            fill=WARNING if needs_attention else SUCCESS,
            outline="",
        )
        title = tk.Label(
            top,
            text=ellipsize(task.title or task.session_id, 20),
            bg=CARD,
            fg=TEXT,
            font=self.f_small_bold,
            anchor="w",
            bd=0,
        )
        title.pack(side="left", fill="x", expand=True)
        elapsed = tk.Label(
            top,
            text=format_elapsed(now_ms - task.started_at) if task.started_at else "",
            bg=CARD,
            fg=MUTED,
            font=self.f_tiny,
            bd=0,
        )
        elapsed.pack(side="right", padx=(self.px(8), 0))
        detail = tk.Frame(inner, bg=CARD)
        detail.pack(fill="x", pady=(self.px(1), 0))
        activity = tk.Label(
            detail,
            text=task.activity,
            bg=CARD,
            fg=WARNING if needs_attention else MUTED,
            font=self.f_tiny_bold if needs_attention else self.f_tiny,
            anchor="w",
            bd=0,
        )
        activity.pack(side="left")
        step_text = f"第 {task.turn} 轮" if task.turn else ""
        step = tk.Label(
            detail,
            text=step_text,
            bg=CARD,
            fg=DIM,
            font=self.f_tiny,
            bd=0,
        )
        step.pack(side="right")
        children = [row, inner, top, row_dot, title, elapsed, detail, activity, step]
        if window_present:
            for widget in children:
                widget.bind("<Button-1>", lambda _event: self.focus_harness())
            self._bind_hover(children, CARD, CARD_HOVER)
        else:
            for widget in children:
                widget.configure(cursor="arrow")

    def _bind_hover(self, widgets, normal: str, hover: str) -> None:
        def enter(_event):
            for widget in widgets:
                if "bg" in widget.keys():
                    widget.configure(bg=hover)

        def leave(_event):
            for widget in widgets:
                if "bg" in widget.keys():
                    widget.configure(bg=normal)

        for widget in widgets:
            widget.bind("<Enter>", enter, add="+")
            widget.bind("<Leave>", leave, add="+")

    # -------------------------------------------------------------------- data
    def _tasks_on(self, value: date) -> list[dict]:
        due = value.isoformat()
        return [task for task in self.tasks if task.get("due") == due]

    def _focus_date(self) -> date:
        return self.selected_date or date.today()

    def _open_count_on(self, value: date) -> int:
        return sum(1 for task in self._tasks_on(value) if not task.get("done"))

    def _update_counts(self) -> None:
        self.badge.configure(text=str(self._open_count_on(self._focus_date())))
        self.pill_count.configure(text=str(self._open_count_on(date.today())))

    def _visible_tasks(self) -> list[dict]:
        if self.selected_date is None:
            return self.tasks
        return self._tasks_on(self.selected_date)

    def _window_state(self) -> dict:
        return {"x": self._x, "y": self._y, "collapsed": self.collapsed, "mode": self.mode}

    def _save_tasks(self) -> None:
        try:
            save_state(self.tasks, self._window_state())
            self._save_error = ""
        except OSError as exc:
            self._save_error = str(exc)
            self.log.exception("Could not save agenda state")
            self._update_footer()

    def _save_window(self) -> None:
        try:
            save_state(self.tasks, self._window_state())
            self._save_error = ""
        except OSError as exc:
            self._save_error = str(exc)
            self.log.exception("Could not save window state")

    def _refresh_task_ui(self, *, scroll_top: bool = False) -> None:
        old_pos = self.task_canvas.yview()[0] if self.task_canvas.winfo_exists() else 0.0
        visible = self._visible_tasks()
        for child in self.task_inner.winfo_children():
            child.destroy()
        if not visible:
            self._build_empty_state(filtered=self.selected_date is not None)
        else:
            for task in visible:
                self._build_task_row(task)
        self.task_inner.update_idletasks()
        content_height = max(self.task_inner.winfo_reqheight(), 1)
        work = work_area()
        max_height = min(self.px(300), int((work[3] - work[1]) * 0.36))
        target_height = min(max(content_height, self.px(46)), max_height)
        self.task_canvas.configure(height=target_height)
        self.task_canvas.configure(scrollregion=(0, 0, max(self.task_canvas.winfo_width(), 1), content_height))
        if scroll_top:
            self.task_canvas.yview_moveto(0.0)
        else:
            self.task_canvas.yview_moveto(old_pos)
        self._update_counts()
        self._update_view_bar()
        self._update_footer()
        self._schedule_resize()

    def _build_empty_state(self, *, filtered: bool = False) -> None:
        empty = tk.Frame(self.task_inner, bg=BG, pady=self.px(12))
        empty.pack(fill="x")
        icon = tk.Canvas(empty, width=self.px(34), height=self.px(34), bg=BG, highlightthickness=0, bd=0)
        icon.pack()
        size = self.px(34)
        icon.create_oval(
            size * 0.14,
            size * 0.14,
            size * 0.86,
            size * 0.86,
            outline=BORDER,
            width=max(1, self.px(1)),
            dash=(2, 3),
        )
        icon.create_line(
            size * 0.5,
            size * 0.34,
            size * 0.5,
            size * 0.66,
            fill=MUTED,
            width=max(1, self.px(1)),
            capstyle=tk.ROUND,
        )
        icon.create_line(
            size * 0.34,
            size * 0.5,
            size * 0.66,
            size * 0.5,
            fill=MUTED,
            width=max(1, self.px(1)),
            capstyle=tk.ROUND,
        )
        tk.Label(
            empty,
            text=f"{self._short_date(self.selected_date)}还没有日程" if filtered and self.selected_date else "还没有日程",
            bg=BG,
            fg=TEXT,
            font=self.f_body_bold,
            bd=0,
        ).pack(pady=(self.px(3), 0))
        hint = "在上方输入，按 Enter 添加"
        if filtered and self.tasks:
            hint = f"还有 {len(self.tasks)} 项其他日程，点“全部”查看"
        tk.Label(
            empty,
            text=hint,
            bg=BG,
            fg=MUTED,
            font=self.f_tiny,
            bd=0,
        ).pack(pady=(self.px(1), 0))

    def _build_task_row(self, task: dict) -> None:
        done = bool(task.get("done"))
        editing = task["id"] == self.editing_task_id
        severity = self._severity_of(task)
        severity_color = BORDER if done else SEVERITY_COLORS[severity]
        due = self._task_date(task)
        row = tk.Frame(self.task_inner, bg=BG, cursor="hand2")
        row.pack(fill="x", pady=self.px(1))
        bar = tk.Frame(row, bg=severity_color, width=self.px(3), cursor="hand2")
        bar.pack(side="left", fill="y")
        inner = tk.Frame(row, bg=BG, padx=self.px(6), pady=self.px(5), cursor="hand2")
        inner.pack(side="left", fill="x", expand=True)

        check_size = self.px(18)
        check = tk.Canvas(
            inner,
            width=check_size,
            height=check_size,
            bg=BG,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        check.pack(side="left", padx=(0, self.px(8)), anchor="n", pady=(self.px(1), 0))
        pad = check_size * 0.12
        if done:
            check.create_oval(pad, pad, check_size - pad, check_size - pad, fill=ACCENT, outline=ACCENT)
            check.create_line(
                check_size * 0.27,
                check_size * 0.52,
                check_size * 0.45,
                check_size * 0.69,
                check_size * 0.75,
                check_size * 0.34,
                fill=ON_ACCENT,
                width=max(1, self.px(1.8)),
                capstyle=tk.ROUND,
                joinstyle=tk.ROUND,
            )
        else:
            check.create_oval(
                pad,
                pad,
                check_size - pad,
                check_size - pad,
                outline=BORDER,
                width=max(1, self.px(1)),
            )

        text_col = tk.Frame(inner, bg=BG, cursor="hand2")
        text_col.pack(side="left", fill="x", expand=True)
        text = tk.Label(
            text_col,
            text=task.get("text", ""),
            bg=BG,
            fg=MUTED if done else TEXT,
            font=self.f_task_done if done else self.f_task,
            anchor="w",
            justify="left",
            wraplength=self.W - self.px(118),
            bd=0,
            cursor="hand2",
        )
        text.pack(fill="x", anchor="w")

        meta = tk.Frame(text_col, bg=BG)
        meta.pack(fill="x", pady=(self.px(1), 0))
        severity_label = tk.Label(
            meta,
            text=f"{SEVERITY_LABELS[severity]} ▾",
            bg=CARD,
            fg=DIM if done else severity_color,
            font=self.f_tiny_bold,
            padx=self.px(5),
            pady=self.px(1),
            bd=0,
            cursor="hand2",
        )
        severity_label.pack(side="left")
        separator = tk.Label(
            meta,
            text=" · ",
            bg=BG,
            fg=DIM,
            font=self.f_tiny,
            bd=0,
        )
        separator.pack(side="left")
        overdue = due is not None and not done and due < date.today()
        date_label = tk.Label(
            meta,
            text=self._due_text(due, done),
            bg=BG,
            fg=DANGER if overdue else (DIM if done else MUTED),
            font=self.f_tiny,
            bd=0,
        )
        date_label.pack(side="left")

        delete = tk.Button(
            inner,
            text="×",
            command=lambda task_id=task["id"]: self._delete_task(task_id),
            font=self.f_small,
            fg=DIM,
            bg=BG,
            activeforeground=DANGER,
            activebackground=ROW_HOVER,
            bd=0,
            relief="flat",
            overrelief="flat",
            highlightthickness=0,
            padx=self.px(5),
            pady=0,
            cursor="hand2",
            takefocus=0,
        )
        delete.pack(side="right", anchor="n", padx=(self.px(4), 0), pady=(self.px(1), 0))
        edit = tk.Button(
            inner,
            text="编辑",
            command=lambda task_id=task["id"]: self._begin_edit(task_id),
            font=self.f_tiny,
            fg=ACCENT if editing else DIM,
            bg=BG,
            activeforeground=ACCENT,
            activebackground=ROW_HOVER,
            bd=0,
            relief="flat",
            overrelief="flat",
            highlightthickness=0,
            padx=self.px(3),
            pady=0,
            cursor="hand2",
            takefocus=0,
        )
        edit.pack(side="right", anchor="n", pady=(self.px(1), 0))

        widgets = [row, inner, check, text_col, text, meta, severity_label, separator, date_label, edit, delete]
        for widget in (row, bar, inner, check, text_col, text):
            widget.bind("<Button-1>", lambda _event, task_id=task["id"]: self._toggle_task(task_id))
        severity_label.bind("<Button-1>", lambda event, task_id=task["id"]: self._show_priority_menu(event, task_id))
        for widget in widgets:
            widget.bind("<Enter>", lambda _event, ws=widgets: self._paint_task_row(ws, ROW_HOVER), add="+")
            widget.bind("<Leave>", lambda _event, ws=widgets: self._paint_task_row(ws, BG), add="+")
        edit.bind("<Enter>", lambda _event: edit.configure(fg=ACCENT), add="+")
        edit.bind("<Leave>", lambda _event: edit.configure(fg=ACCENT if editing else DIM), add="+")
        delete.bind("<Enter>", lambda _event: delete.configure(fg=DANGER), add="+")
        delete.bind("<Leave>", lambda _event: delete.configure(fg=DIM), add="+")

    def _show_priority_menu(self, event, task_id: str) -> str:
        current = "medium"
        for task in self.tasks:
            if task["id"] == task_id:
                current = self._severity_of(task)
                break
        self.priority_menu.delete(0, "end")
        self.priority_menu.add_command(
            label=f"设置优先级 · 当前 {SEVERITY_LABELS[current]}",
            state="disabled",
        )
        self.priority_menu.add_separator()
        for offset, level in enumerate(SEVERITY_ORDER):
            label = f"{'✓ ' if level == current else '   '}{SEVERITY_LABELS[level]}"
            self.priority_menu.add_command(
                label=label,
                command=lambda value=level: self._set_task_severity(task_id, value),
            )
            self.priority_menu.entryconfigure(
                2 + offset,
                foreground=SEVERITY_COLORS[level],
            )
        try:
            self.priority_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.priority_menu.grab_release()
        return "break"

    def _set_task_severity(self, task_id: str, level: str) -> None:
        if level not in SEVERITY_COLORS:
            return
        changed = False
        for task in self.tasks:
            if task["id"] != task_id:
                continue
            if self._severity_of(task) != level:
                task["severity"] = level
                changed = True
            break
        if not changed:
            return
        self._refresh_task_ui(scroll_top=False)
        self._save_tasks()

    @staticmethod
    def _paint_task_row(widgets, color: str) -> None:
        for widget in widgets:
            if "bg" in widget.keys():
                widget.configure(bg=color)

    def _update_footer(self) -> None:
        focus = self._focus_date()
        day_tasks = self._tasks_on(focus)
        open_count = sum(1 for task in day_tasks if not task.get("done"))
        done_count = sum(1 for task in day_tasks if task.get("done"))
        text = f"{self._short_date(focus)} · {open_count} 项待办"
        if done_count:
            text += f" · {done_count} 项已完成"
        if self._save_error:
            text = "保存失败"
            self.footer_count.configure(fg=DANGER)
        else:
            self.footer_count.configure(fg=MUTED)
        self.footer_count.configure(text=text)
        if done_count:
            if not self.clear_btn.winfo_manager():
                self.clear_btn.pack(side="right")
        elif self.clear_btn.winfo_manager():
            self.clear_btn.pack_forget()

    def _update_view_bar(self) -> None:
        text = self._long_date(self.selected_date) if self.selected_date is not None else "全部日程"
        if self.editing_task_id is not None:
            text = f"编辑中 · {text}"
        self.view_label.configure(text=text, fg=TEXT if self.editing_task_id or self.selected_date is not None else MUTED)
        if self.selected_date is not None:
            if not self.all_btn.winfo_manager():
                self.all_btn.pack(side="right")
        else:
            if self.all_btn.winfo_manager():
                self.all_btn.pack_forget()

    # ------------------------------------------------------------------- input
    def _on_entry_focus(self, _event=None) -> None:
        self.input_border.configure(bg=ACCENT)
        self._sync_placeholder()

    def _on_entry_blur(self, _event=None) -> None:
        self.input_border.configure(bg=ACCENT if self.editing_task_id else BORDER)
        self._sync_placeholder()

    def _sync_placeholder(self) -> None:
        has_focus = self.root.focus_get() == self.entry
        if self.editing_task_id is not None:
            text = "修改日程，Enter 保存，Esc 取消"
        else:
            target = self.selected_date or date.today()
            if target == date.today():
                text = "添加今天的日程"
            else:
                text = f"添加 {self._short_date(target)} 的日程"
        self.placeholder.configure(text=text)
        if not self.draft_var.get() and not has_focus:
            self.placeholder.place(x=self.px(10), rely=0.5, anchor="w")
        else:
            self.placeholder.place_forget()

    def _focus_entry(self, _event=None) -> str:
        self.entry.focus_set()
        return "break"

    def _add_task(self, _event=None) -> str:
        if self.editing_task_id is not None:
            return self._save_edit()
        text = " ".join(self.draft_var.get().replace("\r", " ").replace("\n", " ").split())[:240]
        if not text:
            return "break"
        if self.calendar_open:
            self._hide_calendar(resize=False)
        due = self._compose_date() or date.today()
        task = {
            "id": f"task-{int(time.time() * 1000):x}-{os.urandom(4).hex()}",
            "text": text,
            "done": False,
            "created_at": time.time(),
            "severity": self.compose_severity,
            "due": due.isoformat(),
        }
        self.tasks.insert(0, task)
        self.draft_var.set("")
        self._refresh_task_ui(scroll_top=True)
        self._save_tasks()
        self.entry.focus_set()
        return "break"

    def _begin_edit(self, task_id: str) -> None:
        if self.editing_task_id is not None:
            if self.editing_task_id == task_id:
                return
            self._save_edit()
            if self.editing_task_id is not None:
                return
        task = next((item for item in self.tasks if item["id"] == task_id), None)
        if task is None:
            return
        if self.calendar_open:
            self._hide_calendar(resize=False)
        self._edit_previous = {
            "draft": self.draft_var.get(),
            "severity": self.compose_severity,
        }
        self.editing_task_id = task_id
        self._edit_due = self._task_date(task)
        self.compose_severity = self._severity_of(task)
        self.draft_var.set(task.get("text", ""))
        self.add_btn.configure(text="保存", font=self.f_tiny_bold, padx=self.px(8))
        if not self.cancel_edit_btn.winfo_manager():
            self.cancel_edit_btn.pack(side="right", padx=(self.px(4), 0))
        self._render_compose_controls()
        self._refresh_task_ui(scroll_top=False)
        self.entry.focus_set()
        self.entry.icursor("end")

    def _save_edit(self) -> str:
        task_id = self.editing_task_id
        if task_id is None:
            return "break"
        text = " ".join(self.draft_var.get().replace("\r", " ").replace("\n", " ").split())[:240]
        if not text:
            return "break"
        task = next((item for item in self.tasks if item["id"] == task_id), None)
        if task is not None:
            task["text"] = text
            task["severity"] = self.compose_severity
            task["due"] = self._edit_due.isoformat() if self._edit_due is not None else None
        self._finish_edit(restore_draft=True)
        self._refresh_task_ui(scroll_top=False)
        self._save_tasks()
        self.entry.focus_set()
        return "break"

    def _cancel_edit(self) -> None:
        if self.editing_task_id is None:
            return
        self._finish_edit(restore_draft=True)
        self._refresh_task_ui(scroll_top=False)
        self.entry.focus_set()

    def _finish_edit(self, *, restore_draft: bool) -> None:
        previous = self._edit_previous or {}
        self.editing_task_id = None
        self._edit_due = None
        self._edit_previous = None
        self.add_btn.configure(text="+", font=self.f_plus, padx=self.px(8))
        if self.cancel_edit_btn.winfo_manager():
            self.cancel_edit_btn.pack_forget()
        self.draft_var.set(previous.get("draft", "") if restore_draft else "")
        self.compose_severity = previous.get("severity", self.compose_severity)
        self._render_compose_controls()

    def _toggle_task(self, task_id: str) -> None:
        for task in self.tasks:
            if task["id"] == task_id:
                task["done"] = not bool(task.get("done"))
                break
        self._refresh_task_ui(scroll_top=False)
        self._save_tasks()

    def _delete_task(self, task_id: str) -> None:
        if task_id == self.editing_task_id:
            self._finish_edit(restore_draft=False)
        self.tasks = [task for task in self.tasks if task["id"] != task_id]
        self._refresh_task_ui(scroll_top=False)
        self._save_tasks()

    def _clear_done(self) -> None:
        done_ids = {task["id"] for task in self._tasks_on(self._focus_date()) if task.get("done")}
        if self.editing_task_id in done_ids:
            self._finish_edit(restore_draft=False)
        self.tasks = [task for task in self.tasks if task["id"] not in done_ids]
        self._refresh_task_ui(scroll_top=False)
        self._save_tasks()

    # ------------------------------------------------------------------ window
    def _pill_press(self, event) -> None:
        if not self.collapsed:
            return
        self._pill_press_pos = (event.x_root, event.y_root)
        self._pill_drag_origin = (
            event.x_root - self.root.winfo_x(),
            event.y_root - self.root.winfo_y(),
        )
        self._pill_dragging = False

    def _pill_drag(self, event) -> None:
        if self._pill_press_pos is None or self._pill_drag_origin is None:
            return
        dx = event.x_root - self._pill_press_pos[0]
        dy = event.y_root - self._pill_press_pos[1]
        if not self._pill_dragging:
            if abs(dx) < self.px(4) and abs(dy) < self.px(4):
                return
            self._pill_dragging = True
            if self.mode == "dock":
                self.mode = "floating"
                self.mode_btn.configure(text="侧栏")
                self._configure_mode_layout()
        left, top, right, bottom = work_area()
        x = event.x_root - self._pill_drag_origin[0]
        y = event.y_root - self._pill_drag_origin[1]
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = min(max(x, left + 8), max(left + 8, right - width - 8))
        y = min(max(y, top + 8), max(top + 8, bottom - height - 8))
        self._x, self._y = x, y
        self.root.geometry(f"+{x}+{y}")

    def _pill_release(self, _event=None) -> None:
        if self._pill_drag_origin is None:
            return
        was_dragging = self._pill_dragging
        self._pill_press_pos = None
        self._pill_drag_origin = None
        self._pill_dragging = False
        if was_dragging:
            self._save_window()
        elif self.collapsed:
            self.expand()

    def _start_drag(self, event) -> None:
        if self.mode == "dock":
            return
        self._drag_origin = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag(self, event) -> None:
        if self._drag_origin is None:
            return
        left, top, right, bottom = work_area()
        x = event.x_root - self._drag_origin[0]
        y = event.y_root - self._drag_origin[1]
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = min(max(x, left + 8), max(left + 8, right - width - 8))
        y = min(max(y, top + 8), max(top + 8, bottom - height - 8))
        self._x, self._y = x, y
        self.root.geometry(f"+{x}+{y}")

    def _end_drag(self, _event=None) -> None:
        if self._drag_origin is None:
            return
        self._drag_origin = None
        self._save_window()

    def _apply_startup_geometry(self) -> None:
        left, top, right, bottom = work_area()
        self.log.debug(
            "startup work=%s saved=%s,%s mode=%s",
            (left, top, right, bottom),
            self._x,
            self._y,
            self.mode,
        )
        if self.mode == "floating":
            if self._x is None or self._y is None:
                self._x = right - self.W - self.px(22)
                self._y = top + self.px(58)
            self._x = min(max(self._x, left + 8), max(left + 8, right - self.W - 8))
            self._y = min(max(self._y, top + 8), max(top + 8, bottom - self.px(60) - 8))
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self._configure_mode_layout()
        self.root.update_idletasks()
        self._resize_to_fit(force=True)
        apply_window_rounding(self.root, rounded=self._rounded_mode())
        self.root.lift()

    def _schedule_resize(self) -> None:
        if self._resize_pending or self._closing:
            return
        self._resize_pending = True
        self.root.after_idle(self._resize_to_fit)

    def _resize_to_fit(self, force: bool = False) -> None:
        self._resize_pending = False
        if self._closing:
            return
        if self.mode == "dock":
            self._resize_docked(force=force)
            return
        if self.collapsed:
            self._place_window(self.PILL_W, self.PILL_H, force=force)
            return
        self.root.update_idletasks()
        width = self.W
        height = self.main.winfo_reqheight() + 2
        left, top, right, bottom = work_area()
        max_height = max(self.px(180), bottom - top - 16)
        height = min(height, max_height)
        self.log.debug("resize width=%s height=%s x=%s y=%s", width, height, self._x, self._y)
        self._place_window(width, height, force=force)

    def _resize_docked(self, force: bool = False) -> None:
        left, top, right, bottom = work_area()
        if self.collapsed:
            width = self.PILL_W
            height = self.PILL_H
            x = max(left + 8, right - width - 8)
            y = top + 8
        else:
            width = self.W
            height = max(self.px(240), bottom - top)
            x = right - width
            y = top
        self.log.debug("dock width=%s height=%s x=%s y=%s collapsed=%s", width, height, x, y, self.collapsed)
        self._set_geometry(width, height, x, y, force=force)

    def _set_geometry(self, width: int, height: int, x: int, y: int, force: bool = False) -> None:
        self._x, self._y = x, y
        geometry = f"{width}x{height}+{x}+{y}"
        self.log.debug("place width=%s height=%s x=%s y=%s geometry=%s force=%s", width, height, x, y, geometry, force)
        if force or geometry != self._last_geometry:
            self.root.geometry(geometry)
            self._last_geometry = geometry
        if force:
            self.root.update_idletasks()

    def _place_window(self, width: int, height: int, force: bool = False) -> None:
        left, top, right, bottom = work_area()
        if self._x is None:
            self._x = right - width - self.px(22)
        if self._y is None:
            self._y = top + self.px(58)
        x = min(max(self._x, left + 8), max(left + 8, right - width - 8))
        y = min(max(self._y, top + 8), max(top + 8, bottom - height - 8))
        self._set_geometry(width, height, x, y, force=force)

    def _anchor_right_edge(self, old_width: int, new_width: int) -> None:
        left, _top, right, _bottom = work_area()
        x = self._x + old_width - new_width
        self._x = min(max(x, left + 8), max(left + 8, right - new_width - 8))

    def collapse(self) -> None:
        if self.collapsed:
            return
        if self.calendar_open:
            self._hide_calendar(resize=False)
        if self.mode == "floating":
            self._anchor_right_edge(self.W, self.PILL_W)
        self.main.pack_forget()
        self.pill.pack(fill="both", expand=True)
        self.collapsed = True
        self.root.update_idletasks()
        self._resize_to_fit(force=True)
        self.root.after(20, lambda: apply_window_rounding(self.root, rounded=True))
        self._save_window()

    def expand(self) -> None:
        if not self.collapsed:
            return
        if self.mode == "floating":
            self._anchor_right_edge(self.PILL_W, self.W)
        self.pill.pack_forget()
        self.main.pack(fill="both", expand=True)
        self.collapsed = False
        self._configure_mode_layout()
        self.root.update_idletasks()
        self._resize_to_fit(force=True)
        self.root.after(20, lambda: apply_window_rounding(self.root, rounded=self._rounded_mode()))
        self._save_window()

    def toggle_mode(self) -> None:
        if self.collapsed:
            self.pill.pack_forget()
            self.main.pack(fill="both", expand=True)
            self.collapsed = False
        if self.mode == "dock":
            self.mode = "floating"
            left, top, right, _bottom = work_area()
            self._x = right - self.W - self.px(22)
            self._y = top + self.px(58)
        else:
            self.mode = "dock"
        self.mode_btn.configure(text="浮窗" if self.mode == "dock" else "侧栏")
        self._configure_mode_layout()
        self.root.update_idletasks()
        self._resize_to_fit(force=True)
        self.root.after(20, lambda: apply_window_rounding(self.root, rounded=self._rounded_mode()))
        self._save_window()

    def _on_escape(self, _event=None) -> None:
        if self.calendar_open:
            self._hide_calendar()
            return
        if self.editing_task_id is not None:
            self._cancel_edit()
            return
        if not self.collapsed:
            self.collapse()

    def _show_menu(self, event) -> None:
        try:
            self.menu.entryconfigure(0, state="normal" if (self._latest_snapshot and self._latest_snapshot.window_present) else "disabled")
            self.menu.entryconfigure(1, label="恢复悬浮窗" if self.mode == "dock" else "停靠到屏幕右侧")
            self.menu.entryconfigure(2, label="收起为胶囊" if not self.collapsed else "展开日程")
            self.menu.entryconfigure(3, label="关闭日历" if self.calendar_open else "打开日历")
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _on_task_inner_configure(self, _event=None) -> None:
        width = max(self.task_canvas.winfo_width(), 1)
        self.task_canvas.itemconfigure(self._task_window, width=width)
        self.task_canvas.configure(scrollregion=self.task_canvas.bbox("all"))

    def _on_task_canvas_configure(self, event) -> None:
        self.task_canvas.itemconfigure(self._task_window, width=max(event.width, 1))

    def _on_mousewheel(self, event) -> str:
        widget = self.root.winfo_containing(self.root.winfo_pointerx(), self.root.winfo_pointery())
        inside = False
        current = widget
        while current is not None:
            if current is self.task_canvas or current is self.task_inner:
                inside = True
                break
            current = current.master
        if not inside:
            return "break"
        delta = -1 if event.delta > 0 else 1
        self.task_canvas.yview_scroll(delta, "units")
        return "break"

    # ----------------------------------------------------------------- harness
    def focus_harness(self) -> None:
        try:
            if not focus_harness_window():
                self.log.info("No Harness window available to focus")
        except Exception:
            self.log.exception("Could not focus Harness")

    # ------------------------------------------------------------------- close
    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._save_window()
        self._stop_monitor.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=1.0)
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _start_monitor(self) -> None:
        def run() -> None:
            while not self._stop_monitor.is_set():
                snapshot = self.monitor.snapshot()
                try:
                    self.snapshots.put_nowait(snapshot)
                except queue.Full:
                    try:
                        self.snapshots.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.snapshots.put_nowait(snapshot)
                    except queue.Full:
                        pass
                self._stop_monitor.wait(2.0)

        self._monitor_thread = threading.Thread(target=run, name="dsh-activity-monitor", daemon=True)
        self._monitor_thread.start()

    def _poll_snapshots(self) -> None:
        if self._closing:
            return
        snapshot = None
        while True:
            try:
                snapshot = self.snapshots.get_nowait()
            except queue.Empty:
                break
        if snapshot is not None:
            self._render_snapshot(snapshot)
        self.root.after(500, self._poll_snapshots)

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    debug = "--debug" in sys.argv
    setup_logging(debug)
    install_exception_hook()
    enable_dpi_awareness()
    instance = acquire_single_instance()
    if instance is None:
        focus_existing_instance()
        return 0
    try:
        app = AgendaApp()
        app.run()
    except Exception:
        logging.getLogger("agenda").critical("Failed to start overlay", exc_info=True)
        try:
            messagebox.showerror(APP_TITLE, f"悬浮日程启动失败，详见：\n{LOG_FILE}")
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
