import numpy as np
import pytest

from limpa_som import metrics, synth

FS = 48000


def test_a_weighting_curve():
    f = np.array([100.0, 1000.0, 10000.0])
    a = metrics.a_weighting_db(f)
    assert abs(a[1]) < 0.2
    assert abs(a[0] - (-19.1)) < 1.0
    assert abs(a[2] - (-2.5)) < 1.0
    # o filtro no domínio do tempo concorda com a curva analítica (a transformação
    # bilinear a 48 kHz desvia ~1,2 dB perto de 10 kHz, irrelevante para o diagnóstico)
    sos = metrics._a_weighting_sos(FS)
    from scipy import signal
    _, h = signal.sosfreqz(sos, worN=f, fs=FS)
    assert np.allclose(20 * np.log10(abs(h)), a, atol=1.5)


def test_white_noise_level():
    x = synth.white(8, FS, -50.0)
    r = metrics.analyze_noise(x, FS)
    assert -51.0 < r.rms_db < -49.0
    assert r.rms_a_db < r.rms_db  # ponderação A atenua o ruído branco
    assert r.hum is None
    assert not r.tones
    assert r.flatness > 0.6
    assert r.hf_share > 0.4


def test_hum_detected_and_frequency():
    x = synth.white(8, FS, -70.0) + synth.hum(8, FS, -55.0, f0=60.0)
    r = metrics.analyze_noise(x, FS)
    assert r.hum is not None
    assert r.hum.f0 == 60.0
    assert r.hum.harmonics >= 3
    y = synth.white(8, FS, -70.0, seed=3) + synth.hum(8, FS, -55.0, f0=50.0)
    assert metrics.analyze_noise(y, FS).hum.f0 == 50.0


def test_hum_does_not_distort_the_broadband_shape():
    """Zumbido forte não pode fazer o chiado parecer 'ruído grave de ventilador'."""
    x = synth.white(8, FS, -60.0) + synth.hum(8, FS, -42.0, f0=60.0)
    r = metrics.analyze_noise(x, FS)
    assert r.hum is not None and r.hum_share > 0.9
    assert r.lf_share < 0.2          # sem o zumbido, quase nada é grave
    assert r.hf_share > 0.4          # o que sobra é o chiado de banda larga
    assert r.flatness > 0.5


def test_tone_detected():
    x = synth.white(8, FS, -72.0) + synth.tone(8, FS, 3150.0, -60.0)
    r = metrics.analyze_noise(x, FS)
    assert r.tones
    assert abs(r.tones[0].freq - 3150.0) < 25.0
    assert r.tones[0].prominence_db > 15.0
    assert r.tonal_share > 0.5


def test_cutoff_detected_for_lowpassed_noise():
    x = synth.lowpass(synth.white(8, FS, -45.0), FS, 8000.0)
    r = metrics.analyze_noise(x, FS)
    assert r.cutoff_hz is not None
    assert 7000.0 <= r.cutoff_hz <= 8800.0
    full = metrics.analyze_noise(synth.white(8, FS, -45.0, seed=5), FS)
    assert full.cutoff_hz is None


def test_agc_ramp_detected():
    r = metrics.analyze_noise(synth.ramp_noise(9, FS, -70.0, -55.0), FS)
    assert r.drift_db > 8.0 and r.drift_r2 > 0.6
    flat = metrics.analyze_noise(synth.white(9, FS, -60.0, seed=9), FS)
    assert abs(flat.drift_db) < 3.0


def test_speech_levels_and_snr():
    noise = synth.white(6, FS, -55.0, seed=2)
    speech = synth.speech_like(12, FS, level_db=-22.0) + synth.white(12, FS, -55.0, seed=4)
    m = metrics.measure(noise, speech, FS)
    assert m.speech is not None
    assert -24.5 < m.speech.level_db < -20.0
    assert 0.3 < m.speech.active_frac < 0.9
    assert m.speech.clip_runs == 0
    assert 22.0 < m.snr_db < 40.0
    assert m.speech.pause_noise_a_db < m.speech.level_a_db - 20


def test_clipping_detected():
    noise = synth.white(6, FS, -60.0)
    speech = np.clip(synth.speech_like(12, FS, level_db=-6.0) * 4.0, -1.0, 1.0)
    m = metrics.measure(noise, speech, FS)
    assert m.speech.clip_runs > 10
    assert m.speech.peak_db > -0.5


def test_digital_silence_gate_detected():
    x = synth.white(8, FS, -50.0)
    x[int(2 * FS):int(6 * FS)] = 0.0
    r = metrics.analyze_noise(x, FS)
    assert r.digital_silence_frac > 0.3


@pytest.mark.parametrize("floor_db", [-60.0, -75.0, -90.0, -100.0])
def test_detections_work_on_very_quiet_microphones(floor_db):
    """Regressão: com o piso de ruído de microfones bons (-80 dBFS ou menos) nada pode sumir."""
    hum = metrics.analyze_noise(synth.white(8, FS, floor_db) + synth.hum(8, FS, floor_db + 15.0), FS)
    assert hum.hum is not None and hum.hum.f0 == 60.0
    tone = metrics.analyze_noise(synth.white(8, FS, floor_db, seed=2) + synth.tone(8, FS, 2500.0, floor_db + 12.0), FS)
    assert tone.tones and abs(tone.tones[0].freq - 2500.0) < 25.0
    cut = metrics.analyze_noise(synth.lowpass(synth.white(8, FS, floor_db, seed=3), FS, 8000.0), FS)
    assert cut.cutoff_hz is not None and 7000.0 <= cut.cutoff_hz <= 8800.0
    clean = metrics.analyze_noise(synth.white(8, FS, floor_db, seed=4), FS)
    assert clean.hum is None and not clean.tones and clean.cutoff_hz is None
    assert clean.flatness > 0.6


def test_short_recordings_rejected():
    with pytest.raises(ValueError):
        metrics.analyze_noise(np.zeros(FS // 2), FS)
