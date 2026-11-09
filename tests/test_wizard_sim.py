"""Testes de ponta a ponta do assistente com o microfone simulado."""
from pathlib import Path

import pytest

from limpa_som import ui, wizard
from limpa_som.findings import DeviceInfo, build_verdict
from limpa_som.scoring import compute_score
from limpa_som.simulate import SCENARIOS, SimBackend


@pytest.fixture(autouse=True)
def quiet_ui(monkeypatch):
    monkeypatch.setattr(ui, "input_fn", lambda prompt="": "")
    monkeypatch.setattr(ui, "sleep_fn", lambda s: None)


def run(key, tmp_path, allow_changes=True):
    backend = SimBackend(SCENARIOS[key], speed=1e9)
    sess = backend.make_session("Daniel", tmp_path, allow_changes=allow_changes, open_report=False)
    ctx = wizard.full_test(sess)
    assert ctx is not None, "o teste foi cancelado"
    return backend, sess, ctx, build_verdict(ctx)


def keys(verdict, bucket=None):
    return {f.key for f in verdict.findings if bucket is None or f.bucket == bucket}


def test_bom_microfone_tem_nota_alta_e_nenhum_problema_grave(tmp_path):
    _, _, ctx, v = run("bom", tmp_path)
    assert v.score_after.value >= 8.5
    assert v.kind in ("excelente", "bom")
    assert not [f for f in v.findings if f.bucket in ("config", "hardware") and f.severity >= 2]


def test_ganho_mal_dividido_sobe_o_reforco_e_melhora(tmp_path):
    backend, _, ctx, v = run("ganho_mal_dividido", tmp_path)
    assert backend.state.boost_db >= 20.0          # estava em 0
    assert v.score_after.value - v.score_before.value >= 1.5
    assert "ganho_ajustado" in keys(v, "corrigido")


def test_reforco_ruidoso_reduz_o_reforco_e_melhora(tmp_path):
    backend, _, ctx, v = run("reforco_ruidoso", tmp_path)
    assert backend.state.boost_db <= 10.0           # estava em +30
    assert v.score_after.value - v.score_before.value >= 2.0
    assert ctx.after is not None and not ctx.reverted


def test_placa_ruidosa_e_declarada_hardware(tmp_path):
    backend, _, ctx, v = run("placa_ruidosa", tmp_path)
    assert "ruido_entrada" in keys(v, "hardware")
    assert v.kind == "hardware"
    assert v.score_after.value <= 6.5
    assert "NÃO é configuração" in v.headline
    assert not [f for f in v.findings if f.bucket == "config" and f.key == "efeitos_windows"]


def test_zumbido_e_apontado_como_instalacao(tmp_path):
    _, _, ctx, v = run("zumbido", tmp_path)
    assert "zumbido" in keys(v, "hardware")
    hum = [f for f in v.findings if f.key == "zumbido"][0]
    assert "60" in hum.title


def test_efeitos_do_windows_sao_configuracao(tmp_path):
    _, _, ctx, v = run("efeitos_windows", tmp_path)
    assert "efeitos_windows" in keys(v, "config")
    eff = [f for f in v.findings if f.key == "efeitos_windows"][0]
    assert eff.opens_sound_panel
    assert any("Desativar" in a for a in eff.actions)


def test_ruido_de_fora_do_computador_e_apontado_como_origem_externa(tmp_path):
    _, _, ctx, v = run("ambiente", tmp_path)
    assert "ruido_fonte" in keys(v, "ambiente")
    assert "ruido_entrada" not in keys(v)
    assert v.kind == "ambiente"


def test_bluetooth_e_banda_estreita_de_hardware(tmp_path):
    _, _, ctx, v = run("bluetooth", tmp_path)
    assert "banda_bluetooth" in keys(v, "hardware")
    assert v.score_after.value <= 6.0


def test_sem_ajuste_nao_muda_nenhuma_configuracao(tmp_path):
    backend, _, ctx, v = run("reforco_ruidoso", tmp_path, allow_changes=False)
    assert backend.state.boost_db == 30.0
    assert ctx.after is None and not ctx.sweep


def test_gera_arquivos_relatorio_historico_e_perfil(tmp_path):
    _, sess, ctx, v = run("reforco_ruidoso", tmp_path)
    run_dirs = [p for p in tmp_path.iterdir() if p.is_dir() and p.name.startswith("teste_")]
    assert len(run_dirs) == 1
    names = {p.name for p in run_dirs[0].iterdir()}
    assert {"1_antes.wav", "2_depois.wav", "3_limpo.wav", "relatorio.html"} <= names
    html = (run_dirs[0] / "relatorio.html").read_text(encoding="utf-8")
    assert "<svg" in html and "Daniel" in html and "<audio" in html
    assert "Em palavras simples" in html and html.index("Em palavras simples") < html.index("Medições")
    assert (tmp_path / "historico.json").exists()
    assert (tmp_path / "perfil_ruido.npz").exists()
    assert len(wizard.load_history(sess)) == 1


def test_restaurar_volta_ao_ganho_original(tmp_path, monkeypatch):
    backend, sess, ctx, v = run("reforco_ruidoso", tmp_path)
    assert backend.state.boost_db != 30.0
    monkeypatch.setattr(ui, "input_fn", lambda prompt="": "1")   # "como estava antes de tudo"
    wizard.restore_flow(sess)
    assert backend.state.boost_db == 30.0
    assert abs(backend.state.volume_db - (-10.0)) <= 0.8


def test_reteste_usa_a_nota_anterior_como_antes(tmp_path):
    backend, sess, ctx, v = run("reforco_ruidoso", tmp_path)
    again = wizard.full_test(sess, previous=ctx)
    assert again is not None
    assert again.before is ctx.final
    assert again.after is not None


def test_limpeza_digital_nao_promete_mais_que_2_5_pontos(tmp_path):
    _, sess, ctx, v = run("placa_ruidosa", tmp_path)
    from limpa_som.scoring import DIGITAL_CLEANING_MAX_GAIN, cap_digital
    raw = compute_score(ctx.final)
    from limpa_som.scoring import Score
    fake = Score(value=10.0, base=10.0, snr_db=70.0, estimated=False)
    assert cap_digital(fake, raw).value <= raw.value + DIGITAL_CLEANING_MAX_GAIN + 0.05
    assert cap_digital(fake, Score(value=9.0, base=9.0, snr_db=52.0, estimated=False)).value <= 9.5
