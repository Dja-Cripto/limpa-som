"""A janela do Equalizador ao vivo (tkinter) com um Equalizer APO FALSO, sempre em processo novo."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("tkinter")

DRIVER = Path(__file__).resolve().parent / "eq_driver.py"
ROOT = DRIVER.parent.parent


def run_driver(mode: str) -> dict:
    proc = subprocess.run([sys.executable, str(DRIVER), mode], capture_output=True, text=True, timeout=240,
                          cwd=str(ROOT))
    lines = [l for l in proc.stdout.splitlines() if l.startswith("JSON:")]
    assert lines, f"a janela não terminou (código {proc.returncode}):\n{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}"
    return json.loads(lines[-1][5:])


def test_opens_with_exactly_the_equalizer_the_limpa_som_created_and_touches_nothing():
    r = run_driver("perfil")
    s = r["start"]
    assert not r["errors"], r["errors"]
    assert s["rows"] == 7 and s["count"] == "7 filtro(s)"
    assert s["kinds"] == ["Corta graves", "Notch", "Notch", "Pico", "Corta agudos", "Corta agudos", "Notch"]
    assert s["freqs"][:4] == ["80,0", "60,0", "120", "143"]
    assert "Zumbido da rede elétrica: 60 Hz." in s["why"]                  # o porquê de cada filtro aparece
    assert s["enabled"] is True and "LIGADO" in s["power"]
    assert s["preamp"] == "+6,0 dB" and s["rnn"] == 1 and s["gate"] == "45%"
    assert "Criado pelo Limpa Som" in s["origin"] and "Criado em" in s["origin"]
    assert s["reset_disabled"] is False
    assert s["gain_disabled"] == ["disabled", "disabled", "disabled", "normal", "disabled", "disabled", "disabled"]
    assert s["config_untouched"] is True and s["mic_file"] is False        # abrir a janela não escreve nada no APO
    assert s["points_ok"] and r["asked"] == []


def test_every_edit_is_written_to_the_apo_and_the_original_is_never_lost():
    r = run_driver("editar")
    e = r["editar"]
    assert not r["errors"], r["errors"]
    first = e["edit1"]
    assert first["asked"] == 1 and first["installed"] and first["backup"] and first["keeps_original_lines"]
    assert first["gain_line"] and first["preamp_line"]                      # filtro editado e ganho de entrada no arquivo
    assert first["stored_gain"] == -6.0 and first["original_gain"] == -4.2   # o do Limpa Som fica guardado à parte
    assert (first["row_gain"], first["row_freq"]) == ("-6,0", "200")         # a tabela acompanha
    assert e["add"] == {"rows": 1, "selected": 7, "asked": 1, "line": True, "count": "8 filtro(s)"}
    assert e["table"] == {"freq": 2500.5, "gain": -2.5, "q": 2.0, "row_freq": "2500"}   # aceita vírgula decimal
    assert e["kind"]["kind"] == "NO" and e["kind"]["gain"] == 0.0 and e["kind"]["q"] == 30.0   # trocar o tipo ajusta Q
    assert e["kind"]["gain_state"] == "disabled"                            # notch não tem ganho
    assert e["garbage"] == {"freq": 2500.5, "errors": 0}                    # texto inválido: fica o valor, sem erro
    assert e["disable_band"] == {"gone": True, "still_listed": True}        # desmarcar tira do arquivo e mantém na lista
    assert e["remove"] == {"hp_gone": True, "rows": 7, "same_as_bands": True}
    ai = e["ai"]
    assert ai["preamp"] and ai["plugins"] == 2 and ai["gate_chunk"]          # 2 passadas, porta de voz 0,70 no arquivo
    assert (ai["gate_label"], ai["preamp_label"]) == ("70%", "+9,5 dB")
    off = e["off"]
    assert off["disabled_text"] and off["no_filters"] and off["stored_enabled"] is False
    assert "DESLIGADO" in off["power"] and off["active_dot"] == "#b91c1c"
    assert e["clear_declined"] == {"bands": True, "asked_last": True} and e["clear"] == {"bands": 0, "rows": 0}
    reset = e["reset"]
    assert reset["same"] and reset["rows"] == 7 and reset["stored_gain"] == -4.2
    assert reset["enabled_kept"] is False                                    # voltar ao do Limpa Som não religa sozinho
    assert reset["message"].startswith("Voltei ao equalizador que o Limpa Som criou.")
    assert (reset["preamp"], reset["rnn"]) == (6.0, 1)
    assert e["total_confirms"] == 3                                          # instalar (1x) + apagar recusado + apagar


def test_dragging_points_on_the_curve_moves_frequency_and_gain():
    r = run_driver("arrastar")
    a = r["arrastar"]
    assert not r["errors"], r["errors"]
    assert a["pk"] == {"freq": 500.0, "gain": -9.0}                          # pico: x = frequência, y = ganho
    assert a["notch"] == {"freq": 50.0, "gain": 0.0}                         # notch só anda de lado
    assert a["hp"] == 100.0
    assert a["limits"] == {"freq": 20000.0, "gain": 18.0}                    # fora da tela: nos limites
    assert a["picked"] == {"selected": True, "dragging": True, "row_highlight": "#2563eb"}
    assert a["moved"] == {"freq": 70.0, "dragging": None}
    assert a["wheel"] == {"up": 1.1, "back": 1.0}                            # roda do mouse muda o Q
    assert a["far"] == {"selected": None, "dragging": None, "no_change": True}
    assert a["rows_follow"] == "70,0"                                        # a tabela acompanha o arrastar


def test_without_a_limpa_som_equalizer_it_says_so_and_still_lets_you_build_one():
    r = run_driver("sem_perfil")
    s = r["start"]
    assert s["rows"] == 0 and s["enabled"] is False and "DESLIGADO" in s["power"]
    assert s["origin"].startswith("O Limpa Som ainda não criou um equalizador")
    assert s["reset_disabled"] is True
    e = r["empty"]
    assert e["profile_bands"] == 0 and e["stored"] is False and len(e["infos"]) == 1      # explica o que fazer
    assert e["after_add_stored"] is True and e["asked"] == 0                 # montado à mão, ainda desligado: nada no APO
    assert not r["errors"], r["errors"]


def test_a_profile_from_the_first_version_is_converted_to_filters():
    r = run_driver("antigo")
    s = r["start"]
    assert "Corta graves" in s["kinds"] and s["kinds"].count("Notch") >= 2 and "Pico" in s["kinds"]
    assert s["enabled"] is False and s["rnn"] == 1
    assert not r["errors"], r["errors"]


def test_without_equalizer_apo_the_window_explains_and_does_not_break():
    r = run_driver("sem_apo")
    n = r["no_apo"]
    assert not r["errors"], r["errors"]
    assert r["start"]["dots"]["apo"] == "#b91c1c"
    assert "Não encontrei o Equalizer APO" in n["message"] and n["asked"] == 0
    assert n["banner_text"].startswith("Não encontrei o Equalizer APO") and n["buttons_hidden"]
    assert n["stored_freq"] == 100.0                                         # a edição fica guardada para depois


def test_attached_status_follows_the_registry_and_the_open_limpa_som_button_works():
    r = run_driver("ligado")
    assert r["attached"] == {"banner": "", "dot": "#15803d"}
    assert r["lost"] == {"banner": "grid", "dot": "#b91c1c"}
    assert r["opened"] == 1


def test_switching_microphone_loads_its_own_equalizer_and_moves_the_apo_file_to_it():
    r = run_driver("trocar")
    s = r["switch"]
    assert not r["errors"], r["errors"]
    assert s["first_device"] and s["second_device"]                          # o arquivo do APO segue o microfone escolhido
    assert s["empty"] == 0 and s["origin"].startswith("O Limpa Som ainda não criou") and s["reset_disabled"]
    assert s["back"]["rows"] == 7 and s["back"]["freq0"] == 90.0 and s["back"]["device"]
