"""Janela do Limpa Som (tkinter): a mesma lógica do assistente, com botões, medidor e notas em destaque.

A lógica (wizard.py) roda numa thread própria ("motor"), porque grava áudio e fala com o Windows
(COM), e a janela nunca congela. O motor conversa com a janela por uma fila de mensagens; as
perguntas do assistente viram botões e bloqueiam o motor até alguém clicar.
"""
from __future__ import annotations

import ctypes
import queue
import re
import struct
import sys
import threading
import time
import traceback
import zlib
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import ui, wizard
from .plain import WIRING_NAMES
from .ui import Abort, LIVE_HINTS

BG, CARD, TEXT, MUTED, LINE = "#f3f5f8", "#ffffff", "#1c2733", "#5f6e7d", "#d8dee6"
ACCENT, ACCENT_DARK, ACCENT_SOFT = "#2563eb", "#1d4ed8", "#e8efff"
GOOD, OKC, WARN, BAD = "#15803d", "#4d7c0f", "#c2410c", "#b91c1c"
FONT = "Segoe UI"


def score_color(value: float) -> str:
    return GOOD if value >= 8.5 else OKC if value >= 7.0 else WARN if value >= 5.0 else BAD


def meter_color(db: float) -> str:
    return ("#94a3b8" if db < -45 else "#f59e0b" if db < -24 else "#84cc16" if db < -12
            else "#16a34a" if db < -3 else "#dc2626")


def sentence(text: str) -> str:
    """'TESTE DO MICROFONE' -> 'Teste do microfone' (textos já em caixa mista ficam como estão)."""
    return text[0] + text[1:].lower() if text.isupper() else text


def enter_text(prompt: str) -> str:
    """'Aperte ENTER para continuar...' -> texto que faz sentido com um botão."""
    if "ENTER" not in prompt:
        return prompt
    if re.fullmatch(r"\s*Aperte ENTER para continuar\.*\s*", prompt):
        return "Clique em Continuar quando estiver pronto."

    def repl(m: "re.Match") -> str:
        before = prompt[:m.start()].rstrip()
        starts_sentence = not before or before.endswith((".", "!", "?"))
        return "Clique em Continuar" if starts_sentence else "clique em Continuar"

    return re.sub(r"(?:[Aa]perte )?ENTER", repl, prompt)


def draw_meter(canvas: tk.Canvas, peak_db: Optional[float], hold_db: Optional[float] = None) -> None:
    canvas.delete("all")
    w = max(canvas.winfo_width(), 50)
    h = max(canvas.winfo_height(), 20)
    floor = -60.0

    def x(db: float) -> int:
        return int(w * (max(floor, min(0.0, db)) - floor) / (0.0 - floor))

    for lo, hi, color in ((-60, -24, "#e8ecf1"), (-24, -12, "#fcf3d2"), (-12, -3, "#d7f0de"), (-3, 0, "#f8d7d7")):
        canvas.create_rectangle(x(lo), 0, x(hi), h, fill=color, outline="")
    if peak_db is not None:
        canvas.create_rectangle(0, h * 0.18, x(peak_db), h * 0.72, fill=meter_color(peak_db), outline="")
    if hold_db is not None and hold_db > floor:
        canvas.create_line(x(hold_db), 0, x(hold_db), h * 0.78, width=3, fill="#1f2937")
    for db in (-48, -36, -24, -12, -6, 0):
        canvas.create_text(x(db) - 3, h - 1, anchor="se", text=str(db), fill=MUTED, font=(FONT, 7))


def capture_window(widget: tk.Misc, path: Path) -> None:
    """Salva uma imagem PNG da janela (usada só para conferir o visual durante o desenvolvimento)."""
    widget.update_idletasks()
    widget.update()
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    for fn, res, args in (
        (user32.GetParent, wintypes.HWND, [wintypes.HWND]),
        (user32.GetWindowRect, wintypes.BOOL, [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]),
        (user32.GetWindowDC, wintypes.HDC, [wintypes.HWND]),
        (user32.ReleaseDC, ctypes.c_int, [wintypes.HWND, wintypes.HDC]),
        (user32.PrintWindow, wintypes.BOOL, [wintypes.HWND, wintypes.HDC, wintypes.UINT]),
        (gdi32.CreateCompatibleDC, wintypes.HDC, [wintypes.HDC]),
        (gdi32.CreateCompatibleBitmap, wintypes.HBITMAP, [wintypes.HDC, ctypes.c_int, ctypes.c_int]),
        (gdi32.SelectObject, wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ]),
        (gdi32.DeleteObject, wintypes.BOOL, [wintypes.HGDIOBJ]),
        (gdi32.DeleteDC, wintypes.BOOL, [wintypes.HDC]),
    ):
        fn.restype, fn.argtypes = res, args
    hwnd = user32.GetParent(widget.winfo_id()) or widget.winfo_id()
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    user32.PrintWindow(hwnd, mem, 2)  # 2 = PW_RENDERFULLCONTENT
    header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.c_void_p, wintypes.UINT]
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(header), 0)
    gdi32.SelectObject(mem, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)

    raw = bytearray()
    data = buf.raw
    for row in range(h):
        raw.append(0)
        line = data[row * w * 4:(row + 1) * w * 4]
        rgb = bytearray(w * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = line[2::4], line[1::4], line[0::4]
        raw += rgb

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))
    Path(path).write_bytes(png)


# ----------------------------------------------------------------- motor ---
class Engine:
    """Uma thread só para tudo que fala com áudio e COM (cada thread precisa do seu CoInitialize)."""

    def __init__(self) -> None:
        self.jobs: "queue.Queue[Optional[Callable[[], None]]]" = queue.Queue()
        self.thread = threading.Thread(target=self._loop, name="motor", daemon=True)
        self.thread.start()

    def submit(self, fn: Callable[[], None]) -> None:
        self.jobs.put(fn)

    def _loop(self) -> None:
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass
        while True:
            fn = self.jobs.get()
            if fn is None:
                return
            try:
                fn()
            except BaseException:
                pass  # cada tarefa trata os próprios erros


class _Prompt:
    def __init__(self, kind: str, text: str = "", options: Optional[List[str]] = None, default: int = 0,
                 button: Optional[str] = None):
        self.kind, self.text, self.options, self.default = kind, text, options or [], default
        self.button = button
        self.event = threading.Event()
        self.result = None
        self.default_invoke: Callable[[], None] = lambda: None

    def finish(self, value) -> None:
        self.result = value
        self.event.set()


