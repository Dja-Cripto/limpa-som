"""Ajuste automático de ganho: acha a combinação de 'reforço' + volume com menos chiado
para o mesmo nível de voz."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Protocol, Tuple

import numpy as np

from .metrics import NoiseReport, SpeechReport

TARGET_PEAK_DB = -6.0   # pico da voz desejado (dBFS)
TARGET_RMS_DB = -23.0   # nível médio da voz desejado (dBFS)
SAFE_PEAK_DB = -3.0     # acima disso o risco de estourar é alto
SETTLE_S = 1.0          # tempo para o driver estabilizar depois de mudar o ganho
SWEEP_S = 3.6           # segundos medidos de silêncio por configuração


@dataclass
class Settings:
    boost_db: Optional[float]   # None = o dispositivo não tem reforço
    volume_db: float
    muted: bool = False

    @property
    def total_gain_db(self) -> float:
        return self.volume_db + (self.boost_db or 0.0)

    def describe(self) -> str:
        boost = "sem reforço" if self.boost_db is None else f"reforço {self.boost_db:+.0f} dB"
        return f"{boost}, volume {self.volume_db:+.1f} dB".replace(".", ",")


class Control(Protocol):
    has_boost: bool

    def boost_options(self) -> List[float]: ...
    def volume_range(self) -> Tuple[float, float, float]: ...
    def get_settings(self) -> Settings: ...
    def apply(self, settings: Settings) -> Settings: ...


@dataclass
class SweepPoint:
    settings: Settings
    noise_a_db: float
    noise_db: float
    snr_pred_db: float
    peak_pred_db: float
    clamped: bool


def target_gain(ref: Settings, speech: SpeechReport) -> float:
    """Ganho total (dB) que deixaria a voz com pico em ~-6 dBFS e média em ~-23 dBFS."""
    peak = speech.peak_db
    if speech.clip_runs > 0 or peak > -0.5:
        peak = max(peak, 0.0) + 3.0  # estourou: o pico real era maior do que o medido
    delta = min(TARGET_PEAK_DB - peak, TARGET_RMS_DB - speech.level_db)
    return ref.total_gain_db + delta


def _effective_peak(speech: SpeechReport) -> float:
    if speech.clip_runs > 0 or speech.peak_db > -0.5:
        return max(speech.peak_db, 0.0) + 3.0
    return speech.peak_db


def candidates(ctrl: Control, target: float) -> List[Tuple[Settings, bool]]:
    vmin, vmax, _ = ctrl.volume_range()
    options: List[Optional[float]] = list(ctrl.boost_options()) if ctrl.has_boost else [None]
    out = []
    for b in options:
        wanted = target - (b or 0.0)
        v = min(max(wanted, vmin), vmax)
        out.append((Settings(boost_db=b, volume_db=v), abs(v - wanted) > 0.75))
    return out


def sweep(ctrl: Control, record_noise: Callable[[float, float], NoiseReport], ref: Settings,
          ref_speech: SpeechReport, target: float,
          progress: Optional[Callable[[int, int, Settings], None]] = None) -> List[SweepPoint]:
    """Mede o silêncio em cada divisão reforço/volume e prevê a relação sinal/ruído.

    record_noise(settle_s, measure_s) grava e devolve a análise do ruído.
    """
    cands = candidates(ctrl, target)
    peak_ref = _effective_peak(ref_speech)
    points: List[SweepPoint] = []
    for i, (wanted, clamped) in enumerate(cands, 1):
        if progress:
            progress(i, len(cands), wanted)
        actual = ctrl.apply(wanted)
        noise = record_noise(SETTLE_S, SWEEP_S)
        delta = actual.total_gain_db - ref.total_gain_db
        points.append(SweepPoint(
            settings=actual, noise_a_db=noise.rms_a_db, noise_db=noise.rms_db,
            snr_pred_db=ref_speech.level_a_db + delta - noise.rms_a_db,
            peak_pred_db=peak_ref + delta, clamped=clamped,
        ))
    return points


def choose(points: List[SweepPoint], ref: Settings) -> SweepPoint:
    """Melhor ponto: maior SNR previsto, sem risco de estourar; empate -> muda menos."""
    safe = [p for p in points if p.peak_pred_db <= SAFE_PEAK_DB] or points
    best_snr = max(p.snr_pred_db for p in safe)
    near = [p for p in safe if p.snr_pred_db >= best_snr - 1.0]
    ref_boost = ref.boost_db if ref.boost_db is not None else 0.0
    near.sort(key=lambda p: (abs((p.settings.boost_db or 0.0) - ref_boost), -p.snr_pred_db))
    return near[0]


def boost_options_from_range(lo: float, hi: float, step: float) -> List[float]:
    """Degraus de reforço a testar (de ~10 em 10 dB, ou os do próprio hardware)."""
    step = max(step, 10.0) if step < 10.0 else step
    values = list(np.arange(lo, hi + 1e-6, step))
    return [round(float(v), 1) for v in values] or [lo]
