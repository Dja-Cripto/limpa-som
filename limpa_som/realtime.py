"""Limpeza em tempo real para o microfone todo (Discord, OBS, Zoom...) via Equalizer APO.

O programa não baixa nem instala nada: usa o Equalizer APO (e o plugin RNNoise) que já estejam
instalados. Ele escreve um arquivo de filtros calculado a partir do ruído medido do seu microfone
(notches no zumbido e nos apitos, filtro de graves e RNNoise sem gate) e liga esse arquivo ao
config.txt de forma reversível, isolando o microfone dos ajustes gerais que existirem ali.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from .dsp import hum_notches
from .metrics import NoiseReport

APO_CLSID = "EACD2258-FCAC-4FF4-B36D-419E924A6D79"
MIC_FILE = "limpa_som_mic.txt"
BACKUP_NAME = "config.txt.limpa_som.bak"

IF_LINE = 'If: stage != "capture"'
WRAP_BEGIN = "# [Limpa Som] inicio: o trecho abaixo nao vale para microfones"
INC_BEGIN = "# [Limpa Som] filtros do microfone (apague estas linhas ou use o menu do programa para desligar)"
BLOCK_END = "# [Limpa Som] fim"

_COMMAND = re.compile(r"^\s*[A-Za-z]+\s*\d*\s*:")
_DEVICE = re.compile(r"^\s*Device\s*:", re.IGNORECASE)


@dataclass
class ApoInfo:
    install_path: Path
    config_dir: Path
    has_rnnoise_mono: bool
    has_rnnoise_stereo: bool
    configurator: Optional[Path]

    @property
    def can_write(self) -> bool:
        return os.access(self.config_dir, os.W_OK)


def find_apo() -> Optional[ApoInfo]:
    install: Optional[Path] = None
    config: Optional[Path] = None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\EqualizerAPO") as key:
            install = Path(winreg.QueryValueEx(key, "InstallPath")[0])
            config = Path(winreg.QueryValueEx(key, "ConfigPath")[0])
    except Exception:
        default = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "EqualizerAPO"
        if default.exists():
            install, config = default, default / "config"
    if install is None or config is None or not config.exists():
        return None
    vst = install / "VSTPlugins"
    tool = next((install / n for n in ("DeviceSelector.exe", "Configurator.exe") if (install / n).exists()), None)
    return ApoInfo(install, config, (vst / "rnnoise_mono.dll").exists(), (vst / "rnnoise_stereo.dll").exists(), tool)


def open_configurator(info: ApoInfo) -> bool:
    """Abre o Configurator pedindo permissão de administrador (o Windows mostra o aviso UAC)."""
    if info.configurator is None:
        return False
    try:
        import ctypes
        # 'runas' = executar como administrador; devolve > 32 se abriu
        return int(ctypes.windll.shell32.ShellExecuteW(None, "runas", str(info.configurator), None,
                                                       str(info.install_path), 1)) > 32
    except Exception:
        return False


def endpoint_guid(endpoint_id: str) -> Optional[str]:
    m = re.search(r"\{([0-9a-fA-F-]{36})\}\s*$", endpoint_id or "")
    return m.group(1).lower() if m else None


def is_attached(guid: Optional[str], flow: str = "Capture") -> Optional[bool]:
    """O Equalizer APO está registrado neste microfone? (None = não deu para verificar)"""
    if not guid:
        return None
    try:
        import winreg
        path = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\%s\{%s}\FxProperties" % (flow, guid))
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            index = 0
            while True:
                try:
                    _, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    return False
                index += 1
                if isinstance(value, str) and APO_CLSID.lower() in value.lower():
                    return True
    except FileNotFoundError:
        return False
    except Exception:
        return None


def device_pattern(friendly_name: str) -> str:
    """'mi mesa (High Definition Audio Device)' -> 'mi mesa High Definition Audio Device'.

    O APO casa todas as palavras com 'nome do dispositivo + nome da conexão + GUID'. Sem o GUID, o
    filtro continua valendo se o Windows recriar o dispositivo e mudar o ID (o que já aconteceu).
    """
    return " ".join(re.sub(r"[()]", " ", friendly_name).split())


# ------------------------------------------------------------- arquivo de filtros ---
def build_mic_config(pattern: str, noise: Optional[NoiseReport], rnnoise: bool = True,
                     rnnoise_available: Optional[tuple] = (True, True), label: str = "",
                     when: Optional[datetime] = None) -> str:
    """Arquivo de filtros só a partir do ruído medido (sem a voz): corte de graves, notches e a IA anti-chiado."""
    from . import design, eq
    if noise is None:
        profile = eq.Profile(rnnoise=1 if rnnoise else 0, bands=[eq.Band.new("HP", 80.0)])
    else:
        profile = design.design_profile(noise, None, rnnoise=rnnoise)
    return eq.build_config(pattern, profile, label=label, rnnoise_available=rnnoise_available, when=when)


# -------------------------------------------------------- integração ao config.txt ---
def _is_active(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#") and bool(_COMMAND.match(line))


def is_installed(config_text: str) -> bool:
    return INC_BEGIN in config_text


def install_include(text: str, include_name: str = MIC_FILE) -> str:
    """Isola o microfone dos ajustes gerais do config.txt e acrescenta o arquivo do Limpa Som.

    Reversível por uninstall_include. Se já estiver instalado, devolve o texto como está.
    """
    if is_installed(text):
        return text
    nl = "\r\n" if "\r\n" in text else "\n"
    out: List[str] = []
    run: List[str] = []

    def flush() -> None:
        active = [i for i, l in enumerate(run) if _is_active(l)]
        if not active:
            out.extend(run)
        else:
            first, last = active[0], active[-1]
            out.extend(run[:first])
            out.extend([WRAP_BEGIN, IF_LINE])
            out.extend(run[first:last + 1])
            out.extend(["EndIf:", BLOCK_END])
            out.extend(run[last + 1:])
        run.clear()

    for line in text.splitlines():
        if _DEVICE.match(line):
            flush()
            out.append(line)
        else:
            run.append(line)
    flush()
    out += ["", INC_BEGIN, "Include: %s" % include_name, BLOCK_END]
    return nl.join(out) + nl


def uninstall_include(text: str) -> str:
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line == WRAP_BEGIN and i + 1 < len(lines) and lines[i + 1] == IF_LINE:
            j = next((k for k in range(i + 2, len(lines)) if lines[k] == BLOCK_END), None)
            if j is not None and lines[j - 1] == "EndIf:":
                out.extend(lines[i + 2:j - 1])
                i = j + 1
                continue
        if line == INC_BEGIN:
            if out and not out[-1].strip():
                out.pop()
            j = next((k for k in range(i + 1, len(lines)) if lines[k] == BLOCK_END), len(lines) - 1)
            i = j + 1
            continue
        out.append(line)
        i += 1
    return nl.join(out) + nl


# --------------------------------------------------------------------- aplicar ---
@dataclass
class ApplyResult:
    ok: bool
    message: str
    mic_file: Optional[Path] = None
    backup: Optional[Path] = None
    config_changed: bool = False
    notes: List[str] = field(default_factory=list)


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="surrogateescape")


def _write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8", errors="surrogateescape"))


def apply(info: ApoInfo, mic_config: str) -> ApplyResult:
    config = info.config_dir / "config.txt"
    mic_file = info.config_dir / MIC_FILE
    if not info.can_write:
        return ApplyResult(False, "Sem permissão para escrever em %s. Abra o programa como administrador "
                                  "(botão direito no INICIAR.bat > Executar como administrador)." % info.config_dir)
    try:
        original = _read(config) if config.exists() else ""
        backup = info.config_dir / BACKUP_NAME
        if not backup.exists():
            shutil.copy2(config, backup) if config.exists() else _write(backup, "")
        _write(mic_file, mic_config)
        new_text = install_include(original)
        changed = new_text != original
        if changed:
            _write(config, new_text)
        notes = []
        if changed and new_text.count(WRAP_BEGIN):
            notes.append("Os ajustes gerais que já existiam no config.txt continuam valendo para saídas "
                         "(alto-falantes, fones), mas deixaram de valer para microfones.")
        return ApplyResult(True, "Filtros gravados.", mic_file, backup, changed, notes)
    except OSError as exc:
        return ApplyResult(False, "Não consegui gravar os arquivos do Equalizer APO: %s" % exc)


def write_mic_file(info: ApoInfo, mic_config: str) -> ApplyResult:
    """Só reescreve o arquivo de filtros (o config.txt já aponta para ele). O APO relê sozinho em segundos."""
    if not info.can_write:
        return ApplyResult(False, "Sem permissão para escrever em %s (abra como administrador)." % info.config_dir)
    try:
        _write(info.config_dir / MIC_FILE, mic_config)
        return ApplyResult(True, "Filtros atualizados.", info.config_dir / MIC_FILE)
    except OSError as exc:
        return ApplyResult(False, "Não consegui gravar os filtros: %s" % exc)


def remove(info: ApoInfo) -> ApplyResult:
    config = info.config_dir / "config.txt"
    if not info.can_write:
        return ApplyResult(False, "Sem permissão para escrever em %s (abra como administrador)." % info.config_dir)
    try:
        changed = False
        if config.exists():
            text = _read(config)
            if is_installed(text) or WRAP_BEGIN in text:
                _write(config, uninstall_include(text))
                changed = True
        mic_file = info.config_dir / MIC_FILE
        if mic_file.exists():
            mic_file.unlink()
            changed = True
        return ApplyResult(True, "Limpeza em tempo real desligada." if changed else "Nada para desligar.",
                           config_changed=changed)
    except OSError as exc:
        return ApplyResult(False, "Não consegui desfazer: %s" % exc)