class GuiBackend:
    """Implementa a interface do ui.py na janela. Todos os métodos são chamados pelo motor."""

    has_tiles = True  # a nota aparece em cartões grandes, não precisa repetir em texto

    def __init__(self, app: "App") -> None:
        self.app = app
        self.cancelled = False
        self._stop_live = threading.Event()
        self._live_on = False
        self._last_post = 0.0

    # texto ---------------------------------------------------------------
    def say(self, text: str = "", *styles: str) -> None:
        self.app.post(self.app.log_line, text, styles)

    def title(self, text: str) -> None:
        self.app.post(self.app.log_title, text)

    def step(self, text: str) -> None:
        self.app.post(self.app.log_step, text)

    def ok(self, text: str) -> None:
        self.app.post(self.app.log_chip, "●", text, GOOD)

    def warn(self, text: str) -> None:
        self.app.post(self.app.log_chip, "●", text, WARN)

    def bad(self, text: str) -> None:
        self.app.post(self.app.log_chip, "●", text, BAD)

    def info(self, text: str) -> None:
        self.app.post(self.app.log_line, text.strip(), ("gray",))

    def para(self, text: str, indent: int = 5, *styles: str) -> None:
        self.app.post(self.app.log_para, text, styles)

    def reading(self, text: str) -> None:
        self.app.post(self.app.show_reading, text)

    def phase(self, key: str) -> None:
        self.app.post(self.app.set_phase, key)

    def guide(self, kind: str, title: str, text: str = "", note: str = "") -> None:
        self.app.post(self.app.show_guide, kind, title, text, note)

    def working(self, text: Optional[str]) -> None:
        if text:
            self.app.post(self.app.show_working, text)
        else:
            self.app.post(self.app.hide_working)

    def plain(self, report) -> None:
        self.app.post(self.app.show_plain, report)

    def scores(self, items: list) -> None:
        self.app.post(self.app.show_tiles, items)

    def report_ready(self, path) -> None:
        self.app.post(self.app.set_report, str(path))

    # perguntas -------------------------------------------------------------
    def _ask(self, prompt: _Prompt):
        if self.cancelled:
            raise Abort()
        self.app.post(self.app.show_prompt, prompt)
        while not prompt.event.wait(0.1):
            if self.cancelled:
                raise Abort()
        if self.cancelled:
            raise Abort()
        return prompt.result

    def ask_enter(self, prompt: str = "Aperte ENTER para continuar...", button: Optional[str] = None) -> None:
        self._ask(_Prompt("enter", enter_text(prompt), button=button))

    def ask_yes_no(self, question: str, default: bool = True) -> bool:
        return bool(self._ask(_Prompt("yesno", question, default=1 if default else 0)))

    def ask_choice(self, question: str, options: List[str], default: int = 1, eof: Optional[int] = None) -> int:
        options = [o.replace("menu principal", "início") for o in options]
        return int(self._ask(_Prompt("choice", question, options, default - 1)))

    def ask_text(self, question: str, default: str = "") -> str:
        return str(self._ask(_Prompt("text", question)) or default)

    def ask_file(self, question: str) -> str:
        return str(self._ask(_Prompt("file", question)) or "")

    # gravação ----------------------------------------------------------------
    def countdown(self, seconds: int = 3, message: str = "Começando em") -> None:
        for i in range(seconds, 0, -1):
            if self.cancelled:
                raise Abort()
            self.app.post(self.app.show_countdown, f"{message} {i}…")
            ui.sleep_fn(1.0)
        self.app.post(self.app.hide_countdown)

    def record_progress(self, label: str, seconds: float) -> "GuiProgress":
        return GuiProgress(self, label, seconds)

    def key_pressed(self) -> bool:
        return self._stop_live.is_set()

    def live(self, peak: Optional[float], hold: float = -120.0, kind: str = "verylow") -> None:
        if peak is None:
            self._live_on = False
            self.app.post(self.app.hide_live)
            return
        if self.cancelled:
            raise Abort()
        if not self._live_on:
            self._live_on = True
            self._stop_live.clear()
            self.app.post(self.app.show_live)
        now = time.monotonic()
        if now - self._last_post >= 0.04:
            self._last_post = now
            self.app.post(self.app.update_live, peak, hold, kind)


class GuiProgress:
    """Barra de tempo e medidor de nível da gravação."""

    def __init__(self, backend: GuiBackend, label: str, seconds: float) -> None:
        self.b, self.label, self.seconds = backend, label, seconds
        self._last = 0.0
        backend.app.post(backend.app.show_meter, label, seconds)

    def __call__(self, elapsed: float, peak_db: float, rms_db: float) -> None:
        if self.b.cancelled:
            raise Abort()
        now = time.monotonic()
        if now - self._last >= 0.05 or elapsed >= self.seconds:
            self._last = now
            self.b.app.post(self.b.app.update_meter, elapsed, self.seconds, peak_db)

    def finish(self) -> None:
        self.b.app.post(self.b.app.hide_meter)
        if self.label == "sua voz":
            self.b.app.post(self.b.app.hide_reading)


def apply_styles(widget: tk.Misc) -> None:
    st = ttk.Style(widget)
    st.theme_use("clam")
    st.configure("TFrame", background=BG)
    st.configure("TLabel", background=BG, foreground=TEXT, font=(FONT, 10))
    st.configure("Primary.TButton", background=ACCENT, foreground="white", font=(FONT, 11, "bold"),
                 padding=(18, 9), borderwidth=0, focuscolor=ACCENT)
    st.map("Primary.TButton", background=[("active", ACCENT_DARK), ("disabled", "#9db7ee")])
    st.configure("Big.TButton", background=ACCENT, foreground="white", font=(FONT, 13, "bold"),
                 padding=(34, 14), borderwidth=0, focuscolor=ACCENT)
    st.map("Big.TButton", background=[("active", ACCENT_DARK), ("disabled", "#9db7ee")])
    st.configure("Option.TButton", background=CARD, foreground=TEXT, font=(FONT, 10), padding=(14, 9),
                 borderwidth=1, relief="solid", anchor="w")
    st.map("Option.TButton", background=[("active", ACCENT_SOFT)])
    st.configure("Soft.TButton", background=CARD, foreground=TEXT, font=(FONT, 10), padding=(12, 6),
                 borderwidth=1, relief="solid")
    st.map("Soft.TButton", background=[("active", ACCENT_SOFT)])
    st.configure("Bar.Horizontal.TProgressbar", troughcolor="#e4e9f0", background=ACCENT, thickness=14,
                 borderwidth=0)
    st.configure("Eq.Horizontal.TScale", background=CARD, troughcolor="#dbe3ee")
    st.configure("Power.TButton", background=CARD, foreground=ACCENT_DARK, font=(FONT, 13, "bold"),
                 padding=(34, 14), borderwidth=2, relief="solid", bordercolor=ACCENT)
    st.map("Power.TButton", background=[("active", ACCENT_SOFT)])
    st.configure("Stop.TButton", background="#fde8e8", foreground=BAD, font=(FONT, 11, "bold"),
                 padding=(18, 9), borderwidth=1, relief="solid", bordercolor=BAD)
    st.map("Stop.TButton", background=[("active", "#fbd0d0")])
    st.configure("TCombobox", padding=4, font=(FONT, 10))
    st.map("TCombobox", fieldbackground=[("readonly", "white")], background=[("readonly", "white")])


PHASES = [("wiring", "Ligação"), ("silence", "Silêncio"), ("speech", "Voz"), ("adjust", "Ajuste"),
          ("check", "Conferir"), ("cause", "Causa"), ("result", "Resultado")]


