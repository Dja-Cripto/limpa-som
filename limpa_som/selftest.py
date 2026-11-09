"""Autoteste: confere se as bibliotecas e as medições estão funcionando neste computador."""
from __future__ import annotations

from . import __version__, synth, ui, winaudio
from .metrics import measure
from .scoring import compute_score

FS = 48000


def run() -> int:
    ui.title(f"AUTOTESTE (Limpa Som {__version__})")
    problems = 0

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal problems
        if passed:
            ui.ok(label + (f" ({detail})" if detail else ""))
        else:
            problems += 1
            ui.bad(label + (f" ({detail})" if detail else ""))

    import numpy
    import scipy
    check("numpy e scipy", True, f"numpy {numpy.__version__}, scipy {scipy.__version__}")
    try:
        import sounddevice as sd
        check("sounddevice (gravação)", True, sd.__version__)
    except Exception as exc:
        check("sounddevice (gravação)", False, str(exc))
    try:
        import soundfile
        check("soundfile (arquivos de áudio)", True, soundfile.__version__)
    except Exception as exc:
        check("soundfile (arquivos de áudio)", False, str(exc))
    try:
        import tkinter
        check("janela (tkinter)", True, f"Tk {tkinter.TkVersion}")
    except Exception as exc:
        check("janela (tkinter)", False, f"{exc}: o programa funciona no modo texto (--console)")
    check("controle de volume do Windows (pycaw)", winaudio.AVAILABLE,
          "" if winaudio.AVAILABLE else "sem isso o programa só mede e orienta")

    noise = synth.white(6, FS, -50.0)
    speech = synth.speech_like(12, FS, -22.0) + synth.white(12, FS, -50.0, seed=3)
    m = measure(noise, speech, FS)
    score = compute_score(m)
    check("medição de ruído", abs(m.noise.rms_db + 50.0) < 1.5, f"{m.noise.rms_db:.1f} dBFS (esperado -50)")
    check("nota para um sinal de teste conhecido", 3.0 <= score.value <= 8.0, f"{score.text}/10")

    try:
        from .devices import list_input_devices
        devs = list_input_devices()
        check("microfones encontrados", len(devs) > 0, f"{len(devs)} dispositivo(s)")
    except Exception as exc:
        check("microfones encontrados", False, str(exc))

    ui.say()
    if problems:
        ui.bad(f"{problems} problema(s). Rode INICIAR.bat de novo para reinstalar as dependências.")
        return 1
    ui.ok("Tudo certo: pode rodar o teste do microfone.")
    return 0
