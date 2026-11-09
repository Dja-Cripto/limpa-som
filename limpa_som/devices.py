"""Listagem de microfones e gravação (WASAPI no Windows)."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

import numpy as np

TickFn = Callable[[float, float, float], None]  # (segundos, pico dBFS, rms dBFS)


class RecorderError(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


@dataclass
class InputDevice:
    name: str
    index: int
    samplerate: int
    channels: int
    is_default: bool


def _db(x: float) -> float:
    return 20.0 * np.log10(max(x, 1e-9))


def list_input_devices() -> List[InputDevice]:
    import sounddevice as sd

    apis = sd.query_hostapis()
    wasapi = next((i for i, a in enumerate(apis) if "WASAPI" in a["name"]), None)
    api = wasapi if wasapi is not None else sd.default.hostapi
    default_idx = apis[api]["default_input_device"]
    out: List[InputDevice] = []
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] == api and d["max_input_channels"] > 0:
            out.append(InputDevice(
                name=d["name"], index=i, samplerate=int(d["default_samplerate"]),
                channels=min(2, int(d["max_input_channels"])), is_default=(i == default_idx),
            ))
    return out


class Recorder:
    """Gravador real. 'kind' é ignorado aqui (existe para o simulador)."""

    simulated = False

    def __init__(self, device: InputDevice, exclusive: bool = False):
        self.device = device
        self.exclusive = exclusive
        self.samplerate = device.samplerate
        self.channels = device.channels

    def with_exclusive(self, exclusive: bool) -> "Recorder":
        return Recorder(self.device, exclusive)

    def record(self, seconds: float, kind: str = "silence", on_tick: Optional[TickFn] = None,
               stop: Optional[Callable[[], bool]] = None) -> np.ndarray:
        import sounddevice as sd

        blocks: List[np.ndarray] = []
        state = {"peak": 0.0, "rms": 0.0}
        lock = threading.Lock()

        def callback(indata, frames, time_info, status):  # thread do PortAudio
            blocks.append(indata.copy())
            with lock:
                state["peak"] = float(np.max(np.abs(indata)))
                state["rms"] = float(np.sqrt(np.mean(indata ** 2)))

        extra = None
        try:
            extra = sd.WasapiSettings(exclusive=self.exclusive)
        except Exception:
            if self.exclusive:
                raise RecorderError("Modo exclusivo indisponível neste computador.")
        try:
            stream = sd.InputStream(device=self.device.index, channels=self.channels,
                                    samplerate=self.samplerate, dtype="float32",
                                    callback=callback, extra_settings=extra)
        except Exception as exc:
            raise RecorderError(f"Não consegui abrir o microfone ({exc}).",
                                "Feche programas que usam o microfone (Discord, OBS, Zoom) e confira "
                                "Configurações > Privacidade > Microfone.")
        with stream:
            start = time.time()
            while True:
                elapsed = time.time() - start
                if elapsed >= seconds or (stop is not None and stop()):
                    break
                time.sleep(0.05)
                if on_tick:
                    with lock:
                        on_tick(elapsed, _db(state["peak"]), _db(state["rms"]))
        if not blocks:
            raise RecorderError("O microfone não entregou nenhum áudio.",
                                "Confira se ele está conectado e liberado em Configurações > Privacidade > Microfone.")
        data = np.concatenate(blocks, axis=0)
        return data[: int(seconds * self.samplerate)] if stop is None else data

    def exclusive_available(self) -> bool:
        try:
            self.with_exclusive(True).record(0.4)
            return True
        except Exception:
            return False


def best_channel(data: np.ndarray) -> int:
    """Canal mais forte de uma gravação de fala (microfones mono costumam ter canal vazio)."""
    if data.ndim == 1 or data.shape[1] == 1:
        return 0
    return int(np.argmax(np.sqrt(np.mean(data ** 2, axis=0))))


def pick_channel(data: np.ndarray, channel: int) -> np.ndarray:
    if data.ndim == 1:
        return data.astype(np.float64)
    return data[:, min(channel, data.shape[1] - 1)].astype(np.float64)


def channel_report(data: np.ndarray) -> Optional[str]:
    if data.ndim == 1 or data.shape[1] < 2:
        return None
    levels = 20 * np.log10(np.maximum(np.sqrt(np.mean(data ** 2, axis=0)), 1e-9))
    if levels.max() - levels.min() > 6.0:
        return (f"só um canal tem sinal (L {levels[0]:.0f} dBFS / R {levels[1]:.0f} dBFS): "
                "normal para microfone mono, usei o canal mais forte")
    return None
