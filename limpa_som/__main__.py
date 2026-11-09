"""Ponto de entrada: python -m limpa_som  (abre a janela; --console usa o modo texto)"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import __version__, ui, winaudio, wizard
from .findings import DeviceInfo

ROOT = Path(__file__).resolve().parent.parent


def _read_name(cli_name: Optional[str]) -> str:
    if cli_name:
        return cli_name
    try:
        return json.loads((ROOT / "config.json").read_text(encoding="utf-8")).get("nome", "Daniel")
    except Exception:
        return "Daniel"


def parse(argv: Optional[List[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="limpa_som", description="Diagnostica e melhora o áudio do seu microfone.")
    p.add_argument("--nome", help="como o programa deve te chamar (padrão: Daniel, ou o de config.json)")
    p.add_argument("--dispositivo", help="número ou parte do nome do microfone a usar")
    p.add_argument("--saida", help="pasta dos resultados (padrão: resultados/ ao lado do programa)")
    p.add_argument("--sem-ajuste", action="store_true", help="só mede; não muda nenhuma configuração do Windows")
    p.add_argument("--nao-abrir", action="store_true", help="não abre o relatório no navegador ao final")
    p.add_argument("--sem-cor", action="store_true")
    p.add_argument("--console", action="store_true", help="usa o modo texto em vez da janela")
    p.add_argument("--acao", choices=["teste", "ao-vivo", "arquivo", "tempo-real"],
                   help="modo texto: pula o menu e executa direto")
    p.add_argument("--simular", nargs="?", const="lista", metavar="CENARIO",
                   help="usa um microfone de mentira (demonstração/testes); sem valor lista os cenários")
    p.add_argument("--velocidade", type=float, default=8.0, help="velocidade da simulação (padrão 8x)")
    p.add_argument("--autoteste", action="store_true", help="verifica se a instalação está funcionando")
    p.add_argument("--equalizador", action="store_true",
                   help="abre só o Equalizador ao vivo (o segundo programa, que filtra o microfone em segundo plano)")
    p.add_argument("--captura", metavar="PASTA",
                   help="(desenvolvimento, só com --simular) roda a tarefa na janela e salva imagens dela")
    p.add_argument("--captura-tarefa", choices=["teste", "live", "inicio"], default="teste",
                   help=argparse.SUPPRESS)
    return p.parse_args(argv)


def build_sim_session(args, name: str, out_dir: Path) -> wizard.Session:
    from .simulate import SCENARIOS, SimBackend

    if args.simular == "lista" or args.simular not in SCENARIOS:
        ui.say("Cenários de simulação disponíveis:", "bold")
        for key, sc in SCENARIOS.items():
            ui.say(f"  {key:<20} {sc.title}")
        raise SystemExit(0 if args.simular == "lista" else 2)
    backend = SimBackend(SCENARIOS[args.simular], speed=args.velocidade)
    ui.sleep_fn = lambda s: time.sleep(s / max(args.velocidade, 1.0)) if args.velocidade < 1e6 else None
    return backend.make_session(name, out_dir, allow_changes=not args.sem_ajuste, open_report=not args.nao_abrir)


def pick_device(args, devices) -> int:
    default_i = next((i for i, d in enumerate(devices) if d.is_default), 0)
    if args.dispositivo:
        key = args.dispositivo.lower()
        match = [i for i, d in enumerate(devices) if key == str(d.index) or key in d.name.lower()]
        if not match:
            ui.bad(f"Não achei nenhum microfone com '{args.dispositivo}'.")
            raise SystemExit(1)
        return match[0]
    return default_i


def build_real_session(args, name: str, out_dir: Path) -> wizard.Session:
    from .devices import list_input_devices

    try:
        devices = list_input_devices()
    except Exception as exc:
        ui.bad(f"Não consegui listar os microfones: {exc}")
        raise SystemExit(1)
    if not devices:
        ui.bad("Nenhum microfone encontrado. Conecte um microfone e confira Configurações > Som > Entrada.")
        raise SystemExit(1)
    if args.dispositivo or len(devices) == 1:
        idx = pick_device(args, devices) if args.dispositivo else 0
    else:
        default_i = next((i for i, d in enumerate(devices) if d.is_default), 0)
        ui.title("QUAL MICROFONE TESTAR?")
        labels = [f"{d.name}" + ("   <- padrão do Windows" if d.is_default else "") for d in devices]
        idx = ui.ask_choice("Microfones encontrados:", labels, default=default_i + 1, eof=default_i)
    dev = devices[idx]
    sess = wizard.session_for_device(dev, name, out_dir, not args.sem_ajuste, not args.nao_abrir)
    if sess.control is None and winaudio.AVAILABLE:
        ui.warn("Não consegui controlar o ganho deste microfone pelo Windows: vou só medir e orientar.")
    return sess


def run_gui(args, name: str, out_dir: Path) -> int:
    try:
        from . import gui
    except Exception as exc:  # sem tkinter: cai para o modo texto
        ui.warn(f"Não consegui abrir a janela ({exc}). Usando o modo texto.")
        return run_console(args, name, out_dir)
    snap_dir = Path(args.captura) if args.captura else None
    only_home = args.captura_tarefa == "inicio"          # só tira foto da tela inicial: não clica em nada
    if snap_dir is not None and not args.simular and not only_home:
        ui.bad("--captura aperta os botões sozinho e só pode ser usado com --simular (microfone de mentira).")
        return 2
    if args.simular:
        sess = build_sim_session(args, name, out_dir)
        sess.open_report = False if snap_dir else sess.open_report
        labels, default_i, fixed = [sess.info.name], 0, True

        def build(_i: int) -> wizard.Session:
            return sess
    else:
        from .devices import list_input_devices
        devices = list_input_devices()
        if not devices:
            ui.bad("Nenhum microfone encontrado. Conecte um microfone e confira Configurações > Som > Entrada.")
            _alert("Nenhum microfone encontrado.\nConecte um microfone e confira Configurações > Som > Entrada.")
            return 1
        labels = [d.name + ("  (padrão)" if d.is_default else "") for d in devices]
        default_i, fixed = pick_device(args, devices), False

        def build(i: int) -> wizard.Session:
            return wizard.session_for_device(devices[i], name, out_dir, not args.sem_ajuste,
                                             not args.nao_abrir and snap_dir is None)
    app = gui.App(labels, default_i, build, fixed_device=fixed,
                  auto_ms=400 if (snap_dir and not only_home) else None, snap_dir=snap_dir,
                  autorun=args.captura_tarefa if (snap_dir and not only_home) else None, out_dir=out_dir)
    return gui.run(app)


def _alert(message: str) -> None:
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("Limpa Som", message)
        root.destroy()
    except Exception:
        pass


def run_console(args, name: str, out_dir: Path) -> int:
    sess = build_sim_session(args, name, out_dir) if args.simular else build_real_session(args, name, out_dir)
    if args.acao == "teste":
        wizard.full_test(sess)
    elif args.acao == "ao-vivo":
        wizard.live_meter(sess)
    elif args.acao == "arquivo":
        wizard.clean_file_flow(sess)
    elif args.acao == "tempo-real":
        wizard.realtime_flow(sess)
    else:
        wizard.main_menu(sess)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse(argv)
    ui.init(color=not args.sem_cor)
    if args.autoteste:
        from . import selftest
        return selftest.run()
    if args.equalizador:
        from . import equalizador
        forward = (["--dispositivo", args.dispositivo] if args.dispositivo else []) + (
            ["--saida", args.saida] if args.saida else [])
        return equalizador.main(forward)
    name = _read_name(args.nome)
    out_dir = Path(args.saida) if args.saida else ROOT / "resultados"
    out_dir.mkdir(parents=True, exist_ok=True)
    use_console = args.console or bool(args.acao)
    try:
        return run_console(args, name, out_dir) if use_console else run_gui(args, name, out_dir)
    except KeyboardInterrupt:
        ui.say()
        ui.warn("Interrompido. Se o programa tinha mudado o ganho do microfone no meio do teste, use "
                "'Restaurar as configurações originais'.")
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # nunca morre em silêncio: registra e avisa
        log = wizard.log_error(out_dir, exc)
        message = f"O programa encontrou um erro inesperado:\n{type(exc).__name__}: {exc}\n\nDetalhes em {log}"
        ui.bad(message)
        _alert(message)
        return 1


if __name__ == "__main__":
    sys.exit(main())
