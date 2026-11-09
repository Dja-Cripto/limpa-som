import numpy as np

from limpa_som import metrics, scoring, synth

FS = 48000


def measure(snr_target_db, hum_db=None, tone_db=None, clip=False):
    """Mede um áudio sintético: voz a -22 dBFS e ruído branco a (-22 - SNR) dBFS (ponderação A aproximada)."""
    noise_db = -22.0 - snr_target_db - 3.0
    silence = synth.white(7, FS, noise_db, seed=11)
    speech = synth.speech_like(14, FS, -22.0) + synth.white(14, FS, noise_db, seed=12)
    if hum_db is not None:
        silence = silence + synth.hum(7, FS, hum_db)
        speech = speech + synth.hum(14, FS, hum_db)
    if tone_db is not None:
        silence = silence + synth.tone(7, FS, 12006.0, tone_db)
        speech = speech + synth.tone(14, FS, 12006.0, tone_db)
    if clip:
        speech = np.clip(speech * 6.0, -1.0, 1.0)
    return metrics.measure(silence, speech, FS)


def test_score_grows_with_snr():
    values = [scoring.compute_score(measure(s)).value for s in (12, 20, 30, 40, 50)]
    assert values == sorted(values) and values[-1] - values[0] > 5.0


def test_a_poor_audio_with_hum_and_whistle_is_low_but_not_zero():
    """Regressão do teste real (03/11): SNR ~17 dB + zumbido + apito dava 0,0 antes e depois."""
    m = measure(17, hum_db=-50.0, tone_db=-62.0)
    assert m.noise.hum is not None and m.noise.tones
    sc = scoring.compute_score(m)
    assert 0.5 < sc.value < 3.0
    assert sc.penalties and all(p.points > 0 for p in sc.penalties)


def test_before_and_after_stay_distinguishable_at_the_bottom():
    worse = scoring.compute_score(measure(14, hum_db=-45.0, tone_db=-60.0)).value
    better = scoring.compute_score(measure(18, hum_db=-45.0, tone_db=-60.0)).value
    assert better > worse                                     # com subtração e piso em 0 os dois davam 0,0


def test_penalties_reduce_proportionally_and_never_exceed_the_base():
    clean = scoring.compute_score(measure(40))
    humming = scoring.compute_score(measure(40, hum_db=-45.0))
    assert 0.5 * clean.value < humming.value < clean.value
    assert abs((humming.base - humming.value) - sum(p.points for p in humming.penalties)) < 0.2


def test_clipping_is_a_heavy_proportional_penalty():
    sc = scoring.compute_score(measure(40, clip=True))
    assert any("estourando" in p.label for p in sc.penalties)
    assert sc.value < scoring.compute_score(measure(40)).value * 0.6
