import shutil
import subprocess

import numpy as np
import pytest
import soundfile as sf

from limpa_som import fileclean, metrics, synth

FS = 48000


def _noisy_speech(seconds=14.0, noise_db=-45.0):
    return synth.speech_like(seconds, FS, -22.0) + synth.white(seconds, FS, noise_db, seed=21)


def _floor_db(x):
    """Piso de ruído medido na parte final (a voz sintética tem pausas, mas aqui medimos o 1º segundo)."""
    n = int(0.25 * FS)
    return float(metrics.to_db(np.mean(x[:n] ** 2)))


def test_clean_wav_reduces_noise_and_keeps_original(tmp_path):
    src = tmp_path / "aula.wav"
    x = _noisy_speech()
    sf.write(str(src), x, FS, subtype="PCM_16")
    out = fileclean.clean_file(src, say=lambda s: None)
    assert len(out) == 1 and out[0].name == "aula_limpo.wav"
    assert src.exists()                                    # original intacto
    y, fs = sf.read(str(out[0]))
    assert fs == FS and len(y) == len(x)
    # a pausa do começo (antes da primeira palavra) fica bem mais silenciosa
    assert _floor_db(x[: FS // 4]) - _floor_db(y[: FS // 4]) > 8.0
    assert np.max(np.abs(y)) < 1.0


def test_clean_stereo_wav_keeps_channels(tmp_path):
    src = tmp_path / "estereo.wav"
    x = _noisy_speech()
    sf.write(str(src), np.stack([x, x * 0.7], axis=1), FS, subtype="PCM_16")
    out = fileclean.clean_file(src, say=lambda s: None)
    y, _ = sf.read(str(out[0]), always_2d=True)
    assert y.shape[1] == 2


def test_file_without_pauses_uses_gentler_reduction(tmp_path):
    src = tmp_path / "sem_pausas.wav"
    # fala corrida: voz contínua com volume variando, sem nenhum trecho só de ruído
    rng = np.random.default_rng(3)
    t = np.arange(10 * FS) / FS
    env_db = np.interp(t, np.arange(0, 10.4, 0.3), rng.uniform(-40.0, -8.0, 35))   # sílabas fracas e fortes
    voice = sum(np.sin(2 * np.pi * 130 * k * t) / k for k in range(1, 30)) * 10 ** (env_db / 20) * 0.1
    x = voice + synth.white(10, FS, -62.0, seed=2) + synth.hum(10, FS, -58.0, f0=60.0)
    sf.write(str(src), x, FS, subtype="PCM_16")
    messages = []
    fileclean.clean_file(src, say=messages.append)
    assert any("pausas" in m for m in messages)


def test_file_with_pauses_and_hum_reports_and_removes_the_hum(tmp_path):
    src = tmp_path / "com_zumbido.wav"
    x = synth.speech_like(14, FS, -22.0) + synth.white(14, FS, -58.0, seed=3) + synth.hum(14, FS, -48.0, f0=60.0)
    sf.write(str(src), x, FS, subtype="PCM_16")
    messages = []
    out = fileclean.clean_file(src, say=messages.append)
    assert any("zumbido de 60 Hz" in m for m in messages)
    assert not any("pausas" in m for m in messages)          # há pausas: redução total, sem aviso
    y, _ = sf.read(str(out[0]))
    assert metrics.analyze_noise(y, FS).hum is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg não instalado")
def test_clean_video_remuxes_with_cleaned_audio(tmp_path):
    wav = tmp_path / "som.wav"
    sf.write(str(wav), _noisy_speech(8.0), FS, subtype="PCM_16")
    video = tmp_path / "aula.mp4"
    subprocess.run([shutil.which("ffmpeg"), "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=8:r=10",
                    "-i", str(wav), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    str(video)], check=True, capture_output=True)
    out = fileclean.clean_file(video, say=lambda s: None)
    names = {p.name for p in out}
    assert names == {"aula_limpo.wav", "aula_limpo.mp4"}
    probe = subprocess.run([shutil.which("ffmpeg"), "-i", str(tmp_path / "aula_limpo.mp4")],
                           capture_output=True, text=True).stderr
    assert "Video:" in probe and "Audio:" in probe
