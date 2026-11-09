"""Roda a janela do Equalizador num processo NOVO, com um Equalizer APO FALSO numa pasta temporária.

Nada aqui toca no Equalizer APO nem no microfone de verdade: a pasta do APO, o registro (attached_fn), as
confirmações e o botão 'Abrir o Limpa Som' são trocados por versões de mentira.

Uso: python tests/eq_driver.py MODO
     MODO: perfil | editar | arrastar | sem_perfil | antigo | sem_apo | ligado | trocar
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from limpa_som import eq, equalizador, realtime  # noqa: E402

MIC_NAME = "mi mesa (High Definition Audio Device)"
OTHER_NAME = "Microfone (S200)"
ORIGINAL_CONFIG = "Preamp: -6 dB\r\nFilter: ON PK Fc 100 Hz Gain 3 dB Q 1\r\nInclude: outro.txt\r\n"


def pump(app, condition, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        app.update()
        if condition():
            return True
        time.sleep(0.005)
    return False


def settle(app, seconds=0.0):
    pump(app, lambda: app._apply_job is None)
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.005)
    app.update()


def limpa_som_profile() -> eq.Profile:
    bands = [eq.Band.new("HP", 80.0, why="Corta o ronco e a vibração abaixo de 80 Hz."),
             eq.Band.new("NO", 60.0, q=30.0, why="Zumbido da rede elétrica: 60 Hz."),
             eq.Band.new("NO", 120.0, q=30.0, why="Zumbido da rede elétrica: 120 Hz."),
             eq.Band.new("PK", 143.0, -4.2, 1.2, why="Baixa 4.2 dB em volta de 143 Hz."),
             eq.Band.new("NO", 12006.0, q=50.0, why="Apito de interferência em 12006 Hz."),
             eq.Band.new("LP", 12000.0, q=0.541, why="Corta tudo acima de 12000 Hz."),
             eq.Band.new("LP", 12000.0, q=1.307, why="Corta tudo acima de 12000 Hz.")]
    return eq.Profile(enabled=True, preamp_db=6.0, bands=bands, rnnoise=1, rnnoise_gate=0.45,
                      summary="Criado pelo Limpa Som a partir do seu teste (zumbido de 60 Hz).").clamp()


def make_apo(root: Path) -> realtime.ApoInfo:
    config = root / "config"
    config.mkdir(parents=True)
    (root / "VSTPlugins").mkdir()
    (config / "config.txt").write_bytes(ORIGINAL_CONFIG.encode("utf-8"))
    return realtime.ApoInfo(root, config, True, True, None)


class Ev:
    """Evento de mouse de mentira."""

    def __init__(self, x=0, y=0, delta=0):
        self.x, self.y, self.delta = x, y, delta


def main() -> None:
    mode = sys.argv[1]
    work = Path(tempfile.mkdtemp(prefix="limpa_som_eq_"))
    out_dir = work / "resultados"
    out_dir.mkdir()
    apo = None if mode == "sem_apo" else make_apo(work / "apo")
    config_file = (apo.config_dir / "config.txt") if apo else None
    mic_file = (apo.config_dir / realtime.MIC_FILE) if apo else None
    original = limpa_som_profile()
    if mode == "antigo":
        (out_dir / eq.STORE_NAME).write_text(json.dumps({MIC_NAME: {
            "enabled": False, "highpass_hz": 200.0, "bass_db": -4.0, "presence_db": 7.0, "air_db": -15.0,
            "remove_hum": True, "remove_tones": True, "rnnoise": True, "hum_f0": 60.0, "hum_harmonics": 1,
            "tones": [12005.9]}}), encoding="utf-8")
    elif mode != "sem_perfil":
        eq.save_stored(out_dir, MIC_NAME, original, limpa_som=original)

    answers = {"confirm": True}
    asked, infos, opened = [], [], []
    attached = {"value": mode == "ligado"}
    mics = [equalizador.MicChoice(MIC_NAME + "  (padrão)", MIC_NAME, None, None)]
    if mode == "trocar":
        mics.append(equalizador.MicChoice(OTHER_NAME, OTHER_NAME, None, None))
    app = equalizador.EqualizerApp(
        apo, mics, 0, out_dir, attached_fn=lambda guid: attached["value"],
        confirm_fn=lambda text: (asked.append(text), answers["confirm"])[1], info_fn=lambda text: infos.append(text),
        open_main_fn=lambda: opened.append(1))
    app.withdraw()
    app.update()
    r = {"mode": mode}

    def read(path):
        return path.read_bytes().decode("utf-8") if path is not None and path.exists() else None

    def lines():
        return (read(mic_file) or "").splitlines()

    def stored():
        return eq.load_stored(out_dir, MIC_NAME)

    r["start"] = {
        "rows": len(app.rows), "kinds": [row["kind"].get() for row in app.rows],
        "freqs": [row["freq"].get() for row in app.rows], "count": app.count_label.cget("text"),
        "why": [row["why"].cget("text") for row in app.rows], "enabled": app.profile.enabled,
        "power": app.power.cget("text"), "preamp": app.preamp_label.cget("text"), "rnn": app.rnn_var.get(),
        "gate": app.gate_label.cget("text"), "origin": app.origin_label.cget("text"),
        "reset_disabled": app.reset_btn.instate(["disabled"]),
        "gain_disabled": [str(row["spins"][1].cget("state")) for row in app.rows],
        "config_untouched": read(config_file) == ORIGINAL_CONFIG if apo else None,
        "mic_file": mic_file.exists() if apo else None,
        "dots": {k: v[0].cget("fg") for k, v in app.dots.items()},
        "banner": app.banner.winfo_manager(), "banner_text": app.banner_text.cget("text")[:44],
        "points_ok": all(0 <= app.point_xy(i)[0] <= 2000 and 0 <= app.point_xy(i)[1] <= 2000
                         for i in range(len(app.rows))),
        "ai_hint": app.ai_hint.cget("text")[:30],
    }

    if mode == "editar":
        log = {}
        pk = next(i for i, b in enumerate(app.profile.bands) if b.kind == "PK")
        app.set_band(pk, gain=-6.0, freq=200.0)
        settle(app, 0.5)
        log["edit1"] = {"asked": len(asked), "installed": realtime.is_installed(read(config_file)),
                        "backup": (apo.config_dir / realtime.BACKUP_NAME).exists(),
                        "gain_line": "Filter: ON PK Fc 200 Hz Gain -6.0 dB Q 1.20" in lines(),
                        "stored_gain": next(b.gain for b in stored().atual.bands if b.kind == "PK"),
                        "original_gain": next(b.gain for b in stored().limpa_som.bands if b.kind == "PK"),
                        "keeps_original_lines": "Preamp: -6 dB" in read(config_file),
                        "preamp_line": "Preamp: 6.0 dB" in lines(),
                        "row_gain": app.rows[pk]["gain"].get(), "row_freq": app.rows[pk]["freq"].get()}
        n = len(app.rows)
        app.add_band("PK")
        settle(app, 0.5)
        log["add"] = {"rows": len(app.rows) - n, "selected": app.selected, "asked": len(asked),
                      "line": "Filter: ON PK Fc 1000 Hz Gain -3.0 dB Q 1.00" in lines(),
                      "count": app.count_label.cget("text")}
        k = len(app.rows) - 1                                  # mexer pela tabela, como a pessoa faria
        app.rows[k]["freq"].set("2500,5")
        app.rows[k]["gain"].set("-2,5")
        app.rows[k]["q"].set("2")
        app._row_edited(k)
        log["table"] = {"freq": app.profile.bands[k].freq, "gain": app.profile.bands[k].gain,
                        "q": app.profile.bands[k].q, "row_freq": app.rows[k]["freq"].get()}
        app.rows[k]["kind"].set("Notch")
        app._row_edited(k, kind_changed=True)
        b = app.profile.bands[k]
        log["kind"] = {"kind": b.kind, "gain": b.gain, "q": b.q,
                       "gain_state": str(app.rows[k]["spins"][1].cget("state")), "why": b.why}
        app.rows[k]["freq"].set("lixo")                        # texto inválido não quebra: fica o valor anterior
        app._row_edited(k)
        log["garbage"] = {"freq": app.profile.bands[k].freq, "errors": len(app.errors)}
        settle(app, 0.5)
        off = next(i for i, b in enumerate(app.profile.bands) if b.kind == "NO" and abs(b.freq - 120) < 1)
        app.rows[off]["on"].set(False)
        app._row_edited(off)
        settle(app, 0.5)
        log["disable_band"] = {"gone": not any("Fc 120 Hz" in l for l in lines()),
                               "still_listed": len(app.profile.bands) == len(app.rows)}
        app.remove_band(0)                                      # o corta-graves
        settle(app, 0.5)
        log["remove"] = {"hp_gone": not any("HPQ Fc 80" in l for l in lines()), "rows": len(app.rows),
                         "same_as_bands": len(app.rows) == len(app.profile.bands)}
        app.preamp_var.set(9.5)
        app._preamp_changed()
        app.rnn_var.set(2)
        app.gate_var.set(0.7)
        app._rnn_changed()
        settle(app, 0.5)
        text = read(mic_file)
        log["ai"] = {"preamp": "Preamp: 9.5 dB" in lines(), "plugins": text.count("rnnoise_mono.dll"),
                     "gate_chunk": eq.rnnoise_chunk(0.7) in text, "gate_label": app.gate_label.cget("text"),
                     "preamp_label": app.preamp_label.cget("text")}
        app.toggle_power()
        settle(app)
        log["off"] = {"disabled_text": "DESLIGADO" in (read(mic_file) or ""),
                      "no_filters": "Filter:" not in read(mic_file),
                      "stored_enabled": stored().atual.enabled, "power": app.power.cget("text"),
                      "active_dot": app.dots["active"][0].cget("fg")}
        answers["confirm"] = False
        before = len(app.profile.bands)
        app.clear_bands()
        log["clear_declined"] = {"bands": len(app.profile.bands) == before, "asked_last": "Apagar todos" in asked[-1]}
        answers["confirm"] = True
        app.clear_bands()
        log["clear"] = {"bands": len(app.profile.bands), "rows": len(app.rows)}
        app.reset_to_original()
        settle(app, 0.5)
        got = [(b.kind, round(b.freq)) for b in app.profile.bands]
        want = sorted([(b.kind, round(b.freq)) for b in original.bands], key=lambda t: (t[0] != "HP", t[1]))
        log["reset"] = {"same": got == want, "enabled_kept": app.profile.enabled, "rows": len(app.rows),
                        "message": app.message.cget("text"), "preamp": app.profile.preamp_db,
                        "rnn": app.profile.rnnoise,
                        "stored_gain": next(b.gain for b in stored().atual.bands if b.kind == "PK")}
        log["total_confirms"] = len(asked)
        r["editar"] = log

    elif mode == "arrastar":
        log = {}
        pk = next(i for i, b in enumerate(app.profile.bands) if b.kind == "PK")
        app.drag_point(pk, app._x_of(500.0), app._y_of(-9.0))
        b = app.profile.bands[pk]
        log["pk"] = {"freq": b.freq, "gain": b.gain}
        no = next(i for i, b in enumerate(app.profile.bands) if b.kind == "NO" and abs(b.freq - 60) < 1)
        app.drag_point(no, app._x_of(50.0), app._y_of(10.0))
        b = app.profile.bands[no]
        log["notch"] = {"freq": b.freq, "gain": b.gain}                  # notch não tem ganho: só anda de lado
        hp = next(i for i, b in enumerate(app.profile.bands) if b.kind == "HP")
        app.drag_point(hp, app._x_of(100.0), app._y_of(0.0))
        log["hp"] = app.profile.bands[hp].freq
        app.drag_point(pk, app._x_of(100000.0), app._y_of(99.0))         # fora da tela: fica nos limites
        b = app.profile.bands[pk]
        log["limits"] = {"freq": b.freq, "gain": b.gain}
        x, y = app.point_xy(no)                                           # clicar no ponto escolhe esse filtro
        app._curve_press(Ev(x + 3, y - 2))
        log["picked"] = {"selected": app.selected == no, "dragging": app.drag == no,
                         "row_highlight": str(app.rows[no]["frame"].cget("highlightbackground"))}
        app._curve_motion(Ev(app._x_of(70.0), y))
        app._curve_release(Ev())
        log["moved"] = {"freq": app.profile.bands[no].freq, "dragging": app.drag}
        q0 = app.profile.bands[no].q
        app._curve_wheel(Ev(delta=120))
        up = app.profile.bands[no].q
        app._curve_wheel(Ev(delta=-120))
        log["wheel"] = {"up": round(up / q0, 3), "back": round(app.profile.bands[no].q / q0, 3)}
        app._curve_press(Ev(5, 5))                                        # longe de qualquer ponto
        app._curve_motion(Ev(50, 50))
        log["far"] = {"selected": app.selected, "dragging": app.drag,
                      "no_change": abs(app.profile.bands[no].freq - 70.0) < 1.0}
        log["rows_follow"] = app.rows[no]["freq"].get()
        r["arrastar"] = log

    elif mode == "sem_perfil":
        app.reset_to_original()
        r["empty"] = {"infos": list(infos), "profile_bands": len(app.profile.bands), "stored": stored() is not None}
        app.add_band("HS")
        settle(app)
        r["empty"]["after_add_stored"] = stored() is not None and stored().limpa_som is None
        r["empty"]["asked"] = len(asked)                                 # desligado: não pede para instalar nada

    elif mode == "sem_apo":
        app.set_band(0, freq=100.0)
        settle(app, 0.5)
        r["no_apo"] = {"message": app.message.cget("text"), "asked": len(asked),
                       "banner_text": app.banner_text.cget("text")[:40],
                       "buttons_hidden": app.banner_buttons.winfo_manager() == "",
                       "stored_freq": stored().atual.bands[0].freq}
        app.undo_all()

    elif mode == "ligado":
        r["attached"] = {"banner": app.banner.winfo_manager(), "dot": app.dots["attached"][0].cget("fg")}
        attached["value"] = False
        app.refresh_status()
        r["lost"] = {"banner": app.banner.winfo_manager(), "dot": app.dots["attached"][0].cget("fg")}
        app.open_main_fn()
        r["opened"] = len(opened)

    elif mode == "trocar":
        app.set_band(0, freq=90.0)                                       # instala (confirma) e escreve
        settle(app, 0.5)
        first = read(mic_file)
        app.mic_var.set(OTHER_NAME)
        app._mic_changed()
        settle(app, 0.5)
        second = read(mic_file)
        log = {"first_device": "Device: mi mesa High Definition Audio Device" in first,
               "second_device": "Device: Microfone S200" in second, "empty": len(app.profile.bands),
               "origin": app.origin_label.cget("text")[:30], "reset_disabled": app.reset_btn.instate(["disabled"])}
        app.mic_var.set(MIC_NAME + "  (padrão)")
        app._mic_changed()
        settle(app, 0.5)
        log["back"] = {"rows": len(app.rows), "freq0": app.profile.bands[0].freq,
                       "device": "Device: mi mesa High Definition Audio Device" in read(mic_file)}
        r["switch"] = log

    r["errors"] = app.errors
    app.destroy()
    r["asked"] = list(asked)
    print("JSON:" + json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