def draw_icon(canvas: tk.Canvas, kind: str) -> None:
    """Ícones simples desenhados à mão: microfone, onda parada (silêncio), onda de voz, ajuste, plugue."""
    c = canvas
    c.delete("all")
    c.create_oval(4, 4, 92, 92, fill=ACCENT_SOFT, outline="")
    color = {"silence": "#64748b", "isolate": ACCENT_DARK, "adjust": ACCENT_DARK}.get(kind, ACCENT)

    def mic(col: str) -> None:
        c.create_rectangle(38, 26, 58, 48, fill=col, outline=col)
        c.create_oval(38, 17, 58, 35, fill=col, outline=col)
        c.create_oval(38, 40, 58, 58, fill=col, outline=col)
        c.create_arc(29, 31, 67, 68, start=200, extent=140, style="arc", outline=col, width=3)
        c.create_line(48, 68, 48, 76, fill=col, width=3)
        c.create_line(38, 77, 58, 77, fill=col, width=3)

    if kind == "silence":
        for x in range(22, 76, 8):
            c.create_line(x, 48, x, 48, width=6, fill=color, capstyle="round")
        c.create_line(20, 48, 76, 48, width=1, fill=color)
    elif kind == "speech":
        for x, h in zip(range(20, 78, 8), (8, 22, 36, 24, 44, 28, 14, 6)):
            c.create_line(x, 48 - h / 2, x, 48 + h / 2, width=6, fill=color, capstyle="round")
    elif kind == "adjust":
        for x, knob in ((34, 38), (62, 58)):
            c.create_line(x, 22, x, 74, width=4, fill="#94a3b8", capstyle="round")
            c.create_oval(x - 9, knob - 9, x + 9, knob + 9, fill=color, outline="white", width=2)
    elif kind == "wiring":
        c.create_rectangle(35, 38, 61, 62, fill=color, outline=color)
        c.create_line(42, 22, 42, 38, width=5, fill=color)
        c.create_line(54, 22, 54, 38, width=5, fill=color)
        c.create_line(48, 62, 48, 76, width=5, fill=color)
    elif kind == "isolate":
        mic(color)
        c.create_line(24, 24, 72, 72, width=7, fill=BAD, capstyle="round")
    else:
        mic(color)


class ScrollFrame(tk.Frame):
    """Área com rolagem vertical (e roda do mouse) para o diagnóstico em cartões."""

    def __init__(self, master, bg: str = BG):
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._resized)
        self.wrap_labels: List[tk.Label] = []
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _resized(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)
        for label in self.wrap_labels:
            label.config(wraplength=max(220, event.width - 90))

    def _wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def clear(self) -> None:
        for child in self.inner.winfo_children():
            child.destroy()
        self.wrap_labels.clear()
        self.canvas.yview_moveto(0)


# ---------------------------------------------------------------- janela ---
class ActionCard(tk.Frame):
    def __init__(self, master, title: str, desc: str, command: Callable[[], None], primary: bool = False):
        super().__init__(master, bg=CARD, highlightthickness=2,
                         highlightbackground=ACCENT if primary else LINE, cursor="hand2")
        self.command, self.primary, self.enabled = command, primary, True
        self.title_l = tk.Label(self, text=title, bg=CARD, fg=ACCENT_DARK if primary else TEXT,
                                font=(FONT, 13, "bold"), anchor="w", justify="left", wraplength=360)
        self.desc_l = tk.Label(self, text=desc, bg=CARD, fg=MUTED, font=(FONT, 10), anchor="w",
                               justify="left", wraplength=360)
        self.title_l.pack(fill="x", padx=16, pady=(14, 2))
        self.desc_l.pack(fill="x", padx=16, pady=(0, 14))
        for w in (self, self.title_l, self.desc_l):
            w.bind("<Button-1>", self._click)
            w.bind("<Enter>", lambda e: self._hover(True))
            w.bind("<Leave>", lambda e: self._hover(False))

    def _hover(self, on: bool) -> None:
        if self.enabled:
            self.config(highlightbackground=ACCENT if (on or self.primary) else LINE)

    def _click(self, _event=None) -> None:
        if self.enabled:
            self.command()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.config(cursor="hand2" if enabled else "arrow")
        self.title_l.config(fg=(ACCENT_DARK if self.primary else TEXT) if enabled else "#a3adb8")
        self.desc_l.config(fg=MUTED if enabled else "#a3adb8")

    def set_wrap(self, px: int) -> None:
        self.title_l.config(wraplength=px)
        self.desc_l.config(wraplength=px)


