"""Sinais sintéticos (voz fictícia, chiado, zumbido...) usados nos testes e no modo simulado."""
from __future__ import annotations

import numpy as np
from scipy import signal


def _normalize(x: np.ndarray, level_db: float) -> np.ndarray:
    rms = np.sqrt(np.mean(x ** 2)) + 1e-18
    return x * (10.0 ** (level_db / 20.0) / rms)


def white(seconds: float, fs: int, level_db: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return _normalize(rng.standard_normal(int(seconds * fs)), level_db)


def pink(seconds: float, fs: int, level_db: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(seconds * fs)
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1.0 / fs)
    f[0] = f[1]
    spec /= np.sqrt(f)
    return _normalize(np.fft.irfft(spec, n), level_db)


def hum(seconds: float, fs: int, level_db: float, f0: float = 60.0, harmonics: int = 6,
        seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * fs)) / fs
    x = np.zeros_like(t)
    for k in range(1, harmonics + 1):
        x += (1.0 / k) * np.sin(2 * np.pi * f0 * k * t + rng.uniform(0, 2 * np.pi))
    return _normalize(x, level_db)


def tone(seconds: float, fs: int, freq: float, level_db: float) -> np.ndarray:
    t = np.arange(int(seconds * fs)) / fs
    return _normalize(np.sin(2 * np.pi * freq * t), level_db)


def lowpass(x: np.ndarray, fs: int, cutoff: float) -> np.ndarray:
    """Passa-baixas íngreme (elíptico), parecido com o corte de reamostradores e Bluetooth."""
    sos = signal.ellip(8, 0.5, 80, cutoff, "lowpass", fs=fs, output="sos")
    return signal.sosfilt(sos, x)


def ramp_noise(seconds: float, fs: int, start_db: float, end_db: float, seed: int = 0) -> np.ndarray:
    """Ruído branco cujo nível sobe/desce de forma contínua (simula AGC)."""
    base = white(seconds, fs, 0.0, seed)
    gain_db = np.linspace(start_db, end_db, len(base))
    return base * 10.0 ** (gain_db / 20.0)


def speech_like(seconds: float, fs: int, level_db: float = -22.0, seed: int = 1) -> np.ndarray:
    """Fala fictícia: palavras com harmônicos de voz e consoantes fricativas, com pausas."""
    rng = np.random.default_rng(seed)
    n = int(seconds * fs)
    t = np.arange(n) / fs
    env = np.zeros(n)
    fric_env = np.zeros(n)
    pos = 0.3
    while pos < seconds - 0.4:
        dur = rng.uniform(0.3, 0.8)
        end = min(pos + dur, seconds - 0.1)
        seg = (t >= pos) & (t < end)
        mod = 0.55 + 0.45 * np.sin(np.pi * rng.uniform(3, 5) * (t[seg] - pos)) ** 2
        ramp = np.minimum(1.0, (t[seg] - pos) / 0.03) * np.minimum(1.0, (end - t[seg]) / 0.05)
        env[seg] = mod * ramp
        if rng.random() < 0.35:
            fseg = (t >= end - 0.12) & (t < end)
            fric_env[fseg] = 0.5
        pos = end + rng.uniform(0.15, 0.4)
    f0 = 120 + 22 * np.sin(2 * np.pi * 0.7 * t) + 9 * np.sin(2 * np.pi * 2.3 * t)
    phase = 2 * np.pi * np.cumsum(f0) / fs
    voiced = np.zeros(n)
    f0_mean = float(np.mean(f0))
    k = 1
    while k * f0.max() < min(fs / 2 * 0.85, 7000):
        fk = k * f0_mean
        # envoltória parecida com a da fala real: platô até ~450 Hz, queda de 9 dB/oitava, formantes
        weight = (1.0 / np.sqrt(1.0 + (fk / 450.0) ** 3)) * (
            1.0 + 1.5 * np.exp(-(((fk - 1500.0) / 600.0) ** 2)) + 1.0 * np.exp(-(((fk - 2800.0) / 700.0) ** 2)))
        voiced += weight * np.sin(k * phase)
        k += 1
    sos = signal.butter(4, [4000, min(9000, fs / 2 * 0.9)], "bandpass", fs=fs, output="sos")
    fric = signal.sosfilt(sos, rng.standard_normal(n))
    fric *= 0.12 * np.sqrt(np.mean(voiced ** 2)) / (np.sqrt(np.mean(fric ** 2)) + 1e-18)
    x = env * voiced + fric_env * fric
    active = (env > 0.1) | (fric_env > 0.1)
    rms_active = np.sqrt(np.mean(x[active] ** 2)) + 1e-18
    return x * (10.0 ** (level_db / 20.0) / rms_active)
