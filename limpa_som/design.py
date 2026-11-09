"""O Limpa Som projeta o equalizador do seu microfone a partir do que mediu (ruído e voz).

Como decide (tudo vem da medição, nada é chute):
  * Em cada faixa de frequência compara a sua voz com o ruído. Onde o ruído é tão alto quanto a voz (ou mais),
    um filtro estático baixa aquela faixa na medida do que a voz representa nela (filtro de Wiener de longo
    prazo: ganho = voz / (voz + ruído)). Onde só há ruído (graves de vibração, agudos acima da voz), corta forte.
  * Notches estreitos exatamente no zumbido e nos apitos que apareceram.
  * Pré-amplificação para a voz chegar forte à IA anti-chiado (RNNoise), que erra mais com voz fraca.
  * IA anti-chiado com porta de voz (corta o que não é voz nas pausas).
O resultado é um `eq.Profile` comum: o Equalizador mostra a mesma lista e deixa a pessoa mexer em cada filtro.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np

from . import eq
from .dsp import hum_notches
from .metrics import NoiseReport, SpeechReport

WIENER_FLOOR_DB = 15.0         # no máximo -15 dB por faixa
WIENER_STRENGTH = 0.9          # 1.0 = ganho de Wiener puro; um pouco menos evita deixar a voz abafada
LP_MIN_HZ, LP_MAX_HZ = 5000.0, 12000.0
HP_MIN_HZ, HP_MAX_HZ = 70.0, 130.0
MAX_TONES = 16
MAX_CUTS = 6
TONE_Q = 50.0
HUM_Q = 30.0


def _grid(fs: float) -> np.ndarray:
    ratio = 2.0 ** (1.0 / 6.0)
    out, f = [], 40.0
    while f < fs / 2 * 0.95:
        out.append(f)
        f *= ratio
    return np.array(out)


def _on_grid(freqs: np.ndarray, db: np.ndarray, grid: np.ndarray) -> np.ndarray:
    freqs, db = np.asarray(freqs, dtype=float), np.asarray(db, dtype=float)
    keep = freqs > 0
    return np.interp(np.log(grid), np.log(freqs[keep]), db[keep])


def wiener_curve(noise: NoiseReport, speech: SpeechReport) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(grade de frequências, ganho em dB por faixa, voz acima do ruído em dB): quanto baixar cada faixa."""
    grid = _grid(noise.fs)
    s = 10.0 ** (_on_grid(speech.chart_freqs, speech.chart_db, grid) / 10.0)      # voz + ruído, falando
    n = 10.0 ** (_on_grid(noise.chart_freqs, noise.chart_db, grid) / 10.0)        # só ruído, em silêncio
    voice = np.maximum(s - n, 1e-3 * n)
    margin = 10.0 * np.log10(voice / np.maximum(n, 1e-30))
    ratio = np.clip(n / np.maximum(s, 1e-30), 0.0, 1.0)
    gain = 10.0 * np.log10(np.maximum(1.0 - ratio, 10.0 ** (-WIENER_FLOOR_DB / 10.0)))
    gain = np.convolve(np.pad(WIENER_STRENGTH * gain, 1, mode="edge"), np.ones(3) / 3.0, mode="valid")
    return grid, gain, margin


def _high_cut(grid: np.ndarray, gain: np.ndarray) -> Optional[float]:
    """Frequência acima da qual a voz praticamente não passa (o ganho de Wiener fica <= -8 dB até o topo)."""
    edge = None
    for f, g in zip(grid[::-1], gain[::-1]):
        if g <= -8.0:
            edge = float(f)
        else:
            break
    if edge is None:
        return None
    return float(min(max(edge * 1.05, LP_MIN_HZ), LP_MAX_HZ))


def _low_cut(grid: np.ndarray, gain: np.ndarray) -> float:
    """Frequência onde a voz começa a valer (o ganho sobe acima de -3 dB e fica); entre 70 e 130 Hz."""
    for i in range(len(grid) - 2):
        if grid[i] >= 40.0 and gain[i] >= -3.0 and gain[i + 1] >= -3.0 and gain[i + 2] >= -3.0:
            return float(min(max(grid[i], HP_MIN_HZ), HP_MAX_HZ))
    return 80.0


def _butterworth4_lowpass(fc: float, why: str) -> List[eq.Band]:
    """Corte de 24 dB por oitava: dois biquads em cascata (Q de um Butterworth de 4ª ordem)."""
    return [eq.Band.new("LP", fc, q=0.541, why=why), eq.Band.new("LP", fc, q=1.307, why=why)]


