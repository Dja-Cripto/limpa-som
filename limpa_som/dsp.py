"""Limpeza digital: filtro de graves, remoção de zumbido, redução de chiado e nivelamento.

A redução de chiado usa um filtro de Wiener com estimativa "decision-directed", que gera bem
menos o efeito de "ruído musical" (chiadinho metálico) do que um simples gate espectral.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
from scipy import ndimage, signal

from .metrics import to_db


@dataclass
class CleanSettings:
    highpass_hz: float = 75.0
    notch_hz: List[float] = field(default_factory=list)
    reduction_db: float = 15.0
    normalize: bool = True
    target_rms_db: float = -20.0
    peak_ceiling_db: float = -1.5
    nfft: int = 2048


# ---------------------------------------------------------------- perfil ---
def estimate_profile(x: np.ndarray, fs: int, quiet_fraction: float = 0.1) -> Tuple[np.ndarray, np.ndarray]:
    """Perfil de ruído tirado dos trechos mais silenciosos de uma gravação qualquer.

    Devolve (frequências, densidade espectral) no mesmo formato do Welch unilateral.
    """
    nfft = 4096
    f, _, z = signal.stft(x, fs, window="hann", nperseg=nfft, noverlap=nfft // 2, scaling="psd")
    power = np.abs(z) ** 2
    band = (f >= 100) & (f <= 8000)
    energy = power[band].sum(axis=0)
    count = max(3, int(len(energy) * quiet_fraction))
    quiet = np.argsort(energy)[:count]
    return f, 2.0 * power[:, quiet].mean(axis=1)  # *2: unilateral (igual ao Welch)


def profile_from_noise(noise: np.ndarray, fs: int, nfft: int = 2048,
                       quiet_fraction: Optional[float] = None) -> Tuple[np.ndarray, np.ndarray]:
    """Perfil de ruído medido com a MESMA janela (nfft e sobreposição) que o redutor usa.

    Por que não usar o espectro de alta resolução (Welch): ruído real é cheio de linhas estreitas
    (zumbido, apitos, chaveamento). O redutor enxerga a média de cada faixa de ~23 Hz; um perfil
    amostrado em pontos subestima o ruído em ~6 dB e o filtro só chega à metade da redução.
    Devolve (frequências da grade da STFT, densidade unilateral), no mesmo formato de estimate_profile.
    """
    f, _, z = signal.stft(noise, fs, window="hann", nperseg=nfft, noverlap=nfft * 3 // 4, scaling="psd")
    power = np.abs(z) ** 2
    if quiet_fraction is not None:
        band = (f >= 100) & (f <= 8000)
        energy = power[band].sum(axis=0)
        count = max(3, int(len(energy) * quiet_fraction))
        power = power[:, np.argsort(energy)[:count]]
    return f, 2.0 * power.mean(axis=1)


def _profile_on_grid(freqs: np.ndarray, psd: np.ndarray, grid: np.ndarray) -> np.ndarray:
    # a STFT 'psd' é bilateral: metade da densidade unilateral do Welch
    return 0.5 * np.interp(grid, freqs, psd)


def save_profile(path: str, freqs: np.ndarray, psd: np.ndarray, fs: int) -> None:
    np.savez(path, freqs=freqs, psd=psd, fs=fs)


def load_profile(path: str) -> Optional[Tuple[np.ndarray, np.ndarray, int]]:
    try:
        d = np.load(path)
        return d["freqs"], d["psd"], int(d["fs"])
    except Exception:
        return None


# ------------------------------------------------------------- algoritmos ---
def highpass(x: np.ndarray, fs: int, hz: float) -> np.ndarray:
    sos = signal.butter(4, hz, "highpass", fs=fs, output="sos")
    return signal.sosfiltfilt(sos, x)


def remove_hum(x: np.ndarray, fs: int, freqs: List[float], q: float = 40.0) -> np.ndarray:
    for f0 in freqs:
        if f0 < fs / 2 * 0.95:
            b, a = signal.iirnotch(f0, q, fs=fs)
            x = signal.sosfiltfilt(signal.tf2sos(b, a), x)
    return x


def reduce_noise(x: np.ndarray, fs: int, prof_freqs: np.ndarray, prof_psd: np.ndarray,
                 reduction_db: float = 15.0, nfft: int = 2048) -> np.ndarray:
    hop_overlap = nfft * 3 // 4
    f, _, z = signal.stft(x, fs, window="hann", nperseg=nfft, noverlap=hop_overlap, scaling="psd")
    power = np.abs(z) ** 2
    noise = np.maximum(_profile_on_grid(prof_freqs, prof_psd, f), 1e-18) * 1.2
    gmin = 10.0 ** (-reduction_db / 20.0)
    xi_min = 10.0 ** (-2.5)
    alpha = 0.98
    gains = np.empty_like(power)
    prev = power[:, 0] * gmin ** 2
    for i in range(power.shape[1]):
        gamma = power[:, i] / noise
        xi = alpha * prev / noise + (1 - alpha) * np.maximum(gamma - 1.0, 0.0)
        xi = np.maximum(xi, xi_min)
        g = np.maximum(xi / (1.0 + xi), gmin)
        gains[:, i] = g
        prev = (g ** 2) * power[:, i]
    gains = ndimage.uniform_filter1d(gains, size=3, axis=0, mode="nearest")
    gains = np.maximum(gains, gmin)
    _, y = signal.istft(z * gains, fs, window="hann", nperseg=nfft, noverlap=hop_overlap, scaling="psd")
    if len(y) < len(x):
        y = np.pad(y, (0, len(x) - len(y)))
    return y[: len(x)]


def active_level_db(x: np.ndarray, fs: int) -> float:
    n = max(1, int(0.02 * fs))
    count = len(x) // n
    if count == 0:
        return float(to_db(np.mean(x ** 2)))
    p = np.mean(x[: count * n].reshape(count, n) ** 2, axis=1)
    db = to_db(p)
    active = db >= np.percentile(db, 95) - 30.0
    return float(to_db(np.mean(p[active])))


def normalize(x: np.ndarray, fs: int, target_rms_db: float, peak_ceiling_db: float) -> np.ndarray:
    peak = float(np.max(np.abs(x))) + 1e-12
    gain_db = target_rms_db - active_level_db(x, fs)
    gain_db = min(gain_db, peak_ceiling_db - 20.0 * np.log10(peak))
    return x * (10.0 ** (gain_db / 20.0))


def denoise_chain(x: np.ndarray, fs: int, profile: Tuple[np.ndarray, np.ndarray],
                  settings: CleanSettings) -> np.ndarray:
    """Tudo menos o nivelamento final (para poder medir antes/depois de forma justa)."""
    y = np.asarray(x, dtype=np.float64)
    y = y - y.mean()
    y = highpass(y, fs, settings.highpass_hz)
    if settings.notch_hz:
        y = remove_hum(y, fs, settings.notch_hz)
    if settings.reduction_db > 0:
        y = reduce_noise(y, fs, profile[0], profile[1], settings.reduction_db, settings.nfft)
    return y


def clean(x: np.ndarray, fs: int, profile: Tuple[np.ndarray, np.ndarray],
          settings: Optional[CleanSettings] = None) -> np.ndarray:
    settings = settings or CleanSettings()
    y = denoise_chain(x, fs, profile, settings)
    if settings.normalize:
        y = normalize(y, fs, settings.target_rms_db, settings.peak_ceiling_db)
    return np.clip(y, -1.0, 1.0)


def hum_notches(f0: float, harmonics: int) -> List[float]:
    return [f0 * k for k in range(1, max(2, min(harmonics + 1, 8)) + 1) if f0 * k < 1200]


# --------------------------------------------------------------- arquivos ---
def clean_channels(data: np.ndarray, fs: int, profile: Tuple[np.ndarray, np.ndarray],
                   settings: Optional[CleanSettings] = None) -> np.ndarray:
    if data.ndim == 1:
        return clean(data, fs, profile, settings)
    return np.stack([clean(data[:, c], fs, profile, settings) for c in range(data.shape[1])], axis=1)
