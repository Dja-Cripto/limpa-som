"""Equalizador do microfone: filtros editáveis, arquivo do Equalizer APO, curva de resposta e armazenamento.

O Equalizer APO é quem processa o áudio, em segundo plano, dentro do Windows (para todos os programas).
Este módulo descreve o que ele deve fazer: uma lista de filtros (pico, prateleiras, corta-graves, corta-agudos,
notch), uma pré-amplificação e a supressão de ruído por IA (RNNoise, com porta de voz ajustável). Gera o texto
que o APO lê e calcula a curva de resposta (os mesmos biquads 'Audio EQ Cookbook' do APO) para mostrar na tela.

Quem CRIA o equalizador é o Limpa Som (design.py), a partir do ruído e da voz que ele mediu. A janela do
Equalizador (equalizador.py) só mostra e deixa editar.
"""
from __future__ import annotations

import base64
import json
import struct
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import signal

STORE_NAME = "equalizador.json"

FREQ_MIN, FREQ_MAX = 20.0, 20000.0
GAIN_MIN, GAIN_MAX = -24.0, 24.0
Q_MIN, Q_MAX = 0.1, 100.0
PREAMP_MIN, PREAMP_MAX = -20.0, 30.0
GATE_MAX = 0.95

# tipo -> (nome na tela, usa ganho?, Q padrão)
KINDS: Dict[str, Tuple[str, bool, float]] = {
    "PK": ("Pico (aumenta ou corta uma região)", True, 1.0),
    "LS": ("Prateleira de graves", True, 0.71),
    "HS": ("Prateleira de agudos", True, 0.71),
    "HP": ("Corta graves (passa-altas)", False, 0.71),
    "LP": ("Corta agudos (passa-baixas)", False, 0.71),
    "NO": ("Notch (tira uma frequência só)", False, 30.0),
}
KIND_NAMES = {k: v[0] for k, v in KINDS.items()}


def _clip(value: float, lo: float, hi: float) -> float:
    return float(min(max(float(value), lo), hi))


@dataclass
class Band:
    kind: str = "PK"
    freq: float = 1000.0
    gain: float = 0.0               # dB (só PK, LS e HS usam)
    q: float = 1.0
    enabled: bool = True
    why: str = ""                   # explicação em português (o Limpa Som escreve o porquê de cada filtro)

    def clamp(self) -> "Band":
        if self.kind not in KINDS:
            self.kind = "PK"
        self.freq = _clip(self.freq, FREQ_MIN, FREQ_MAX)
        self.gain = _clip(self.gain, GAIN_MIN, GAIN_MAX) if KINDS[self.kind][1] else 0.0
        self.q = _clip(self.q, Q_MIN, Q_MAX)
        return self

    @staticmethod
    def new(kind: str, freq: float, gain: float = 0.0, q: Optional[float] = None, why: str = "") -> "Band":
        return Band(kind, freq, gain, KINDS[kind][2] if q is None else q, True, why).clamp()


@dataclass
class Profile:
    enabled: bool = True
    preamp_db: float = 0.0
    bands: List[Band] = field(default_factory=list)
    rnnoise: int = 0                 # passadas da IA anti-chiado: 0 desligada, 1 normal, 2 mais forte
    rnnoise_gate: float = 0.45       # porta de voz: 0 = nunca corta; maior = corta mais o que não é voz
    summary: str = ""                # o que o Limpa Som viu e fez (texto pronto)

    def clamp(self) -> "Profile":
        self.preamp_db = _clip(self.preamp_db, PREAMP_MIN, PREAMP_MAX)
        self.rnnoise = int(min(max(int(self.rnnoise), 0), 2))
        self.rnnoise_gate = _clip(self.rnnoise_gate, 0.0, GATE_MAX)
        for band in self.bands:
            band.clamp()
        return self

    def copy(self) -> "Profile":
        return Profile.from_json(self.to_json())

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def from_json(text: str) -> "Profile":
        try:
            data = json.loads(text)
        except Exception:
            return Profile()
        return Profile.from_dict(data) if isinstance(data, dict) else Profile()

    @staticmethod
    def from_dict(data: dict) -> "Profile":
        if "bands" not in data and "highpass_hz" in data:        # formato antigo (4 faixas fixas)
            return _from_legacy(data)
        p = Profile()
        for key in ("enabled", "preamp_db", "rnnoise", "rnnoise_gate", "summary"):
            if key in data:
                setattr(p, key, data[key])
        p.bands = []
        for item in data.get("bands", []):
            try:
                p.bands.append(Band(**{k: item[k] for k in ("kind", "freq", "gain", "q", "enabled", "why")
                                       if k in item}))
            except TypeError:
                continue
        return p.clamp()

    def active_bands(self) -> List[Band]:
        return [b for b in self.bands if b.enabled]


