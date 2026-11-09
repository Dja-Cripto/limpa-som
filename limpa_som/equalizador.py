"""Equalizador ao vivo: o editor do equalizador do seu microfone (segundo programa do Limpa Som).

O Limpa Som mede o ruído e a sua voz, projeta um equalizador sob medida e aplica no Equalizer APO. Esta janela
mostra EXATAMENTE esse equalizador e deixa mexer em tudo, como no editor do Equalizer APO: lista de filtros (pico,
prateleiras, corta-graves, corta-agudos, notch), curva em que se arrastam os pontos, ganho de entrada e a supressão
de ruído por IA (RNNoise). O que fica aqui vale em segundo plano, para todos os programas (Discord, OBS, Zoom...),
mesmo com a janela fechada.
"""
from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np
import tkinter as tk
from tkinter import messagebox, ttk

from . import eq, realtime
from .gui import (ACCENT, ACCENT_DARK, ACCENT_SOFT, BAD, BG, CARD, FONT, GOOD, LINE, MUTED, TEXT, WARN,
                  ScrollFrame, apply_styles, capture_window)

ROOT = Path(__file__).resolve().parent.parent
POLL_MS = 100
SHORT = {"PK": "Pico", "LS": "Prateleira de graves", "HS": "Prateleira de agudos", "HP": "Corta graves",
         "LP": "Corta agudos", "NO": "Notch"}
SHORT_TO_KIND = {v: k for k, v in SHORT.items()}
DEFAULT_FREQ = {"PK": 1000.0, "LS": 120.0, "HS": 8000.0, "HP": 80.0, "LP": 8000.0, "NO": 1000.0}
DEFAULT_GAIN = {"PK": -3.0, "LS": 3.0, "HS": -3.0}
POINT_COLOR = {"PK": "#2563eb", "LS": "#7c3aed", "HS": "#7c3aed", "HP": "#d97706", "LP": "#d97706", "NO": "#dc2626"}
CURVE_DB_MIN, CURVE_DB_MAX = -24.0, 18.0
COLUMNS = (("", 30), ("Tipo", 166), ("Freq (Hz)", 84), ("Ganho (dB)", 76), ("Q", 70))   # larguras em pixels


@dataclass
class MicChoice:
    label: str                 # texto mostrado na lista
    name: str                  # nome do Windows (vira o padrão de 'Device:' do APO)
    guid: Optional[str] = None
    device: object = None


def _num(text: str, default: float) -> float:
    try:
        return float(str(text).strip().replace(",", ".").replace("dB", "").replace("Hz", "").strip())
    except ValueError:
        return default


