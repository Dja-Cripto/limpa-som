"""Medições de áudio: piso de ruído, fala, espectro, zumbido (hum), tons e largura de banda.

Convenção de níveis: dBFS RMS (uma senoide em fundo de escala = -3 dBFS).
Os níveis "(A)" usam ponderação A, que se aproxima do que o ouvido percebe como chiado.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import List, Optional, Tuple

import numpy as np
from scipy import ndimage, signal

EPS = 1e-12
PSD_EPS = 1e-30     # densidades espectrais de microfones silenciosos ficam bem abaixo de 1e-12
TRIM_START_S = 0.4  # descarta o começo da gravação (clique do teclado, respiração)


def to_db(power) -> np.ndarray:
    return 10.0 * np.log10(np.maximum(power, EPS))


def psd_db(psd) -> np.ndarray:
    """dB de uma densidade espectral (piso muito mais baixo que to_db)."""
    return 10.0 * np.log10(np.maximum(psd, PSD_EPS))


def amp_to_db(amp) -> float:
    return float(20.0 * np.log10(max(float(amp), EPS)))


# ---------------------------------------------------------------- filtros ---
@lru_cache(maxsize=8)
def _a_weighting_sos(fs: int) -> np.ndarray:
    zeros = [0.0, 0.0, 0.0, 0.0]
    poles = -2 * np.pi * np.array(
        [20.598997, 20.598997, 107.65265, 737.86223, 12194.217, 12194.217]
    )
    gain = (2 * np.pi * 12194.217) ** 2
    zd, pd, kd = signal.bilinear_zpk(zeros, poles, gain, fs)
    sos = signal.zpk2sos(zd, pd, kd)
    _, h = signal.sosfreqz(sos, worN=[1000.0], fs=fs)
    sos[0, :3] /= abs(h[0])
    return sos


@lru_cache(maxsize=8)
def _hp20_sos(fs: int) -> np.ndarray:
    return signal.butter(2, 20.0, "highpass", fs=fs, output="sos")


def a_weighting_db(freq: np.ndarray) -> np.ndarray:
    """Curva de ponderação A (IEC 61672) em dB para cada frequência."""
    f2 = np.maximum(np.asarray(freq, dtype=float), 1e-3) ** 2
    ra = (12194.0 ** 2 * f2 ** 2) / (
        (f2 + 20.6 ** 2)
        * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2))
        * (f2 + 12194.0 ** 2)
    )
    return 20.0 * np.log10(ra) + 2.0


def prepare(x: np.ndarray, fs: int) -> Tuple[np.ndarray, np.ndarray]:
    """Retorna (sinal sem DC e sem infra-som, mesmo sinal com ponderação A)."""
    x = np.asarray(x, dtype=np.float64)
    x = x - x.mean()
    flat = signal.sosfilt(_hp20_sos(fs), x)
    weighted = signal.sosfilt(_a_weighting_sos(fs), flat)
    return flat, weighted


def frame_power(x: np.ndarray, fs: int, win_s: float) -> np.ndarray:
    n = max(1, int(win_s * fs))
    count = len(x) // n
    if count == 0:
        return np.array([float(np.mean(x ** 2))])
    return np.mean(x[: count * n].reshape(count, n) ** 2, axis=1)


# ------------------------------------------------------------- estruturas ---
@dataclass
class Tone:
    freq: float
    prominence_db: float
    power_db: float


@dataclass
class Hum:
    f0: float
    harmonics: int
    prominence_db: float
    power_db: float


@dataclass
class NoiseReport:
    seconds: float
    fs: int
    rms_db: float
    rms_a_db: float
    peak_db: float
    dc: float
    bands: dict
    hf_share: float
    lf_share: float
    flatness: float
    hum: Optional[Hum]
    tones: List[Tone]
    tonal_share: float
    hum_share: float
    drift_db: float
    drift_r2: float
    digital_silence_frac: float
    transients: int
    cutoff_hz: Optional[float]
    cutoff_drop_db: float
    raw_freqs: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(1))
    raw_psd: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(1))
    chart_freqs: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(1))
    chart_db: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(1))


@dataclass
class SpeechReport:
    seconds: float
    fs: int
    active_frac: float
    level_db: float
    level_a_db: float
    peak_db: float
    crest_db: float
    clip_runs: int
    clip_samples: int
    pause_noise_db: float
    pause_noise_a_db: float
    gate_frac: float
    chart_freqs: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(1))
    chart_db: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(1))


@dataclass
class Measurement:
    noise: NoiseReport
    speech: Optional[SpeechReport]

    @property
    def snr_db(self) -> Optional[float]:
        if self.speech is None:
            return None
        return self.speech.level_a_db - self.noise.rms_a_db


# --------------------------------------------------------------- espectro ---
def _welch(x: np.ndarray, fs: int) -> Tuple[np.ndarray, np.ndarray]:
    nper = 2048
    while nper * 2 <= min(16384, max(2048, len(x) // 4)):
        nper *= 2
    nper = min(nper, len(x))
    return signal.welch(x, fs, window="hann", nperseg=nper, noverlap=nper // 2, detrend=False)


def octave_smooth(f: np.ndarray, p: np.ndarray, fs: int, fraction: int = 6) -> Tuple[np.ndarray, np.ndarray]:
    """Média de potência em bandas de 1/fraction de oitava (para gráficos)."""
    df = f[1] - f[0]
    centers, levels = [], []
    c = 31.5
    nyq = fs / 2
    ratio = 2.0 ** (1.0 / fraction)
    half = 2.0 ** (1.0 / (2 * fraction))
    while c * half < nyq * 0.995:
        lo, hi = c / half, c * half
        mask = (f >= lo) & (f < hi)
        if mask.any():
            centers.append(c)
            levels.append(float(np.sum(p[mask]) * df))
        c *= ratio
    return np.array(centers), to_db(np.array(levels))


def band_power(f: np.ndarray, p: np.ndarray, lo: float, hi: float) -> float:
    mask = (f >= lo) & (f < hi)
    if not mask.any():
        return 0.0
    return float(np.sum(p[mask]) * (f[1] - f[0]))


def detect_hum(f: np.ndarray, p: np.ndarray) -> Optional[Hum]:
    df = f[1] - f[0]
    best: Optional[Hum] = None
    best_score = 0.0
    for f0 in (50.0, 60.0):
        proms, powers = [], []
        for k in range(1, 9):
            fk = f0 * k
            if fk > 1000:
                break
            win = max(1.6, df * 1.6)
            idx = np.where((f >= fk - win) & (f <= fk + win))[0]
            ref = np.where(
                ((f >= fk - 14) & (f <= fk - 5)) | ((f >= fk + 5) & (f <= fk + 14))
            )[0]
            if len(idx) == 0 or len(ref) == 0:
                continue
            base = float(np.median(p[ref]))
            peak = float(np.max(p[idx]))
            proms.append(float(psd_db(peak) - psd_db(base)))
            powers.append(max(float(np.sum(p[idx] - base)) * df, 0.0))
        if not proms:
            continue
        strong = [pr for pr in proms if pr >= 8.0]
        if len(strong) >= 2 or proms[0] >= 12.0:
            score = sum(max(pr, 0) for pr in proms)
            if score > best_score:
                best_score = score
                best = Hum(
                    f0=f0,
                    harmonics=len(strong),
                    prominence_db=max(proms),
                    power_db=float(to_db(sum(powers))),
                )
    return best


def detect_tones(f: np.ndarray, p: np.ndarray, hum: Optional[Hum]) -> Tuple[List[Tone], float]:
    df = f[1] - f[0]
    pdb = psd_db(p)
    size = int(max(9, 600 / df)) | 1
    base_db = ndimage.median_filter(pdb, size=size, mode="nearest")
    prom = pdb - base_db
    cand = np.where((prom >= 12.0) & (f >= 100.0))[0]
    tones: List[Tone] = []
    total_tonal = 0.0
    if len(cand):
        groups = np.split(cand, np.where(np.diff(cand) > 3)[0] + 1)
        for g in groups:
            peak_i = g[np.argmax(prom[g])]
            fpk = float(f[peak_i])
            if hum is not None:
                k = round(fpk / hum.f0)
                if k >= 1 and abs(fpk - k * hum.f0) <= 4.0:
                    continue
            lo, hi = max(g[0] - 2, 0), min(g[-1] + 3, len(p))
            power = max(float(np.sum(p[lo:hi] - 10 ** (base_db[lo:hi] / 10))) * df, 0.0)
            total_tonal += power
            tones.append(Tone(fpk, float(prom[peak_i]), float(to_db(power))))
    tones.sort(key=lambda t: -t.prominence_db)
    return tones[:8], total_tonal


def mask_narrow(f: np.ndarray, p: np.ndarray, centers: List[float], half_width: float) -> np.ndarray:
    """Troca faixas estreitas (zumbido, tons) pela mediana vizinha: sobra só o ruído de banda larga."""
    out = p.copy()
    for c in centers:
        inside = np.where((f >= c - half_width) & (f <= c + half_width))[0]
        flank = np.where(((f >= c - half_width - 20) & (f < c - half_width))
                         | ((f > c + half_width) & (f <= c + half_width + 20)))[0]
        if len(inside) and len(flank):
            out[inside] = np.median(p[flank])
    return out


def find_cutoff(f: np.ndarray, p: np.ndarray, fs: int) -> Tuple[Optional[float], float]:
    """Procura um 'corte seco' no espectro (típico de Bluetooth, 8/16 kHz, drivers)."""
    nyq = fs / 2
    df = f[1] - f[0]
    w = max(3, int(200 / df))
    pdb = psd_db(np.convolve(p, np.ones(w) / w, mode="same"))
    candidates: List[Tuple[float, float]] = []
    fc = 2800.0
    while fc + 1500 <= nyq * 0.98:
        below = pdb[(f >= fc - 1500) & (f < fc - 300)]
        above = pdb[(f >= fc + 300) & (f < fc + 1500)]
        if len(below) and len(above):
            candidates.append((fc, float(np.median(below) - np.median(above))))
        fc += 100.0
    if not candidates:
        return None, 0.0
    best_drop = max(d for _, d in candidates)
    if best_drop < 25.0:
        return None, best_drop
    rough = next(fc for fc, d in candidates if d >= best_drop - 6.0)
    # o vazamento da janela espectral borra a borda: refina pelo ponto -12 dB em relação à banda passante
    lo = max(500.0, rough - 3000.0)
    passband = float(np.percentile(pdb[(f >= lo) & (f < rough - 1000.0)], 60))
    below_thr = np.where((f >= lo) & (pdb < passband - 12.0))[0]
    edge = float(f[below_thr[0]]) if len(below_thr) else rough
    return edge, best_drop


# ----------------------------------------------------------------- ruído ---
def analyze_noise(x: np.ndarray, fs: int) -> NoiseReport:
    x = np.asarray(x, dtype=np.float64)
    x = x[int(TRIM_START_S * fs):]
    if len(x) < fs:
        raise ValueError("Gravação de silêncio curta demais para analisar.")
    dc = float(np.mean(x))
    flat, weighted = prepare(x, fs)
    rms_db = float(to_db(np.mean(flat ** 2)))
    rms_a_db = float(to_db(np.mean(weighted ** 2)))
    peak_db = amp_to_db(np.max(np.abs(x - dc)))

    f, p = _welch(flat, fs)
    total = max(band_power(f, p, 20.0, fs / 2), EPS)
    edges = [("sub", 20, 100), ("grave", 100, 500), ("medio", 500, 2000),
             ("agudo", 2000, 6000), ("chiado", 6000, 12000), ("ar", 12000, fs / 2)]
    bands = {name: float(to_db(band_power(f, p, lo, hi))) for name, lo, hi in edges}

    hum = detect_hum(f, p)
    tones, tonal_power = detect_tones(f, p, hum)
    tonal_share = float(min(1.0, tonal_power / total))
    hum_share = float(min(1.0, 10 ** ((hum.power_db - rms_db) / 10))) if hum else 0.0

    # forma do ruído de banda larga, sem o zumbido e os tons (senão eles dominam a estatística)
    narrow = [t.freq for t in tones]
    if hum:
        narrow += [hum.f0 * k for k in range(1, 9) if hum.f0 * k < 1000]
    p_wide = mask_narrow(f, p, narrow, max(4.0, 2.5 * (f[1] - f[0])))
    total_wide = max(band_power(f, p_wide, 20.0, fs / 2), EPS)
    hf_share = band_power(f, p_wide, 4000, min(16000, fs / 2)) / total_wide
    lf_share = band_power(f, p_wide, 20, 150) / total_wide
    sel = (f >= 500) & (f <= fs / 2 * 0.9)
    flatness = float(np.exp(np.mean(np.log(p_wide[sel] + PSD_EPS))) / (np.mean(p_wide[sel]) + PSD_EPS))

    frames = to_db(frame_power(flat, fs, 0.25))
    drift_db, drift_r2 = 0.0, 0.0
    if len(frames) >= 8:
        t = np.arange(len(frames)) * 0.25
        slope, icpt = np.polyfit(t, frames, 1)
        drift_db = float(slope * (t[-1] - t[0]))
        drift_r2 = float(np.corrcoef(t, frames)[0, 1] ** 2)

    short = to_db(frame_power(flat, fs, 0.02))
    digital_silence = float(np.mean(short < -110.0))
    hot = short > (np.median(short) + 15.0)
    transients = int(np.sum(hot[1:] & ~hot[:-1]) + (1 if hot[0] else 0))

    cutoff, drop = find_cutoff(f, p, fs)
    cf, cdb = octave_smooth(f, p, fs)
    return NoiseReport(
        seconds=len(x) / fs, fs=fs, rms_db=rms_db, rms_a_db=rms_a_db, peak_db=peak_db,
        dc=dc, bands=bands, hf_share=float(hf_share), lf_share=float(lf_share),
        flatness=flatness, hum=hum, tones=tones, tonal_share=tonal_share, hum_share=hum_share,
        drift_db=drift_db, drift_r2=drift_r2, digital_silence_frac=digital_silence,
        transients=transients, cutoff_hz=cutoff, cutoff_drop_db=drop,
        raw_freqs=f, raw_psd=p, chart_freqs=cf, chart_db=cdb,
    )


# ------------------------------------------------------------------ fala ---
def _clip_runs(x: np.ndarray, threshold: float = 0.995, min_len: int = 3) -> Tuple[int, int]:
    hot = np.abs(x) >= threshold
    if not hot.any():
        return 0, 0
    padded = np.concatenate(([False], hot, [False]))
    starts = np.where(padded[1:] & ~padded[:-1])[0]
    ends = np.where(~padded[1:] & padded[:-1])[0]
    lengths = ends - starts
    keep = lengths >= min_len
    return int(np.sum(keep)), int(np.sum(lengths[keep]))


def analyze_speech(x: np.ndarray, fs: int, noise_a_db: float) -> SpeechReport:
    x = np.asarray(x, dtype=np.float64)
    x = x[int(TRIM_START_S * fs):]
    if len(x) < fs:
        raise ValueError("Gravação de fala curta demais para analisar.")
    flat, weighted = prepare(x, fs)
    win = 0.02
    n = int(win * fs)
    count = len(flat) // n
    pf = np.mean(flat[: count * n].reshape(count, n) ** 2, axis=1)
    pa = np.mean(weighted[: count * n].reshape(count, n) ** 2, axis=1)
    fa_db = to_db(pa)
    threshold = max(noise_a_db + 10.0, float(np.percentile(fa_db, 95)) - 25.0)
    active = fa_db >= threshold
    # fecha buracos curtos (consoantes surdas, pausas entre sílabas)
    active = ndimage.binary_closing(active, structure=np.ones(5, dtype=bool))
    active_frac = float(np.mean(active))

    if active.any():
        level_db = float(to_db(np.mean(pf[active])))
        level_a_db = float(to_db(np.mean(pa[active])))
    else:
        level_db = float(to_db(np.mean(pf)))
        level_a_db = float(to_db(np.mean(pa)))

    quiet = ~active
    if quiet.sum() >= 5:
        order = np.argsort(pa[quiet])
        take = order[: max(5, int(0.5 * len(order)))]
        pause_a = float(to_db(np.mean(pa[quiet][take])))
        pause_f = float(to_db(np.mean(pf[quiet][take])))
        gate_frac = float(np.mean(to_db(pf[quiet]) < -110.0))
    else:
        pause_a, pause_f, gate_frac = level_a_db, level_db, 0.0

    peak_db = amp_to_db(np.max(np.abs(x - np.mean(x))))
    runs, samples = _clip_runs(x)

    # espectro médio só dos trechos com voz (>= 200 ms contínuos)
    chart_f, chart_db = np.zeros(1), np.zeros(1)
    segs = np.split(np.arange(count), np.where(np.diff(active.astype(int)) != 0)[0] + 1)
    psds, weights = [], []
    freqs = None
    for seg in segs:
        if len(seg) >= 10 and active[seg[0]]:
            chunk = flat[seg[0] * n:(seg[-1] + 1) * n]
            nper = min(4096, len(chunk))
            ff, pp = signal.welch(chunk, fs, window="hann", nperseg=nper, noverlap=nper // 2)
            if freqs is None:
                freqs = ff
            if len(ff) == len(freqs):
                psds.append(pp)
                weights.append(len(chunk))
    if psds:
        avg = np.average(np.array(psds), axis=0, weights=weights)
        chart_f, chart_db = octave_smooth(freqs, avg, fs)

    return SpeechReport(
        seconds=len(x) / fs, fs=fs, active_frac=active_frac, level_db=level_db,
        level_a_db=level_a_db, peak_db=peak_db, crest_db=peak_db - level_db,
        clip_runs=runs, clip_samples=samples, pause_noise_db=pause_f,
        pause_noise_a_db=pause_a, gate_frac=gate_frac,
        chart_freqs=chart_f, chart_db=chart_db,
    )


def measure(silence: np.ndarray, speech: Optional[np.ndarray], fs: int) -> Measurement:
    noise = analyze_noise(silence, fs)
    spk = analyze_speech(speech, fs, noise.rms_a_db) if speech is not None else None
    return Measurement(noise=noise, speech=spk)
