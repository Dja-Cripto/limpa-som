"""Roda a janela do programa num processo NOVO e imprime um resumo em JSON.

Os testes da janela usam este script em vez de criar várias janelas Tk dentro do pytest: criar e
destruir Tk repetidas vezes no mesmo processo do pytest (que redireciona a saída) falha de forma
intermitente, e o programa de verdade sempre abre a janela em um processo novo.

Uso: python tests/gui_driver.py CENARIO MODO     (MODO: teste | live | guarda | cancelar | botoes)
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from limpa_som import gui, ui, wizard  # noqa: E402
from limpa_som.findings import DeviceInfo  # noqa: E402
from limpa_som.simulate import SCENARIOS, SimBackend  # noqa: E402


def pump(app, condition, timeout=120.0):
    end = time.time() + timeout
    while time.time() < end:
        app.update()
        if condition():
            return True
        time.sleep(0.005)
    return False


def main() -> None:
    scenario, mode = sys.argv[1], sys.argv[2]
    ui.sleep_fn = lambda s: None
    out_dir = Path(tempfile.mkdtemp(prefix="limpa_som_gui_"))
    backend = SimBackend(SCENARIOS[scenario], speed=1e9)
    sc = backend.scenario
    info = DeviceInfo(name=backend.device.name, connection=sc.connection, chip=sc.chip, simulated=True)
    sess = wizard.Session(name="Daniel", recorder=backend.recorder, control=backend.control, info=info,
                          out_dir=out_dir, allow_changes=True, open_report=False)
    manual = mode in ("cancelar", "botoes")
    app = gui.App([info.name], 0, lambda i: sess, fixed_device=True, auto_ms=None if manual else 2,
                  out_dir=out_dir)
    app.withdraw()
    result = {"mode": mode, "ready": pump(app, lambda: app.sess is not None and not app.busy)}
    start = backend.control.get_settings()

    if mode in ("teste", "live"):
        app.start(mode)
        result["started"] = app.busy
        result["finished"] = pump(app, lambda: not app.busy)
        if mode == "teste":
            result["views"] = {"resultados": app.results.winfo_manager(), "log": app.logbox.winfo_manager(),
                               "guia": app.guide_card.winfo_manager()}
            result["cards"] = len(app.results.inner.winfo_children())
            result["chips_done"] = sum(1 for c in app.chips.values() if "✓" in c.cget("text"))
            result["current_chip"] = [k for k, c in app.chips.items() if c.cget("bg") == "#2563eb"]
            app.toggle_details()
            result["views_details"] = {"resultados": app.results.winfo_manager(), "log": app.logbox.winfo_manager()}
            app.toggle_details()
            result["views_back"] = {"resultados": app.results.winfo_manager(), "log": app.logbox.winfo_manager()}
    elif mode == "guarda":
        for key in ("tempo", "restaurar"):
            app.start(key)
            result[f"busy_{key}"] = app.busy
    elif mode == "cancelar":
        app.start("teste")
        result["prompt"] = pump(app, lambda: app.current_prompt is not None)
        app.backend.cancelled = True            # o que acontece ao fechar a janela no meio da tarefa
        app.current_prompt.event.set()
        result["finished"] = pump(app, lambda: not app.busy)
    elif mode == "botoes":
        app.start("teste")
        result["prompt"] = pump(app, lambda: app.current_prompt is not None)
        buttons = list(app.prompt_buttons.winfo_children())
        result["button_texts"] = [b.cget("text") for b in buttons]
        buttons[0].invoke()                     # clique de verdade
        result["advanced"] = pump(app, lambda: app.current_prompt is None or app.current_prompt.kind != "enter"
                                  or app.stage.cget("text") != "")
        app.backend.cancelled = True
        if app.current_prompt is not None:
            app.current_prompt.event.set()
        result["finished"] = pump(app, lambda: not app.busy)

    now = backend.control.get_settings()
    result.update({
        "errors": app.errors,
        "log": app.log.get("1.0", "end"),
        "tiles": len(app.tiles_frame.winfo_children()),
        "report": app.report_path,
        "report_exists": bool(app.report_path) and Path(app.report_path).exists(),
        "status": app.status.cget("text"),
        "back_enabled": str(app.back_btn.cget("state")) != "disabled",
        "live_updates": app._live_updates,
        "start_boost": start.boost_db, "start_volume": start.volume_db,
        "boost": now.boost_db, "volume": now.volume_db,
        "erro_log": (out_dir / "erro.log").exists(),
        "last_ok": sess.last is not None and sess.last.after is not None,
    })
    app.destroy()
    print("JSON:" + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
