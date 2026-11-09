"""Interface do programa: console por padrão, trocável pela janela (gui.py) via `use(backend)`.

A lógica (wizard.py) só fala com as funções deste módulo. Cada função repassa para o `backend`
ativo, então o mesmo fluxo roda no console ou na janela sem mudar uma linha.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Callable, List, Optional

_RESET = "\033[0m"
_STYLES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
           "blue": "34", "magenta": "35", "cyan": "36", "gray": "90"}

color_enabled = False
input_fn: Callable[[str], str] = input
sleep_fn: Callable[[float], None] = time.sleep

# texto do medidor ao vivo: tipo -> (mensagem, estilos)
LIVE_HINTS = {
    "clip": ("ESTOURANDO: abaixe o ganho", ("red", "bold")),
    "great": ("ótimo", ("green", "bold")),
    "low": ("um pouco baixo", ("yellow",)),
    "verylow": ("muito baixo (ou sem som)", ("gray",)),
}


class Abort(Exception):
    """O teste foi cancelado (pela pessoa ou ao fechar a janela)."""


def init(color: bool = True) -> None:
    global color_enabled
    if os.name == "nt":
        try:
            import ctypes
            kernel = ctypes.windll.kernel32
            handle = kernel.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            else:
                color = False
        except Exception:
            color = False
    color_enabled = bool(color and sys.stdout is not None and sys.stdout.isatty())
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def paint(text: str, *styles: str) -> str:
    """Aplica cores ANSI. Estilos vazios ou desconhecidos são ignorados (nunca dão erro)."""
    codes = [_STYLES[s] for s in styles if s in _STYLES]
    if not color_enabled or not codes:
        return text
    return f"\033[{';'.join(codes)}m{text}{_RESET}"


def wrap(text: str, indent: int = 5, width: int = 92) -> List[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width - indent:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def level_bar(db: float, width: int = 30, floor: float = -60.0) -> str:
    frac = max(0.0, min(1.0, (db - floor) / (0.0 - floor)))
    filled = int(round(frac * width))
    color = "dim" if db < -45 else "green" if db < -12 else "yellow" if db < -3 else "red"
    return paint("█" * filled, color) + paint("░" * (width - filled), "gray")


# ------------------------------------------------------------------ console ---
class ConsoleBackend:
    """Implementação em texto (a que já existia)."""

    has_tiles = False  # a nota só aparece em texto

    def say(self, text: str = "", *styles: str) -> None:
        print(paint(text, *styles))

    def title(self, text: str) -> None:
        bar = "=" * min(78, max(len(text) + 4, 30))
        self.say()
        self.say(bar, "cyan")
        self.say(f"  {text}", "bold", "cyan")
        self.say(bar, "cyan")

    def step(self, text: str) -> None:
        self.say()
        self.say(f">> {text}", "bold", "yellow")

    def ok(self, text: str) -> None:
        self.say(f"[OK] {text}", "green")

    def warn(self, text: str) -> None:
        self.say(f"[!]  {text}", "yellow")

    def bad(self, text: str) -> None:
        self.say(f"[X]  {text}", "red")

    def info(self, text: str) -> None:
        self.say(f"     {text}", "gray")

    def para(self, text: str, indent: int = 5, *styles: str) -> None:
        for line in wrap(text, indent):
            self.say(" " * indent + line, *styles)

    def reading(self, text: str) -> None:
        self.say()
        for line in wrap(text, 4, 80):
            self.say("    " + line, "bold")
        self.say()

    def scores(self, items: list) -> None:  # a nota já é impressa pelo veredito em texto
        pass

    def phase(self, key: str) -> None:       # etapa atual (a janela mostra um indicador de etapas)
        pass

    def guide(self, kind: str, title: str, text: str = "", note: str = "") -> None:
        """Instrução clara de uma etapa. Na janela vira uma tela grande; no console, título + parágrafo."""
        self.step(title + (f" ({note})" if note else ""))
        if text:
            self.para(text)

    def working(self, text: Optional[str]) -> None:
        if text:
            self.info(text)

    def plain(self, report) -> None:
        """Diagnóstico em linguagem simples (o que eu vi / por que costuma acontecer / o que fazer)."""
        self.say()
        self.para(report.headline, 2, "bold")
        for n, item in enumerate(report.items, 1):
            self.say()
            self.say(f"  {n}. {item.title}", "bold", "yellow")
            self.para(f"O que eu vi: {item.saw}", 5)
            self.para(f"Por que costuma acontecer: {item.why}", 5)
            self.say("     O que fazer:", "bold")
            for todo in item.todo:
                self.para(f"- {todo}", 7)
        if report.first_steps:
            self.say()
            self.say("  POR ONDE COMEÇAR", "bold", "green")
            for n, step in enumerate(report.first_steps, 1):
                self.para(f"{n}. {step}", 5)
        if report.free_fix:
            self.say()
            self.para(report.free_fix, 5, "cyan")

    def report_ready(self, path) -> None:
        pass

    # perguntas ---------------------------------------------------------------
    def ask_enter(self, prompt: str = "Aperte ENTER para continuar...", button: Optional[str] = None) -> None:
        try:
            input_fn(paint(prompt, "bold") + " ")
        except EOFError:
            pass

    def ask_yes_no(self, question: str, default: bool = True) -> bool:
        hint = "[S/n]" if default else "[s/N]"
        while True:
            try:
                ans = input_fn(paint(f"{question} {hint} ", "bold")).strip().lower()
            except EOFError:
                return False  # sem ninguém digitando, nunca assume 'sim'
            if not ans:
                return default
            if ans in ("s", "sim", "y", "yes"):
                return True
            if ans in ("n", "nao", "não", "no"):
                return False

    def ask_choice(self, question: str, options: List[str], default: int = 1, eof: Optional[int] = None) -> int:
        self.say(question, "bold")
        for i, opt in enumerate(options, 1):
            mark = " (padrão)" if i == default else ""
            self.say(f"  {i}) {opt}{mark}")
        while True:
            try:
                ans = input_fn(paint(f"Escolha [1-{len(options)}]: ", "bold")).strip()
            except EOFError:
                return len(options) - 1 if eof is None else eof
            if not ans:
                return default - 1
            if ans.isdigit() and 1 <= int(ans) <= len(options):
                return int(ans) - 1

    def ask_text(self, question: str, default: str = "") -> str:
        try:
            ans = input_fn(paint(question + " ", "bold")).strip()
        except EOFError:
            return default
        return ans or default

    def ask_file(self, question: str) -> str:
        return self.ask_text(question)

    # gravação ----------------------------------------------------------------
    def countdown(self, seconds: int = 3, message: str = "Começando em") -> None:
        if not sys.stdout.isatty():
            sleep_fn(float(seconds))
            return
        for i in range(seconds, 0, -1):
            sys.stdout.write(paint(f"\r  {message} {i}... ", "bold", "yellow"))
            sys.stdout.flush()
            sleep_fn(1.0)
        sys.stdout.write("\r" + " " * 40 + "\r")
        sys.stdout.flush()

    def record_progress(self, label: str, seconds: float) -> "ConsoleProgress":
        return ConsoleProgress(label, seconds)

    def key_pressed(self) -> bool:
        try:
            import msvcrt
            if msvcrt.kbhit():
                msvcrt.getch()
                return True
        except Exception:
            pass
        return False

    def live(self, peak: Optional[float], hold: float = -120.0, kind: str = "verylow") -> None:
        if peak is None:
            print()
            return
        hint, styles = LIVE_HINTS[kind]
        print(f"\r  {level_bar(peak, 36)} pico {peak:6.1f} dB | máx recente {hold:6.1f} dB  "
              f"{paint(hint, *styles)}      ", end="", flush=True)


class ConsoleProgress:
    """Barra de tempo e medidor de nível durante a gravação."""

    def __init__(self, label: str, seconds: float):
        self.label = label
        self.seconds = seconds
        self._last = 0.0

    def __call__(self, elapsed: float, peak_db: float, rms_db: float) -> None:
        if not sys.stdout.isatty() or (elapsed - self._last < 0.1 and elapsed < self.seconds):
            return
        self._last = elapsed
        width = 20
        done = int(width * min(1.0, elapsed / max(self.seconds, 0.01)))
        bar = paint("█" * done, "cyan") + paint("░" * (width - done), "gray")
        sys.stdout.write(f"\r  {self.label:<22} {bar} {elapsed:4.1f}/{self.seconds:.0f}s  "
                         f"nível {level_bar(peak_db, 24)} {peak_db:6.1f} dB ")
        sys.stdout.flush()

    def finish(self) -> None:
        if sys.stdout.isatty():
            sys.stdout.write("\r" + " " * 110 + "\r")
            sys.stdout.flush()


# --------------------------------------------------- funções que o wizard usa ---
backend = ConsoleBackend()


def use(new_backend) -> None:
    global backend
    backend = new_backend


def say(text: str = "", *styles: str) -> None:
    backend.say(text, *styles)


def title(text: str) -> None:
    backend.title(text)


def step(text: str) -> None:
    backend.step(text)


def ok(text: str) -> None:
    backend.ok(text)


def warn(text: str) -> None:
    backend.warn(text)


def bad(text: str) -> None:
    backend.bad(text)


def info(text: str) -> None:
    backend.info(text)


def para(text: str, indent: int = 5, *styles: str) -> None:
    backend.para(text, indent, *styles)


def reading(text: str) -> None:
    backend.reading(text)


def scores(items: list) -> None:
    backend.scores(items)


def report_ready(path) -> None:
    backend.report_ready(path)


def ask_enter(prompt: str = "Aperte ENTER para continuar...", button: Optional[str] = None) -> None:
    backend.ask_enter(prompt, button)


def phase(key: str) -> None:
    backend.phase(key)


def guide(kind: str, title: str, text: str = "", note: str = "") -> None:
    backend.guide(kind, title, text, note)


def working(text: Optional[str]) -> None:
    backend.working(text)


def plain(report) -> None:
    backend.plain(report)


def ask_yes_no(question: str, default: bool = True) -> bool:
    return backend.ask_yes_no(question, default)


def ask_choice(question: str, options: List[str], default: int = 1, eof: Optional[int] = None) -> int:
    return backend.ask_choice(question, options, default, eof)


def ask_text(question: str, default: str = "") -> str:
    return backend.ask_text(question, default)


def ask_file(question: str) -> str:
    return backend.ask_file(question)


def countdown(seconds: int = 3, message: str = "Começando em") -> None:
    backend.countdown(seconds, message)


def RecordProgress(label: str, seconds: float):
    return backend.record_progress(label, seconds)


def key_pressed() -> bool:
    return backend.key_pressed()


def live(peak: Optional[float], hold: float = -120.0, kind: str = "verylow") -> None:
    backend.live(peak, hold, kind)