class App(tk.Tk):
    def __init__(self, labels: List[str], default_index: int, build_session: Callable[[int], wizard.Session],
                 fixed_device: bool = False, auto_ms: Optional[int] = None, snap_dir: Optional[Path] = None,
                 autorun: Optional[str] = None, out_dir: Optional[Path] = None) -> None:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        super().__init__()
        self.title("Limpa Som")
        self.geometry("1020x780")
        self.minsize(860, 640)
        self.configure(bg=BG)
        self.labels, self.build_session = labels, build_session
        self.auto_ms, self.snap_dir, self.autorun = auto_ms, snap_dir, autorun
        self.out_dir = out_dir or Path(".")
        self.sess: Optional[wizard.Session] = None
        self.busy = False
        self.report_path: Optional[str] = None
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.engine = Engine()
        self.backend = GuiBackend(self)
        self.current_prompt: Optional[_Prompt] = None
        self.snapped: set = set()
        self.job_name = ""
        self.errors: List[str] = []
        self._live_updates = 0
        self._closed = False
        self._after_ids: set = set()
        self._style()
        self._build(default_index, fixed_device)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.report_callback_exception = self._tk_error
        ui.use(self.backend)
        self._poll_id = self.after(25, self._poll)
        self.set_busy(True, "Preparando o microfone...")
        self.engine.submit(lambda: self._prepare(default_index))

    # -- estilo -------------------------------------------------------------
    def _style(self) -> None:
        apply_styles(self)

    # -- construção -------------------------------------------------------------
    def _build(self, default_index: int, fixed: bool) -> None:
        header = tk.Frame(self, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        header.pack(fill="x")
        left = tk.Frame(header, bg=CARD)
        left.pack(side="left", padx=20, pady=12)
        tk.Label(left, text="Limpa Som", bg=CARD, fg=ACCENT_DARK, font=(FONT, 20, "bold")).pack(anchor="w")
        tk.Label(left, text="Diagnóstico e correção de chiado do microfone", bg=CARD, fg=MUTED,
                 font=(FONT, 10)).pack(anchor="w")
        right = tk.Frame(header, bg=CARD)
        right.pack(side="right", padx=20, pady=12)
        tk.Label(right, text="Microfone", bg=CARD, fg=MUTED, font=(FONT, 9)).pack(anchor="e")
        self.device_var = tk.StringVar(value=self.labels[default_index] if self.labels else "")
        self.combo = ttk.Combobox(right, textvariable=self.device_var, values=self.labels, state="readonly", width=58)
        self.combo.pack(anchor="e")
        if fixed:
            self.combo.config(state="disabled")
        self.combo.bind("<<ComboboxSelected>>", self._device_changed)
        statusbar = tk.Frame(self, bg=BG)
        statusbar.pack(fill="x", padx=22, pady=(6, 0))
        self.status = tk.Label(statusbar, text="", bg=BG, fg=MUTED, font=(FONT, 9), anchor="w")
        self.status.pack(side="left")
        self.wiring_btn = ttk.Button(statusbar, text="Como meu microfone está ligado?", style="Soft.TButton",
                                     command=lambda: self.start("ligacao"))
        self.wiring_btn.pack(side="right")

        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True, padx=20, pady=10)
        self.body.grid_rowconfigure(0, weight=1)
        self.body.grid_columnconfigure(0, weight=1)
        self.home = tk.Frame(self.body, bg=BG)
        self.work = tk.Frame(self.body, bg=BG)
        for page in (self.home, self.work):
            page.grid(row=0, column=0, sticky="nsew")
        self._build_home()
        self._build_work()
        self.home.tkraise()
        tk.Label(self, text="Seus áudios nunca saem do computador.", bg=BG, fg=MUTED,
                 font=(FONT, 8)).pack(pady=(0, 6))

    def _build_home(self) -> None:
        tk.Label(self.home, text="O que você quer fazer?", bg=BG, fg=TEXT, font=(FONT, 15, "bold"),
                 anchor="w").pack(fill="x", pady=(6, 10))
        grid = tk.Frame(self.home, bg=BG)
        grid.pack(fill="both", expand=True)
        grid.grid_columnconfigure(0, weight=1, uniform="c")
        grid.grid_columnconfigure(1, weight=1, uniform="c")
        self.cards: Dict[str, ActionCard] = {}
        specs = [
            ("teste", "Testar o microfone e corrigir o chiado",
             "Ouve o ruído e a sua voz, testa o ganho, descobre de onde vem o chiado e explica o que fazer, com "
             "uma nota de 0 a 10. Recomendado.", True),
            ("equalizador", "Equalizador ao vivo",
             "Mostra o equalizador que o Limpa Som criou para o seu microfone, com todos os filtros editáveis, "
             "como no Equalizer APO. Abre em outra janela.", False),
            ("live", "Ajuste ao vivo",
             "Medidor grande para regular o ganho da mesa ou interface enquanto você fala, vendo a relação "
             "voz/ruído.", False),
            ("arquivo", "Limpar um arquivo de áudio ou vídeo",
             "Tira chiado, zumbido e graves de uma gravação que você já fez. O original não muda.", False),
            ("tempo", "Criar e aplicar o equalizador",
             "Projeta um equalizador sob medida (filtros e IA anti-chiado) a partir do último teste e instala no "
             "Equalizer APO. O teste completo já faz isso no fim.", False),
            ("restaurar", "Restaurar configurações do microfone",
             "Volta o ganho do microfone para o que era antes de o programa mexer.", False),
        ]
        for i, (key, title, desc, primary) in enumerate(specs):
            card = ActionCard(grid, title, desc, lambda k=key: self.start(k), primary)
            card.grid(row=i // 2, column=i % 2, sticky="nsew", padx=8, pady=8)
            self.cards[key] = card
        grid.bind("<Configure>", lambda e: [c.set_wrap(max(200, e.width // 2 - 70)) for c in self.cards.values()])

    def _build_work(self) -> None:
        w = self.work
        self.work_grid = w
        w.grid_rowconfigure(3, weight=1)
        w.grid_columnconfigure(0, weight=1)
        self.guided = False
        self.has_results = False
        self.details_open = False
        self.guide_kind = ""

        # linha 0: título da tarefa (ou indicador de etapas, no teste)
        top = tk.Frame(w, bg=BG)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.stage = tk.Label(top, text="", bg=BG, fg=ACCENT_DARK, font=(FONT, 12, "bold"), anchor="w")
        self.stage.pack(fill="x")
        self.stepper = tk.Frame(top, bg=BG)
        self.chips: Dict[str, tk.Label] = {}
        for i, (key, label) in enumerate(PHASES):
            chip = tk.Label(self.stepper, text=f"{i + 1}  {label}", font=(FONT, 9, "bold"), padx=10, pady=4,
                            bg="#e8ecf1", fg=MUTED)
            chip.pack(side="left", padx=(0, 6))
            self.chips[key] = chip

        self.tiles_frame = tk.Frame(w, bg=BG)
        self.tiles_frame.grid(row=1, column=0, sticky="ew")

        # linha 2: instrução da etapa (ícone, título grande, texto curto, texto de leitura, aviso)
        self.guide_card = tk.Frame(w, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        self.guide_card.grid_columnconfigure(1, weight=1)
        self.guide_icon = tk.Canvas(self.guide_card, width=96, height=96, bg=CARD, highlightthickness=0)
        self.guide_icon.grid(row=0, column=0, rowspan=2, padx=(22, 6), pady=18, sticky="n")
        textbox = tk.Frame(self.guide_card, bg=CARD)
        textbox.grid(row=0, column=1, sticky="ew", padx=(10, 22), pady=(18, 4))
        self.guide_note = tk.Label(textbox, text="", bg=CARD, fg=MUTED, font=(FONT, 9), anchor="w")
        self.guide_note.pack(fill="x")
        self.guide_title = tk.Label(textbox, text="", bg=CARD, fg=TEXT, font=(FONT, 22, "bold"), anchor="w",
                                    justify="left", wraplength=700)
        self.guide_title.pack(fill="x")
        self.guide_text = tk.Label(textbox, text="", bg=CARD, fg=TEXT, font=(FONT, 12), anchor="w", justify="left",
                                   wraplength=700)
        self.guide_text.pack(fill="x", pady=(6, 0))
        self.reading_label = tk.Label(self.guide_card, text="", bg=ACCENT_SOFT, fg=TEXT, font=(FONT, 16),
                                      justify="left", anchor="w", wraplength=760, padx=16, pady=12)
        self.notice = tk.Label(self.guide_card, text="", bg=CARD, fg=MUTED, font=(FONT, 10, "bold"), anchor="w",
                               justify="left", wraplength=800)
        self.notice.grid(row=2, column=0, columnspan=2, sticky="ew", padx=22, pady=(0, 12))

        def rewrap(event) -> None:
            width = max(300, event.width - 190)
            for label in (self.guide_title, self.guide_text, self.reading_label):
                label.config(wraplength=width)
            self.notice.config(wraplength=max(300, event.width - 50))

        self.guide_card.bind("<Configure>", rewrap)

        # linha 3: detalhes técnicos (texto) ou o diagnóstico em cartões (mesma célula)
        self.logbox = tk.Frame(w, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        self.logbox.grid(row=3, column=0, sticky="nsew", pady=(6, 6))
        self.logbox.grid_rowconfigure(0, weight=1)
        self.logbox.grid_columnconfigure(0, weight=1)
        self.log = tk.Text(self.logbox, bg=CARD, fg=TEXT, font=(FONT, 10), wrap="word", relief="flat", padx=16,
                           pady=10, spacing1=2, spacing3=2, cursor="arrow", state="disabled", borderwidth=0)
        scroll = ttk.Scrollbar(self.logbox, command=self.log.yview)
        self.log.config(yscrollcommand=scroll.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self._tags()
        self.results = ScrollFrame(w)

        # linha 4: gravação / contagem / "analisando" / medidor ao vivo
        self.meter_frame = tk.Frame(w, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        self.countdown_label = tk.Label(self.meter_frame, text="", bg=CARD, fg=ACCENT_DARK, font=(FONT, 34, "bold"))
        self.meter_label = tk.Label(self.meter_frame, text="", bg=CARD, fg=TEXT, font=(FONT, 11, "bold"), anchor="w")
        self.progress = ttk.Progressbar(self.meter_frame, style="Bar.Horizontal.TProgressbar", maximum=1000)
        self.meter = tk.Canvas(self.meter_frame, height=30, bg=CARD, highlightthickness=0)
        self.meter_db = tk.Label(self.meter_frame, text="", bg=CARD, fg=MUTED, font=(FONT, 9), anchor="e")
        self.meter.bind("<Configure>", lambda e: self._redraw_meter())
        self._meter_state = (None, None)
        self._meter_name = ""

        self.working_frame = tk.Frame(w, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        self.working_label = tk.Label(self.working_frame, text="", bg=CARD, fg=TEXT, font=(FONT, 12, "bold"))
        self.working_label.pack(pady=(16, 6))
        self.working_bar = ttk.Progressbar(self.working_frame, style="Bar.Horizontal.TProgressbar",
                                           mode="indeterminate", length=360)
        self.working_bar.pack(pady=(0, 16))

        self.live_frame = tk.Frame(w, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        self.live_hint = tk.Label(self.live_frame, text="", bg=CARD, fg=TEXT, font=(FONT, 22, "bold"))
        self.live_hint.pack(pady=(14, 4))
        self.live_canvas = tk.Canvas(self.live_frame, height=64, bg=CARD, highlightthickness=0)
        self.live_canvas.pack(fill="x", padx=18, pady=4)
        self.live_db = tk.Label(self.live_frame, text="", bg=CARD, fg=MUTED, font=(FONT, 10))
        self.live_db.pack()
        ttk.Button(self.live_frame, text="Parar", style="Primary.TButton",
                   command=self.backend._stop_live.set).pack(pady=(8, 14))
        self._live_state = (None, None)
        self.live_canvas.bind("<Configure>", lambda e: draw_meter(self.live_canvas, *self._live_state))

        # linha 5: botões das perguntas
        self.prompt_frame = tk.Frame(w, bg=ACCENT_SOFT, highlightthickness=1, highlightbackground=ACCENT)
        self.prompt_text = tk.Label(self.prompt_frame, text="", bg=ACCENT_SOFT, fg=TEXT, font=(FONT, 11, "bold"),
                                    justify="left", anchor="w", wraplength=880)
        self.prompt_text.pack(fill="x", padx=16, pady=(12, 6))
        self.prompt_buttons = tk.Frame(self.prompt_frame, bg=ACCENT_SOFT)
        self.prompt_buttons.pack(fill="x", padx=16, pady=(0, 12))
        self.prompt_frame.bind("<Configure>", lambda e: self.prompt_text.config(wraplength=max(300, e.width - 40)))

        # linha 7 (sobra de espaço) e linha 8 (barra de baixo, sempre embaixo)
        self.bottom = tk.Frame(w, bg=BG)
        self.bottom.grid(row=8, column=0, sticky="ew")
        self.back_btn = ttk.Button(self.bottom, text="← Início", style="Soft.TButton", command=self.go_home)
        self.back_btn.pack(side="left")
        self.details_btn = ttk.Button(self.bottom, text="Ver detalhes técnicos", style="Soft.TButton",
                                      command=self.toggle_details)
        self.report_btn = ttk.Button(self.bottom, text="Abrir relatório completo", style="Soft.TButton",
                                     command=self.open_report)
        self.folder_btn = ttk.Button(self.bottom, text="Abrir pasta dos resultados", style="Soft.TButton",
                                     command=self.open_folder)
        self.folder_btn.pack(side="right")

    def _tags(self) -> None:
        t = self.log
        t.tag_configure("title", font=(FONT, 15, "bold"), foreground=ACCENT_DARK, spacing1=10, spacing3=4)
        t.tag_configure("step", font=(FONT, 11, "bold"), foreground=WARN, spacing1=12, spacing3=2)
        t.tag_configure("bold", font=(FONT, 10, "bold"))
        t.tag_configure("mono", font=("Consolas", 9))
        t.tag_configure("mono_bold", font=("Consolas", 9, "bold"))
        t.tag_configure("para", lmargin1=2, lmargin2=2)
        t.tag_configure("item", lmargin1=18, lmargin2=34)
        for name, color in (("green", GOOD), ("yellow", WARN), ("red", BAD), ("cyan", "#0e7490"),
                            ("gray", MUTED), ("blue", ACCENT)):
            t.tag_configure(name, foreground=color)

    def later(self, ms: int, fn: Callable) -> None:
        """after() que é cancelado junto com a janela (evita 'invalid command name' depois de fechar)."""
        if not self._closed:
            self._after_ids.add(self.after(ms, fn))

    def destroy(self) -> None:
        self._closed = True
        for after_id in [getattr(self, "_poll_id", None), *self._after_ids]:
            if after_id:
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
        super().destroy()

    # -- fila de mensagens do motor -----------------------------------------------
    def post(self, fn: Callable, *args) -> None:
        self.queue.put((fn, args))

    def _poll(self) -> None:
        try:
            for _ in range(300):
                fn, args = self.queue.get_nowait()
                try:
                    fn(*args)
                except Exception as exc:  # um erro de desenho nunca pode travar a janela
                    self.errors.append(f"{fn.__name__}: {exc}")
        except queue.Empty:
            pass
        if not self._closed:
            self._poll_id = self.after(25, self._poll)

    # -- log -------------------------------------------------------------------
    def _insert(self, text: str, tags) -> None:
        self.log.config(state="normal")
        self.log.insert("end", text, tuple(tags))
        self.log.config(state="disabled")
        self.log.see("end")

    def log_line(self, text: str, styles) -> None:
        names = [s for s in styles if s in ("bold", "green", "yellow", "red", "cyan", "gray", "blue", "mono")]
        if "mono" in names:
            names = [n for n in names if n != "mono"] + (["mono_bold"] if "bold" in names else ["mono"])
            names = [n for n in names if n != "bold"]
        self._insert(text + "\n", names)

    def log_title(self, text: str) -> None:
        self.hide_reading()
        if text.strip().upper() == "RESULTADO":
            self.log.mark_set("verdict_start", "end-1c")
            self.log.mark_gravity("verdict_start", "left")
        self._insert(sentence(text) + "\n", ["title"])
        if not self.guided:
            self.stage.config(text=sentence(text))

    def log_step(self, text: str) -> None:
        self._insert(text + "\n", ["step"])
        if not self.guided:
            self.stage.config(text=text)

    def log_chip(self, mark: str, text: str, color: str) -> None:
        self.log.config(state="normal")
        self.log.tag_configure(f"chip{color}", foreground=color, font=(FONT, 10, "bold"))
        self.log.insert("end", mark + " ", (f"chip{color}",))
        self.log.insert("end", text + "\n", ("bold" if color == BAD else "para",))
        self.log.config(state="disabled")
        self.log.see("end")
        if self.guided and not self.has_results:      # no teste guiado o log fica escondido: mostra o aviso no cartão
            self.notice.config(text=f"{mark} {text}", fg=color)

    def log_para(self, text: str, styles) -> None:
        item = text.startswith("- ") or text.startswith("-> ")
        names = ["item" if item else "para"] + [s for s in styles if s in ("bold", "green", "yellow", "red", "cyan",
                                                                           "gray", "blue")]
        self._insert(text + "\n", names)

    # -- cartões de nota -----------------------------------------------------------
    def show_tiles(self, items: list) -> None:
        for child in self.tiles_frame.winfo_children():
            child.destroy()
        for i, it in enumerate(items):
            tile = tk.Frame(self.tiles_frame, bg=CARD, highlightthickness=1, highlightbackground=LINE)
            tile.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0), pady=(0, 6))
            self.tiles_frame.grid_columnconfigure(i, weight=1, uniform="t")
            tk.Label(tile, text=it["label"], bg=CARD, fg=MUTED, font=(FONT, 10)).pack(pady=(10, 0))
            tk.Label(tile, text=it["text"], bg=CARD, fg=score_color(it["value"]), font=(FONT, 40, "bold")).pack()
            tk.Label(tile, text="de 10", bg=CARD, fg=MUTED, font=(FONT, 9)).pack()
            tk.Label(tile, text=it["caption"], bg=CARD, fg=score_color(it["value"]), font=(FONT, 9, "bold"),
                     wraplength=240).pack(pady=(0, 10))

    def clear_tiles(self) -> None:
        for child in self.tiles_frame.winfo_children():
            child.destroy()
        for column in range(4):
            self.tiles_frame.grid_columnconfigure(column, weight=0, uniform="")

    # -- leitura, medidor, contagem --------------------------------------------------
    def show_reading(self, text: str) -> None:
        self.reading_label.config(text=text)
        self.reading_label.grid(row=1, column=1, sticky="ew", padx=(10, 22), pady=(4, 8))

    def hide_reading(self) -> None:
        self.reading_label.grid_remove()

    # -- etapas, instrução, "analisando", diagnóstico em cartões ----------------------------
    def set_phase(self, key: str) -> None:
        order = [k for k, _ in PHASES]
        idx = order.index(key) if key in order else -1
        self.stepper.pack(fill="x", pady=(2, 0))
        self.stage.pack_forget()
        for i, (k, label) in enumerate(PHASES):
            chip = self.chips[k]
            if i < idx:
                chip.config(bg="#dcfce7", fg=GOOD, text=f"✓  {label}")
            elif i == idx:
                chip.config(bg=ACCENT, fg="white", text=f"{i + 1}  {label}")
            else:
                chip.config(bg="#e8ecf1", fg=MUTED, text=f"{i + 1}  {label}")

    def _rows(self, log: bool) -> None:
        """Dá a sobra de espaço ao log/diagnóstico (quando visível) ou ao fundo da tela."""
        self.work.grid_rowconfigure(3, weight=1 if log else 0)
        self.work.grid_rowconfigure(7, weight=0 if log else 1)

    def _show_log(self, visible: bool) -> None:
        self.results.grid_remove()
        if visible:
            self.logbox.grid(row=3, column=0, sticky="nsew", pady=(6, 6))
        else:
            self.logbox.grid_remove()
        self._rows(visible)

    def _show_results(self) -> None:
        self.guide_card.grid_remove()
        self.logbox.grid_remove()
        self.results.grid(row=3, column=0, sticky="nsew", pady=(6, 6))
        self._rows(True)
        self.has_results = True
        self.details_open = False
        self.details_btn.config(text="Ver detalhes técnicos")
        self.details_btn.pack(side="left", padx=(10, 0), after=self.back_btn)

    def toggle_details(self) -> None:
        if not self.details_open:
            self._show_log(True)
            self.details_open = True
            self.details_btn.config(text="← Voltar ao resumo" if self.has_results else "Esconder detalhes técnicos")
        elif self.has_results:
            self._show_results()
        else:
            self._show_log(False)
            self.details_open = False
            self.details_btn.config(text="Ver detalhes técnicos")

    def show_guide(self, kind: str, title: str, text: str, note: str) -> None:
        self.guided = True
        self.guide_kind = kind
        draw_icon(self.guide_icon, kind)
        self.guide_note.config(text=note)
        self.guide_title.config(text=title)
        self.guide_text.config(text=text)
        self.notice.config(text="")
        self.hide_reading()
        self.guide_card.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        if not self.details_open and not self.has_results:
            self._show_log(False)
        self.details_btn.pack(side="left", padx=(10, 0), after=self.back_btn)

    def show_working(self, text: str) -> None:
        self.hide_meter()
        self.working_label.config(text=text)
        self.working_frame.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        self.working_bar.start(12)

    def hide_working(self) -> None:
        self.working_bar.stop()
        self.working_frame.grid_remove()

    def _section(self, parent, caption: str, text: str, scroll: ScrollFrame) -> None:
        tk.Label(parent, text=caption, bg=CARD, fg=MUTED, font=(FONT, 8, "bold"), anchor="w").pack(
            fill="x", pady=(8, 0))
        label = tk.Label(parent, text=text, bg=CARD, fg=TEXT, font=(FONT, 10), justify="left", anchor="w",
                         wraplength=700)
        label.pack(fill="x")
        scroll.wrap_labels.append(label)

    def show_plain(self, report) -> None:
        scroll = self.results
        scroll.clear()
        inner = scroll.inner

        def card(accent: str) -> tk.Frame:
            outer = tk.Frame(inner, bg=CARD, highlightthickness=1, highlightbackground=LINE)
            outer.pack(fill="x", padx=2, pady=5)
            tk.Frame(outer, bg=accent, width=6).pack(side="left", fill="y")
            body = tk.Frame(outer, bg=CARD)
            body.pack(side="left", fill="both", expand=True, padx=16, pady=12)
            return body

        def label(parent, text: str, font, color: str = TEXT, pad=(0, 0)) -> tk.Label:
            item = tk.Label(parent, text=text, bg=CARD, fg=color, font=font, justify="left", anchor="w",
                            wraplength=700)
            item.pack(fill="x", pady=pad)
            scroll.wrap_labels.append(item)
            return item

        label(card(ACCENT), report.headline, (FONT, 14, "bold"))
        for number, item in enumerate(report.items, 1):
            accent = GOOD if item.key == "alternativa" else BAD if item.severity >= 3 else WARN if item.severity == 2 \
                else MUTED
            body = card(accent)
            label(body, f"{number}. {item.title}", (FONT, 13, "bold"))
            self._section(body, "O QUE EU VI", item.saw, scroll)
            self._section(body, "POR QUE COSTUMA ACONTECER", item.why, scroll)
            tk.Label(body, text="O QUE FAZER", bg=CARD, fg=MUTED, font=(FONT, 8, "bold"), anchor="w").pack(
                fill="x", pady=(8, 0))
            for todo in item.todo:
                label(body, "•  " + todo, (FONT, 10), pad=(1, 1))
        if report.first_steps:
            body = card(GOOD)
            label(body, "POR ONDE COMEÇAR", (FONT, 11, "bold"), GOOD)
            for number, step_text in enumerate(report.first_steps, 1):
                label(body, f"{number}.  {step_text}", (FONT, 11), pad=(2, 2))
        if report.free_fix:
            body = card(ACCENT)
            label(body, report.free_fix, (FONT, 10))
            ttk.Button(body, text="Abrir o Equalizador ao vivo", style="Primary.TButton",
                       command=self.open_equalizer).pack(anchor="w", pady=(8, 0))
        self._show_results()
        self.set_phase("result")

    def _show_meter_frame(self) -> None:
        self.meter_frame.grid(row=4, column=0, sticky="ew", pady=(0, 6))

    def show_countdown(self, text: str) -> None:
        self._show_meter_frame()
        for widget in (self.meter_label, self.progress, self.meter, self.meter_db):
            widget.pack_forget()
        self.countdown_label.config(text=text)
        self.countdown_label.pack(pady=10)

    def hide_countdown(self) -> None:
        self.countdown_label.pack_forget()
        self.meter_frame.grid_remove()

    def show_meter(self, label: str, seconds: float) -> None:
        self._show_meter_frame()
        self.countdown_label.pack_forget()
        self._meter_name = label
        self.meter_label.config(text=f"Gravando: {label}")
        self.meter_label.pack(fill="x", padx=16, pady=(10, 4))
        self.progress.config(value=0)
        self.progress.pack(fill="x", padx=16, pady=2)
        self.meter.pack(fill="x", padx=16, pady=(8, 2))
        self.meter_db.pack(fill="x", padx=16, pady=(0, 10))
        self._meter_state = (None, None)

    def update_meter(self, elapsed: float, seconds: float, peak_db: float) -> None:
        self.progress.config(value=int(1000 * min(1.0, elapsed / max(seconds, 0.01))))
        self.meter_label.config(text=f"Gravando: {self._meter_name} — {elapsed:0.1f} de {seconds:0.0f} s")
        self._meter_state = (peak_db, None)
        self.meter_db.config(text=f"nível {peak_db:5.1f} dB   (alvo da voz: entre -12 e -3 dB)")
        self._redraw_meter()
        self._maybe_snap_recording(elapsed, seconds)

    def _redraw_meter(self) -> None:
        draw_meter(self.meter, *self._meter_state)

    def hide_meter(self) -> None:
        self.meter_frame.grid_remove()

    def show_live(self) -> None:
        self._live_updates = 0
        self.live_frame.grid(row=4, column=0, sticky="ew", pady=(0, 6))

    def update_live(self, peak: float, hold: float, kind: str) -> None:
        hint, _styles = LIVE_HINTS[kind]
        color = {"clip": BAD, "great": GOOD, "low": WARN, "verylow": MUTED}[kind]
        self.live_hint.config(text=hint.capitalize(), fg=color)
        self.live_db.config(text=f"pico {peak:5.1f} dB   ·   máximo recente {hold:5.1f} dB   ·   alvo: -12 a -6 dB")
        self._live_state = (peak, hold)
        draw_meter(self.live_canvas, peak, hold)
        self._live_updates += 1
        if self.snap_dir is not None and self._live_updates == 25:
            self._snap("ao_vivo")
        if (self.snap_dir is not None or self.auto_ms is not None) and self._live_updates >= 40:
            self.backend._stop_live.set()      # só em teste automático: ninguém para clicar em Parar

    def hide_live(self) -> None:
        self.live_frame.grid_remove()

    # -- perguntas (botões) --------------------------------------------------------------
    def show_prompt(self, prompt: _Prompt) -> None:
        self.current_prompt = prompt
        for child in self.prompt_buttons.winfo_children():
            child.destroy()
        self.prompt_text.pack_forget()
        if prompt.text and not (self.guided and prompt.kind == "enter" and prompt.button):
            self.prompt_text.config(text=prompt.text)
            self.prompt_text.pack(fill="x", padx=16, pady=(12, 6), before=self.prompt_buttons)
        self.prompt_frame.grid(row=5, column=0, sticky="ew", pady=(0, 6))

        def answer(value) -> None:
            if prompt.event.is_set():
                return
            prompt.finish(value)
            if self.current_prompt is prompt:
                self.prompt_frame.grid_remove()
                self.current_prompt = None

        if prompt.kind == "file":
            self.prompt_frame.grid_remove()
            if self.auto_ms is not None:      # teste automático: ninguém para escolher o arquivo
                prompt.finish("")
                self.current_prompt = None
                return
            path = filedialog.askopenfilename(
                title="Escolha o arquivo de áudio ou vídeo", parent=self,
                filetypes=[("Áudio e vídeo", "*.wav *.mp3 *.flac *.ogg *.m4a *.mp4 *.mov *.mkv *.avi *.webm"),
                           ("Todos os arquivos", "*.*")])
            prompt.finish(path or "")
            self.current_prompt = None
            return
        if prompt.kind == "enter":
            btn = ttk.Button(self.prompt_buttons, text=prompt.button or "Continuar",
                             style="Big.TButton" if prompt.button else "Primary.TButton",
                             command=lambda: answer(None))
            btn.pack(side="left")
            prompt.default_invoke = btn.invoke
        elif prompt.kind == "yesno":
            yes = ttk.Button(self.prompt_buttons, text="Sim", style="Primary.TButton", command=lambda: answer(True))
            no = ttk.Button(self.prompt_buttons, text="Não", style="Soft.TButton", command=lambda: answer(False))
            first, second = (yes, no) if prompt.default else (no, yes)
            first.pack(side="left")
            second.pack(side="left", padx=(10, 0))
            prompt.default_invoke = (yes if prompt.default else no).invoke
        elif prompt.kind == "choice":
            cols = 2 if len(prompt.options) >= 3 else 1
            for c in range(2):
                self.prompt_buttons.grid_columnconfigure(c, weight=1 if c < cols else 0, uniform="o")
            for i, option in enumerate(prompt.options):
                btn = ttk.Button(self.prompt_buttons, text=option,
                                 style="Primary.TButton" if i == prompt.default else "Option.TButton",
                                 command=lambda i=i: answer(i))
                btn.grid(row=i // cols, column=i % cols, sticky="ew", padx=3, pady=3)
                if i == prompt.default:
                    prompt.default_invoke = btn.invoke
        else:  # texto
            var = tk.StringVar()
            entry = ttk.Entry(self.prompt_buttons, textvariable=var, width=60)
            entry.pack(side="left", padx=(0, 10))
            ok_btn = ttk.Button(self.prompt_buttons, text="OK", style="Primary.TButton",
                                command=lambda: answer(var.get()))
            ok_btn.pack(side="left")
            entry.focus_set()
            entry.bind("<Return>", lambda e: answer(var.get()))
            prompt.default_invoke = ok_btn.invoke
        if prompt.kind != "text":
            self.focus_set()
            self.bind("<Return>", lambda e: prompt.default_invoke() if self.current_prompt is prompt else None)
        self._snap_prompt(prompt)
        if self.auto_ms is not None:
            self.later(self.auto_ms, lambda: prompt.default_invoke() if self.current_prompt is prompt else None)

    # -- tarefas -----------------------------------------------------------------------
    def _prepare(self, index: int) -> None:
        try:
            self.sess = self.build_session(index)
            text = self._status_text()
        except Exception as exc:
            text = f"Não consegui preparar este microfone: {exc}"
            self.sess = None
        self.post(self._ready, text)

    def _status_text(self) -> str:
        sess = self.sess
        if sess is None:
            return ""
        parts = []
        if sess.control is not None and sess.gain_control:
            try:
                parts.append("Ganho atual: " + sess.control.get_settings().describe())
            except Exception:
                pass
        elif sess.control is not None:
            parts.append("O ganho deste microfone é ajustado no próprio equipamento, não no Windows")
        parts.append(sess.info.connection_text[0].upper() + sess.info.connection_text[1:])
        if sess.wiring in WIRING_NAMES:
            parts.append("Ligação: " + WIRING_NAMES[sess.wiring].lower())
        return "   ·   ".join(parts)

    def _ready(self, text: str) -> None:
        self.status.config(text=text)
        self.set_busy(False)
        self.later(150, lambda: self._snap("inicio"))
        if self.autorun:
            key, self.autorun = self.autorun, None
            self.later(400, lambda: self.start(key))
        elif self.snap_dir is not None:       # modo "só a tela inicial": tira a foto e fecha
            self.later(600, self.destroy)

    def _device_changed(self, _event=None) -> None:
        if self.busy:
            return
        index = self.labels.index(self.device_var.get())
        self.set_busy(True, "Preparando o microfone...")
        self.engine.submit(lambda: self._prepare(index))

    def set_busy(self, busy: bool, status: Optional[str] = None) -> None:
        self.busy = busy
        for card in self.cards.values():
            card.set_enabled(not busy)
        self.wiring_btn.state(["disabled"] if busy else ["!disabled"])
        if status:
            self.status.config(text=status)

    def open_equalizer(self) -> None:
        """Abre o Equalizador ao vivo (outro programa, em outra janela)."""
        if self.auto_ms is not None:
            self.errors.append("o modo automático não abre outros programas")
            return
        import subprocess
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        args = [str(pyw if pyw.exists() else exe), "-m", "limpa_som.equalizador"]
        if self.sess is not None:
            args += ["--dispositivo", self.sess.info.name, "--saida", str(self.sess.out_dir)]
        try:
            subprocess.Popen(args, cwd=str(Path(__file__).resolve().parent.parent))
        except OSError as exc:
            messagebox.showerror("Equalizador ao vivo", f"Não consegui abrir o Equalizador: {exc}", parent=self)

    def start(self, key: str) -> None:
        if key == "equalizador":
            self.open_equalizer()
            return
        if self.busy or self.sess is None:
            return
        if self.auto_ms is not None and key in ("tempo", "restaurar"):
            self.errors.append("o modo automático não executa mudanças no sistema")
            return
        flows = {"teste": wizard.test_with_followups, "live": wizard.live_meter,
                 "tempo": wizard.realtime_flow, "arquivo": wizard.clean_file_flow, "restaurar": wizard.restore_flow,
                 "ligacao": lambda sess: wizard.ask_wiring(sess, force=True)}
        self.job_name = key
        self.set_busy(True)
        self._reset_work()
        self.work.tkraise()
        sess, backend = self.sess, self.backend
        backend.cancelled = False

        def job() -> None:
            try:
                flows[key](sess)
            except Abort:
                ui.warn("Cancelado.")
            except BaseException as exc:
                log = wizard.log_error(sess.out_dir, exc)
                ui.bad(f"Deu um erro inesperado ({type(exc).__name__}: {exc}). Detalhes em {log}.")
            finally:
                status = self._status_text()
                self.post(self._job_done, status)

        self.engine.submit(job)

    def _reset_work(self) -> None:
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        self.clear_tiles()
        self.hide_reading()
        self.hide_meter()
        self.hide_live()
        self.hide_working()
        self.prompt_frame.grid_remove()
        self.guide_card.grid_remove()
        self.results.clear()
        self.results.grid_remove()
        self.stepper.pack_forget()
        self.stage.pack(fill="x")
        self.guided = self.has_results = self.details_open = False
        self.guide_kind = ""
        self._show_log(True)
        self.details_btn.pack_forget()
        self.report_btn.pack_forget()
        self.report_path = None
        self.stage.config(text="")
        self.notice.config(text="")
        self.back_btn.state(["disabled"])

    def _job_done(self, status: str) -> None:
        self.status.config(text=status)
        self.hide_meter()
        self.hide_live()
        self.hide_working()
        self.prompt_frame.grid_remove()
        if self.guided and not self.has_results and self.job_name != "ligacao":
            self.guide_card.grid_remove()          # terminou sem resultado (cancelou/erro): mostra o que houve
            self._show_log(True)
        self.back_btn.state(["!disabled"])
        self.set_busy(False)
        self._snap("fim")
        if self.job_name == "ligacao" and self.snap_dir is None and self.auto_ms is None:
            self.go_home()
        if self.snap_dir is not None:             # modo de captura: tira a foto final e fecha
            self.later(500, self.destroy)

    def go_home(self) -> None:
        if not self.busy:
            self.home.tkraise()

    def set_report(self, path: str) -> None:
        self.report_path = path
        self.report_btn.pack(side="left", padx=(10, 0))
        if "verdict_start" in self.log.mark_names():
            self.later(60, lambda: self.log.yview("verdict_start"))

    def open_report(self) -> None:
        if self.report_path:
            wizard.open_file(Path(self.report_path))

    def open_folder(self) -> None:
        folder = self.sess.out_dir if self.sess else self.out_dir
        folder.mkdir(parents=True, exist_ok=True)
        wizard.open_file(folder)

    # -- fechar e erros ------------------------------------------------------------------
    def on_close(self) -> None:
        if self.busy and self.job_name:
            if not messagebox.askyesno(
                    "Fechar o Limpa Som",
                    "Há uma tarefa em andamento. Se ela estiver ajustando o ganho do microfone, vou voltar ao "
                    "ajuste original antes de fechar.\n\nFechar mesmo assim?", parent=self):
                return
            self.backend.cancelled = True
            if self.current_prompt is not None:
                self.current_prompt.event.set()
            self._closing_since = time.monotonic()
            self.later(100, self._finish_close)
            return
        self.destroy()

    def _finish_close(self) -> None:
        if not self.busy or time.monotonic() - self._closing_since > 5.0:
            self.destroy()
        else:
            self.later(100, self._finish_close)

    def _tk_error(self, exc, val, tb) -> None:
        self.errors.append("".join(traceback.format_exception(exc, val, tb)))
        try:
            wizard.log_error(self.out_dir, val)
        except Exception:
            pass

    # -- capturas para conferir o visual (desenvolvimento) ---------------------------------------
    def _snap(self, name: str) -> None:
        if self.snap_dir is None or name in self.snapped:
            return
        self.snapped.add(name)
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        try:
            if self.state() == "iconic":      # janela minimizada por outro programa: restaura antes da foto
                self.deiconify()
            capture_window(self, self.snap_dir / f"{name}.png")
        except Exception as exc:
            self.errors.append(f"captura {name}: {exc}")

    def _snap_prompt(self, prompt: _Prompt) -> None:
        if self.snap_dir is None:
            return
        key = f"pergunta_{prompt.kind}"
        if prompt.kind == "enter" and self.guided and self.guide_kind:
            key = f"guia_{self.guide_kind}"
        self.later(150, lambda: self._snap(key))

    def _maybe_snap_recording(self, elapsed: float, seconds: float) -> None:
        if self.snap_dir is not None and elapsed > seconds * 0.55:
            self._snap("gravando_voz" if "voz" in self._meter_name else "gravando_silencio")


def run(app: App) -> int:
    app.mainloop()
    return 0
