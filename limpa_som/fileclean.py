"""Limpeza de arquivos de áudio ou vídeo que você já gravou."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np
import soundfile as sf

from . import dsp, metrics

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def load_audio(path: Path):
    """Lê wav/flac/ogg direto; outros formatos (mp3, mp4...) passam pelo ffmpeg, se existir."""
    try:
        data, fs = sf.read(str(path), always_2d=True, dtype="float32")
        return data, fs
    except Exception as first_error:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError(
                "Não consegui abrir este formato e o ffmpeg não está instalado. "
                "Converta para WAV ou instale o ffmpeg (winget install Gyan.FFmpeg)."
            ) from first_error
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "entrada.wav"
            subprocess.run([ffmpeg, "-y", "-i", str(path), "-vn", "-acodec", "pcm_f32le", str(wav)],
                           check=True, capture_output=True)
            return sf.read(str(wav), always_2d=True, dtype="float32")


def quiet_audio(mono: np.ndarray, fs: int, fraction: float = 0.12, block_s: float = 0.25) -> np.ndarray:
    """Junta os blocos mais quietos do arquivo (as pausas), para medir o ruído com alta resolução."""
    n = int(block_s * fs)
    count = len(mono) // n
    if count < 4:
        return mono[:0]
    blocks = (mono[:count * n] - mono.mean()).reshape(count, n)
    power = np.mean(blocks ** 2, axis=1)
    keep = np.sort(np.argsort(power)[:max(2, int(count * fraction))])
    return blocks[keep].reshape(-1)


def has_clear_pauses(mono: np.ndarray, fs: int) -> bool:
    """Há trechos só de ruído? Eles formam um patamar estável nos quadros mais quietos.

    Com pausas, os 5% mais quietos e os 25% mais quietos têm quase a mesma energia (é o ruído de
    fundo). Sem pausas (fala corrida), os quadros mais quietos são só as sílabas mais fracas e a
    energia varia muito. Olhar a diferença entre o som mais alto e o mais baixo não serve: em áudio
    muito ruidoso ela é pequena mesmo havendo pausas.
    """
    frames = metrics.to_db(metrics.frame_power(mono - mono.mean(), fs, 0.05))
    if len(frames) < 20:
        return False
    # medido: com pausas ~0,3 dB (até ~1,5 dB para ruído grave, que oscila mais); fala corrida 4 a 5 dB
    return float(np.percentile(frames, 25) - np.percentile(frames, 5)) < 2.5


def clean_file(path_in: Path, reduction_db: float = 15.0,
               say: Callable[[str], None] = print) -> List[Path]:
    data, fs = load_audio(path_in)
    mono = data.mean(axis=1)
    say(f"Arquivo: {data.shape[0] / fs:.1f} s, {fs} Hz, {data.shape[1]} canal(is).")

    if not has_clear_pauses(mono, fs):
        reduction_db = min(reduction_db, 8.0)
        say("Não achei pausas claras para medir o ruído: usei uma redução mais suave (8 dB) para não "
            "estragar a voz.")
    quiet = quiet_audio(mono, fs)
    hum = None
    if len(quiet) >= int(1.5 * fs):          # precisa de ouvido suficiente para enxergar linhas de 60 Hz
        fq, pq = metrics._welch(metrics.prepare(quiet, fs)[0], fs)
        hum = metrics.detect_hum(fq, pq)
    notch = dsp.hum_notches(hum.f0, hum.harmonics) if hum else []
    if hum:
        say(f"Detectei zumbido de {hum.f0:.0f} Hz e vou removê-lo.")

    settings = dsp.CleanSettings(notch_hz=notch, reduction_db=reduction_db)
    say("Limpando (pode levar alguns segundos)...")
    profile = dsp.profile_from_noise(mono, fs, settings.nfft, quiet_fraction=0.1)
    cleaned = dsp.clean_channels(data.astype(np.float64), fs, profile, settings)

    out_wav = path_in.with_name(path_in.stem + "_limpo.wav")
    sf.write(str(out_wav), cleaned, fs, subtype="PCM_16")
    created = [out_wav]

    ffmpeg = find_ffmpeg()
    if path_in.suffix.lower() in VIDEO_EXT and ffmpeg:
        out_video = path_in.with_name(path_in.stem + "_limpo.mp4")
        result = subprocess.run(
            [ffmpeg, "-y", "-i", str(path_in), "-i", str(out_wav), "-map", "0:v:0", "-map", "1:a:0",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out_video)],
            capture_output=True)
        if result.returncode == 0:
            created.append(out_video)
        else:
            say("Não consegui montar o vídeo novo; o áudio limpo ficou em WAV.")
    return created
