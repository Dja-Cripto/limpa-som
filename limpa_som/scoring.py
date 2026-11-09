"""Nota de 0 a 10 para a qualidade do áudio gravado."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import numpy as np

from .metrics import Measurement

# (relação sinal/ruído em dB com ponderação A, nota)
SNR_ANCHORS = [(10, 0.5), (20, 2.5), (30, 5.0), (36, 6.5), (42, 7.8), (48, 8.8), (54, 9.5), (60, 10.0)]
ASSUMED_SPEECH_A_DB = -26.0  # usado só quando não há gravação de fala


@dataclass
class Penalty:
    label: str
    points: float


@dataclass
class Score:
    value: float
    base: float
    snr_db: float
    estimated: bool
    penalties: List[Penalty] = field(default_factory=list)

    @property
    def label(self) -> str:
        v = self.value
        if v >= 9.0:
            return "Excelente, nível de estúdio"
        if v >= 8.0:
            return "Muito bom"
        if v >= 7.0:
            return "Bom"
        if v >= 6.0:
            return "Razoável, chiado perceptível em fones"
        if v >= 4.5:
            return "Fraco, chiado evidente"
        return "Ruim"

    @property
    def text(self) -> str:
        return f"{self.value:.1f}".replace(".", ",")


DIGITAL_CLEANING_MAX_GAIN = 2.5  # limpeza digital melhora, mas não faz milagre
DIGITAL_CLEANING_CEILING = 9.5   # e áudio tratado por software nunca é "estúdio perfeito"


def cap_digital(cleaned: Score, raw: Score) -> Score:
    """A nota da limpeza digital mede o piso nas pausas e promete demais: limita a +2,5 sobre a real."""
    ceiling = min(DIGITAL_CLEANING_CEILING, raw.value + DIGITAL_CLEANING_MAX_GAIN)
    if cleaned.value <= ceiling:
        return cleaned
    return Score(value=round(ceiling, 1), base=cleaned.base, snr_db=cleaned.snr_db,
                 estimated=True, penalties=cleaned.penalties)


def snr_to_score(snr_db: float) -> float:
    xs, ys = zip(*SNR_ANCHORS)
    return float(np.interp(snr_db, xs, ys, left=0.0, right=10.0))


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def compute_score(m: Measurement) -> Score:
    """Nota = nota da relação sinal/ruído x (1 - cada defeito).

    Os defeitos reduzem a nota em PROPORÇÃO (e não por subtração): assim um áudio ruim mas com algum
    valor nunca vira 0,0 de repente, e antes/depois continuam distinguíveis.
    """
    n = m.noise
    estimated = m.speech is None
    snr = m.snr_db if m.snr_db is not None else ASSUMED_SPEECH_A_DB - n.rms_a_db
    base = snr_to_score(snr)
    cuts: List[tuple] = []   # (descrição, fração da nota perdida)

    if m.speech is not None:
        runs = m.speech.clip_runs
        if runs > 0:
            frac = 0.12 if runs <= 5 else 0.30 if runs <= 30 else 0.55 if runs <= 200 else 0.70
            cuts.append((f"áudio estourando ({runs} trechos cortados)", frac))
        excess = m.speech.pause_noise_a_db - n.rms_a_db
        if excess >= 6.0:
            cuts.append(("ruído sobe quando você fala", _clamp((excess - 6.0) / 10.0) * 0.20))

    if n.hum is not None:
        cuts.append((f"zumbido de {n.hum.f0:.0f} Hz", _clamp((n.hum.prominence_db - 6.0) / 10.0) * 0.35))

    if n.tones:
        top = n.tones[0]
        frac = 0.35 * _clamp((top.prominence_db - 12.0) / 15.0) * _clamp(n.tonal_share / 0.3, 0.25, 1.0)
        cuts.append((f"apito/tom em {top.freq:.0f} Hz", frac))

    if n.cutoff_hz is not None and n.cutoff_hz < 17000:
        c = n.cutoff_hz
        cuts.append((f"som cortado acima de {c / 1000:.1f} kHz",
                     0.50 if c < 5000 else 0.35 if c < 9000 else 0.15 if c < 13000 else 0.06))

    if abs(n.drift_db) >= 5.0 and n.drift_r2 >= 0.5:
        cuts.append(("ganho automático oscilando (AGC)", 0.20 if abs(n.drift_db) < 10 else 0.30))

    if n.digital_silence_frac >= 0.2:
        cuts.append(("silêncio digital (supressor/gate ativo)", 0.10))

    if n.transients >= 3:
        cuts.append(("estalos no silêncio", 0.08))

    value, penalties = base, []
    for label, frac in cuts:
        if frac < 0.02:
            continue
        lost = value * frac
        value -= lost
        penalties.append(Penalty(label, round(lost, 2)))
    value = max(0.0, min(10.0, value))
    return Score(value=round(value, 1), base=round(base, 1), snr_db=float(snr),
                 estimated=estimated, penalties=penalties)
