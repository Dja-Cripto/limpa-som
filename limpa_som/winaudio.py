"""Controle do microfone no Windows: volume, mudo e 'Aumento de sensibilidade' (mic boost)."""
from __future__ import annotations

import os
import re
import subprocess
from ctypes import byref
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .tuning import Settings, boost_options_from_range

try:  # tudo isso só existe no Windows
    import comtypes  # noqa: F401
    from comtypes import CLSCTX_ALL, GUID
    from pycaw.pycaw import AudioUtilities

    from ._topology import IAudioVolumeLevel, IDeviceTopology, IPart
    AVAILABLE = True
except Exception:  # pragma: no cover - fora do Windows
    AVAILABLE = False

_BOOST_WORDS = re.compile(r"boost|aumento|refor|ganho|gain|sensib|amplif|preamp|refuerzo|ganancia", re.I)
_CHIPS = {"10ec": "Realtek", "8086": "Intel", "1022": "AMD", "14f1": "Conexant", "111d": "IDT",
          "1106": "VIA", "10de": "NVIDIA", "13f6": "C-Media", "1013": "Cirrus Logic"}


@dataclass
class _Level:
    name: str
    iface: object
    lo: float
    hi: float
    step: float


def classify_adapter(adapter_id: str) -> Tuple[str, str]:
    """Descobre o tipo de ligação a partir do caminho do dispositivo do Windows."""
    s = (adapter_id or "").lower()
    chip = ""
    m = re.search(r"ven_([0-9a-f]{4})", s)
    if m:
        chip = _CHIPS.get(m.group(1), "")
    if "usb#" in s:
        return "usb", chip
    if "hdaudio#" in s:
        return "analogica", chip
    if "bth" in s or "bluetooth" in s:
        return "bluetooth", chip
    if s.startswith("{2}.\\\\?\\root#") or "swd#" in s or "sonar" in s or "vb-audio" in s:
        return "virtual", chip
    return "desconhecida", chip


def _walk(part, found: List[_Level], seen: set, depth: int = 0) -> None:
    if depth > 12:
        return
    try:
        gid = part.GetGlobalId()
    except Exception:
        gid = None
    if gid in seen:
        return
    seen.add(gid)
    try:
        name = part.GetName() or ""
    except Exception:
        name = ""
    try:
        for i in range(part.GetControlInterfaceCount()):
            ci = part.GetControlInterface(i)
            iid = str(ci.GetIID()).upper()
            if iid == str(IAudioVolumeLevel._iid_).upper():
                try:
                    unk = part.Activate(CLSCTX_ALL, byref(GUID(iid)))
                    lvl = unk.QueryInterface(IAudioVolumeLevel)
                    lo, hi, st = lvl.GetLevelRange(0)
                    found.append(_Level(name, lvl, float(lo), float(hi), float(st)))
                except Exception:
                    pass
    except Exception:
        pass
    try:
        outgoing = part.EnumPartsOutgoing()
        for i in range(outgoing.GetCount()):
            _walk(outgoing.GetPart(i), found, seen, depth + 1)
    except Exception:
        pass


class EndpointControl:
    """Controla volume, mudo e reforço de um microfone específico do Windows."""

    def __init__(self, device):
        self._dev = device
        self.name: str = device.FriendlyName
        self.endpoint_id: str = device.id
        self._vol = device.EndpointVolume
        self._boost: Optional[_Level] = None
        self.connection = "desconhecida"
        self.chip = ""
        self.adapter_id = ""
        self._scan_topology()

    # -- topologia -----------------------------------------------------------
    def _scan_topology(self) -> None:
        try:
            topo = self._dev._dev.Activate(IDeviceTopology._iid_, CLSCTX_ALL, None).QueryInterface(IDeviceTopology)
            if topo.GetConnectorCount() < 1:
                return
            conn = topo.GetConnector(0)
            if not conn.IsConnected():
                return
            self.adapter_id = conn.GetDeviceIdConnectedTo() or ""
            self.connection, self.chip = classify_adapter(self.adapter_id)
            start = conn.GetConnectedTo().QueryInterface(IPart)
            found: List[_Level] = []
            _walk(start, found, set())
            vlo, vhi, _ = self.volume_range()
            others = [p for p in found if not (abs(p.lo - vlo) < 0.05 and abs(p.hi - vhi) < 0.05)]
            named = [p for p in others if _BOOST_WORDS.search(p.name)]
            plain = [p for p in others if p.lo >= 0 and p.hi <= 60 and p.step >= 5]
            self._boost = (named or plain or [None])[0]
        except Exception:
            self._boost = None

    # -- interface Control ---------------------------------------------------
    @property
    def has_boost(self) -> bool:
        return self._boost is not None

    @property
    def has_gain_control(self) -> bool:
        """O Windows controla o ganho deste microfone? (Alguns USB, como a placa da mesa, expõem uma faixa
        de volume sem efeito, tipo -96 dB: o ganho deles é no próprio equipamento.)"""
        lo, hi, _ = self.volume_range()
        return self._boost is not None or (hi - lo) >= 6.0

    @property
    def boost_name(self) -> str:
        return self._boost.name if self._boost else ""

    def boost_options(self) -> List[float]:
        if not self._boost:
            return []
        return boost_options_from_range(self._boost.lo, self._boost.hi, self._boost.step)

    def volume_range(self) -> Tuple[float, float, float]:
        lo, hi, st = self._vol.GetVolumeRange()
        return float(lo), float(hi), float(st)

    def get_settings(self) -> Settings:
        boost = float(self._boost.iface.GetLevel(0)) if self._boost else None
        return Settings(boost_db=boost, volume_db=float(self._vol.GetMasterVolumeLevel()),
                        muted=bool(self._vol.GetMute()))

    def apply(self, s: Settings) -> Settings:
        if s.boost_db is not None and self._boost is not None:
            level = min(max(s.boost_db, self._boost.lo), self._boost.hi)
            self._boost.iface.SetLevelUniform(level, None)
        lo, hi, _ = self.volume_range()
        self._vol.SetMasterVolumeLevel(min(max(s.volume_db, lo), hi), None)
        self._vol.SetMute(1 if s.muted else 0, None)
        return self.get_settings()


def find_control(device_name: str) -> Optional[EndpointControl]:
    """Acha o controle do Windows para o microfone com esse nome (None se não achar)."""
    if not AVAILABLE:
        return None
    try:
        devices = AudioUtilities.GetAllDevices(data_flow=1, device_state=1)  # captura, ativos
    except Exception:
        return None
    wanted = device_name.strip().lower()
    for d in devices:
        fn = (d.FriendlyName or "").strip().lower()
        if fn == wanted:
            return EndpointControl(d)
    for d in devices:  # nomes do PortAudio às vezes vêm cortados (31 caracteres)
        fn = (d.FriendlyName or "").strip().lower()
        if fn.startswith(wanted) or wanted.startswith(fn):
            return EndpointControl(d)
    return None


# -- atalhos para telas do Windows ------------------------------------------
def open_recording_panel() -> None:
    """Abre o painel clássico de som, já na aba Gravação (Propriedades > Melhorias)."""
    subprocess.Popen(["rundll32.exe", "shell32.dll,Control_RunDLL", "mmsys.cpl,,1"])


def open_sound_settings() -> None:
    os.startfile("ms-settings:sound")  # type: ignore[attr-defined]


def open_mic_privacy() -> None:
    os.startfile("ms-settings:privacy-microphone")  # type: ignore[attr-defined]