def design_profile(noise: NoiseReport, speech: Optional[SpeechReport] = None, rnnoise: bool = True) -> eq.Profile:
    fs = noise.fs
    bands: List[eq.Band] = []
    facts: List[str] = []
    grid = gain = margin = None
    if speech is not None and len(speech.chart_freqs) > 3 and len(noise.chart_freqs) > 3:
        grid, gain, margin = wiener_curve(noise, speech)

    # --- cortes grandes: graves e agudos onde não há voz
    hp = _low_cut(grid, gain) if gain is not None else 80.0
    bands.append(eq.Band.new("HP", hp, why="Corta o ronco e a vibração abaixo de %.0f Hz: a voz quase não tem "
                                           "energia ali." % hp))
    lp = _high_cut(grid, gain) if gain is not None else None
    if lp is not None and lp < fs / 2 * 0.9:
        bands += _butterworth4_lowpass(lp, "Corta tudo acima de %.0f Hz (24 dB por oitava): nessa faixa só há "
                                           "chiado e apitos, a voz quase não passa." % lp)

    # --- cortes médios: onde o ruído é do tamanho da voz (greedy sobre o que sobrou)
    if gain is not None:
        residual = gain.copy()
        for b in bands:
            residual = residual - eq.band_response_db(b, grid, fs)
        lo, hi = hp * 1.3, (lp * 0.9 if lp else fs / 2 * 0.9)
        window = (grid >= lo) & (grid <= hi)
        for _ in range(MAX_CUTS):
            masked = np.where(window, residual, 0.0)
            i = int(np.argmin(masked))
            if masked[i] > -2.5:
                break
            cut = max(masked[i] * 0.9, -15.0)
            band = eq.Band.new("PK", float(grid[i]), cut, q=1.2)
            snr_here = float(margin[i])
            band.why = ("Baixa %.1f dB em volta de %.0f Hz: nessa faixa a sua voz está só %.0f dB acima do ruído."
                        % (abs(cut), grid[i], max(snr_here, 0.0)))
            bands.append(band)
            residual = residual - eq.band_response_db(band, grid, fs)

    # --- notches: zumbido da rede e apitos medidos
    used: List[float] = []
    if noise.hum is not None and noise.hum.prominence_db >= 8.0:
        for f in hum_notches(float(noise.hum.f0), int(noise.hum.harmonics)):
            bands.append(eq.Band.new("NO", f, q=HUM_Q, why="Zumbido da rede elétrica: %.0f Hz." % f))
            used.append(f)
        facts.append("zumbido de %.0f Hz" % noise.hum.f0)
    tones = sorted((t for t in noise.tones if t.prominence_db >= 12.0 and 250.0 <= t.freq < fs / 2 * 0.9),
                   key=lambda t: -t.prominence_db)
    count = 0
    for t in tones:
        if count >= MAX_TONES or any(abs(t.freq - u) / u < 0.03 for u in used):
            continue
        if lp is not None and t.freq > lp * 2.0:                 # o corte de agudos já derruba mais de 24 dB
            continue
        bands.append(eq.Band.new("NO", float(t.freq), q=TONE_Q, why="Apito de interferência em %.0f Hz (%.0f dB "
                                                                    "acima do chiado ao redor)."
                                                                    % (t.freq, t.prominence_db)))
        used.append(float(t.freq))
        count += 1
    if count:
        facts.append("%d apito(s)" % count)

    # --- pré-amplificação e IA anti-chiado
    preamp = 0.0
    snr = None
    if speech is not None:
        preamp = min(max(-4.0 - speech.peak_db, 0.0), max(-24.0 - speech.level_db, 0.0), 12.0)
        preamp = round(preamp * 2) / 2 if preamp >= 1.0 else 0.0
        snr = speech.level_a_db - noise.rms_a_db
        facts.append("voz %.0f dB acima do ruído" % snr)
    gate = 0.45 if (snr is None or snr >= 15.0) else 0.35
    bands.sort(key=lambda b: (b.kind != "HP", b.freq))
    summary = ("Criado pelo Limpa Som a partir do seu teste (" + ", ".join(facts) + ")." if facts
               else "Criado pelo Limpa Som a partir do ruído medido.")
    if speech is None:
        summary += " Sem a sua voz medida: só cortes gerais, notches e a IA. Rode o teste completo para refinar."
    return eq.Profile(enabled=True, preamp_db=preamp, bands=bands, rnnoise=1 if rnnoise else 0,
                      rnnoise_gate=gate, summary=summary).clamp()


# ============================================================ teste do equalizador na sua gravação ===
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402
from scipy import signal  # noqa: E402

from . import metrics, vsthost  # noqa: E402

SILENT_DB = -100.0            # abaixo disso, a pausa ficou em silêncio digital (a IA fechou a porta)
RATIO_CAP_DB = 45.0           # acima disso a relação voz/ruído deixa de significar algo (pausa muda)


@dataclass
class Evaluation:
    """O que um equalizador fez na gravação de voz da própria pessoa (medido, com o RNNoise de verdade)."""
    name: str
    voice_change_db: float
    pause_change_db: float          # quanto o ruído das pausas caiu (negativo = caiu)
    pause_silent: bool              # as pausas viraram silêncio total
    ratio_before_db: float
    ratio_after_db: float
    tones_before: int
    tones_after: int
    ran_ai: bool                    # o plugin de IA rodou de verdade nesta medição

    @property
    def gain_db(self) -> float:
        return min(self.ratio_after_db, RATIO_CAP_DB) - min(self.ratio_before_db, RATIO_CAP_DB)

    @property
    def score(self) -> float:
        """Quanto mais alto melhor: voz contra ruído, sem apagar a voz e sem deixar apitos."""
        voice_loss = max(0.0, -self.voice_change_db - 0.5)
        return min(self.ratio_after_db, RATIO_CAP_DB) - 3.0 * voice_loss - 0.5 * self.tones_after


