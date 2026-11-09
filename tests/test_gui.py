"""A janela de verdade (tkinter) rodando os fluxos com o microfone simulado, sempre em processo novo."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

tkinter = pytest.importorskip("tkinter")

from limpa_som import gui  # noqa: E402

DRIVER = Path(__file__).resolve().parent / "gui_driver.py"
ROOT = DRIVER.parent.parent


def run_driver(scenario: str, mode: str) -> dict:
    proc = subprocess.run([sys.executable, str(DRIVER), scenario, mode], capture_output=True, text=True,
                          timeout=240, cwd=str(ROOT))
    lines = [l for l in proc.stdout.splitlines() if l.startswith("JSON:")]
    assert lines, f"a janela não terminou (código {proc.returncode}):\n{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}"
    return json.loads(lines[-1][5:])


def test_full_test_runs_in_the_window():
    r = run_driver("reforco_ruidoso", "teste")
    assert r["ready"] and r["started"] and r["finished"]
    assert not r["errors"], r["errors"]
    assert "Resultado" in r["log"] and "Daniel" in r["log"]
    assert r["tiles"] == 3                                    # antes, depois e limpeza digital
    assert r["report_exists"] and r["last_ok"]
    assert r["boost"] <= 10.0 and r["start_boost"] == 30.0     # o ajuste automático rodou
    assert not r["erro_log"]
    assert "Ganho atual" in r["status"] and r["back_enabled"]


def test_result_screen_shows_plain_cards_and_hides_technical_details_until_asked():
    r = run_driver("sinal_fraco", "teste")
    assert r["views"] == {"resultados": "grid", "log": "", "guia": ""}     # só o resumo; log e instrução escondidos
    assert r["cards"] >= 4                                                  # manchete + problemas + por onde começar
    assert r["current_chip"] == ["result"] and r["chips_done"] == 6         # todas as etapas anteriores marcadas
    assert r["views_details"] == {"resultados": "", "log": "grid"}          # "Ver detalhes técnicos"
    assert r["views_back"] == {"resultados": "grid", "log": ""}             # "Voltar ao resumo"
    assert "O que eu vi:" not in r["log"] or "DETALHES TÉCNICOS" in r["log"]


def test_verdict_has_no_duplicated_score_lines_in_the_window():
    r = run_driver("zumbido", "teste")
    assert r["tiles"] >= 2 and "Nota do seu áudio" not in r["log"]    # a nota só aparece nos cartões


def test_live_meter_runs_and_stops():
    r = run_driver("bom", "live")
    assert r["finished"] and not r["errors"], r["errors"]
    assert "Ajuste ao vivo" in r["log"]
    assert r["live_updates"] >= 40                             # o medidor foi atualizado várias vezes


def test_automatic_mode_never_touches_the_real_system():
    r = run_driver("zumbido", "guarda")
    assert r["busy_tempo"] is False and r["busy_restaurar"] is False
    assert any("automático" in e for e in r["errors"])


def test_closing_during_a_task_cancels_it_and_keeps_the_gain():
    r = run_driver("reforco_ruidoso", "cancelar")
    assert r["prompt"] and r["finished"]
    assert "Cancelado" in r["log"]
    assert r["boost"] == r["start_boost"] and abs(r["volume"] - r["start_volume"]) <= 0.76


def test_prompt_buttons_are_real_widgets():
    r = run_driver("bom", "botoes")
    assert r["prompt"] and r["button_texts"] == ["Começar"]          # o botão diz o que vai acontecer
    assert r["advanced"] and r["finished"]


def test_button_texts_make_sense_without_the_enter_key():
    f = gui.enter_text
    assert f("Aperte ENTER para continuar...") == "Clique em Continuar quando estiver pronto."
    assert f("Fique em silêncio. ENTER para começar...") == "Fique em silêncio. Clique em Continuar para começar..."
    assert f("Quando estiver pronto, aperte ENTER e fique em silêncio...") == \
        "Quando estiver pronto, clique em Continuar e fique em silêncio..."
    assert f("Aperte ENTER e comece a ler quando a gravação iniciar...").startswith("Clique em Continuar e comece")
    assert "ENTER" not in f("Leia o texto de novo (mais perto, voz normal). ENTER para começar...")
    assert f("Sem tecla nenhuma") == "Sem tecla nenhuma"
    assert gui.sentence("TESTE DO MICROFONE") == "Teste do microfone"
    assert gui.sentence("Já em caixa mista") == "Já em caixa mista"