def _from_legacy(d: dict) -> Profile:
    """Converte o formato da primeira versão da janela (HPF + 4 faixas + zumbido + apitos)."""
    from .dsp import hum_notches
    p = Profile(enabled=bool(d.get("enabled", True)), rnnoise=1 if d.get("rnnoise", False) else 0)
    if d.get("highpass_hz", 0) > 0:
        p.bands.append(Band.new("HP", d["highpass_hz"], why="Corte de graves"))
    if d.get("remove_hum", False) and d.get("hum_f0", 0) > 0:
        for f in hum_notches(d["hum_f0"], int(d.get("hum_harmonics", 0))):
            p.bands.append(Band.new("NO", f, why="Zumbido da rede elétrica"))
    if d.get("remove_tones", False):
        for f in d.get("tones", []):
            p.bands.append(Band.new("NO", f, why="Apito de interferência"))
    for attr, kind, fc, q in (("bass_db", "PK", 120.0, 0.7), ("body_db", "PK", 300.0, 1.0),
                              ("presence_db", "PK", 3000.0, 1.0), ("air_db", "PK", 10000.0, 0.5)):
        if abs(d.get(attr, 0.0)) >= 0.25:
            p.bands.append(Band.new(kind, fc, d[attr], q))
    return p.clamp()


# --------------------------------------------------------------- texto do Equalizer APO ---
def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _f(value: float) -> str:
    return ("%.0f" % value) if (value >= 100 or abs(value - round(value)) < 0.05) else ("%.1f" % value)


def band_line(b: Band) -> str:
    """Uma linha do config do APO. PK/LSC/HSC/NO/HPQ/LPQ são os tipos documentados com Q explícito."""
    if b.kind == "PK":
        return "Filter: ON PK Fc %s Hz Gain %.1f dB Q %.2f" % (_f(b.freq), b.gain, b.q)
    if b.kind == "LS":
        return "Filter: ON LSC Fc %s Hz Gain %.1f dB Q %.2f" % (_f(b.freq), b.gain, b.q)
    if b.kind == "HS":
        return "Filter: ON HSC Fc %s Hz Gain %.1f dB Q %.2f" % (_f(b.freq), b.gain, b.q)
    if b.kind == "HP":
        return "Filter: ON HPQ Fc %s Hz Q %.3f" % (_f(b.freq), b.q)
    if b.kind == "LP":
        return "Filter: ON LPQ Fc %s Hz Q %.3f" % (_f(b.freq), b.q)
    return "Filter: ON NO Fc %s Hz Q %.1f" % (_f(b.freq), b.q)


def rnnoise_chunk(gate: float, grace: float = 18.0, retro: float = 10.0) -> str:
    """Estado do plugin RNNoise (o mesmo formato que o próprio Equalizer APO grava), com a porta de voz pedida."""
    xml = ('<?xml version="1.0" encoding="UTF-8"?> <RNNoise><PARAM id="vad_grace_period" value="%.1f"/>'
           '<PARAM id="vad_retroactive_grace_period" value="%.1f"/><PARAM id="vad_threshold" value="%s"/></RNNoise>'
           % (grace, retro, "%.16g" % float(np.float32(gate)))).encode("utf-8")
    return base64.b64encode(b"VC2!" + struct.pack("<I", len(xml)) + xml + b"\0").decode("ascii")


