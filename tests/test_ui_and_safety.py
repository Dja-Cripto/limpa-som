"""Regressões do erro de 03/11 (estilo vazio com cor ligada) e da restauração do ganho em qualquer falha."""
import pytest

from limpa_som import ui, wizard
from limpa_som.findings import DeviceInfo
from limpa_som.simulate import SCENARIOS, SimBackend


@pytest.fixture(autouse=True)
def quiet_ui(monkeypatch):
    monkeypatch.setattr(ui, "input_fn", lambda prompt="": "")
    monkeypatch.setattr(ui, "sleep_fn", lambda s: None)
    yield
    ui.use(ui.ConsoleBackend())


def session(key, tmp_path):
    backend = SimBackend(SCENARIOS[key], speed=1e9)
    sc = backend.scenario
    info = DeviceInfo(name=backend.device.name, connection=sc.connection, chip=sc.chip, simulated=True)
    return backend, wizard.Session(name="Daniel", recorder=backend.recorder, control=backend.control, info=info,
                                   out_dir=tmp_path, allow_changes=True, open_report=False)


def test_paint_never_fails_on_empty_or_unknown_styles(monkeypatch):
    monkeypatch.setattr(ui, "color_enabled", True)
    assert ui.paint("x", "") == "x"
    assert ui.paint("x", "nao_existe") == "x"
    assert ui.paint("x", "", "green") == "\033[32mx\033[0m"
    ui.say("linha", "", "mono")          # não pode levantar KeyError


def test_full_flow_works_with_colors_on(monkeypatch, tmp_path, capsys):
    """O erro real: com cor ligada (terminal de verdade) a tabela do ajuste quebrava o teste."""
    monkeypatch.setattr(ui, "color_enabled", True)
    backend, sess = session("reforco_ruidoso", tmp_path)
    ctx = wizard.full_test(sess)
    assert ctx is not None and ctx.after is not None
    assert "escolhido" in capsys.readouterr().out
    assert not (tmp_path / "erro.log").exists()


def test_error_while_showing_the_sweep_restores_the_original_gain(monkeypatch, tmp_path, capsys):
    backend, sess = session("reforco_ruidoso", tmp_path)
    start = backend.control.get_settings()
    real_say = ui.say

    def exploding_say(text="", *styles):
        if "Sinal/ruído previsto" in text:
            raise RuntimeError("falha simulada ao desenhar a tabela")
        real_say(text, *styles)

    monkeypatch.setattr(wizard.ui, "say", exploding_say)
    ctx = wizard.full_test(sess)
    out = capsys.readouterr().out
    assert ctx is None
    now = backend.control.get_settings()
    assert now.boost_db == start.boost_db                         # voltou ao original
    assert abs(now.volume_db - start.volume_db) <= 0.76           # (o simulador arredonda em degraus de 1,5 dB)
    assert "erro inesperado" in out and "voltou ao original" in out
    assert (tmp_path / "erro.log").exists()
    assert "falha simulada" in (tmp_path / "erro.log").read_text(encoding="utf-8")


def test_keyboard_interrupt_during_sweep_also_restores(monkeypatch, tmp_path):
    backend, sess = session("reforco_ruidoso", tmp_path)
    start = backend.control.get_settings()
    real_apply = backend.control.apply
    calls = {"n": 0}

    def interrupting_apply(s):
        calls["n"] += 1
        if calls["n"] == 3:      # no meio da varredura
            raise KeyboardInterrupt
        return real_apply(s)

    monkeypatch.setattr(backend.control, "apply", interrupting_apply)
    with pytest.raises(KeyboardInterrupt):
        wizard.full_test(sess)
    monkeypatch.undo()
    now = backend.control.get_settings()
    assert now.boost_db == start.boost_db
    assert abs(now.volume_db - start.volume_db) <= 0.76