def _fmt(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


class EqualizerApp(tk.Tk):
    NOT_ATTACHED_TEXT = (
        "O Equalizer APO ainda não aparece ligado a este microfone, então o equalizador ainda não tem efeito no som "
        "(pode editar já: vale assim que ligar). Para ligar, uma vez só: clique em 'Abrir o Configurator' (o Windows "
        "pede permissão de administrador), na aba 'Capture devices' marque o seu microfone, clique em OK e reinicie "
        "o Windows. Depois volte aqui e clique em 'Verificar de novo'.")
    NO_APO_TEXT = ("Não encontrei o Equalizer APO neste computador. Ele é o programa gratuito que filtra o "
                   "microfone dentro do Windows (equalizerapo.com). Instale e abra esta janela de novo.")
    NO_PROFILE_TEXT = ("O Limpa Som ainda não criou um equalizador para este microfone. Abra o Limpa Som e rode "
                       "'Testar o microfone': ele mede o ruído e a sua voz, cria o equalizador e ele aparece aqui. "
                       "Enquanto isso você pode montar o seu à mão.")

    def __init__(self, apo: Optional[realtime.ApoInfo], mics: List[MicChoice], start_index: int = 0,
                 out_dir: Optional[Path] = None, attached_fn: Callable = realtime.is_attached,
                 confirm_fn: Optional[Callable] = None, info_fn: Optional[Callable] = None,
                 open_main_fn: Optional[Callable] = None, snap_dir: Optional[Path] = None) -> None:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        super().__init__()
        self.title("Limpa Som · Equalizador ao vivo")
        height = min(960, max(720, self.winfo_screenheight() - 80))
        self.geometry(f"1180x{height}")
        self.minsize(1040, min(760, height))
        self.configure(bg=BG)
        apply_styles(self)
        self.apo, self.mics = apo, mics
        self.out_dir = out_dir or (ROOT / "resultados")
        self.attached_fn = attached_fn
        self.confirm_fn = confirm_fn or (lambda text: messagebox.askyesno("Equalizador ao vivo", text, parent=self))
        self.info_fn = info_fn or (lambda text: messagebox.showinfo("Equalizador ao vivo", text, parent=self))
        self.open_main_fn = open_main_fn or self._open_main
        self.snap_dir = snap_dir
        self.errors: List[str] = []
        self.mic = mics[start_index]
        self.profile = eq.Profile(enabled=False)
        self.original: Optional[eq.Profile] = None
        self.created_at = ""
        self.has_stored = False
        self.rows: List[dict] = []
        self.selected: Optional[int] = None
        self.drag: Optional[int] = None
        self.attached: Optional[bool] = None
        self.last_apply_ok: Optional[bool] = None
        self._closed = False
        self._loading = False
        self._after_ids: set = set()
        self._apply_job = None
        self._ticks = 0
        self._note = ""                                  # frase que vai na frente da próxima mensagem de gravação
        self.report_callback_exception = self._tk_error
        self._load_mic()
        self._build()
        self._rebuild_rows()
        self._refresh_all()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._tick()
        self.refresh_status()

    # -- dados ------------------------------------------------------------------------
    def _load_mic(self) -> None:
        stored = eq.load_stored(self.out_dir, self.mic.name)
        self.has_stored = stored is not None
        if stored is None:
            self.profile, self.original, self.created_at = eq.Profile(enabled=False), None, ""
        else:
            self.profile = stored.atual
            self.profile.bands.sort(key=lambda b: (b.kind != "HP", b.freq))
            self.original, self.created_at = stored.limpa_som, stored.criado_em

    def _save(self) -> None:
        try:
            eq.save_stored(self.out_dir, self.mic.name, self.profile)
            self.has_stored = True
        except OSError:
            pass

    # -- construção da janela ---------------------------------------------------------
    def _card(self, parent, **grid) -> tk.Frame:
        frame = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        frame.grid(**grid)
        return frame

    def _build(self) -> None:
        header = tk.Frame(self, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        header.pack(fill="x")
        left_head = tk.Frame(header, bg=CARD)
        left_head.pack(side="left", padx=20, pady=12)
        tk.Label(left_head, text="Equalizador ao vivo", bg=CARD, fg=ACCENT_DARK, font=(FONT, 20, "bold")).pack(
            anchor="w")
        tk.Label(left_head, text="O equalizador que o Limpa Som criou para o seu microfone: veja e mexa em tudo",
                 bg=CARD, fg=MUTED, font=(FONT, 10)).pack(anchor="w")
        right_head = tk.Frame(header, bg=CARD)
        right_head.pack(side="right", padx=20, pady=12)
        tk.Label(right_head, text="Microfone", bg=CARD, fg=MUTED, font=(FONT, 9)).pack(anchor="e")
        self.mic_var = tk.StringVar(value=self.mic.label)
        self.combo = ttk.Combobox(right_head, textvariable=self.mic_var, values=[m.label for m in self.mics],
                                  state="readonly", width=52)
        self.combo.pack(anchor="e")
        self.combo.bind("<<ComboboxSelected>>", self._mic_changed)

        footer = tk.Frame(self, bg=BG)
        footer.pack(side="bottom", fill="x", padx=20, pady=(4, 10))
        self.message = tk.Label(footer, text="", bg=BG, fg=MUTED, font=(FONT, 10, "bold"), anchor="w")
        self.message.pack(side="left")
        ttk.Button(footer, text="Fechar (o equalizador continua ativo)", style="Soft.TButton",
                   command=self.on_close).pack(side="right")
        ttk.Button(footer, text="Desligar tudo e desfazer", style="Soft.TButton", command=self.undo_all).pack(
            side="right", padx=8)
        ttk.Button(footer, text="Abrir o Limpa Som", style="Soft.TButton",
                   command=lambda: self.open_main_fn()).pack(side="right")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=20, pady=10)
        body.grid_columnconfigure(0, weight=11, uniform="c")
        body.grid_columnconfigure(1, weight=10, uniform="c")
        body.grid_rowconfigure(1, weight=1)

        # situação do Equalizer APO e de onde veio este equalizador
        status = self._card(body, row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        status.grid_columnconfigure(5, weight=1)
        self.dots: Dict[str, tuple] = {}
        for i, (key, text) in enumerate((("apo", "Equalizer APO instalado"),
                                         ("attached", "Ligado a este microfone"),
                                         ("active", "Equalizador do Limpa Som ativo"))):
            dot = tk.Label(status, text="●", bg=CARD, fg=MUTED, font=(FONT, 14))
            dot.grid(row=0, column=i * 2, padx=(16 if i == 0 else 26, 4), pady=8)
            label = tk.Label(status, text=text, bg=CARD, fg=TEXT, font=(FONT, 10, "bold"))
            label.grid(row=0, column=i * 2 + 1, pady=8, sticky="w")
            self.dots[key] = (dot, label, text)
        self.origin_label = tk.Label(status, text="", bg=CARD, fg=MUTED, font=(FONT, 9), anchor="w", justify="left",
                                     wraplength=1080)
        self.origin_label.grid(row=1, column=0, columnspan=6, sticky="ew", padx=16, pady=(0, 8))
        self.banner = tk.Frame(status, bg="#fff4e0")
        self.banner_buttons = tk.Frame(self.banner, bg="#fff4e0")
        self.banner_buttons.pack(side="right", padx=14, pady=10)
        ttk.Button(self.banner_buttons, text="Abrir o Configurator", style="Primary.TButton",
                   command=self.open_configurator).pack(fill="x")
        ttk.Button(self.banner_buttons, text="Verificar de novo", style="Soft.TButton",
                   command=self.refresh_status).pack(fill="x", pady=(6, 0))
        self.banner_text = tk.Label(self.banner, bg="#fff4e0", fg=TEXT, font=(FONT, 10), justify="left",
                                    anchor="w", wraplength=760, text=self.NOT_ATTACHED_TEXT)
        self.banner_text.pack(side="left", fill="both", expand=True, padx=14, pady=10)

        # esquerda: a lista de filtros
        filters = self._card(body, row=1, column=0, sticky="nsew", padx=(0, 10))
        filters.grid_rowconfigure(2, weight=1)
        filters.grid_columnconfigure(0, weight=1)
        top = tk.Frame(filters, bg=CARD)
        top.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 2))
        tk.Label(top, text="Filtros", bg=CARD, fg=TEXT, font=(FONT, 12, "bold")).pack(side="left")
        self.count_label = tk.Label(top, text="", bg=CARD, fg=MUTED, font=(FONT, 9))
        self.count_label.pack(side="left", padx=10)
        self.add_menu_btn = ttk.Menubutton(top, text="+ Adicionar filtro", style="Soft.TButton")
        menu = tk.Menu(self.add_menu_btn, tearoff=0)
        for kind in eq.KINDS:
            menu.add_command(label=f"{SHORT[kind]}  ({eq.KIND_NAMES[kind]})", command=lambda k=kind: self.add_band(k))
        self.add_menu_btn["menu"] = menu
        self.add_menu_btn.pack(side="right")
        ttk.Button(top, text="Apagar todos", style="Soft.TButton", command=self.clear_bands).pack(side="right",
                                                                                                   padx=6)
        self.reset_btn = ttk.Button(top, text="Voltar ao do Limpa Som", style="Soft.TButton",
                                    command=self.reset_to_original)
        self.reset_btn.pack(side="right")
        head = tk.Frame(filters, bg=CARD)
        head.grid(row=1, column=0, sticky="ew", padx=(18, 24))
        for col, (text, minsize) in enumerate(COLUMNS):
            head.grid_columnconfigure(col, minsize=minsize)
            tk.Label(head, text=text, bg=CARD, fg=MUTED, font=(FONT, 8), anchor="w").grid(row=0, column=col,
                                                                                           sticky="w")
        self.scroller = ScrollFrame(filters, bg=CARD)
        self.scroller.grid(row=2, column=0, sticky="nsew", padx=(10, 6), pady=(2, 10))
        self.scroller.canvas.configure(height=360)

        # direita: ligar, curva, ganho e supressão de ruído
        right = tk.Frame(body, bg=BG)
        right.grid(row=1, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        self.power = ttk.Button(right, text="", style="Big.TButton", command=self.toggle_power)
        self.power.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        curve_card = self._card(right, row=1, column=0, sticky="nsew")
        tk.Label(curve_card, text="Curva do equalizador", bg=CARD, fg=TEXT, font=(FONT, 11, "bold"),
                 anchor="w").pack(fill="x", padx=16, pady=(12, 0))
        tk.Label(curve_card, text="Arraste os pontos: para os lados muda a frequência, para cima e para baixo o ganho. "
                 "A roda do mouse muda a largura (Q) do ponto escolhido.", bg=CARD, fg=MUTED, font=(FONT, 9),
                 anchor="w", justify="left", wraplength=500).pack(fill="x", padx=16)
        self.curve = tk.Canvas(curve_card, bg=CARD, highlightthickness=0, height=250)
        self.curve.pack(fill="both", expand=True, padx=12, pady=8)
        self.curve.bind("<Configure>", lambda e: self.draw_curve())
        self.curve.bind("<ButtonPress-1>", self._curve_press)
        self.curve.bind("<B1-Motion>", self._curve_motion)
        self.curve.bind("<ButtonRelease-1>", self._curve_release)
        self.curve.bind("<MouseWheel>", self._curve_wheel)

        gain_card = self._card(right, row=2, column=0, sticky="ew", pady=(10, 0))
        gain_card.grid_columnconfigure(0, weight=1)
        head_g = tk.Frame(gain_card, bg=CARD)
        head_g.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 0))
        tk.Label(head_g, text="Ganho de entrada (antes de tudo)", bg=CARD, fg=TEXT, font=(FONT, 10, "bold")).pack(
            side="left")
        self.preamp_label = tk.Label(head_g, text="", bg=CARD, fg=ACCENT_DARK, font=(FONT, 10, "bold"))
        self.preamp_label.pack(side="right")
        self.preamp_var = tk.DoubleVar()
        ttk.Scale(gain_card, from_=eq.PREAMP_MIN, to=eq.PREAMP_MAX, variable=self.preamp_var,
                  style="Eq.Horizontal.TScale", command=lambda _v: self._preamp_changed()).grid(
            row=1, column=0, sticky="ew", padx=16, pady=(2, 10))

        ai_card = self._card(right, row=3, column=0, sticky="ew", pady=(10, 0))
        ai_card.grid_columnconfigure(0, weight=1)
        tk.Label(ai_card, text="Supressão de ruído por IA (RNNoise)", bg=CARD, fg=TEXT,
                 font=(FONT, 10, "bold"), anchor="w").grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 2))
        self.rnn_var = tk.IntVar()
        radios = tk.Frame(ai_card, bg=CARD)
        radios.grid(row=1, column=0, sticky="ew", padx=12)
        for value, text in ((0, "Desligada"), (1, "Normal (recomendada)"), (2, "Forte (2 passadas)")):
            tk.Radiobutton(radios, text=text, value=value, variable=self.rnn_var, command=self._rnn_changed,
                           bg=CARD, activebackground=CARD, font=(FONT, 10), selectcolor="white").pack(side="left",
                                                                                                    padx=4)
        gate_head = tk.Frame(ai_card, bg=CARD)
        gate_head.grid(row=2, column=0, sticky="ew", padx=16, pady=(6, 0))
        tk.Label(gate_head, text="Porta de voz (corta o que não parece voz nas pausas)", bg=CARD, fg=TEXT,
                 font=(FONT, 10)).pack(side="left")
        self.gate_label = tk.Label(gate_head, text="", bg=CARD, fg=ACCENT_DARK, font=(FONT, 10, "bold"))
        self.gate_label.pack(side="right")
        self.gate_var = tk.DoubleVar()
        ttk.Scale(ai_card, from_=0.0, to=eq.GATE_MAX, variable=self.gate_var, style="Eq.Horizontal.TScale",
                  command=lambda _v: self._rnn_changed()).grid(row=3, column=0, sticky="ew", padx=16, pady=(2, 0))
        self.ai_hint = tk.Label(ai_card, text="", bg=CARD, fg=MUTED, font=(FONT, 9), anchor="w", justify="left",
                                wraplength=500)
        self.ai_hint.grid(row=4, column=0, sticky="ew", padx=16, pady=(2, 12))

    # -- linhas da lista de filtros -----------------------------------------------------
    def _rebuild_rows(self) -> None:
        for row in self.rows:
            row["frame"].destroy()
        self.rows = []
        self._loading = True
        for i, band in enumerate(self.profile.bands):
            self.rows.append(self._make_row(i, band))
        self._loading = False
        self.count_label.config(text=f"{len(self.profile.bands)} filtro(s)")
        self.scroller.canvas.update_idletasks()

    def _make_row(self, i: int, band: eq.Band) -> dict:
        frame = tk.Frame(self.scroller.inner, bg=CARD, highlightthickness=1, highlightbackground=CARD)
        frame.pack(fill="x", pady=1, padx=2)
        line = tk.Frame(frame, bg=CARD)
        line.pack(fill="x")
        for col, (_text, minsize) in enumerate(COLUMNS):
            line.grid_columnconfigure(col, minsize=minsize)
        line.grid_columnconfigure(len(COLUMNS), weight=1)
        var_on = tk.BooleanVar(value=band.enabled)
        var_kind = tk.StringVar(value=SHORT[band.kind])
        var_freq = tk.StringVar(value=_fmt(band.freq, 0 if band.freq >= 100 else 1))
        var_gain = tk.StringVar(value=_fmt(band.gain))
        var_q = tk.StringVar(value=_fmt(band.q, 2))
        check = tk.Checkbutton(line, variable=var_on, bg=CARD, activebackground=CARD, selectcolor="white",
                               command=lambda: self._row_edited(i))
        check.grid(row=0, column=0, sticky="w")
        kind = ttk.Combobox(line, textvariable=var_kind, values=list(SHORT.values()), state="readonly", width=19)
        kind.grid(row=0, column=1, sticky="w", padx=2)
        kind.bind("<<ComboboxSelected>>", lambda _e: self._row_edited(i, kind_changed=True))
        spins = []
        for col, (var, lo, hi, step, width) in enumerate(((var_freq, eq.FREQ_MIN, eq.FREQ_MAX, 1.0, 8),
                                                          (var_gain, eq.GAIN_MIN, eq.GAIN_MAX, 0.5, 7),
                                                          (var_q, eq.Q_MIN, eq.Q_MAX, 0.1, 6)), start=2):
            spin = ttk.Spinbox(line, textvariable=var, from_=lo, to=hi, increment=step, width=width,
                               command=lambda: self._row_edited(i))
            spin.grid(row=0, column=col, sticky="w", padx=2)
            spin.bind("<Return>", lambda _e: self._row_edited(i))
            spin.bind("<FocusOut>", lambda _e: self._row_edited(i))
            spins.append(spin)
        delete = ttk.Button(line, text="✕", width=3, style="Soft.TButton", command=lambda: self.remove_band(i))
        delete.grid(row=0, column=len(COLUMNS), sticky="e", padx=2)
        why = tk.Label(frame, text=band.why or "", bg=CARD, fg=MUTED, font=(FONT, 8), anchor="w", justify="left",
                       wraplength=470)
        if band.why:
            why.pack(fill="x", padx=28)
        for widget in (frame, line, why):
            widget.bind("<Button-1>", lambda _e, k=i: self.select(k))
        row = {"frame": frame, "on": var_on, "kind": var_kind, "freq": var_freq, "gain": var_gain, "q": var_q,
               "spins": spins, "why": why,
               "shown": {"freq": var_freq.get(), "gain": var_gain.get(), "q": var_q.get()}}
        self._row_state(row, band)
        return row

    @staticmethod
    def _row_state(row: dict, band: eq.Band) -> None:
        row["spins"][1].config(state="normal" if eq.KINDS[band.kind][1] else "disabled")   # só alguns têm ganho

    def _row_edited(self, i: int, kind_changed: bool = False) -> None:
        if self._loading or i >= len(self.rows):
            return
        row, band = self.rows[i], self.profile.bands[i]
        if kind_changed:
            new_kind = SHORT_TO_KIND.get(row["kind"].get(), band.kind)
            if new_kind != band.kind:
                band.kind, band.q, band.why = new_kind, eq.KINDS[new_kind][2], ""
                if not eq.KINDS[new_kind][1]:
                    band.gain = 0.0
                self._write_row(i)                      # a tela passa a mostrar o Q e o ganho do novo tipo
        band.enabled = bool(row["on"].get())
        for field in ("freq", "gain", "q"):             # o texto da tela é arredondado: só lê o que foi mexido
            text = row[field].get()
            if text != row["shown"][field]:
                setattr(band, field, _num(text, getattr(band, field)))
        band.clamp()
        self._write_row(i)
        self._row_state(row, band)
        self.select(i)
        self._changed()

    def _write_row(self, i: int) -> None:
        row, band = self.rows[i], self.profile.bands[i]
        row["on"].set(band.enabled)
        row["kind"].set(SHORT[band.kind])
        row["freq"].set(_fmt(band.freq, 0 if band.freq >= 100 else 1))
        row["gain"].set(_fmt(band.gain))
        row["q"].set(_fmt(band.q, 2))
        row["shown"] = {"freq": row["freq"].get(), "gain": row["gain"].get(), "q": row["q"].get()}
        if band.why:
            row["why"].config(text=band.why)
            row["why"].pack(fill="x", padx=28)
        else:
            row["why"].pack_forget()

    def select(self, i: Optional[int]) -> None:
        self.selected = i
        for k, row in enumerate(self.rows):
            row["frame"].config(highlightbackground=ACCENT if k == i else CARD, bg=ACCENT_SOFT if k == i else CARD)
        self.draw_curve()

    # -- edição do perfil ---------------------------------------------------------------
    def add_band(self, kind: str) -> None:
        self.profile.bands.append(eq.Band.new(kind, DEFAULT_FREQ[kind], DEFAULT_GAIN.get(kind, 0.0),
                                              why="Filtro seu."))
        self._rebuild_rows()
        self.select(len(self.profile.bands) - 1)
        self._changed()

    def remove_band(self, i: int) -> None:
        if 0 <= i < len(self.profile.bands):
            del self.profile.bands[i]
            self.selected = None
            self._rebuild_rows()
            self._changed()

    def clear_bands(self) -> None:
        if not self.profile.bands or not self.confirm_fn(
                "Apagar todos os filtros da lista? (Dá para voltar ao que o Limpa Som criou pelo botão "
                "'Voltar ao do Limpa Som'.)"):
            return
        self.profile.bands = []
        self.selected = None
        self._rebuild_rows()
        self._changed()

    def set_band(self, i: int, **fields) -> None:
        """Muda campos de um filtro (o arrastar da curva, a roda do mouse e os testes passam por aqui)."""
        band = self.profile.bands[i]
        for key, value in fields.items():
            setattr(band, key, value)
        band.clamp()
        self._write_row(i)
        self._row_state(self.rows[i], band)
        self._changed()

    def _preamp_changed(self) -> None:
        if self._loading:
            return
        self.profile.preamp_db = round(float(self.preamp_var.get()) * 2) / 2
        self._changed()

    def _rnn_changed(self) -> None:
        if self._loading:
            return
        self.profile.rnnoise = int(self.rnn_var.get())
        self.profile.rnnoise_gate = round(float(self.gate_var.get()) * 100) / 100
        self._changed()

    def toggle_power(self) -> None:
        self.profile.enabled = not self.profile.enabled
        self._changed(immediate=True)

    def reset_to_original(self) -> None:
        if self.original is None:
            self.info_fn("O Limpa Som ainda não criou um equalizador para este microfone: rode 'Testar o microfone' "
                         "no Limpa Som.")
            return
        enabled = self.profile.enabled
        self.profile = self.original.copy()
        self.profile.enabled = enabled
        self.profile.bands.sort(key=lambda b: (b.kind != "HP", b.freq))
        self.selected = None
        self._rebuild_rows()
        self._note = "Voltei ao equalizador que o Limpa Som criou. "
        self._changed(immediate=True)
        self.set_message(self._note.strip(), ACCENT_DARK)

    def _changed(self, immediate: bool = False) -> None:
        self._refresh_all()
        self._schedule_apply(immediate)

    def _refresh_all(self) -> None:
        self._loading = True
        self.preamp_var.set(self.profile.preamp_db)
        self.rnn_var.set(self.profile.rnnoise)
        self.gate_var.set(self.profile.rnnoise_gate)
        self._loading = False
        self.preamp_label.config(text=("%+.1f dB" % self.profile.preamp_db).replace(".", ","))
        self.gate_label.config(text="%d%%" % round(self.profile.rnnoise_gate * 100))
        have = bool(self.apo and (self.apo.has_rnnoise_mono or self.apo.has_rnnoise_stereo))
        if self.profile.rnnoise == 0:
            hint = "A IA está desligada: sobram só os filtros, que quase não tiram o chiado espalhado na faixa da voz."
        elif not have:
            hint = "Plugin RNNoise não encontrado na pasta do Equalizer APO: a IA não vai funcionar."
        else:
            hint = ("Porta mais alta silencia mais o chiado entre as palavras; alta demais pode cortar voz baixa. "
                    "Duas passadas limpam mais em alguns casos e pioram em outros.")
        self.ai_hint.config(text=hint, fg=WARN if (self.profile.rnnoise and not have) else MUTED)
        self.power.config(text="Equalizador: LIGADO  (clique para desligar)" if self.profile.enabled
                          else "Equalizador: DESLIGADO  (clique para ligar)",
                          style="Big.TButton" if self.profile.enabled else "Power.TButton")
        self.reset_btn.state(["!disabled"] if self.original is not None else ["disabled"])
        if self.has_stored and self.original is not None:
            when = ""
            try:
                when = " Criado em " + datetime.fromisoformat(self.created_at).strftime("%d/%m às %H:%M") + "."
            except ValueError:
                pass
            self.origin_label.config(text=(self.profile.summary or "Criado pelo Limpa Som.") + when, fg=MUTED)
        elif self.has_stored:
            self.origin_label.config(text=self.profile.summary or "Equalizador seu.", fg=MUTED)
        else:
            self.origin_label.config(text=self.NO_PROFILE_TEXT, fg=WARN)
        self.draw_curve()

    # -- curva ---------------------------------------------------------------------------
    def _geom(self):
        c = self.curve
        w, h = max(c.winfo_width(), 240), max(c.winfo_height(), 160)
        ml, mr, mt, mb = 40, 12, 10, 24
        return w, h, ml, mr, mt, mb, w - ml - mr, h - mt - mb

    def _x_of(self, f: float) -> float:
        w, h, ml, mr, mt, mb, pw, ph = self._geom()
        return ml + pw * (np.log10(max(f, 20.0)) - np.log10(20.0)) / (np.log10(20000.0) - np.log10(20.0))

    def _f_of(self, x: float) -> float:
        w, h, ml, mr, mt, mb, pw, ph = self._geom()
        t = min(max((x - ml) / pw, 0.0), 1.0)
        return float(10 ** (np.log10(20.0) + t * (np.log10(20000.0) - np.log10(20.0))))

    def _y_of(self, db: float) -> float:
        w, h, ml, mr, mt, mb, pw, ph = self._geom()
        t = (min(max(db, CURVE_DB_MIN), CURVE_DB_MAX) - CURVE_DB_MIN) / (CURVE_DB_MAX - CURVE_DB_MIN)
        return mt + ph * (1 - t)

    def _db_of(self, y: float) -> float:
        w, h, ml, mr, mt, mb, pw, ph = self._geom()
        t = 1 - min(max((y - mt) / ph, 0.0), 1.0)
        return CURVE_DB_MIN + t * (CURVE_DB_MAX - CURVE_DB_MIN)

    def point_xy(self, i: int):
        """Onde o ponto do filtro `i` aparece na curva: (x, y) em pixels."""
        band = self.profile.bands[i]
        db = band.gain if eq.KINDS[band.kind][1] else (-8.0 if band.kind == "NO" else -3.0)
        return self._x_of(band.freq), self._y_of(db)

    def draw_curve(self) -> None:
        c = self.curve
        c.delete("all")
        w, h, ml, mr, mt, mb, pw, ph = self._geom()
        for db in (-24, -12, 0, 12):
            c.create_line(ml, self._y_of(db), w - mr, self._y_of(db), fill="#cbd5e1" if db == 0 else "#e6ebf1",
                          width=2 if db == 0 else 1)
            c.create_text(ml - 5, self._y_of(db), text=f"{db:+d}" if db else "0", anchor="e", fill=MUTED,
                          font=(FONT, 8))
        for f, label in ((50, "50"), (100, "100"), (500, "500"), (1000, "1k"), (5000, "5k"), (10000, "10k")):
            c.create_line(self._x_of(f), mt, self._x_of(f), mt + ph, fill="#eef2f6")
            c.create_text(self._x_of(f), h - 11, text=label, fill=MUTED, font=(FONT, 8))
        on = self.profile.enabled
        freqs, db = eq.frequency_response(self.profile, 48000.0, points=360, force_enabled=True)
        pts = []
        for f, g in zip(freqs, db):
            pts += [self._x_of(f), self._y_of(g)]
        c.create_line(*pts, fill=ACCENT if on else "#94a3b8", width=2.5, dash=None if on else (6, 4))
        if self.selected is not None and self.selected < len(self.profile.bands):
            sel = eq.band_response_db(self.profile.bands[self.selected], freqs, 48000.0)
            spts = []
            for f, g in zip(freqs, sel):
                spts += [self._x_of(f), self._y_of(g)]
            c.create_line(*spts, fill="#f59e0b", width=1.5)
        for i, band in enumerate(self.profile.bands):
            x, y = self.point_xy(i)
            r = 7 if i == self.selected else 5
            c.create_oval(x - r, y - r, x + r, y + r, fill=POINT_COLOR[band.kind] if band.enabled else "#cbd5e1",
                          outline="white" if band.enabled else "#94a3b8", width=2)
        if not on:
            c.create_text(ml + 8, mt + 6, anchor="nw", fill=MUTED, font=(FONT, 9),
                          text="Desligado: o tracejado mostra o que muda quando você ligar")

    def _nearest_point(self, x: float, y: float) -> Optional[int]:
        best, best_d = None, 15.0
        for i in range(len(self.profile.bands)):
            px, py = self.point_xy(i)
            d = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
            if d < best_d:
                best, best_d = i, d
        return best

    def _curve_press(self, event) -> None:
        self.drag = self._nearest_point(event.x, event.y)
        self.select(self.drag)

    def _curve_motion(self, event) -> None:
        if self.drag is not None:
            self.drag_point(self.drag, event.x, event.y)

    def _curve_release(self, _event) -> None:
        self.drag = None

    def _curve_wheel(self, event) -> None:
        if self.selected is not None and self.selected < len(self.profile.bands):
            self.set_band(self.selected, q=self.profile.bands[self.selected].q * (1.1 if event.delta > 0 else 1 / 1.1))

    def drag_point(self, i: int, x: float, y: float) -> None:
        """Move o ponto: x muda a frequência e, nos filtros que têm ganho, y muda o ganho."""
        fields = {"freq": round(self._f_of(x), 1)}
        if eq.KINDS[self.profile.bands[i].kind][1]:
            fields["gain"] = round(self._db_of(y) * 2) / 2
        self.set_band(i, **fields)

    # -- escrever no Equalizer APO -------------------------------------------------------
    def later(self, ms: int, fn: Callable) -> None:
        if not self._closed:
            self._after_ids.add(self.after(ms, fn))

    def _schedule_apply(self, immediate: bool = False) -> None:
        if self._apply_job is not None:
            try:
                self.after_cancel(self._apply_job)
            except Exception:
                pass
        self._apply_job = self.after(0 if immediate else 350, self.apply_now)

    def _read_config(self) -> str:
        try:
            return (self.apo.config_dir / "config.txt").read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    def _config_text(self) -> str:
        have = (self.apo.has_rnnoise_mono, self.apo.has_rnnoise_stereo) if self.apo else (False, False)
        return eq.build_config(realtime.device_pattern(self.mic.name), self.profile, label=self.mic.name,
                               rnnoise_available=have)

    def apply_now(self) -> None:
        self._apply_job = None
        note, self._note = self._note, ""
        self._save()
        if self.apo is None:
            self.set_message("Não encontrei o Equalizer APO neste computador.", BAD)
            return
        installed = realtime.is_installed(self._read_config())
        if not installed and not self.profile.enabled:
            self.set_message(note + "Equalizador desligado. Nada foi alterado no Equalizer APO.", MUTED)
            return
        text = self._config_text()
        if not installed:
            if not self.confirm_fn(
                    "Para o equalizador funcionar preciso acrescentar uma linha ao config.txt do Equalizer APO "
                    "(guardo uma cópia em config.txt.limpa_som.bak). Os ajustes que já existem lá continuam valendo "
                    "para alto-falantes e fones, mas deixam de valer para microfones.\n\nPosso fazer isso?"):
                self.set_message("Nada foi alterado no Equalizer APO.", MUTED)
                self.last_apply_ok = None
                return
            result = realtime.apply(self.apo, text)
        else:
            result = realtime.write_mic_file(self.apo, text)
        self.last_apply_ok = result.ok
        if result.ok:
            what = "Equalizador ligado" if self.profile.enabled else "Equalizador desligado"
            self.set_message(f"{note}{what} às " + time.strftime("%H:%M:%S") + ". O Windows relê em poucos segundos.",
                             GOOD)
        else:
            self.set_message(result.message, BAD)
        self.refresh_status()

    def undo_all(self) -> None:
        if self.apo is None:
            return
        if not self.confirm_fn("Tirar o equalizador do Limpa Som do Equalizer APO e restaurar o config.txt como "
                               "estava antes?"):
            return
        result = realtime.remove(self.apo)
        self.last_apply_ok = None
        self.set_message(result.message, GOOD if result.ok else BAD)
        self.refresh_status()

    # -- situação do APO -----------------------------------------------------------------
    def set_message(self, text: str, color: str = MUTED) -> None:
        self.message.config(text=text, fg=color)

    def _dot(self, key: str, state: Optional[bool], extra: str = "") -> None:
        dot, label, base = self.dots[key]
        dot.config(fg={True: GOOD, False: BAD, None: MUTED}[state])
        label.config(text=base + extra)

    def refresh_status(self) -> None:
        self._dot("apo", self.apo is not None)
        attached = self.attached_fn(self.mic.guid) if self.apo is not None else False
        self.attached = attached
        self._dot("attached", attached, "" if attached is not None else " (não deu para verificar)")
        active = False
        if self.apo is not None:
            active = (realtime.is_installed(self._read_config())
                      and (self.apo.config_dir / realtime.MIC_FILE).exists() and self.profile.enabled)
        self._dot("active", active, "" if active else " (ainda não)")
        if self.apo is None:
            self.banner_text.config(text=self.NO_APO_TEXT)
            self.banner_buttons.pack_forget()
            self.banner.grid(row=2, column=0, columnspan=6, sticky="ew")
        elif attached is not True:
            self.banner_text.config(text=self.NOT_ATTACHED_TEXT)
            self.banner_buttons.pack(side="right", padx=14, pady=10)
            self.banner.grid(row=2, column=0, columnspan=6, sticky="ew")
        else:
            self.banner.grid_remove()

    def open_configurator(self) -> None:
        if self.apo is None or not realtime.open_configurator(self.apo):
            self.info_fn("Procure 'Equalizer APO Configurator' no menu Iniciar e abra com o botão direito > "
                         "Executar como administrador.")

    @staticmethod
    def _open_main() -> None:
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        try:
            subprocess.Popen([str(pyw if pyw.exists() else exe), "-m", "limpa_som"], cwd=str(ROOT))
        except OSError:
            pass

    # -- troca de microfone --------------------------------------------------------------
    def _mic_changed(self, _event=None) -> None:
        labels = [m.label for m in self.mics]
        self.mic = self.mics[labels.index(self.mic_var.get())]
        self._load_mic()
        self.selected = None
        self._rebuild_rows()
        self._refresh_all()
        self.refresh_status()
        if realtime.is_installed(self._read_config()):      # o arquivo de filtros vale para um microfone só
            self._schedule_apply(immediate=True)

    # -- ciclo de vida -------------------------------------------------------------------
    def _tick(self) -> None:
        if self._closed:
            return
        self._ticks += 1
        if self._ticks % 20 == 0:                           # a cada ~2 s, confere o registro do Windows
            self.refresh_status()
        self._tick_id = self.after(POLL_MS, self._tick)

    def on_close(self) -> None:
        self.destroy()

    def destroy(self) -> None:
        self._closed = True
        for after_id in [getattr(self, "_tick_id", None), self._apply_job, *self._after_ids]:
            if after_id:
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
        super().destroy()

    def _tk_error(self, exc, val, tb) -> None:
        import traceback
        self.errors.append("".join(traceback.format_exception(exc, val, tb)))
        try:
            self.set_message(f"Erro inesperado: {val}", BAD)
            from . import wizard
            wizard.log_error(self.out_dir, val)
        except Exception:
            pass

    def snap(self, name: str) -> None:
        if self.snap_dir is None:
            return
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        if self.state() == "iconic":
            self.deiconify()
        capture_window(self, self.snap_dir / f"{name}.png")


