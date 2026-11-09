"""Regressão: ruído real (cheio de linhas estreitas) tem de ser reduzido tanto quanto o ruído branco.

No teste real do Daniel (03/11), o perfil de alta resolução subestimava o ruído em ~6 dB e a limpeza
digital só tirava 7 dB dos 15 dB prometidos.
"""
import numpy as np

from limpa_som import dsp, metrics

FS = 48000


def spiky_noise(seconds: float, seed: int, lines: int = 120) -> np.ndarray:
    """Ruído colorido + muitas linhas espectrais (zumbido, apitos, pente de chaveamento)."""
    rng = np.random.default_rng(seed)
    n = int(seconds * FS)
    f = np.fft.rfftfreq(n, 1 / FS)
    psd = 10 ** (-(95 + 6 * np.log10(np.maximum(f, 50) / 50)) / 10)           # cai ~6 dB por década
    spec = np.fft.rfft(rng.standard_normal(n)) * np.sqrt(psd / (2.0 / FS))
    x = np.fft.irfft(spec, n)
    t = np.arange(n) / FS
    line_rng = np.random.default_rng(99)                                       # as MESMAS linhas em toda gravação
    for fr, amp in zip(line_rng.uniform(150, 16000, lines), line_rng.uniform(0.0002, 0.002, lines)):
        x += amp * np.sin(2 * np.pi * fr * t + line_rng.uniform(0, 6.28))
    return x


def band_reduction(before, after, lo=300, hi=6000):
    fb, pb = metrics._welch(before, FS)
    fa, pa = metrics._welch(after, FS)
    m = (fb >= lo) & (fb < hi)
    return 10 * np.log10(pb[m].sum() / pa[m].sum())


def test_profile_from_noise_matches_what_the_reducer_sees():
    from scipy import signal
    a, b = spiky_noise(10, 1), spiky_noise(10, 2)
    f, psd = dsp.profile_from_noise(a, FS)
    fz, _, z = signal.stft(b, FS, window="hann", nperseg=2048, noverlap=1536, scaling="psd")
    ratio = np.abs(z) ** 2 / dsp._profile_on_grid(f, psd, fz)[:, None]
    mid = (fz >= 300) & (fz < 6000)
    assert abs(10 * np.log10(ratio[mid].mean())) < 0.6          # perfil e ruído concordam (média de P/N ~ 1)


def test_noise_reduction_reaches_the_promised_15_db_on_spiky_noise():
    a, b = spiky_noise(10, 1), spiky_noise(10, 2)
    new = dsp.reduce_noise(b, FS, *dsp.profile_from_noise(a, FS), reduction_db=15.0)
    assert band_reduction(b, new) >= 13.0


def test_the_old_point_sampled_profile_really_was_worse():
    """Documenta o defeito: o perfil Welch de alta resolução deixava a redução abaixo de 10 dB."""
    a, b = spiky_noise(10, 1), spiky_noise(10, 2)
    r = metrics.analyze_noise(a, FS)
    old = dsp.reduce_noise(b, FS, r.raw_freqs, r.raw_psd, reduction_db=15.0)
    new = dsp.reduce_noise(b, FS, *dsp.profile_from_noise(a, FS), reduction_db=15.0)
    assert band_reduction(b, new) > band_reduction(b, old) + 3.0


def test_clean_file_uses_the_matched_profile(tmp_path):
    import soundfile as sf
    from limpa_som import fileclean, synth
    speech = synth.speech_like(14, FS, -22.0)
    x = speech + spiky_noise(14, 5) * 3.0
    src = tmp_path / "aula.wav"
    sf.write(str(src), x, FS, subtype="PCM_16")
    out = fileclean.clean_file(src, say=lambda s: None)
    y, _ = sf.read(str(out[0]))

    # quadros de 50 ms com fala e sem fala (pela fala pura de referência)
    n = int(0.05 * FS)
    count = len(x) // n
    ref = metrics.to_db(np.mean(speech[:count * n].reshape(count, n) ** 2, axis=1))
    talking, pause = ref > -35, ref < -80

    def voice_over_pauses(sig):          # quanto a voz fica acima das pausas (o nivelamento de volume se cancela)
        frames = sig[:count * n].reshape(count, n)
        return (metrics.to_db(np.mean(frames[talking] ** 2)) - metrics.to_db(np.mean(frames[pause] ** 2)))

    assert voice_over_pauses(y) - voice_over_pauses(x) > 10.0
