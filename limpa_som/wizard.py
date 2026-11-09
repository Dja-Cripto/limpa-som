"""Assistente: grava, mede, ajusta o ganho, testa a causa do chiado, limpa e dá o veredito."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np
import soundfile as sf

from . import design, dsp, eq, fileclean, metrics, realtime, tuning, ui, winaudio
from .devices import RecorderError, best_channel, channel_report, pick_channel
from .findings import BUCKET_TITLES, Context, DeviceInfo, Verdict, build_verdict, num
from .metrics import Measurement, NoiseReport, analyze_noise
from .plain import WIRING_NAMES, WIRING_OPTIONS
from .report import ReportData, build_html
from .scoring import Score, cap_digital, compute_score
from .tuning import Settings

READING_TEXT = (
    "O investidor paciente sabe que os juros compostos trabalham a favor de quem começa cedo. "
    "Todo mês, uma pequena parcela guardada com disciplina vira, com o tempo, um patrimônio sólido. "
    "Falar com clareza, sem pressa, ajuda o microfone a captar cada sílaba: s, ch, p, t, f."
)
SILENCE_S = 7.0
SPEECH_S = 14.0
EXCLUSIVE_S = 5.0
ISOLATION_S = 6.0


@dataclass
class Session:
    name: str
    recorder: object
    control: Optional[object]
    info: DeviceInfo
    out_dir: Path
    allow_changes: bool = True
    open_report: bool = True
    exclusive_ok: Optional[bool] = None
    last: Optional[Context] = None
    last_speech: Optional[np.ndarray] = None
    last_channel: int = 0
    wiring: Optional[str] = None        # mesa | direto | interface | usb_mic | headset
    wiring_known: bool = False
    alternatives: List[str] = field(default_factory=list)   # outros microfones USB (candidatos à mesa/interface)
    apo_info: Optional[object] = None   # Equalizer APO já localizado (testes passam uma pasta descartável)
    look_for_apo: bool = False          # só sessões de microfone real procuram o APO de verdade no computador

    @property
    def gain_control(self) -> bool:
        return self.control is not None and bool(getattr(self.control, "has_gain_control", True))

    @property
    def fs(self) -> int:
        return int(self.recorder.samplerate)


Abort = ui.Abort


# ------------------------------------------------------------------ utilitários ---
def session_for_device(dev, name: str, out_dir: Path, allow_changes: bool = True,
                       open_report: bool = True) -> Session:
    """Sessão de um microfone real: gravador + controle de ganho do Windows (se houver)."""
    from .devices import Recorder
    control = winaudio.find_control(dev.name)
    info = DeviceInfo(name=dev.name, connection=control.connection if control else "desconhecida",
                      chip=control.chip if control else "", samplerate=dev.samplerate)
    return Session(name=name, recorder=Recorder(dev), control=control, info=info, out_dir=out_dir,
                   allow_changes=allow_changes, open_report=open_report,
                   alternatives=usb_alternatives(dev.name), look_for_apo=True)


def usb_alternatives(current: str) -> List[str]:
    """Outros microfones USB ativos (menos webcams): podem ser a mesa ou a interface, por um caminho digital."""
    try:
        from .devices import list_input_devices
        names = []
        for d in list_input_devices():
            if d.name == current or re.search(r"webcam|camera|câmera", d.name, re.I):
                continue
            c = winaudio.find_control(d.name)
            if c is not None and c.connection == "usb":
                names.append(d.name)
        return names
    except Exception:
        return []


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:40] or "microfone"


def _score_style(v: float) -> str:
    return "green" if v >= 8.0 else "yellow" if v >= 6.0 else "red"


def save_wav(path: Path, x: np.ndarray, fs: int) -> None:
    sf.write(str(path), np.clip(np.asarray(x, dtype=np.float64), -1.0, 1.0), fs, subtype="PCM_16")


def record(sess: Session, kind: str, seconds: float, label: str, countdown: int = 3) -> np.ndarray:
    """Grava com contagem regressiva e barra de nível; refaz se o microfone falhar."""
    for attempt in range(3):
        try:
            if countdown:
                ui.countdown(countdown)
            tick = ui.RecordProgress(label, seconds)
            data = sess.recorder.record(seconds, kind=kind, on_tick=tick)
            tick.finish()
            if float(np.max(np.abs(data))) < 1e-7:
                raise RecorderError(
                    "O microfone gravou silêncio absoluto (só zeros).",
                    "Ele pode estar no mudo, desligado ou bloqueado. Confira Configurações > Privacidade > "
                    "Microfone (apps de desktop) e a chave de mudo do microfone.")
            return data
        except RecorderError as exc:
            ui.bad(str(exc))
            if exc.hint:
                ui.para(exc.hint)
            if attempt == 2 or not ui.ask_yes_no("Tentar gravar de novo?"):
                raise Abort()
    raise Abort()


def analyze_takes(sess: Session, silence: np.ndarray, speech: np.ndarray, channel: Optional[int] = None):
    ch = best_channel(speech) if channel is None else channel
    m = metrics.measure(pick_channel(silence, ch), pick_channel(speech, ch), sess.fs)
    return m, ch


def describe_measurement(m: Measurement, sc: Score) -> None:
    ui.say(f"  Nota: {sc.text} / 10 - {sc.label}", "bold", _score_style(sc.value))
    ui.info(f"Piso de ruído: {num(m.noise.rms_a_db)} dBFS(A)")
    if m.speech:
        ui.info(f"Voz: média {num(m.speech.level_a_db)} dBFS(A), pico {num(m.speech.peak_db)} dBFS")
        ui.info(f"Relação sinal/ruído: {num(m.snr_db)} dB (para soar limpo: acima de 48 dB)")
    for p in sc.penalties:
        ui.info(f"Desconto: {p.label} (-{num(p.points)})")


def _backup_files(sess: Session):
    base = sess.out_dir / f"backup_{_safe(sess.info.name)}"
    return base.with_name(base.name + "_original.json"), base.with_name(base.name + "_ultimo.json")


def save_backup(sess: Session, s: Settings) -> None:
    original, last = _backup_files(sess)
    data = {"dispositivo": sess.info.name, "quando": datetime.now().isoformat(timespec="seconds"),
            "reforco_db": s.boost_db, "volume_db": s.volume_db, "mudo": s.muted}
    sess.out_dir.mkdir(parents=True, exist_ok=True)
    if not original.exists():
        original.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    last.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_backup(path: Path) -> Optional[Settings]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return Settings(d.get("reforco_db"), float(d["volume_db"]), bool(d.get("mudo", False)))
    except Exception:
        return None


def _history_path(sess: Session) -> Path:
    return sess.out_dir / "historico.json"


def load_history(sess: Session) -> List[dict]:
    try:
        items = json.loads(_history_path(sess).read_text(encoding="utf-8"))
        return [h for h in items if h.get("dispositivo") == sess.info.name]
    except Exception:
        return []


def append_history(sess: Session, ctx: Context, verdict: Verdict) -> None:
    try:
        items = json.loads(_history_path(sess).read_text(encoding="utf-8"))
    except Exception:
        items = []
    s = ctx.settings_after or ctx.settings_before
    items.append({
        "dispositivo": sess.info.name,
        "quando": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "antes": verdict.score_before.text, "depois": verdict.score_after.text,
        "config": s.describe() if s else "",
    })
    _history_path(sess).write_text(json.dumps(items[-50:], indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------- etapas ---
def _prefs_path(sess: Session) -> Path:
    return sess.out_dir / "preferencias.json"


def load_prefs(sess: Session) -> dict:
    try:
        return json.loads(_prefs_path(sess).read_text(encoding="utf-8")).get(sess.info.name, {})
    except Exception:
        return {}


def save_pref(sess: Session, key: str, value) -> None:
    try:
        data = json.loads(_prefs_path(sess).read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data.setdefault(sess.info.name, {})[key] = value
    try:
        sess.out_dir.mkdir(parents=True, exist_ok=True)
        _prefs_path(sess).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def ask_wiring(sess: Session, force: bool = False) -> None:
    """Pergunta como o microfone está ligado (muda o diagnóstico) e lembra a resposta."""
    if sess.wiring_known and not force:
        return
    conn = sess.info.connection
    if not force and conn in ("usb", "bluetooth"):
        sess.wiring, sess.wiring_known = ("usb_mic" if conn == "usb" else "headset"), True
        return
    saved = load_prefs(sess).get("ligacao")
    if not force and saved in WIRING_NAMES:
        sess.wiring, sess.wiring_known = saved, True
        return
    ui.phase("wiring")
    ui.guide("wiring", "Como o seu microfone está ligado?",
             "Isso muda o diagnóstico: o conselho para quem usa mesa de som é bem diferente do de quem liga o "
             "microfone direto no computador. Você pode mudar essa resposta depois.")
    labels = [label for _, label in WIRING_OPTIONS]
    i = ui.ask_choice("Como o seu microfone está ligado ao computador?", labels, default=len(labels),
                      eof=len(labels) - 1)
    key = WIRING_OPTIONS[i][0]
    sess.wiring, sess.wiring_known = key, True
    if key:
        save_pref(sess, "ligacao", key)


def record_pair(sess: Session, tag: str, step_no: str, phase: str = "silence") -> tuple:
    note = f"Gravação {step_no} · {tag}"
    ui.phase("silence" if phase == "silence" else "check")
    ui.guide("silence", "Fique em silêncio",
             "Não fale e não mexe no mouse nem no teclado. Vou ouvir o ruído do seu microfone por "
             f"{SILENCE_S:.0f} segundos.".replace("não mexe", "não mexa"), note)
    ui.ask_enter("Aperte ENTER quando estiver em silêncio...", button="Estou em silêncio, pode começar")
    silence = record(sess, "silence", SILENCE_S, "silêncio")
    ui.phase("speech" if phase == "silence" else "check")
    ui.guide("speech", "Agora fale", "Leia o texto abaixo em voz normal, na distância em que você costuma falar.",
             note)
    ui.reading(READING_TEXT)
    ui.ask_enter("Aperte ENTER e comece a ler quando a gravação iniciar...", button="Pronto, vou ler")
    speech = record(sess, "speech", SPEECH_S, "sua voz")
    return silence, speech


def ensure_voice(sess: Session, silence, speech):
    """Garante que a gravação de fala tem voz suficiente para medir."""
    m, ch = analyze_takes(sess, silence, speech)
    if m.speech and m.speech.active_frac < 0.15:
        ui.warn("Quase não ouvi sua voz nessa gravação.")
        if ui.ask_yes_no("Gravar a fala de novo?"):
            ui.reading(READING_TEXT)
            ui.ask_enter("Leia o texto de novo (mais perto, voz normal). ENTER para começar...",
                         button="Pronto, vou ler de novo")
            speech = record(sess, "speech", SPEECH_S, "sua voz")
            m, ch = analyze_takes(sess, silence, speech)
    return m, ch, speech


def run_sweep(sess: Session, m1: Measurement, ch: int, settings0: Settings):
    ctrl = sess.control
    n_tests = len(ctrl.boost_options()) if ctrl.has_boost else 1
    secs = (tuning.SETTLE_S + tuning.SWEEP_S) * max(1, n_tests)
    ui.phase("adjust")
    if ctrl.has_boost:
        text = (f"O ganho do microfone tem dois botões no Windows: o 'Aumento de sensibilidade' e o volume. Vou "
                f"testar {n_tests} combinações para ver se alguma chia menos. Você fica em silêncio por cerca de "
                f"{secs:.0f} segundos. Guardo as configurações originais e volto a elas se não adiantar.")
    else:
        text = (f"Vou ajustar o volume de entrada do Windows para a sua voz ficar no nível ideal e medir o ruído "
                f"nesse ponto. Você fica em silêncio por cerca de {secs:.0f} segundos.")
    ui.guide("adjust", "Teste de ganho", text)
    if not ui.ask_yes_no("Posso testar essas combinações no seu microfone? (guardo as originais para desfazer)"):
        return None
    ui.ask_enter("Fique em silêncio. ENTER para começar...", button="Estou em silêncio, pode começar")
    ui.countdown(3)
    target = tuning.target_gain(settings0, m1.speech)

    def record_noise(settle: float, measure: float) -> NoiseReport:
        tick = ui.RecordProgress("medindo o ruído", settle + measure)
        data = sess.recorder.record(settle + measure, kind="silence", on_tick=tick)
        tick.finish()
        x = pick_channel(data, ch)
        return analyze_noise(x[int(settle * sess.fs):], sess.fs)

    def progress(i: int, n: int, s: Settings) -> None:
        ui.say(f"  Teste {i}/{n}: {s.describe()}", "gray")

    try:
        points = tuning.sweep(ctrl, record_noise, settings0, m1.speech, target, progress)
    except Exception:
        ctrl.apply(settings0)
        raise
    chosen = tuning.choose(points, settings0)
    applied = ctrl.apply(chosen.settings)
    chosen.settings = applied
    ui.say()
    ui.say("  Reforço     Volume     Ruído medido     Sinal/ruído previsto     Pico previsto", "bold", "mono")
    for p in points:
        boost = "     -   " if p.settings.boost_db is None else f"{p.settings.boost_db:+6.0f} dB"
        mark = "  <-- escolhido" if p is chosen else ""
        if p.clamped:
            mark += "  (volume no limite: não chega ao nível ideal)"
        ui.say(f"  {boost}  {p.settings.volume_db:+7.1f} dB   {p.noise_a_db:7.1f} dBFS(A)   "
               f"{p.snr_pred_db:12.1f} dB   {p.peak_pred_db:12.1f} dBFS{mark}",
               "mono", *(("green",) if p is chosen else ()))
    return points, chosen, applied


def _changed(a: Settings, b: Settings) -> bool:
    return (abs((a.boost_db or 0) - (b.boost_db or 0)) > 0.4 or abs(a.volume_db - b.volume_db) > 0.75
            or a.muted != b.muted)


def cause_tests(sess: Session, ch: int):
    """Mede o ruído sem efeitos do Windows (modo exclusivo) e com a fonte silenciada."""
    exclusive: Optional[NoiseReport] = None
    isolation: Optional[NoiseReport] = None
    ui.phase("cause")

    if sess.exclusive_ok is not False:
        ui.guide("silence", "Fique em silêncio mais um pouco",
                 "Teste A: vou ouvir o microfone de outro jeito (o 'modo exclusivo', que ignora os efeitos do "
                 f"Windows) por {EXCLUSIVE_S:.0f} segundos. Se o chiado diminuir, os efeitos são o culpado.",
                 "Teste A de 2")
        ui.ask_enter("Continue em silêncio. ENTER para começar...", button="Estou em silêncio, pode começar")
        try:
            ui.countdown(2)
            rec_ex = sess.recorder.with_exclusive(True)
            tick = ui.RecordProgress("modo exclusivo", EXCLUSIVE_S)
            data = rec_ex.record(EXCLUSIVE_S + 0.6, kind="silence", on_tick=tick)
            tick.finish()
            if float(np.max(np.abs(data))) > 1e-7:
                exclusive = analyze_noise(pick_channel(data, ch), sess.fs)
                sess.exclusive_ok = True
            else:
                sess.exclusive_ok = False
        except RecorderError:
            sess.exclusive_ok = False
        if exclusive is None:
            ui.info("Não consegui usar o modo exclusivo neste microfone (normal em alguns drivers); pulei o teste A.")

    ui.guide("isolate", "Agora silencie a fonte do som",
             "Teste B: descobre se o chiado vem de ANTES do computador (microfone, mesa, cabo, sala) ou da "
             "entrada do PC. Se o microfone passa por uma mesa ou interface: ZERE o ganho do canal do microfone, "
             "abaixe o fader e ligue o MUTE do canal, deixando o cabo ligado ao PC. Se o microfone liga direto no "
             "computador: cubra-o com uma almofada ou coloque-o numa gaveta ou cobertor.", "Teste B de 2")
    i = ui.ask_choice("O teste B é o que aponta a causa com mais certeza (leva 1 minuto).",
                      ["Já silenciei a fonte, pode gravar", "Pular este teste"], default=1, eof=1)
    if i == 0:
        data = record(sess, "isolated", ISOLATION_S + 0.6, "fonte silenciada", countdown=3)
        isolation = analyze_noise(pick_channel(data, ch), sess.fs)
        ui.ok("Teste B concluído. Pode religar o canal da mesa ou descobrir o microfone.")
    return exclusive, isolation


def digital_cleaning(sess: Session, silence: np.ndarray, speech: np.ndarray, ch: int, m: Measurement,
                     run_dir: Path):
    """Aplica a limpeza digital e mede o resultado (silêncio dividido ao meio: perfil x teste)."""
    fs = sess.fs
    s = pick_channel(silence, ch)
    v = pick_channel(speech, ch)
    s = s[int(metrics.TRIM_START_S * fs):]
    half = len(s) // 2
    notch = dsp.hum_notches(m.noise.hum.f0, m.noise.hum.harmonics) if m.noise.hum else []
    settings = dsp.CleanSettings(notch_hz=notch, normalize=False)
    profile_half = dsp.profile_from_noise(s[:half], fs, settings.nfft)
    speech_clean = dsp.denoise_chain(v, fs, profile_half, settings)
    noise_clean = dsp.denoise_chain(
        np.concatenate([np.zeros(int(metrics.TRIM_START_S * fs)), s[half:]]), fs, profile_half, settings)
    m_clean = metrics.measure(noise_clean, speech_clean, fs)
    score_clean = cap_digital(compute_score(m_clean), compute_score(m))

    # arquivo final: perfil com o silêncio inteiro + nivelamento
    profile = dsp.profile_from_noise(s, fs)
    final = dsp.clean(v, fs, profile, dsp.CleanSettings(notch_hz=notch))
    save_wav(run_dir / "3_limpo.wav", final, fs)
    dsp.save_profile(str(sess.out_dir / "perfil_ruido.npz"), m.noise.raw_freqs, m.noise.raw_psd, fs)
    return m_clean, score_clean


def log_error(out_dir: Path, exc: BaseException) -> Path:
    """Guarda o erro técnico num arquivo para eu poder consertar depois."""
    import traceback
    path = out_dir / "erro.log"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} =====\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except OSError:
        pass
    return path


# --------------------------------------------------------------- apresentação ---
def _score_items(ctx: Context, verdict: Verdict, score_clean: Optional[Score]) -> list:
    def item(label: str, sc: Score, caption: str) -> dict:
        return {"label": label, "value": sc.value, "text": sc.text, "caption": caption or sc.label}

    items = []
    if ctx.after is not None and not ctx.reverted:
        items.append(item("Antes", verdict.score_before, verdict.score_before.label))
        items.append(item("Depois do ajuste", verdict.score_after, verdict.score_after.label))
    else:
        items.append(item("Seu áudio agora", verdict.score_after, verdict.score_after.label))
    if score_clean is not None:
        items.append(item("Com limpeza digital", score_clean, "estimativa, só no arquivo limpo"))
    return items


def print_verdict(ctx: Context, verdict: Verdict, score_clean: Optional[Score]) -> None:
    ui.title("RESULTADO")
    ui.scores(_score_items(ctx, verdict, score_clean))
    ui.say()
    if not getattr(ui.backend, "has_tiles", False):
        if ctx.after is not None and not ctx.reverted:
            ui.say(f"  Nota ANTES:             {verdict.score_before.text} / 10  - {verdict.score_before.label}",
                   _score_style(verdict.score_before.value))
            ui.say(f"  Nota DEPOIS (config):   {verdict.score_after.text} / 10  - {verdict.score_after.label}",
                   "bold", _score_style(verdict.score_after.value))
        else:
            ui.say(f"  Nota do seu áudio:      {verdict.score_after.text} / 10  - {verdict.score_after.label}",
                   "bold", _score_style(verdict.score_after.value))
        if score_clean is not None:
            ui.say(f"  Com limpeza digital:    ~{score_clean.text} / 10  (estimativa, só no arquivo gerado; "
                   "o microfone não muda)", _score_style(score_clean.value))
        ui.say()
    if verdict.plain is not None:
        ui.plain(verdict.plain)
        ui.say()
        ui.say("  DETALHES TÉCNICOS (para quem quer ver os números)", "bold", "gray")
    ui.para(verdict.headline, 2, "bold")
    for bucket in ("corrigido", "config", "hardware", "ambiente", "ok"):
        items = verdict.bucket(bucket)
        if not items:
            continue
        style = {"corrigido": "green", "config": "yellow", "hardware": "red", "ambiente": "cyan", "ok": "green"}[bucket]
        ui.say()
        ui.say(f"  {BUCKET_TITLES[bucket]}", "bold", style)
        for f in items:
            ui.para(f"- {f.title}", 4, "bold")
            if f.detail:
                ui.para(f.detail, 6)
            for a in f.actions:
                ui.para(f"-> {a}", 6, style)


def open_file(path: Path) -> None:
    try:
        if hasattr(os, "startfile"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            import webbrowser
            webbrowser.open(path.as_uri())
    except Exception:
        ui.info(f"Abra o relatório manualmente: {path}")


# ----------------------------------------------------------- fluxo principal ---
@dataclass
class Phase:
    """Resultado das gravações e do ajuste, antes dos testes de causa."""
    ctx: Context
    silence: np.ndarray
    speech: np.ndarray
    channel: int
    chosen: Optional[tuning.SweepPoint] = None
    files: dict = field(default_factory=dict)


def _first_run(sess: Session, settings0: Optional[Settings], run_dir: Path) -> Phase:
    ctrl = sess.control
    if ctrl and sess.allow_changes and settings0:
        save_backup(sess, settings0)
    silence1, speech1 = record_pair(sess, "como está agora", "1 de 2")
    m1, ch, speech1 = ensure_voice(sess, silence1, speech1)
    files = {"antes": "1_antes.wav"}
    save_wav(run_dir / "1_antes.wav", pick_channel(speech1, ch), sess.fs)
    ui.step("Primeira medição")
    describe_measurement(m1, compute_score(m1))

    sweep_points: list = []
    chosen = None
    after: Optional[Measurement] = None
    settings_after: Optional[Settings] = None
    reverted = False
    revert_reason = ""
    silence_f, speech_f = silence1, speech1
    uncommitted = False
    try:
        if ctrl and sess.gain_control and sess.allow_changes and settings0 and m1.speech:
            uncommitted = True  # a partir daqui o ganho pode mudar; qualquer erro restaura o original
            result = run_sweep(sess, m1, ch, settings0)
            if result is None:
                uncommitted = False
            else:
                sweep_points, chosen, applied = result
                sp = m1.speech
                # mudar o ganho do computador só vale se melhorar a relação voz/ruído ou consertar o nível
                needs_level_fix = sp.clip_runs > 0 or sp.peak_db > -3.0 or sp.peak_db < -24.0
                predicted_gain = chosen.snr_pred_db - (m1.snr_db or 0.0)
                if not _changed(settings0, applied):
                    ui.ok("O ganho do computador já estava no ponto certo: nada a mudar.")
                elif predicted_gain < 1.5 and not needs_level_fix:
                    ctrl.apply(settings0)
                    reverted, revert_reason = True, "sem_ganho"
                    ui.warn("Mexer no ganho do computador não melhora a relação voz/ruído (nas combinações que "
                            "testei ela quase não muda). Deixei como estava.")
                else:
                    ui.ok(f"Ganho ajustado para: {applied.describe()}")
                    silence2, speech2 = record_pair(sess, "com o ganho ajustado", "2 de 2", phase="check")
                    m2, _ = analyze_takes(sess, silence2, speech2, ch)
                    describe_measurement(m2, compute_score(m2))
                    s1, s2 = compute_score(m1), compute_score(m2)
                    fixed_clip = sp.clip_runs > 0 and m2.speech is not None and m2.speech.clip_runs == 0
                    snr_gain = (m2.snr_db or 0.0) - (m1.snr_db or 0.0)
                    if snr_gain >= 1.5 or fixed_clip or s2.value >= s1.value + 0.3:
                        after, settings_after = m2, applied
                        silence_f, speech_f = silence2, speech2
                        files["depois"] = "2_depois.wav"
                        save_wav(run_dir / "2_depois.wav", pick_channel(speech2, ch), sess.fs)
                    else:
                        ctrl.apply(settings0)
                        reverted = True
                        revert_reason = "pior" if s2.value < s1.value - 0.3 else "sem_ganho"
                        ui.warn(f"O ajuste não melhorou (relação voz/ruído de {m1.snr_db:.0f} para "
                                f"{m2.snr_db or 0.0:.0f} dB). Voltei ao ganho original.")
                uncommitted = False
    except BaseException:
        if uncommitted and ctrl and settings0:
            try:
                ctrl.apply(settings0)
                ui.warn("Interrompido no meio do ajuste: voltei o microfone para a configuração original.")
            except Exception:
                ui.bad("Não consegui voltar o microfone ao ajuste original: use 'Restaurar as configurações'.")
        raise
    ctx = Context(name=sess.name, device=sess.info, before=m1, after=after, settings_before=settings0,
                  settings_after=settings_after, sweep=sweep_points, has_control=ctrl is not None,
                  reverted=reverted, revert_reason=revert_reason, wiring=sess.wiring,
                  gain_control=sess.gain_control, alternatives=sess.alternatives,
                  channel_note=channel_report(speech1))
    return Phase(ctx, silence_f, speech_f, ch, chosen, files)


def _retest_run(sess: Session, previous: Context, run_dir: Path) -> Phase:
    ctrl = sess.control
    silence2, speech2 = record_pair(sess, "depois dos seus ajustes", "1 de 1")
    m2, ch, speech2 = ensure_voice(sess, silence2, speech2)
    files = {"depois": "2_depois.wav"}
    if sess.last_speech is not None:
        files["antes"] = "1_antes.wav"
        save_wav(run_dir / "1_antes.wav", pick_channel(sess.last_speech, sess.last_channel), sess.fs)
    save_wav(run_dir / "2_depois.wav", pick_channel(speech2, ch), sess.fs)
    ui.step("Medição")
    describe_measurement(m2, compute_score(m2))
    ctx = Context(name=sess.name, device=sess.info, before=previous.final, after=m2,
                  settings_before=previous.settings_after or previous.settings_before,
                  settings_after=ctrl.get_settings() if ctrl else None,
                  has_control=ctrl is not None, wiring=sess.wiring, gain_control=sess.gain_control,
                  alternatives=sess.alternatives, channel_note=channel_report(speech2))
    return Phase(ctx, silence2, speech2, ch, None, files)


def _intro(sess: Session, retest: bool, settings0: Optional[Settings]) -> None:
    ui.title("TESTE DE NOVO (depois dos seus ajustes)" if retest else "TESTE DO MICROFONE")
    ui.say(f"  Microfone: {sess.info.name}", "bold")
    ui.say(f"  Ligação:   {sess.info.connection_text}")
    if settings0 and sess.gain_control:
        ui.say(f"  Ganho atual: {settings0.describe()}" + ("  (EM MUDO)" if settings0.muted else ""))
    elif settings0:
        ui.say("  O ganho deste microfone é ajustado no próprio equipamento, não no Windows.")
    ui.say()
    if not retest:
        ui.guide("intro", "Vamos testar o seu microfone",
                 "Leva uns 5 minutos. Feche Discord, OBS, Zoom e Teams e fique num lugar o mais silencioso "
                 "possível. Eu vou: ouvir o ruído, ouvir a sua voz, testar o ganho, descobrir de onde vem o "
                 "chiado e gerar uma versão limpa com um relatório.")
        ui.ask_enter(button="Começar")


def full_test(sess: Session, previous: Optional[Context] = None) -> Optional[Context]:
    restore = suspend_equalizer(sess)
    state = {"applied": False}
    try:
        return _full_test(sess, previous, state)
    finally:
        if restore is not None and not state["applied"]:
            restore()


def _full_test(sess: Session, previous: Optional[Context], state: dict) -> Optional[Context]:
    retest = previous is not None
    ctrl = sess.control
    settings0: Optional[Settings] = ctrl.get_settings() if ctrl else None
    _intro(sess, retest, settings0)
    try:
        if not retest:
            ask_wiring(sess)
        if ctrl and settings0 and settings0.muted:
            ui.warn("O microfone está no MUDO no Windows. Vou tirar do mudo para poder testar.")
            if sess.allow_changes:
                save_backup(sess, settings0)
                ctrl.apply(Settings(settings0.boost_db, settings0.volume_db, False))
                settings0 = ctrl.get_settings()
        run_dir = sess.out_dir / f"teste_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        run_dir.mkdir(parents=True, exist_ok=True)

        phase = _retest_run(sess, previous, run_dir) if retest else _first_run(sess, settings0, run_dir)
        ctx = phase.ctx
        ctx.exclusive, ctx.isolation = cause_tests(sess, phase.channel)
        verdict = build_verdict(ctx)

        ui.phase("result")
        ui.working("Analisando tudo e gerando a versão com limpeza digital...")
        m_clean, score_clean = digital_cleaning(sess, phase.silence, phase.speech, phase.channel, ctx.final, run_dir)
        ui.working(None)
        phase.files["limpo"] = "3_limpo.wav"
        ui.ok(f"Áudio limpo salvo em: {run_dir / '3_limpo.wav'}")

        print_verdict(ctx, verdict, score_clean)
        data = ReportData(name=sess.name, when=datetime.now(), ctx=ctx, verdict=verdict, score_clean=score_clean,
                          m_clean=m_clean, chosen=phase.chosen, files=phase.files, history=load_history(sess))
        report_path = run_dir / "relatorio.html"
        report_path.write_text(build_html(data), encoding="utf-8")
        append_history(sess, ctx, verdict)
        ui.say()
        ui.ok(f"Relatório completo (com gráficos e áudios para ouvir): {report_path}")
        ui.report_ready(report_path)
        if sess.open_report:
            open_file(report_path)
        sess.last, sess.last_speech, sess.last_channel = ctx, phase.speech, phase.channel
        try:
            state["applied"] = equalizer_step(sess, ctx.final.noise, ctx.final.speech,
                                              pick_channel(phase.speech, phase.channel))
        except (Abort, RecorderError):
            raise
        except Exception as exc:          # o equalizador é um extra: se falhar, o teste continua valendo
            log_error(sess.out_dir, exc)
            ui.warn("Não consegui criar o equalizador agora (detalhes em erro.log). O resto do teste está pronto.")
        return ctx
    except (Abort, RecorderError) as exc:
        if isinstance(exc, RecorderError):
            ui.bad(str(exc))
        ui.warn("Teste cancelado.")
        return None
    except Exception as exc:  # erro nosso: registra, avisa com clareza e segue vivo
        log = log_error(sess.out_dir, exc)
        ui.bad(f"Deu um erro inesperado no teste ({type(exc).__name__}: {exc}).")
        ui.para(f"Se o ganho do microfone chegou a ser mudado, o programa já voltou ao original. Detalhes técnicos "
                f"em {log}.")
        return None


def next_steps(sess: Session, ctx: Context) -> str:
    while True:
        options = ["Já fiz os ajustes que faltavam: testar de novo",
                   "Abrir o painel de Som do Windows (aba Gravação)",
                   "Desfazer as mudanças que o programa fez no microfone",
                   "Voltar ao menu principal"]
        ui.say()
        i = ui.ask_choice("O que você quer fazer agora?", options, default=4)
        if i == 0:
            return "retest"
        if i == 1:
            if winaudio.AVAILABLE:
                winaudio.open_recording_panel()
            else:
                ui.warn("Só funciona no Windows.")
        elif i == 2:
            restore_flow(sess)
        else:
            return "menu"


def live_kind(hold_db: float) -> str:
    if hold_db > -2.0:
        return "clip"
    if hold_db > -12.0:
        return "great"
    if hold_db > -24.0:
        return "low"
    return "verylow"


def live_meter(sess: Session) -> None:
    ui.title("AJUSTE AO VIVO")
    ui.para("Fale normalmente e ajuste, ao vivo, o ganho da sua mesa/interface (ou o volume do microfone) até os "
            "PICOS da voz ficarem entre -12 e -6 dB (zona verde/amarela, sem chegar no vermelho). "
            "Ruído de fundo deve ficar o mais baixo possível. Para sair, aperte qualquer tecla (ou o botão Parar).")
    ui.ask_enter()
    hold = {"v": -120.0, "t": 0.0}

    def tick(elapsed: float, peak: float, rms: float) -> None:
        if peak > hold["v"] or elapsed - hold["t"] > 2.0:
            hold["v"], hold["t"] = peak, elapsed
        ui.live(peak, hold["v"], live_kind(hold["v"]))

    try:
        while True:
            sess.recorder.record(60.0, kind="live", on_tick=tick, stop=ui.key_pressed)
            if ui.key_pressed():
                break
    except RecorderError as exc:
        ui.live(None)
        ui.bad(str(exc))
        if exc.hint:
            ui.para(exc.hint)
    except KeyboardInterrupt:
        pass
    ui.live(None)


def restore_flow(sess: Session) -> None:
    if not sess.control:
        ui.warn("Não consigo controlar este microfone pelo Windows, então não há o que restaurar.")
        return
    original, last = _backup_files(sess)
    options, targets = [], []
    if original.exists():
        s = _load_backup(original)
        if s:
            options.append(f"Como estava ANTES DE TUDO ({s.describe()})")
            targets.append(s)
    if last.exists():
        s = _load_backup(last)
        if s:
            options.append(f"Como estava antes do ÚLTIMO teste ({s.describe()})")
            targets.append(s)
    if not options:
        ui.warn("Ainda não há configuração salva: o programa só guarda quando faz o primeiro ajuste.")
        return
    options.append("Cancelar")
    i = ui.ask_choice("Para qual configuração voltar?", options, default=len(options))
    if i >= len(targets):
        return
    applied = sess.control.apply(targets[i])
    ui.ok(f"Microfone restaurado: {applied.describe()}")


def clean_file_flow(sess: Session) -> None:
    ui.title("LIMPAR UM ARQUIVO QUE VOCÊ JÁ GRAVOU")
    ui.para("Funciona com áudio (wav, mp3, flac, ogg) e vídeo (mp4, mov, mkv). Eu meço o ruído nas pausas do "
            "próprio arquivo e removo chiado, zumbido e graves. O original não é alterado: crio uma cópia "
            "com '_limpo' no nome.")
    raw = ui.ask_file("Arraste o arquivo para esta janela (ou cole o caminho) e aperte ENTER:")
    path = Path(raw.strip().strip('"').strip("'"))
    if not raw or not path.exists() or not path.is_file():
        ui.bad("Não encontrei esse arquivo.")
        return
    try:
        outputs = fileclean.clean_file(path, say=ui.info)
    except Exception as exc:
        ui.bad(f"Não consegui limpar este arquivo: {exc}")
        return
    for out in outputs:
        ui.ok(f"Criado: {out}")


def _measure_raw_noise(sess: Session):
    """7 s de silêncio, de preferência em modo exclusivo (que ignora filtros como o Equalizer APO)."""
    ui.para(f"Vou medir o ruído do seu microfone ({SILENCE_S:.0f} segundos de silêncio) para descobrir o "
            "zumbido e os apitos exatos dele. Fique em silêncio.")
    ui.ask_enter()
    ui.countdown(3)
    mode = "exclusivo"
    data = None
    try:
        tick = ui.RecordProgress("ruído (exclusivo)", SILENCE_S)
        data = sess.recorder.with_exclusive(True).record(SILENCE_S, kind="silence", on_tick=tick)
        tick.finish()
        if float(np.max(np.abs(data))) < 1e-7:
            data = None
    except RecorderError:
        data = None
    if data is None:
        mode = "normal"
        data = record(sess, "silence", SILENCE_S, "ruído", countdown=0)
    return analyze_noise(pick_channel(data, sess.last_channel), sess.fs), mode


def get_apo(sess: Session) -> Optional[realtime.ApoInfo]:
    """O Equalizer APO desta sessão. Sessões simuladas nunca olham o computador de verdade."""
    if sess.apo_info is not None:
        return sess.apo_info
    return realtime.find_apo() if sess.look_for_apo else None


def _read_config(apo: realtime.ApoInfo) -> str:
    try:
        return (apo.config_dir / "config.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _is_attached(sess: Session) -> Optional[bool]:
    return realtime.is_attached(realtime.endpoint_guid(getattr(sess.control, "endpoint_id", "") or ""))


def suspend_equalizer(sess: Session) -> Optional[Callable[[], None]]:
    """Se o equalizador do Limpa Som está ligado, ele esconderia o ruído de verdade e o teste mediria um som já
    filtrado. Desliga durante o teste e devolve a função que religa. None = não havia o que desligar."""
    apo = get_apo(sess)
    if apo is None:
        return None
    mic_file = apo.config_dir / realtime.MIC_FILE
    if not realtime.is_installed(_read_config(apo)) or not mic_file.exists():
        return None
    try:
        original = mic_file.read_text(encoding="utf-8")
    except OSError:
        return None
    if "DESLIGADO" in original or _is_attached(sess) is False:
        return None
    if not sess.allow_changes:
        ui.warn("O equalizador do Limpa Som está ligado e vai esconder parte do ruído neste teste. Para um teste "
                "fiel, desligue-o no 'Equalizador ao vivo' antes.")
        return None
    off = eq.build_config(realtime.device_pattern(sess.info.name), eq.Profile(enabled=False), label=sess.info.name)
    if not realtime.write_mic_file(apo, off).ok:
        return None
    ui.info("Desliguei o equalizador durante o teste, para ele não esconder o ruído de verdade. Religo no fim.")
    ui.sleep_fn(3.0)                      # o APO relê o arquivo em instantes

    def restore() -> None:
        if realtime.write_mic_file(apo, original).ok:
            ui.info("Equalizador religado como estava.")
    return restore


def _evaluation_sentence(ev: "design.Evaluation") -> str:
    if ev.pause_silent:
        return ("Testei na sua própria gravação, com o plugin de IA de verdade: o ruído das pausas some por completo "
                "e a sua voz continua com o mesmo volume.")
    return ("Testei na sua própria gravação, com o plugin de IA de verdade: o ruído das pausas cai %.1f dB e a sua voz "
            "fica %.1f dB mais forte que o ruído (de %.0f para %.0f dB)."
            % (abs(ev.pause_change_db), ev.gain_db, ev.ratio_before_db, min(ev.ratio_after_db, 99)))


def equalizer_step(sess: Session, noise: NoiseReport, speech, voice: Optional[np.ndarray],
                   apo: Optional[realtime.ApoInfo] = None) -> bool:
    """O Limpa Som projeta o equalizador do microfone a partir do ruído e da voz que mediu, testa na gravação da
    própria pessoa, guarda (o 'Equalizador ao vivo' mostra e deixa editar) e, se ela quiser, aplica no Equalizer
    APO. Devolve True se aplicou."""
    apo = apo or get_apo(sess)
    ui.step("Criando o equalizador do seu microfone")
    ui.working("Projetando o equalizador a partir do seu ruído e da sua voz...")
    try:
        profile, ev, _tried = design.choose_profile(noise, speech, voice, sess.fs,
                                                    apo.install_path if apo else None, progress=ui.working)
    finally:
        ui.working(None)
    eq.save_stored(sess.out_dir, sess.info.name, profile, limpa_som=profile)
    ui.ok("Criei um equalizador para o seu microfone e guardei. Abra o 'Equalizador ao vivo' para ver cada filtro "
          "e mexer no que quiser.")
    for line in eq.describe(profile):
        ui.info(line)
    if ev is not None and ev.ran_ai:
        ui.info(_evaluation_sentence(ev))
    elif ev is None:
        ui.info("Não deu para testar na sua voz agora (faltou a gravação de voz, o plugin de IA ou o microfone não "
                "é de 48 kHz): o equalizador veio só das medidas.")
    if apo is None:
        ui.warn("Não encontrei o Equalizer APO neste computador. Ele é o programa que aplica o equalizador no Discord, "
                "OBS e Zoom (sourceforge.net/projects/equalizerapo). O equalizador já fica guardado para quando "
                "você o instalar.")
        return False
    return _apply_equalizer(sess, apo, profile)


def _apply_equalizer(sess: Session, apo: realtime.ApoInfo, profile: eq.Profile) -> bool:
    attached = _is_attached(sess)
    config_text = _read_config(apo)
    already = realtime.is_installed(config_text)
    config = eq.build_config(realtime.device_pattern(sess.info.name), profile, label=sess.info.name,
                             rnnoise_available=(apo.has_rnnoise_mono, apo.has_rnnoise_stereo))
    if profile.rnnoise and not (apo.has_rnnoise_mono or apo.has_rnnoise_stereo):
        ui.warn("Plugin RNNoise não encontrado: só os filtros serão aplicados (a IA anti-chiado é o que mais limpa).")
    ui.say()
    ui.para(f"Escrevo {realtime.MIC_FILE} em {apo.config_dir}. No config.txt (guardo uma cópia em "
            f"{realtime.BACKUP_NAME}) acrescento uma linha 'Include'.")
    general = [l.strip() for l in config_text.splitlines()
               if realtime._is_active(l) and not realtime._DEVICE.match(l) and l.strip() != realtime.IF_LINE]
    if general and not already:
        ui.para("O seu config.txt tem ajustes gerais, sem restrição de dispositivo, que valeriam também para o "
                "microfone assim que o APO fosse ligado a ele:", 5)
        for line in general[:5]:
            ui.say("       " + (line if len(line) < 80 else line[:77] + "..."), "yellow")
        ui.para("Para o microfone não herdar isso (ganho extra e reforço de graves, por exemplo, que "
                "amplificariam o zumbido), isolo esses ajustes: continuam valendo para alto-falantes e fones, "
                "mas deixam de valer para microfones. É reversível.", 5)
    if not ui.ask_yes_no("Aplicar esse equalizador ao seu microfone agora?"):
        ui.info("Nada foi alterado no Equalizer APO. O equalizador continua guardado.")
        return False
    result = realtime.apply(apo, config)
    if not result.ok:
        ui.bad(result.message)
        return False
    ui.ok(f"Filtros gravados em {result.mic_file}")
    if result.config_changed:
        ui.ok(f"config.txt atualizado (original guardado em {result.backup})")
    for note in result.notes:
        ui.info(note)
    ui.step("Falta ligar o Equalizer APO ao microfone" if attached is not True else "Pronto")
    if attached is True:
        ui.para("O Equalizer APO já está ligado a este microfone: as mudanças valem em segundos, sem reiniciar. "
                "Use o Discord/OBS/Zoom em modo normal.")
    else:
        ui.para("Este passo só o Windows deixa fazer com a sua confirmação: (1) abra o Configurator do Equalizer APO; "
                "(2) na aba 'Capture devices', marque o seu microfone; (3) OK e reinicie o Windows.")
        if apo.configurator is not None and ui.ask_yes_no("Abrir o Configurator agora?"):
            if not realtime.open_configurator(apo):
                ui.bad("Não consegui abrir o Configurator. Procure 'Equalizer APO Configurator' no menu Iniciar "
                       "(clique com o botão direito > Executar como administrador).")
    ui.para("No Discord/Zoom, deixe a supressão de ruído deles em 'baixa' ou desligada, para não processar duas vezes. "
            "Para ajustar qualquer filtro, abra o 'Equalizador ao vivo'.")
    return True


def realtime_flow(sess: Session, apo: Optional[realtime.ApoInfo] = None) -> None:
    ui.title("EQUALIZADOR DO MICROFONE (Discord, OBS, Zoom...)")
    ui.para("Crio um equalizador sob medida para o seu microfone, a partir do ruído e da sua voz, e instalo no "
            "Windows: tudo o que usa o microfone em modo normal (Discord, OBS, Zoom, Teams, Meet) passa a receber "
            "o áudio já tratado. Uso o Equalizer APO e o plugin RNNoise que você já tem; não baixo nada.")
    apo = apo or get_apo(sess)
    if apo is None:
        ui.bad("Não encontrei o Equalizer APO neste computador.")
        ui.para("Para usar esta função, instale-o pelo site oficial (sourceforge.net/projects/equalizerapo), "
                "escolha o seu microfone na aba 'Capture devices' do Configurator e reinicie o Windows. "
                "Depois volte aqui.")
        return
    guid = realtime.endpoint_guid(getattr(sess.control, "endpoint_id", "") or "")
    attached = realtime.is_attached(guid)
    config_text = ""
    try:
        config_text = (apo.config_dir / "config.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    ui.say()
    ui.say(f"  Equalizer APO: {apo.install_path}")
    ui.say("  Plugin RNNoise: " + ("encontrado" if (apo.has_rnnoise_mono or apo.has_rnnoise_stereo) else "NÃO encontrado"))
    state = {True: "ligado a este microfone", False: "NÃO está ligado a este microfone (veja o passo do Configurator)",
             None: "não deu para verificar"}[attached]
    ui.say(f"  Equalizer APO neste microfone: {state}")
    ui.say("  Limpa Som já configurado: " + ("sim" if realtime.is_installed(config_text) else "não"))

    i = ui.ask_choice("O que fazer?", ["Ativar ou atualizar a limpeza em tempo real para este microfone",
                                       "Desligar (tirar os filtros do Limpa Som do config.txt)",
                                       "Voltar"], default=1, eof=2)
    if i == 2:
        return
    if i == 1:
        result = realtime.remove(apo)
        (ui.ok if result.ok else ui.bad)(result.message)
        return

    already = realtime.is_installed(config_text)
    speech = voice = None
    if sess.last is not None and sess.last_speech is not None:
        noise, speech = sess.last.final.noise, sess.last.final.speech
        voice = pick_channel(sess.last_speech, sess.last_channel)
        ui.info("Uso o que o último teste mediu: o ruído e a sua voz.")
    else:
        try:
            noise, mode = _measure_raw_noise(sess)
        except (Abort, RecorderError) as exc:
            if isinstance(exc, RecorderError):
                ui.bad(str(exc))
            return
        if mode == "normal" and already and attached:
            ui.warn("Não consegui medir em modo exclusivo e o equalizador atual está ativo: o ruído medido já viria "
                    "filtrado e eu apagaria os notches certos. Desligue (opção 2), meça de novo e ative.")
            return
        ui.info("Sem a sua voz medida o equalizador fica mais simples. Rode 'Testar o microfone' para ele se "
                "ajustar também à sua voz.")
    equalizer_step(sess, noise, speech, voice, apo=apo)


def test_with_followups(sess: Session) -> None:
    """Teste completo e, se a pessoa pedir, novos testes depois dos ajustes dela."""
    ctx = full_test(sess)
    while ctx is not None and next_steps(sess, ctx) == "retest":
        ctx = full_test(sess, previous=ctx)


def main_menu(sess: Session) -> None:
    while True:
        ui.title(f"LIMPA SOM — olá, {sess.name}!")
        ui.say(f"  Microfone: {sess.info.name}  ({sess.info.connection_text})")
        ui.say()
        i = ui.ask_choice("O que você quer fazer?", [
            "Testar o microfone e corrigir o chiado (recomendado)",
            "Ajuste ao vivo (medidor de nível para regular o ganho)",
            "Limpar um arquivo de áudio/vídeo que eu já gravei",
            "Limpeza em tempo real para Discord, OBS, Zoom... (Equalizer APO)",
            "Restaurar as configurações originais do microfone",
            "Sair",
        ], default=1)
        if i == 0:
            test_with_followups(sess)
        elif i == 1:
            live_meter(sess)
        elif i == 2:
            clean_file_flow(sess)
        elif i == 3:
            realtime_flow(sess)
        elif i == 4:
            restore_flow(sess)
        else:
            ui.say("Até a próxima!", "cyan")
            return
