"""Diagnóstico em linguagem simples, provas sobre o caminho do sinal e a pergunta da ligação."""
import json

import pytest

from limpa_som import evidence, plain, ui, wizard
from limpa_som.findings import build_verdict
from limpa_som.simulate import SCENARIOS, SimBackend


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(ui, "input_fn", lambda prompt="": "")
    monkeypatch.setattr(ui, "sleep_fn", lambda s: None)
    yield
    ui.use(ui.ConsoleBackend())


def run(key, tmp_path, tweak=None):
    backend = SimBackend(SCENARIOS[key], speed=1e9)
    sess = backend.make_session("Daniel", tmp_path, open_report=False)
    if tweak:
        tweak(backend, sess)
    ctx = wizard.full_test(sess)
    assert ctx is not None
    return backend, sess, ctx, build_verdict(ctx)


def test_the_real_signature_of_daniels_chain_is_recognised():
    """Os 8 apitos medidos no 'mi mesa' repetem-se de ~800 em 800 Hz (chaveamento digital)."""
    real = [12006, 16008, 15208, 11206, 8004, 4002, 7204, 16811]
    base = evidence.tone_comb(real)
    assert base is not None and 795 <= base <= 805
    assert evidence.tone_comb([3000, 4100, 9500]) is None           # sem padrão
    assert evidence.tone_comb([1000, 2000]) is None                  # poucos tons


def test_weak_signal_through_a_mixer_is_explained_as_a_gain_problem_at_the_mixer(tmp_path):
    backend, sess, ctx, v = run("sinal_fraco", tmp_path)
    ev = evidence.chain_evidence(ctx)
    assert ev.gain_cannot_help and ev.weak_signal
    assert ev.noise_slope is not None and ev.noise_slope > 0.8      # o ruído sobe 1 dB por dB de ganho
    assert ev.snr_spread_db <= 3.0                                  # a relação voz/ruído não muda com o ganho

    assert v.kind == "config" and "mesa" in v.headline
    assert "sinal_fraco" in {f.key for f in v.findings if f.bucket == "config"}
    assert "ganho_nao_ajuda" in {f.key for f in v.findings}
    assert ctx.reverted and ctx.revert_reason == "sem_ganho"
    now = backend.control.get_settings()
    assert now.boost_db == 30.0 and abs(now.volume_db - 6.0) <= 0.8  # voltou ao original: não adiantava mexer

    keys = [i.key for i in v.plain.items]
    assert "sinal" in keys and "zumbido" in keys
    sinal = next(i for i in v.plain.items if i.key == "sinal")
    assert "antes de o computador amplificar" in sinal.saw and "mesa" in sinal.why
    assert any("GAIN" in t for t in sinal.todo)                      # conselho concreto para a mesa
    assert v.plain.first_steps and v.plain.free_fix
    assert v.plain.headline.startswith("Daniel,")


def test_plain_report_is_printed_in_simple_words_before_the_numbers(tmp_path, capsys):
    run("sinal_fraco", tmp_path)
    out = capsys.readouterr().out
    assert out.index("O que eu vi:") < out.index("DETALHES TÉCNICOS")
    assert "Por que costuma acontecer:" in out and "POR ONDE COMEÇAR" in out


def test_the_wiring_question_is_asked_once_and_remembered(tmp_path, monkeypatch):
    backend = SimBackend(SCENARIOS["zumbido"], speed=1e9)
    sess = backend.make_session("Daniel", tmp_path, open_report=False)
    assert sess.wiring is None and not sess.wiring_known
    monkeypatch.setattr(ui, "input_fn", lambda prompt="": "2" if "Escolha" in prompt else "")
    wizard.ask_wiring(sess)
    assert sess.wiring == "mesa"
    assert json.loads((tmp_path / "preferencias.json").read_text(encoding="utf-8"))[sess.info.name] == {
        "ligacao": "mesa"}

    again = SimBackend(SCENARIOS["zumbido"], speed=1e9).make_session("Daniel", tmp_path, open_report=False)

    def never(prompt=""):
        raise AssertionError("não devia perguntar de novo")

    monkeypatch.setattr(ui, "input_fn", never)
    wizard.ask_wiring(again)
    assert again.wiring == "mesa"


def test_unknown_wiring_gets_both_possibilities_and_an_invitation_to_answer(tmp_path):
    backend, sess, ctx, v = run("sinal_fraco", tmp_path, tweak=lambda b, s: setattr(s, "wiring", None) or
                                setattr(s, "wiring_known", False))
    assert ctx.wiring is None
    sinal = next(i for i in v.plain.items if i.key == "sinal")
    assert "dois jeitos" in sinal.why and "Como meu microfone está ligado" in sinal.why


def test_direct_mic_on_the_board_is_not_blamed_on_the_mixer(tmp_path):
    backend, sess, ctx, v = run("sinal_fraco", tmp_path, tweak=lambda b, s: setattr(s, "wiring", "direto"))
    assert "sinal_fraco" not in {f.key for f in v.findings}          # não manda mexer numa mesa que não existe
    sinal = next(i for i in v.plain.items if i.key == "sinal")
    assert "placa-mãe" in sinal.why and any("interface" in t for t in sinal.todo)


def test_another_usb_microphone_is_offered_as_the_first_step_for_analog_noise(tmp_path):
    def add_alt(backend, sess):
        sess.alternatives = ["Microfone (S200)"]

    backend, sess, ctx, v = run("zumbido", tmp_path, tweak=add_alt)
    items = v.plain.items
    assert items[0].key == "alternativa" and "S200" in items[0].saw
    assert "cabo P2" in " ".join(items[0].todo)
    assert v.plain.first_steps[0].startswith("Teste o microfone USB")
    assert "alternativa" not in v.plain.headline.lower()             # o problema principal continua sendo o ruído


def test_devices_without_windows_gain_control_skip_the_gain_test(tmp_path):
    def no_gain(backend, sess):
        backend.control.has_gain_control = False                      # como o S200: volume sem efeito

    backend, sess, ctx, v = run("reforco_ruidoso", tmp_path, tweak=no_gain)
    assert not ctx.gain_control and ctx.sweep == [] and ctx.after is None
    assert backend.control.get_settings().boost_db == 30.0            # nada foi mexido
    assert evidence.chain_evidence(ctx).gain_db is None


def test_a_good_microphone_gets_no_problem_items(tmp_path):
    backend, sess, ctx, v = run("bom", tmp_path)
    assert v.plain.items == [] or all(i.severity < 3 for i in v.plain.items)
    assert "não achei nenhum problema sério" in v.plain.headline or v.score_after.value >= 8.0
