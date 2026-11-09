"""Microfone simulado: mesma interface do gravador real, com 'física' de ganho realista.

Serve para testar o programa inteiro sem microfone e para o modo de demonstração (--simular).
O ruído total é a soma de:
  pre   : fonte/ambiente/pré da mesa (escala com o ganho total)
  mid   : ruído da entrada do conversor (escala só com o volume)
  boost : ruído do próprio estágio de reforço (cresce com o reforço)
  post  : ruído depois de todo o ganho (EMI/conversor/digital), constante
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import numpy as np

from . import synth
from .devices import InputDevice, TickFn
from .tuning import Settings, boost_options_from_range

FS = 48000


@dataclass
class Scenario:
    key: str
    title: str
    speech_db0: float = -45.0
    pre_db0: float = -85.0
    mid_db0: float = -90.0
    boost_db0: float = -120.0
    post_db: float = -78.0
    isolated_pre_drop_db: float = 40.0
    hum_db: Optional[float] = None
    hum_f0: float = 60.0
    hum_pre_gain: bool = False                 # o zumbido entra junto com o sinal e cresce com o ganho
    tone_hz: Optional[float] = None
    tone_db: float = -60.0
    effects_noise_db: Optional[float] = None   # ruído extra só no modo compartilhado
    realtime_clean_db: float = 0.0             # filtro em tempo real (APO) que reduz o ruído só no modo compartilhado
    agc_ramp_db: float = 0.0                   # ruído sobe ao longo da gravação (modo compartilhado)
    cutoff_hz: Optional[float] = None
    start: Settings = field(default_factory=lambda: Settings(20.0, 2.0))
    connection: str = "analogica"
    chip: str = "Realtek"
    wiring: Optional[str] = None             # como o microfone está ligado (mesa, direto...)


SCENARIOS = {
    "bom": Scenario("bom", "Microfone bom, bem configurado", pre_db0=-95, mid_db0=-97, post_db=-82,
                    start=Settings(20.0, 0.0)),
    "ganho_mal_dividido": Scenario(
        "ganho_mal_dividido", "Volume do Windows no máximo e reforço zerado (chiado de ganho digital)",
        pre_db0=-88, mid_db0=-80, boost_db0=-110, post_db=-75, start=Settings(0.0, 12.0)),
    "reforco_ruidoso": Scenario(
        "reforco_ruidoso", "Reforço alto demais (+30 dB) injetando chiado",
        pre_db0=-92, mid_db0=-96, boost_db0=-83, post_db=-80, start=Settings(30.0, -10.0)),
    "placa_ruidosa": Scenario(
        "placa_ruidosa", "Entrada da placa-mãe com ruído elétrico (não é configuração)",
        pre_db0=-95, mid_db0=-98, post_db=-52, wiring="direto", start=Settings(20.0, 2.0)),
    "sinal_fraco": Scenario(
        "sinal_fraco", "Mesa mandando sinal fraco: o ruído da entrada fica perto da voz",
        speech_db0=-64.0, pre_db0=-82.0, mid_db0=-98, post_db=-95, hum_db=-84, hum_pre_gain=True, wiring="mesa",
        isolated_pre_drop_db=0.0,   # como no teste real: silenciar a mesa NÃO tira o ruído da entrada
        start=Settings(30.0, 6.0)),
    "zumbido": Scenario(
        "zumbido", "Zumbido de 60 Hz (loop de terra)", pre_db0=-95, mid_db0=-98, post_db=-72,
        hum_db=-50, start=Settings(20.0, 2.0)),
    "efeitos_windows": Scenario(
        "efeitos_windows", "Efeitos de áudio do Windows/driver somando chiado", pre_db0=-95, mid_db0=-98,
        post_db=-80, effects_noise_db=-52, agc_ramp_db=9.0, start=Settings(20.0, 2.0)),
    "ambiente": Scenario(
        "ambiente", "Ruído do ambiente/mesa entrando no microfone", pre_db0=-70, mid_db0=-98, post_db=-80,
        start=Settings(20.0, 2.0)),
    "apo_ativo": Scenario(
        "apo_ativo", "Entrada ruidosa com o Equalizer APO filtrando em tempo real", pre_db0=-95, mid_db0=-98,
        post_db=-55, hum_db=-50, realtime_clean_db=18.0, start=Settings(20.0, 2.0)),
    "bluetooth": Scenario(
        "bluetooth", "Microfone Bluetooth (banda estreita)", pre_db0=-80, mid_db0=-90, post_db=-60,
        cutoff_hz=3800.0, connection="bluetooth", chip="", start=Settings(0.0, 0.0)),
}


class SimBackend:
    """Guarda o estado (ganho) e fabrica o gravador e o controle simulados."""

    def __init__(self, scenario: Scenario, speed: float = 8.0, seed: int = 100):
        self.scenario = scenario
        self.speed = speed
        self.state = Settings(scenario.start.boost_db, scenario.start.volume_db, False)
        self.seed = seed
        self.device = InputDevice(name=f"[SIMULADO] {scenario.title}", index=-1,
                                  samplerate=FS, channels=1, is_default=True)
        self.recorder = SimRecorder(self, exclusive=False)
        self.control = SimControl(self)

    def next_seed(self) -> int:
        self.seed += 1
        return self.seed

    def make_session(self, name: str, out_dir, allow_changes: bool = True, open_report: bool = True):
        """Sessão pronta para o assistente, com a ligação do microfone que o cenário descreve."""
        from pathlib import Path
        from . import wizard
        from .findings import DeviceInfo
        sc = self.scenario
        info = DeviceInfo(name=self.device.name, connection=sc.connection, chip=sc.chip,
                          samplerate=self.device.samplerate, simulated=True)
        sess = wizard.Session(name=name, recorder=self.recorder, control=self.control, info=info,
                              out_dir=Path(out_dir), allow_changes=allow_changes, open_report=open_report)
        if sc.wiring:
            sess.wiring, sess.wiring_known = sc.wiring, True
        return sess


class SimControl:
    has_boost = True
    has_gain_control = True

    def __init__(self, backend: SimBackend):
        self.b = backend

    def boost_options(self) -> List[float]:
        return boost_options_from_range(0.0, 30.0, 10.0)

    def volume_range(self) -> Tuple[float, float, float]:
        return -34.5, 12.0, 1.5

    def get_settings(self) -> Settings:
        s = self.b.state
        return Settings(s.boost_db, s.volume_db, s.muted)

    def apply(self, s: Settings) -> Settings:
        lo, hi, st = self.volume_range()
        vol = min(max(s.volume_db, lo), hi)
        vol = lo + round((vol - lo) / st) * st
        boost = s.boost_db
        if boost is not None:
            boost = min(max(round(boost / 10.0) * 10.0, 0.0), 30.0)
        self.b.state = Settings(boost, vol, s.muted)
        return self.get_settings()


class SimRecorder:
    simulated = True

    def __init__(self, backend: SimBackend, exclusive: bool):
        self.b = backend
        self.exclusive = exclusive
        self.device = backend.device
        self.samplerate = FS
        self.channels = 1

    def with_exclusive(self, exclusive: bool) -> "SimRecorder":
        return SimRecorder(self.b, exclusive)

    def exclusive_available(self) -> bool:
        return True

    # -- geração -------------------------------------------------------------
    def _noise(self, seconds: float, isolated: bool) -> np.ndarray:
        sc, st = self.b.scenario, self.b.state
        seed = self.b.next_seed()
        g_total = st.total_gain_db
        boost = st.boost_db or 0.0
        pre = sc.pre_db0 + g_total - (sc.isolated_pre_drop_db if isolated else 0.0)
        terms = [10 ** (pre / 10), 10 ** ((sc.mid_db0 + st.volume_db) / 10), 10 ** (sc.post_db / 10),
                 10 ** ((sc.boost_db0 + boost) / 10)]
        shared = not self.exclusive
        if shared and sc.effects_noise_db is not None:
            terms.append(10 ** (sc.effects_noise_db / 10))
        level = 10 * np.log10(sum(terms))
        x = synth.white(seconds, FS, level, seed)
        if shared and sc.agc_ramp_db:
            x = x * 10 ** (np.linspace(0, sc.agc_ramp_db, len(x)) / 20.0)
        if sc.hum_db is not None:
            hum_level = sc.hum_db + (g_total if sc.hum_pre_gain else 0.0)
            x = x + synth.hum(seconds, FS, hum_level, sc.hum_f0, seed=seed)
        if sc.tone_hz is not None:
            x = x + synth.tone(seconds, FS, sc.tone_hz, sc.tone_db)
        if shared and sc.realtime_clean_db:
            x = x * 10 ** (-sc.realtime_clean_db / 20.0)
        return x

    def _render(self, seconds: float, kind: str) -> np.ndarray:
        sc, st = self.b.scenario, self.b.state
        isolated = kind == "isolated"
        x = self._noise(seconds, isolated)
        if kind == "speech":
            x = x + synth.speech_like(seconds, FS, sc.speech_db0 + st.total_gain_db, seed=self.b.next_seed())
        if sc.cutoff_hz:
            x = synth.lowpass(x, FS, sc.cutoff_hz)
        if st.muted:
            x = x * 0.0
        return np.clip(x, -1.0, 1.0).astype(np.float32)

    def record(self, seconds: float, kind: str = "silence", on_tick: Optional[TickFn] = None,
               stop: Optional[Callable[[], bool]] = None) -> np.ndarray:
        x = self._render(seconds, "silence" if kind == "live" else kind)
        step = int(0.1 * FS)
        if on_tick or self.b.speed < 1e6:
            for i in range(0, len(x), step):
                if stop is not None and stop():
                    x = x[: i + step]
                    break
                block = x[i:i + step]
                if on_tick and len(block):
                    pk = 20 * np.log10(max(float(np.max(np.abs(block))), 1e-9))
                    rm = 20 * np.log10(max(float(np.sqrt(np.mean(block ** 2))), 1e-9))
                    on_tick(i / FS, pk, rm)
                if self.b.speed < 1e6:
                    time.sleep(0.1 / self.b.speed)
        return x.reshape(-1, 1)
