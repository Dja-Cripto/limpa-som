"""O fluxo guiado de limpeza em tempo real, com microfone simulado e uma pasta do APO descartável."""
import pytest

from limpa_som import realtime, ui, wizard
from limpa_som.findings import DeviceInfo, build_verdict
from limpa_som.simulate import SCENARIOS, SimBackend

DANIEL_CONFIG = (
    "Preamp: 4.4 dB\nInclude: example.txt\nGraphicEQ: 25 0; 40 0; 63 0\nInclude: microfonet.txt\n"
)


@pytest.fixture(autouse=True)
def quiet_ui(monkeypatch):
    monkeypatch.setattr(ui, "sleep_fn", lambda s: None)


def make_session(key, tmp_path):
    backend = SimBackend(SCENARIOS[key], speed=1e9)
    sc = backend.scenario
    info = DeviceInfo(name="mi mesa (High Definition Audio Device)", connection=sc.connection, chip=sc.chip,
                      simulated=True)
    return backend, wizard.Session(name="Daniel", recorder=backend.recorder, control=backend.control, info=info,
                                   out_dir=tmp_path / "res", allow_changes=False, open_report=False)


def answers(monkeypatch, menu_choice="1"):
    monkeypatch.setattr(ui, "input_fn", lambda prompt="": menu_choice if "Escolha" in prompt else "")


def apo_folder(tmp_path):
    cfg = tmp_path / "apo" / "config"
    cfg.mkdir(parents=True)
    (cfg / "config.txt").write_text(DANIEL_CONFIG, encoding="utf-8")
    return realtime.ApoInfo(tmp_path / "apo", cfg, True, True, None), cfg


def test_activate_writes_filters_measured_from_the_microphone(tmp_path, monkeypatch, capsys):
    _, sess = make_session("zumbido", tmp_path)
    apo, cfg = apo_folder(tmp_path)
    answers(monkeypatch, "1")
    wizard.realtime_flow(sess, apo=apo)
    out = capsys.readouterr().out
    mic = (cfg / realtime.MIC_FILE).read_text(encoding="utf-8")
    assert "Device: mi mesa High Definition Audio Device" in mic
    assert "Filter: ON NO Fc 60 Hz" in mic and "Filter: ON NO Fc 120 Hz" in mic
    assert "VSTPlugin: Library rnnoise_mono.dll ChunkData" in mic
    text = (cfg / "config.txt").read_text(encoding="utf-8")
    assert realtime.is_installed(text) and realtime.IF_LINE in text
    assert (cfg / realtime.BACKUP_NAME).read_text(encoding="utf-8") == DANIEL_CONFIG
    # o aviso lista o que o microfone herdaria
    assert "Preamp: 4.4 dB" in out and "notch" in out and "Criei um equalizador" in out
    assert "Falta ligar o Equalizer APO ao microfone" in out or "verificar" in out


def test_declining_changes_nothing(tmp_path, monkeypatch):
    _, sess = make_session("zumbido", tmp_path)
    apo, cfg = apo_folder(tmp_path)
    monkeypatch.setattr(ui, "input_fn",
                        lambda prompt="": "n" if "Aplicar" in prompt else ("1" if "Escolha" in prompt else ""))
    wizard.realtime_flow(sess, apo=apo)
    assert (cfg / "config.txt").read_text(encoding="utf-8") == DANIEL_CONFIG
    assert not (cfg / realtime.MIC_FILE).exists()


def test_turn_off_restores_the_config(tmp_path, monkeypatch):
    _, sess = make_session("zumbido", tmp_path)
    apo, cfg = apo_folder(tmp_path)
    answers(monkeypatch, "1")
    wizard.realtime_flow(sess, apo=apo)
    answers(monkeypatch, "2")
    wizard.realtime_flow(sess, apo=apo)
    assert (cfg / "config.txt").read_text(encoding="utf-8").rstrip() == DANIEL_CONFIG.rstrip()
    assert not (cfg / realtime.MIC_FILE).exists()


def test_no_one_typing_never_changes_the_system(tmp_path, monkeypatch):
    """Regressão: entrada acabando (EOF) tem de significar 'não', nunca 'sim' para mudanças no sistema."""
    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr(ui, "input_fn", eof)
    _, sess = make_session("zumbido", tmp_path)
    apo, cfg = apo_folder(tmp_path)
    wizard.realtime_flow(sess, apo=apo)
    assert (cfg / "config.txt").read_text(encoding="utf-8") == DANIEL_CONFIG
    assert not (cfg / realtime.MIC_FILE).exists() and not (cfg / realtime.BACKUP_NAME).exists()
    assert ui.ask_yes_no("Mudar o sistema?", default=True) is False
    assert ui.ask_choice("Menu", ["a", "b", "Voltar"], default=1) == 2