# ------------------------------------------------------------------------ entrada ---
def build_mics(preferred: Optional[str] = None):
    from .devices import list_input_devices
    from . import winaudio
    devices = list_input_devices()
    mics: List[MicChoice] = []
    start = 0
    for d in devices:
        control = winaudio.find_control(d.name)
        guid = realtime.endpoint_guid(control.endpoint_id) if control is not None else None
        mics.append(MicChoice(d.name + ("  (padrão)" if d.is_default else ""), d.name, guid, d))
        if (preferred and preferred.lower() in d.name.lower()) or (not preferred and d.is_default):
            start = len(mics) - 1
    return mics, start


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="limpa_som.equalizador")
    p.add_argument("--dispositivo", help="parte do nome do microfone")
    p.add_argument("--saida", help="pasta dos resultados")
    p.add_argument("--captura", help="(desenvolvimento) salva uma imagem da janela e fecha")
    args = p.parse_args(argv)
    try:
        mics, start = build_mics(args.dispositivo)
        if not mics:
            raise RuntimeError("Nenhum microfone encontrado.")
        app = EqualizerApp(realtime.find_apo(), mics, start, Path(args.saida) if args.saida else None,
                           snap_dir=Path(args.captura) if args.captura else None)
        if args.captura:
            app.later(1500, lambda: (app.snap("equalizador"), app.destroy()))
        app.mainloop()
        return 0
    except Exception as exc:
        try:
            from . import wizard
            wizard.log_error(ROOT / "resultados", exc)
            messagebox.showerror("Equalizador ao vivo", f"Não consegui abrir: {exc}")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