def build_config(pattern: str, profile: Profile, label: str = "", rnnoise_available: Optional[tuple] = (True, True),
                 when: Optional[datetime] = None) -> str:
    """Texto do limpa_som_mic.txt (Equalizer APO): pré-amplificação, filtros e, por último, a IA anti-chiado."""
    when = when or datetime.now()
    lines = [
        "# Gerado pelo Limpa Som em %s" % when.strftime("%d/%m/%Y %H:%M"),
        "# Microfone: %s" % _ascii(label or pattern),
        "# Para desligar: apague as linhas '[Limpa Som]' do config.txt ou use o menu do programa.",
        "Device: %s" % pattern,
        "Channel: all",
    ]
    if not profile.enabled:
        lines.append("# Equalizador DESLIGADO no painel: nenhum filtro ativo.")
        return "\n".join(lines) + "\n"
    if abs(profile.preamp_db) >= 0.05:
        lines += ["# ganho antes de tudo: a voz chega mais forte ao filtro de ruido",
                  "Preamp: %.1f dB" % profile.preamp_db]
    for b in profile.active_bands():
        if b.why:
            lines.append("# " + _ascii(b.why))
        lines.append(band_line(b))
    have_mono, have_stereo = rnnoise_available or (False, False)
    if profile.rnnoise > 0 and (have_mono or have_stereo):
        chunk = rnnoise_chunk(profile.rnnoise_gate)
        lines.append("# RNNoise (IA de supressao de ruido), %d passada(s), porta de voz %.2f"
                     % (profile.rnnoise, profile.rnnoise_gate))
        for _ in range(profile.rnnoise):
            if have_mono and have_stereo:
                lines += ["If: inputChannelCount == 1", 'VSTPlugin: Library rnnoise_mono.dll ChunkData "%s"' % chunk,
                          "Else:", 'VSTPlugin: Library rnnoise_stereo.dll ChunkData "%s"' % chunk, "EndIf:"]
            else:
                lines.append('VSTPlugin: Library %s ChunkData "%s"'
                             % ("rnnoise_mono.dll" if have_mono else "rnnoise_stereo.dll", chunk))
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ filtros (iguais aos do APO) ---
def _rbj(kind: str, fs: float, fc: float, q: float, gain_db: float = 0.0) -> np.ndarray:
    w0 = 2.0 * np.pi * min(fc, fs / 2 * 0.99) / fs
    cos_w, sin_w = np.cos(w0), np.sin(w0)
    alpha = sin_w / (2.0 * q)
    if kind in ("peak", "lowshelf", "highshelf"):
        a = 10.0 ** (gain_db / 40.0)
    if kind == "peak":
        b = [1 + alpha * a, -2 * cos_w, 1 - alpha * a]
        den = [1 + alpha / a, -2 * cos_w, 1 - alpha / a]
    elif kind == "notch":
        b = [1.0, -2 * cos_w, 1.0]
        den = [1 + alpha, -2 * cos_w, 1 - alpha]
    elif kind == "highpass":
        b = [(1 + cos_w) / 2, -(1 + cos_w), (1 + cos_w) / 2]
        den = [1 + alpha, -2 * cos_w, 1 - alpha]
    elif kind == "lowpass":
        b = [(1 - cos_w) / 2, 1 - cos_w, (1 - cos_w) / 2]
        den = [1 + alpha, -2 * cos_w, 1 - alpha]
    elif kind == "lowshelf":
        s = 2.0 * np.sqrt(a) * alpha
        b = [a * ((a + 1) - (a - 1) * cos_w + s), 2 * a * ((a - 1) - (a + 1) * cos_w),
             a * ((a + 1) - (a - 1) * cos_w - s)]
        den = [(a + 1) + (a - 1) * cos_w + s, -2 * ((a - 1) + (a + 1) * cos_w), (a + 1) + (a - 1) * cos_w - s]
    elif kind == "highshelf":
        s = 2.0 * np.sqrt(a) * alpha
        b = [a * ((a + 1) + (a - 1) * cos_w + s), -2 * a * ((a - 1) + (a + 1) * cos_w),
             a * ((a + 1) + (a - 1) * cos_w - s)]
        den = [(a + 1) - (a - 1) * cos_w + s, 2 * ((a - 1) - (a + 1) * cos_w), (a + 1) - (a - 1) * cos_w - s]
    else:
        raise ValueError(kind)
    b, den = np.asarray(b) / den[0], np.asarray(den) / den[0]
    return np.array([[b[0], b[1], b[2], 1.0, den[1], den[2]]])


_KIND_TO_RBJ = {"PK": "peak", "LS": "lowshelf", "HS": "highshelf", "HP": "highpass", "LP": "lowpass", "NO": "notch"}
_FLAT = np.array([[1.0, 0.0, 0.0, 1.0, 0.0, 0.0]])


def band_sos(b: Band, fs: float) -> np.ndarray:
    return _rbj(_KIND_TO_RBJ[b.kind], fs, b.freq, b.q, b.gain)