def test_sweep_is_refused_when_nobody_answers(tmp_path, monkeypatch):
    def eof(prompt=""):
        raise EOFError

    backend, sess = make_session("reforco_ruidoso", tmp_path)
    sess.allow_changes = True
    # as gravações usam ENTER (EOF = seguir), mas a permissão de mudar o ganho exige resposta explícita
    monkeypatch.setattr(ui, "input_fn", eof)
    ctx = wizard.full_test(sess)
    assert backend.state.boost_db == 30.0          # ganho intocado
    assert ctx is not None and ctx.after is None and not ctx.sweep


def test_missing_apo_is_explained_not_installed(tmp_path, monkeypatch, capsys):
    _, sess = make_session("zumbido", tmp_path)
    monkeypatch.setattr(realtime, "find_apo", lambda: None)
    wizard.realtime_flow(sess)
    out = capsys.readouterr().out
    assert "Não encontrei o Equalizer APO" in out and "sourceforge" in out


def test_test_reports_the_benefit_of_an_active_realtime_filter(tmp_path, monkeypatch):
    backend = SimBackend(SCENARIOS["apo_ativo"], speed=1e9)
    sc = backend.scenario
    info = DeviceInfo(name=backend.device.name, connection=sc.connection, chip=sc.chip, simulated=True)
    sess = wizard.Session(name="Daniel", recorder=backend.recorder, control=backend.control, info=info,
                          out_dir=tmp_path, allow_changes=False, open_report=False)
    monkeypatch.setattr(ui, "input_fn", lambda prompt="": "")
    ctx = wizard.full_test(sess)
    keys = {f.key for f in build_verdict(ctx).findings}
    assert "filtro_tempo_real" in keys
    assert "efeitos_windows" not in keys and "sem_efeitos" not in keys
    f = [f for f in build_verdict(ctx).findings if f.key == "filtro_tempo_real"][0]
    import re
    reduction = int(re.search(r"em (\d+) dB", f.title).group(1))
    assert f.bucket == "ok" and 15 <= reduction <= 21     # o cenário simula 18 dB de filtro


# ---------------------------------------------------------------- equalizador criado pelo Limpa Som ---
import json  # noqa: E402

from limpa_som import eq  # noqa: E402


def sim_session(key, tmp_path, apo=None, allow_changes=True):
    backend = SimBackend(SCENARIOS[key], speed=1e9)
    sess = backend.make_session("Daniel", tmp_path / "res", allow_changes=allow_changes, open_report=False)
    sess.apo_info = apo
    return backend, sess


def stored(tmp_path, name="mi mesa (High Definition Audio Device)"):
    return eq.load_stored(tmp_path / "res", name)


def say(monkeypatch, apply_answer=""):
    monkeypatch.setattr(ui, "input_fn", lambda prompt="": apply_answer if "Aplicar esse equalizador" in prompt else "")


def test_full_test_creates_stores_and_applies_the_equalizer(tmp_path, monkeypatch, capsys):
    apo, cfg = apo_folder(tmp_path)
    backend, sess = sim_session("zumbido", tmp_path, apo)
    say(monkeypatch)
    ctx = wizard.full_test(sess)
    out = capsys.readouterr().out
    assert ctx is not None and "Criei um equalizador" in out
    s = stored(tmp_path, sess.info.name)                          # o Equalizador vai abrir ESTE perfil
    assert s is not None and s.limpa_som is not None and s.atual.bands and s.criado_em
    assert any(b.kind == "NO" and abs(b.freq - 60) < 1 for b in s.atual.bands)           # o zumbido que foi medido
    assert any(b.kind == "HP" for b in s.atual.bands) and s.atual.rnnoise == 1
    mic = (cfg / realtime.MIC_FILE).read_text(encoding="utf-8")
    assert f"Device: {realtime.device_pattern(sess.info.name)}" in mic
    assert "ChunkData" in mic and "Filter: ON NO Fc 60 Hz" in mic
    assert realtime.is_installed((cfg / "config.txt").read_text(encoding="utf-8"))


