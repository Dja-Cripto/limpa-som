import numpy as np

from limpa_som import dsp, metrics, synth

FS = 48000


def _profile_from(noise):
    r = metrics.analyze_noise(noise, FS)
    return r.raw_freqs, r.raw_psd


def test_profile_scale_matches_stft():
    # perfil do Welch convertido para a escala da STFT tem de bater com a medida direta
    from scipy import signal
    noise = synth.white(10, FS, -50.0, seed=1)
    f, p = _profile_from(noise)
    fz, _, z = signal.stft(noise, FS, window="hann", nperseg=2048, noverlap=1536, scaling="psd")
    direct = np.mean(np.abs(z) ** 2, axis=1)
    converted = dsp._profile_on_grid(f, p, fz)
    band = (fz > 500) & (fz < 20000)
    ratio_db = 10 * np.log10(np.mean(converted[band]) / np.mean(direct[band]))
    assert abs(ratio_db) < 0.6


def test_noise_reduction_on_noise_only():
    noise = synth.white(8, FS, -50.0, seed=2)
    profile = _profile_from(noise)
    test_noise = synth.white(8, FS, -50.0, seed=7)
    out = dsp.reduce_noise(test_noise, FS, *profile, reduction_db=15.0)
    before = metrics.analyze_noise(test_noise, FS).rms_db
    after = metrics.analyze_noise(out, FS).rms_db
    assert before - after > 12.0


def test_speech_preserved_and_snr_improves():
    noise_ref = synth.white(8, FS, -45.0, seed=3)
    profile = _profile_from(noise_ref)
    speech = synth.speech_like(14, FS, level_db=-22.0) + synth.white(14, FS, -45.0, seed=11)
    settings = dsp.CleanSettings(normalize=False)
    speech_c = dsp.denoise_chain(speech, FS, profile, settings)
    noise_c = dsp.denoise_chain(synth.white(8, FS, -45.0, seed=13), FS, profile, settings)
    m_before = metrics.measure(noise_ref, speech, FS)
    m_after = metrics.measure(noise_c, speech_c, FS)
    assert m_after.snr_db - m_before.snr_db > 10.0
    # a voz não pode ser engolida: nível ativo cai menos de 2 dB
    assert m_before.speech.level_db - m_after.speech.level_db < 2.0


def test_hum_removed():
    base = synth.white(8, FS, -65.0, seed=5) + synth.hum(8, FS, -50.0, f0=60.0)
    r = metrics.analyze_noise(base, FS)
    assert r.hum is not None
    cleaned = dsp.remove_hum(base, FS, dsp.hum_notches(r.hum.f0, r.hum.harmonics))
    assert metrics.analyze_noise(cleaned, FS).hum is None


def test_normalize_respects_ceiling():
    x = synth.speech_like(10, FS, level_db=-35.0)
    y = dsp.normalize(x, FS, target_rms_db=-20.0, peak_ceiling_db=-1.5)
    assert 20 * np.log10(np.max(np.abs(y))) <= -1.4
    assert abs(dsp.active_level_db(y, FS) - (-20.0)) < 3.5 or 20 * np.log10(np.max(np.abs(y))) > -1.7


def test_estimated_profile_from_speech_recording():
    speech = synth.speech_like(14, FS, level_db=-22.0) + synth.white(14, FS, -50.0, seed=17)
    f, p = dsp.estimate_profile(speech, FS)
    truth = metrics.analyze_noise(synth.white(8, FS, -50.0, seed=19), FS)
    band = (f > 500) & (f < 20000)
    est_level = 10 * np.log10(np.sum(p[band]) * (f[1] - f[0]))
    true_level = 10 * np.log10(np.sum(truth.raw_psd[(truth.raw_freqs > 500) & (truth.raw_freqs < 20000)])
                               * (truth.raw_freqs[1] - truth.raw_freqs[0]))
    assert abs(est_level - true_level) < 3.0