def profile_sos(profile: Profile, fs: float, force_enabled: bool = False) -> np.ndarray:
    """Todos os filtros ativos, em cascata (sem a IA anti-chiado, que só existe dentro do Windows)."""
    if not (profile.enabled or force_enabled):
        return _FLAT
    sections = [band_sos(b, fs) for b in profile.active_bands() if b.freq < fs / 2 * 0.99]
    return np.vstack(sections) if sections else _FLAT


def frequency_response(profile: Profile, fs: float = 48000.0, points: int = 300,
                       force_enabled: bool = False) -> Tuple[np.ndarray, np.ndarray]:
    """(frequências em Hz, ganho em dB) de 20 Hz a 20 kHz, para desenhar a curva (sem a pré-amplificação)."""
    freqs = np.geomspace(20.0, min(20000.0, fs / 2 * 0.99), points)
    _w, h = signal.sosfreqz(profile_sos(profile, fs, force_enabled), worN=freqs, fs=fs)
    return freqs, 20.0 * np.log10(np.maximum(np.abs(h), 1e-9))


def band_response_db(b: Band, freqs: np.ndarray, fs: float = 48000.0) -> np.ndarray:
    _w, h = signal.sosfreqz(band_sos(b, fs), worN=freqs, fs=fs)
    return 20.0 * np.log10(np.maximum(np.abs(h), 1e-9))


def describe(profile: Profile) -> List[str]:
    """Resumo em português do que o equalizador faz (para a tela e para o relatório)."""
    out: List[str] = []
    hp = [b for b in profile.active_bands() if b.kind == "HP"]
    lp = [b for b in profile.active_bands() if b.kind == "LP"]
    notches = [b for b in profile.active_bands() if b.kind == "NO"]
    others = [b for b in profile.active_bands() if b.kind in ("PK", "LS", "HS")]
    if abs(profile.preamp_db) >= 0.05:
        out.append("Ganho de %+.1f dB antes de tudo, para a voz chegar forte ao filtro de ruído." % profile.preamp_db)
    if hp:
        out.append("Corta os graves abaixo de %.0f Hz (ronco, vibração, sopro)." % min(b.freq for b in hp))
    if lp:
        out.append("Corta os agudos acima de %.0f Hz (onde a voz quase não passa e o chiado mora)." % min(
            b.freq for b in lp))
    if notches:
        out.append("%d notch(es) em frequências fixas de zumbido e apitos." % len(notches))
    if others:
        out.append("%d ajuste(s) de equalização para baixar o ruído nas faixas onde ele é maior que a voz." % len(others))
    if profile.rnnoise:
        out.append("IA anti-chiado (RNNoise) com %d passada(s) e porta de voz em %d%%." % (
            profile.rnnoise, round(profile.rnnoise_gate * 100)))
    return out or ["Nenhum filtro ativo."]


# ------------------------------------------------------------------------- armazenamento ---
@dataclass
class Stored:
    atual: Profile
    limpa_som: Optional[Profile] = None       # o que o Limpa Som criou (para voltar atrás)
    criado_em: str = ""


def store_path(out_dir: Path) -> Path:
    return Path(out_dir) / STORE_NAME


def _read_store(out_dir: Path) -> dict:
    try:
        data = json.loads(store_path(out_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_stored(out_dir: Path, mic_name: str) -> Optional[Stored]:
    entry = _read_store(out_dir).get(mic_name)
    if not isinstance(entry, dict):
        return None
    if "atual" in entry:
        original = entry.get("limpa_som")
        return Stored(Profile.from_dict(entry["atual"]), Profile.from_dict(original) if original else None,
                      str(entry.get("criado_em", "")))
    return Stored(Profile.from_dict(entry))                  # entrada antiga: só os ajustes


def save_stored(out_dir: Path, mic_name: str, atual: Profile, limpa_som: Optional[Profile] = None,
                criado_em: Optional[str] = None, keep_original: bool = True) -> None:
    """Grava o perfil do microfone. `limpa_som` novo substitui o original; sem ele, o original é mantido."""
    data = _read_store(out_dir)
    entry = data.get(mic_name) if isinstance(data.get(mic_name), dict) else {}
    original = json.loads(limpa_som.to_json()) if limpa_som is not None else (
        entry.get("limpa_som") if keep_original else None)
    when = criado_em or (datetime.now().isoformat(timespec="minutes") if limpa_som is not None
                         else entry.get("criado_em", ""))
    data[mic_name] = {"atual": json.loads(atual.to_json()), "limpa_som": original, "criado_em": when}
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    store_path(out_dir).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
