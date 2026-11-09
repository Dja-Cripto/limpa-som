"""Roda o plugin RNNoise (VST2) do Equalizer APO fora do Windows, para MEDIR o efeito real numa gravação.

É um anfitrião VST2 mínimo, só o necessário para o RNNoise: carrega a DLL, entrega a configuração (porta de voz)
e processa o áudio em blocos de 480 amostras (10 ms a 48 kHz), como o APO faz. O Limpa Som usa isso para testar o
equalizador que projetou na sua própria voz e escolher os ajustes que de fato limpam, em vez de chutar.

O plugin é código nativo: roda sempre num processo à parte (`process_isolated`), para um defeito dele nunca
derrubar o programa.
"""
from __future__ import annotations

import base64
import ctypes
import os
import subprocess
import sys
import tempfile
from ctypes import CFUNCTYPE, POINTER, Structure, c_float, c_int32, c_ssize_t, c_void_p
from pathlib import Path
from typing import Optional

import numpy as np

FS = 48000
BLOCK = 480
PLUGIN_NAME = "rnnoise_mono.dll"


def plugin_path(apo_install: Path) -> Path:
    return Path(apo_install) / "VSTPlugins" / PLUGIN_NAME


def _resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return x
    from math import gcd

    from scipy import signal
    g = gcd(src, dst)
    return signal.resample_poly(x, dst // g, src // g)


def _load(dll: Path):
    master_t = CFUNCTYPE(c_ssize_t, c_void_p, c_int32, c_int32, c_ssize_t, c_void_p, c_float)
    dispatch_t = CFUNCTYPE(c_ssize_t, c_void_p, c_int32, c_int32, c_ssize_t, c_void_p, c_float)
    process_t = CFUNCTYPE(None, c_void_p, POINTER(POINTER(c_float)), POINTER(POINTER(c_float)), c_int32)

    class AEffect(Structure):
        _fields_ = [("magic", c_int32), ("dispatcher", dispatch_t), ("process", c_void_p), ("setParameter", c_void_p),
                    ("getParameter", c_void_p), ("numPrograms", c_int32), ("numParams", c_int32),
                    ("numInputs", c_int32), ("numOutputs", c_int32), ("flags", c_int32),
                    ("resvd1", c_ssize_t), ("resvd2", c_ssize_t), ("initialDelay", c_int32),
                    ("realQualities", c_int32), ("offQualities", c_int32), ("ioRatio", c_float),
                    ("object", c_void_p), ("user", c_void_p), ("uniqueID", c_int32), ("version", c_int32),
                    ("processReplacing", process_t), ("processDoubleReplacing", c_void_p),
                    ("future", ctypes.c_char * 56)]

    def master(effect, opcode, index, value, ptr, opt):           # o plugin só pergunta a versão do anfitrião
        return 2400 if opcode == 1 else 0

    callback = master_t(master)
    os.add_dll_directory(str(dll.parent.parent))                  # onde ficam as DLLs do runtime do APO
    os.add_dll_directory(str(dll.parent))
    lib = ctypes.WinDLL(str(dll))
    entry = lib.VSTPluginMain
    entry.restype = POINTER(AEffect)
    entry.argtypes = [master_t]
    return entry(callback), callback


def process(x: np.ndarray, fs: int, dll: Path, gate: float, passes: int = 1) -> np.ndarray:
    """Passa `x` (mono) `passes` vezes pelo RNNoise com a porta de voz `gate`. Roda no processo atual."""
    from . import eq
    mono = _resample(np.asarray(x, dtype=np.float64), fs, FS).astype(np.float32)
    chunk = base64.b64decode(eq.rnnoise_chunk(gate))
    for _ in range(max(1, passes)):
        eff, _keep = _load(dll)
        effect = eff.contents
        ptr = ctypes.cast(eff, c_void_p)

        def dispatch(opcode, index=0, value=0, data=None, opt=0.0):
            return effect.dispatcher(ptr, opcode, index, value, data, opt)

        dispatch(0)                                               # effOpen
        dispatch(10, 0, 0, None, float(FS))                       # effSetSampleRate
        dispatch(11, 0, BLOCK)                                    # effSetBlockSize
        buf = ctypes.create_string_buffer(chunk, len(chunk))
        dispatch(24, 0, len(chunk), ctypes.cast(buf, c_void_p))   # effSetChunk: a configuração do plugin
        dispatch(12, 0, 1)                                        # effMainsChanged(ligado)
        dispatch(71)                                              # effStartProcess
        n_in, n_out = effect.numInputs, effect.numOutputs
        out = np.zeros(len(mono), dtype=np.float32)
        for i in range(0, len(mono) - BLOCK + 1, BLOCK):
            block = np.ascontiguousarray(mono[i:i + BLOCK])
            ins = (POINTER(c_float) * n_in)(*[block.ctypes.data_as(POINTER(c_float)) for _ in range(n_in)])
            bufs = [np.zeros(BLOCK, dtype=np.float32) for _ in range(n_out)]
            outs = (POINTER(c_float) * n_out)(*[b.ctypes.data_as(POINTER(c_float)) for b in bufs])
            effect.processReplacing(ptr, ins, outs, BLOCK)
            out[i:i + BLOCK] = bufs[0]
        dispatch(72)                                              # effStopProcess
        dispatch(12, 0, 0)
        dispatch(1)                                               # effClose
        mono = out
    return _resample(mono.astype(np.float64), FS, fs)[: len(x)]


def process_isolated(x: np.ndarray, fs: int, apo_install: Path, gate: float, passes: int = 1,
                     timeout: float = 180.0) -> Optional[np.ndarray]:
    """O mesmo, num processo à parte. Devolve None se o plugin não existir ou falhar."""
    dll = plugin_path(apo_install)
    if not dll.exists() or sys.platform != "win32":
        return None
    with tempfile.TemporaryDirectory(prefix="limpa_som_vst_") as tmp:
        src, dst = Path(tmp) / "entrada.npy", Path(tmp) / "saida.npy"
        np.save(src, np.asarray(x, dtype=np.float32))
        try:
            subprocess.run([sys.executable, "-m", "limpa_som.vsthost", str(src), str(dst), str(fs), str(dll),
                            repr(float(gate)), str(int(passes))],
                           cwd=str(Path(__file__).resolve().parent.parent), timeout=timeout, check=True,
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return np.load(dst).astype(np.float64)
        except Exception:
            return None


if __name__ == "__main__":            # processo filho: entrada.npy saida.npy fs dll gate passes
    _src, _dst, _fs, _dll, _gate, _passes = sys.argv[1:7]
    np.save(_dst, process(np.load(_src), int(_fs), Path(_dll), float(_gate), int(_passes)).astype(np.float32))
