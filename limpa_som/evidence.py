"""Provas sobre o caminho do sinal: onde o ruído entra e se o ganho do computador pode ajudar.

Duas ideias simples sustentam o diagnóstico:

1. Se o ruído sobe exatamente tanto quanto o ganho do computador (1 dB de ruído por 1 dB de ganho) e a
   relação voz/ruído não muda, o ruído entra JUNTO com a voz, antes do ganho: mexer no volume ou no
   reforço do Windows não pode ajudar. A saída está antes do computador (ou trocando a entrada).
2. O nível da voz "na entrada" (descontado o ganho aplicado) diz se o sinal chega forte (mesa, nível de
   linha) ou fraco (microfone ligado direto): quanto mais fraco, mais o computador precisa amplificar e
   mais o ruído da entrada aparece.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np

TARGET_VOICE_A_DB = -25.0   # nível médio da voz (dBFS ponderado A) que soa normal
FLAT_SNR_SPREAD_DB = 3.0    # relação voz/ruído "igual" em todas as combinações de ganho
PRE_GAIN_SLOPE = 0.8        # dB de ruído por dB de ganho a partir do qual o ruído é "anterior ao ganho"


@dataclass
class ChainEvidence:
    snr_db: Optional[float]
    gain_db: Optional[float]                 # ganho total do computador na medição final
    voice_unity_a_db: Optional[float]        # voz na entrada (sem o ganho do PC), dBFS(A)
    noise_unity_a_db: Optional[float]        # ruído na entrada (sem o ganho do PC), dBFS(A)
    needed_gain_db: Optional[float]          # ganho que o PC precisa somar para a voz soar normal
    noise_slope: Optional[float]             # dB de ruído por dB de ganho
    snr_spread_db: Optional[float]           # variação da relação voz/ruído entre combinações de ganho
    gain_cannot_help: bool
    narrow_share: float                      # fração da energia do ruído que é zumbido + apitos
    tested: int                              # quantas configurações de ganho entraram na conta

    @property
    def weak_signal(self) -> bool:
        return self.needed_gain_db is not None and self.needed_gain_db >= 20.0


def _final_settings(ctx):
    if not getattr(ctx, "gain_control", True):
        return None
    if ctx.after is not None and not ctx.reverted and ctx.settings_after is not None:
        return ctx.settings_after
    return ctx.settings_before


def _slope_and_spread(ctx):
    """(inclinação do ruído por dB de ganho, variação da relação voz/ruído, nº de pontos)."""
    points = [p for p in (ctx.sweep or [])]
    if len(points) >= 3:
        gains = np.array([p.settings.total_gain_db for p in points])
        if np.ptp(gains) >= 8.0:
            noise = np.array([p.noise_a_db for p in points])
            snr = np.array([p.snr_pred_db for p in points])
            return float(np.polyfit(gains, noise, 1)[0]), float(np.ptp(snr)), len(points)
    a, b = ctx.before, ctx.after
    sa, sb = ctx.settings_before, ctx.settings_after
    if a is not None and b is not None and sa is not None and sb is not None and a.speech and b.speech:
        dg = sb.total_gain_db - sa.total_gain_db
        if abs(dg) >= 3.0:
            slope = (b.noise.rms_a_db - a.noise.rms_a_db) / dg
            return float(slope), float(abs((b.snr_db or 0) - (a.snr_db or 0))), 2
    return None, None, len(points)


def chain_evidence(ctx) -> ChainEvidence:
    m = ctx.final
    n = m.noise
    settings = _final_settings(ctx)
    gain = settings.total_gain_db if settings is not None else None
    voice_u = noise_u = needed = None
    if gain is not None:
        noise_u = n.rms_a_db - gain
        if m.speech is not None:
            voice_u = m.speech.level_a_db - gain
            needed = TARGET_VOICE_A_DB - voice_u
    slope, spread, tested = _slope_and_spread(ctx)
    cannot = bool(slope is not None and spread is not None
                  and slope >= PRE_GAIN_SLOPE and spread <= FLAT_SNR_SPREAD_DB)
    narrow = min(1.0, (n.hum_share if n.hum else 0.0) + (n.tonal_share if n.tones else 0.0))
    return ChainEvidence(snr_db=m.snr_db, gain_db=gain, voice_unity_a_db=voice_u, noise_unity_a_db=noise_u,
                         needed_gain_db=needed, noise_slope=slope, snr_spread_db=spread,
                         gain_cannot_help=cannot, narrow_share=float(narrow), tested=tested)


def tone_comb(freqs: List[float], max_error_hz: float = 15.0) -> Optional[float]:
    """Se os apitos são múltiplos de uma mesma frequência (típico de chaveamento digital), devolve-a.

    Procura a MAIOR base (300 a 5000 Hz) da qual pelo menos 75% dos apitos (e no mínimo 3) são múltiplos,
    com erro de até `max_error_hz` (a medição tem ~3 Hz de incerteza e o erro da base cresce com o múltiplo).
    """
    if len(freqs) < 3:
        return None
    need = max(3, int(np.ceil(0.75 * len(freqs))))
    arr = np.asarray(sorted(freqs), dtype=float)
    for base in np.arange(5000.0, 299.0, -1.0):
        multiples = np.round(arr / base)
        ok = (multiples >= 1) & (np.abs(arr - multiples * base) <= max_error_hz)
        if int(ok.sum()) >= need:
            return float(base)
    return None