def test_declining_keeps_the_equalizer_stored_but_does_not_touch_the_apo(tmp_path, monkeypatch):
    apo, cfg = apo_folder(tmp_path)
    _, sess = sim_session("zumbido", tmp_path, apo)
    say(monkeypatch, "n")
    wizard.full_test(sess)
    assert stored(tmp_path, sess.info.name) is not None
    assert (cfg / "config.txt").read_text(encoding="utf-8") == DANIEL_CONFIG
    assert not (cfg / realtime.MIC_FILE).exists()


def test_without_the_apo_the_equalizer_is_still_stored_for_later(tmp_path, monkeypatch, capsys):
    _, sess = sim_session("zumbido", tmp_path, None)
    say(monkeypatch)
    wizard.full_test(sess)
    assert stored(tmp_path, sess.info.name).atual.bands
    assert "Não encontrei o Equalizer APO" in capsys.readouterr().out


def active_equalizer(apo, sess):
    """Um equalizador do Limpa Som já ligado, como numa segunda rodada do teste."""
    profile = eq.Profile(bands=[eq.Band.new("HP", 90.0), eq.Band.new("NO", 60.0, q=30.0)], rnnoise=1)
    text = eq.build_config(realtime.device_pattern(sess.info.name), profile, label=sess.info.name)
    assert realtime.apply(apo, text).ok
    return text


def test_an_active_equalizer_is_turned_off_during_the_test_and_restored_if_declined(tmp_path, monkeypatch):
    apo, cfg = apo_folder(tmp_path)
    _, sess = sim_session("zumbido", tmp_path, apo)
    before = active_equalizer(apo, sess)
    seen = []
    monkeypatch.setattr(ui, "sleep_fn", lambda s: seen.append((cfg / realtime.MIC_FILE).read_text(encoding="utf-8")))
    say(monkeypatch, "n")
    wizard.full_test(sess)
    assert seen and "DESLIGADO" in seen[0] and "Filter:" not in seen[0]       # durante o teste não filtra nada
    assert (cfg / realtime.MIC_FILE).read_text(encoding="utf-8") == before   # e volta como estava


def test_an_active_equalizer_is_replaced_by_the_new_one_when_accepted(tmp_path, monkeypatch):
    apo, cfg = apo_folder(tmp_path)
    _, sess = sim_session("zumbido", tmp_path, apo)
    before = active_equalizer(apo, sess)
    say(monkeypatch)
    wizard.full_test(sess)
    now = (cfg / realtime.MIC_FILE).read_text(encoding="utf-8")
    assert now != before and "DESLIGADO" not in now and "ChunkData" in now


def test_measure_only_mode_warns_and_never_touches_an_active_equalizer(tmp_path, monkeypatch, capsys):
    apo, cfg = apo_folder(tmp_path)
    _, sess = sim_session("zumbido", tmp_path, apo, allow_changes=False)
    before = active_equalizer(apo, sess)
    say(monkeypatch, "n")
    wizard.full_test(sess)
    assert (cfg / realtime.MIC_FILE).read_text(encoding="utf-8") == before
    assert "vai esconder parte do ruído" in capsys.readouterr().out


def test_a_simulated_session_never_looks_for_the_real_apo(tmp_path, monkeypatch):
    _, sess = sim_session("zumbido", tmp_path, None)
    monkeypatch.setattr(realtime, "find_apo", lambda: (_ for _ in ()).throw(AssertionError("olhou o APO de verdade")))
    assert wizard.get_apo(sess) is None and wizard.suspend_equalizer(sess) is None
    sess.look_for_apo = True
    with pytest.raises(AssertionError):
        wizard.get_apo(sess)


def test_menu_option_uses_the_last_test_and_its_voice(tmp_path, monkeypatch):
    apo, cfg = apo_folder(tmp_path)
    _, sess = sim_session("zumbido", tmp_path, None)
    say(monkeypatch)
    wizard.full_test(sess)                                       # sem APO: só guarda
    assert sess.last is not None and not (cfg / realtime.MIC_FILE).exists()
    monkeypatch.setattr(ui, "input_fn", lambda prompt="": "1" if "Escolha" in prompt else "")
    wizard.realtime_flow(sess, apo=apo)                          # agora aplica o que o teste mediu
    mic = (cfg / realtime.MIC_FILE).read_text(encoding="utf-8")
    assert "Filter: ON NO Fc 60 Hz" in mic and "Filter: ON HPQ" in mic