def chain_levels(x: np.ndarray, fs: float) -> Tuple[float, float]:
    """(voz, ruído das pausas) em dB ponderado A: voz = 30% dos blocos de 250 ms mais fortes, ruído = 12% mais fracos."""
    y = signal.sosfilt(metrics._a_weighting_sos(int(fs)), np.asarray(x, dtype=np.float64))[int(0.5 * fs):]
    n = max(int(0.25 * fs), 1)
    count = len(y) // n
    if count < 8:
        return float("nan"), float("nan")
    power = np.sort(np.mean(y[: count * n].reshape(count, n) ** 2, axis=1))
    voice = float(np.mean(power[-max(2, int(count * 0.30)):]))
    floor = float(np.mean(power[: max(2, int(count * 0.12))]))
    return 10.0 * np.log10(max(voice, 1e-14)), 10.0 * np.log10(max(floor, 1e-14))


def interference_count(x: np.ndarray, fs: float) -> int:
    """Quantos apitos e zumbidos de frequência fixa aparecem nas pausas da gravação."""
    from . import fileclean
    y = np.asarray(x, dtype=np.float64)[int(0.5 * fs):]
    try:
        report = metrics.analyze_noise(fileclean.quiet_audio(y, int(fs), fraction=0.25), int(fs))
    except ValueError:
        return 0
    tones = sum(1 for t in report.tones if t.prominence_db >= 15.0 and t.freq >= 300.0)
    return tones + (1 if report.hum is not None and report.hum.prominence_db >= 8.0 else 0)


def simulate(profile: eq.Profile, x: np.ndarray, fs: int, apo_install: Optional[Path]) -> Tuple[np.ndarray, bool]:
    """O que o Equalizer APO faria com `x`: filtros do perfil e depois o RNNoise de verdade. (áudio, IA rodou?)"""
    y = signal.sosfilt(eq.profile_sos(profile, fs, force_enabled=True), np.asarray(x, dtype=np.float64))
    if profile.rnnoise > 0 and apo_install is not None:
        out = vsthost.process_isolated(y, fs, Path(apo_install), profile.rnnoise_gate, profile.rnnoise)
        if out is not None:
            return out, True
    return y, False


def evaluate(profile: eq.Profile, x: np.ndarray, fs: int, apo_install: Optional[Path], name: str = "") -> Evaluation:
    voice0, floor0 = chain_levels(x, fs)
    y, ran = simulate(profile, x, fs, apo_install)
    voice1, floor1 = chain_levels(y, fs)
    silent = floor1 < SILENT_DB
    return Evaluation(
        name=name, voice_change_db=voice1 - voice0, pause_change_db=floor1 - floor0, pause_silent=silent,
        ratio_before_db=voice0 - floor0, ratio_after_db=voice1 - floor1,
        tones_before=interference_count(x, fs), tones_after=0 if silent else interference_count(y, fs), ran_ai=ran)


def candidates(noise: NoiseReport, speech: Optional[SpeechReport]) -> List[Tuple[str, eq.Profile]]:
    """Variantes do equalizador para testar: completo (com cortes de Wiener e de agudos) e enxuto (só o que é certo)."""
    full = design_profile(noise, speech)
    lean = full.copy()
    lean.bands = [b for b in lean.bands if b.kind in ("HP", "NO")]
    lean.summary = full.summary
    return [("completo", full), ("enxuto", lean)]


def choose_profile(noise: NoiseReport, speech: Optional[SpeechReport], voice: Optional[np.ndarray], fs: int,
                   apo_install: Optional[Path], progress=None):
    """Testa as variantes na sua gravação de voz e fica com a que mede melhor.

    Devolve (perfil, avaliação ou None, lista de (nome, avaliação)). Sem a gravação, sem o plugin ou fora de 48 kHz
    não há como testar de verdade: devolve o projeto completo e avaliação None.
    """
    options = candidates(noise, speech)
    can_test = (voice is not None and len(voice) > fs * 4 and apo_install is not None
                and vsthost.plugin_path(Path(apo_install)).exists())
    if not can_test:
        return options[0][1], None, []
    tried = []
    for name, profile in options:
        if progress:
            progress(f"Testando o equalizador '{name}' na sua voz...")
        tried.append((name, evaluate(profile, voice, fs, apo_install, name), profile))
    best = max(tried, key=lambda t: (t[1].score, -len(t[2].bands)))
    chosen = best[2]
    chosen.summary += (" Testado na sua própria gravação (%s): a voz ficou %+.1f dB mais forte que o ruído."
                       % (best[0], best[1].gain_db)) if best[1].ran_ai else ""
    return chosen, best[1], [(n, e) for n, e, _p in tried]
