"""Diagnóstico em linguagem simples: o que eu vi, por que costuma acontecer e o que fazer."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .evidence import ChainEvidence, chain_evidence, tone_comb
from .findings import Context, Verdict, num

WIRING_OPTIONS = [
    ("direto", "Direto no computador (plugue de microfone ou P2/P3)"),
    ("mesa", "Por uma mesa de som / mixer"),
    ("interface", "Por uma interface de áudio USB ou pré-amplificador"),
    ("usb_mic", "É um microfone USB"),
    ("headset", "Fone com microfone, headset ou webcam"),
    (None, "Não sei / pular"),
]
WIRING_NAMES = {key: label for key, label in WIRING_OPTIONS if key}


@dataclass
class PlainItem:
    key: str
    title: str
    saw: str
    why: str
    todo: List[str]
    severity: int = 2


@dataclass
class PlainReport:
    headline: str
    items: List[PlainItem] = field(default_factory=list)
    first_steps: List[str] = field(default_factory=list)
    free_fix: str = ""


def _tone_list(ctx: Context) -> List[float]:
    return [t.freq for t in ctx.final.noise.tones if t.prominence_db >= 15.0 and t.freq >= 300.0]


def _signal_item(ctx: Context, ev: ChainEvidence) -> Optional[PlainItem]:
    snr = ev.snr_db
    if snr is None or snr >= 38.0:
        return None
    wiring = ctx.wiring
    saw = (f"A sua voz fica só {num(snr, 0)} dB acima do ruído (para soar limpo, o ideal é passar de 40 dB).")
    if ev.gain_cannot_help:
        saw += (f" Testei {ev.tested} combinações de ganho no computador e em todas a diferença voz/ruído ficou "
                "praticamente igual: o ruído entra junto com a sua voz, antes de o computador amplificar. "
                "Por isso mexer no volume ou no reforço do Windows não adianta.")
    if ev.weak_signal:
        saw += (f" Além disso, a voz chega fraca na entrada: o computador precisa somar cerca de "
                f"{num(ev.needed_gain_db, 0)} dB de ganho só para ela soar normal, e esse ganho aumenta o ruído "
                "da entrada junto.")
    if wiring == "mesa":
        why = ("Com mesa de som, normalmente é a mesa mandando pouco sinal para o computador. Quanto menos sinal "
               "sai da mesa, mais o computador precisa amplificar, e amplifica o ruído junto.")
        todo = ["Na mesa, suba o ganho (GAIN) do canal do microfone até o medidor chegar no verde/amarelo quando "
                "você fala, sem encostar no vermelho.",
                "Suba também o fader do canal e o MASTER/Main até a saída ficar perto de 0 dB.",
                "Confirme que o cabo sai da saída principal (Main Out), e não da saída de fone ou de Aux.",
                "Abra 'Ajuste ao vivo', fale e veja a relação voz/ruído subir. Depois diminua o reforço do "
                "Windows (0 ou +10 dB), porque agora a mesa já entrega sinal forte."]
    elif wiring == "direto":
        why = ("Microfone ligado direto na placa-mãe é a causa mais comum de chiado: a entrada de microfone da "
               "placa tem um pré-amplificador fraco, que precisa amplificar muito e capta o ruído do próprio "
               "gabinete.")
        todo = ["Aproxime o microfone da boca (8 a 12 cm): cada vez que a distância dobra, a voz perde 6 dB em "
                "relação ao mesmo ruído.",
                "O que resolve de vez é amplificar a voz ANTES da placa: uma interface de áudio USB ou um "
                "pré-amplificador (a partir de uns R$ 250) ou um microfone USB.",
                "Enquanto isso, use o Equalizador ao vivo: ele tira o zumbido e os apitos em todos os programas."]
    elif wiring in ("interface", "usb_mic"):
        why = ("Com interface ou microfone USB o ruído costuma vir de baixo ganho no equipamento (botão de ganho "
               "baixo), de cabo ruim ou de porta USB com interferência.")
        todo = ["Suba o botão de ganho da interface/microfone até o medidor ficar no verde/amarelo quando você fala.",
                "Teste outra porta USB (traseira, direto na placa-mãe, sem hub) e outro cabo.",
                "Aproxime o microfone da boca (8 a 12 cm)."]
    elif wiring == "headset":
        why = ("Microfones de fone, headset e webcam são pequenos e simples: o ruído é grande para a voz que "
               "captam.")
        todo = ["Aproxime o microfone da boca.", "Use o Equalizador ao vivo para tirar o chiado em todos os "
                "programas.", "Para qualidade de gravação, um microfone de verdade (USB) faz muita diferença."]
    else:
        why = ("Isso costuma acontecer de dois jeitos: (1) com mesa de som, a mesa manda pouco sinal; (2) com "
               "microfone ligado direto, a entrada de microfone da placa-mãe é fraca. Me diga como o seu está "
               "ligado (botão 'Como meu microfone está ligado?') para eu dar o conselho certo.")
        todo = ["Se tem mesa: suba o ganho do canal e o master dela até a voz ficar forte, sem estourar.",
                "Se é direto na placa: aproxime o microfone e considere uma interface USB.",
                "Aproxime o microfone da boca (8 a 12 cm)."]
    return PlainItem("sinal", "O ruído está muito perto da sua voz", saw, why, todo, 3 if snr < 25 else 2)


def _hum_item(ctx: Context) -> Optional[PlainItem]:
    hum = ctx.final.noise.hum
    if hum is None or hum.prominence_db < 8.0:
        return None
    share = ctx.final.noise.hum_share
    saw = (f"Um som grave constante em {num(hum.f0, 0)} Hz, com {hum.harmonics} harmônicos"
           + (f", que concentra {num(100 * share, 0)}% da energia do ruído." if share >= 0.2 else "."))
    why = ("Normalmente é 'loop de terra': computador e mesa/microfone ligados em tomadas ou réguas diferentes, "
           "cabo sem blindagem, ou cabo passando perto de fonte, transformador ou carregador.")
    todo = ["Ligue o computador e a mesa na MESMA régua de tomadas.",
            "Um isolador de loop de terra (uns R$ 30 a 60) entre a mesa e o computador costuma acabar com o zumbido.",
            "Afaste os cabos de áudio dos cabos de energia e use cabos curtos.",
            "Sem comprar nada: o Equalizador ao vivo remove o zumbido de todos os programas."]
    return PlainItem("zumbido", f"Zumbido grave da rede elétrica ({num(hum.f0, 0)} Hz)", saw, why, todo,
                     3 if hum.prominence_db >= 18 else 2)


def _tones_item(ctx: Context) -> Optional[PlainItem]:
    freqs = _tone_list(ctx)
    if not freqs:
        return None
    shown = ", ".join(f"{f / 1000:.1f} kHz".replace(".", ",") for f in sorted(freqs)[:6])
    comb = tone_comb(freqs)
    saw = f"Apitos agudos em {shown}."
    if comb:
        saw += f" Eles se repetem de {num(comb, 0)} em {num(comb, 0)} Hz: é o padrão de ruído digital de chaveamento."
    why = ("Normalmente é interferência digital do próprio computador: placa de vídeo, USB e fonte geram ruído que "
           "entra pela entrada analógica da placa de som. É comum em entradas de microfone de placa-mãe, e é "
           "isso que muita gente percebe como 'chiado' fino.")
    todo = ["Se a mesa tiver saída USB, use-a como placa de som em vez da entrada analógica.",
            "Use a entrada traseira da placa-mãe (a do painel frontal capta mais ruído do gabinete) e um cabo curto.",
            "Se o apito muda quando a placa de vídeo trabalha (jogo, vídeo), é ela: só uma interface USB isola.",
            "Sem comprar nada: o Equalizador ao vivo corta esses apitos de todos os programas."]
    return PlainItem("apitos", "Apitos agudos de interferência", saw, why, todo, 2)


def _alternative_item(ctx: Context, ev: ChainEvidence) -> Optional[PlainItem]:
    """O mesmo microfone por outro caminho (USB da mesa/interface) costuma evitar zumbido e apitos."""
    n = ctx.final.noise
    if ctx.device.connection != "analogica" or not ctx.alternatives:
        return None
    if not (n.hum is not None or n.tones or (ev.snr_db is not None and ev.snr_db < 38.0)):
        return None
    names = ", ".join(ctx.alternatives)
    return PlainItem(
        "alternativa", "Você tem outro caminho para o mesmo microfone: o USB",
        f"Encontrei outros microfones USB no computador: {names}.",
        "Se algum deles é a sua mesa ou interface, ele manda o som já digitalizado por dentro dela, sem passar pelo "
        "cabo analógico nem pela entrada de microfone da placa-mãe, que é de onde costumam vir o zumbido de 60 Hz e "
        "os apitos.",
        ["Na caixa 'Microfone', no alto do programa, escolha esse microfone e rode o teste de novo.",
         "No Windows, deixe-o como microfone padrão e tire o cabo P2 que liga a mesa à placa-mãe.",
         "Nos programas (Discord, OBS, Zoom), escolha esse mesmo microfone."], 4)


def _origin_items(ctx: Context, verdict: Verdict) -> List[PlainItem]:
    """De onde vem o ruído, segundo o teste de isolamento da fonte."""
    by_key = {f.key: f for f in verdict.findings}
    iso = ctx.isolation
    drop = (ctx.final.noise.rms_a_db - iso.rms_a_db) if iso is not None else None
    out: List[PlainItem] = []
    if "ruido_fonte" in by_key:
        f = by_key["ruido_fonte"]
        out.append(PlainItem(
            "origem_fora", "O ruído vem de antes do computador",
            f"Quando silenciei a fonte (canal da mesa zerado ou microfone coberto), o ruído caiu "
            f"{num(drop or 0, 0)} dB: ele estava vindo dela.",
            "Normalmente é o ganho do canal da mesa alto demais (a mesa chia quando o ganho está no máximo), um "
            "cabo ruim ou barulho do ambiente (ventilador, ar-condicionado).", f.actions, f.severity))
    if "ruido_entrada" in by_key:
        f = by_key["ruido_entrada"]
        out.append(PlainItem(
            "origem_entrada", "O ruído nasce na entrada de áudio do computador",
            "Mesmo silenciando a fonte, o ruído continuou igual: ele já está dentro da entrada de áudio.",
            "Normalmente é a entrada de microfone da placa-mãe: o pré-amplificador dela é simples e capta ruído "
            "elétrico do gabinete (placa de vídeo, fonte, USB).", f.actions, f.severity))
    if "ruido_misto" in by_key:
        f = by_key["ruido_misto"]
        out.append(PlainItem(
            "origem_mista", "O ruído vem de dois lugares",
            f"Silenciar a fonte reduziu o ruído só {num(drop or 0, 0)} dB.",
            "Uma parte vem da fonte (mesa, cabo ou sala) e outra da entrada do computador.", f.actions,
            f.severity))
    return out


def _generic_items(ctx: Context, verdict: Verdict) -> List[PlainItem]:
    out: List[PlainItem] = []
    by_key = {f.key: f for f in verdict.findings}
    if "efeitos_windows" in by_key:
        f = by_key["efeitos_windows"]
        out.append(PlainItem("efeitos", "O Windows está acrescentando chiado", f.detail,
                             "Os 'efeitos de áudio' do Windows ou do driver (ganho automático, supressão de ruído) "
                             "às vezes pioram o chiado em vez de melhorar.", f.actions, 3))
    if "estouro" in by_key:
        f = by_key["estouro"]
        out.append(PlainItem("estouro", "A voz está estourando (distorcendo)", f.detail,
                             "Normalmente o ganho está alto demais, na mesa ou no Windows.", f.actions, 3))
    if "agc" in by_key:
        f = by_key["agc"]
        out.append(PlainItem("agc", "O volume do microfone muda sozinho", f.title,
                             "Algum programa ou o driver aumenta o ganho quando você fica quieto.", f.actions, 2))
    if "banda" in by_key or "banda_bluetooth" in by_key:
        f = by_key.get("banda") or by_key["banda_bluetooth"]
        out.append(PlainItem("banda", "O som está sem agudos", f.title, f.detail, f.actions, 2))
    if "voz_baixa" in by_key and not any(i.key == "sinal" for i in out):
        f = by_key["voz_baixa"]
        out.append(PlainItem("voz_baixa", "Sua voz está baixa", f.title, f.detail, f.actions, 2))
    return out


def explain(ctx: Context, verdict: Verdict) -> PlainReport:
    ev = chain_evidence(ctx)
    items: List[PlainItem] = []
    for item in (_alternative_item(ctx, ev), _signal_item(ctx, ev), _hum_item(ctx), _tones_item(ctx)):
        if item is not None:
            items.append(item)
    items.extend(_origin_items(ctx, verdict))
    items.extend(_generic_items(ctx, verdict))
    items.sort(key=lambda i: -i.severity)

    first: List[str] = []
    alt = next((i for i in items if i.key == "alternativa"), None)
    if alt is not None:
        first.append("Teste o microfone USB da mesa: escolha-o na caixa 'Microfone' e rode o teste de novo.")
    if any(i.key == "sinal" for i in items):
        first.append(next(i for i in items if i.key == "sinal").todo[0])
    if any(i.key in ("zumbido", "apitos") for i in items):
        first.append("Ligue o Equalizador ao vivo: ele tira o zumbido e os apitos de todos os programas, sem "
                     "comprar nada.")
    if any(i.key == "zumbido" for i in items):
        first.append("Ligue o computador e a mesa na mesma régua de tomadas (ou use um isolador de loop de terra).")
    for i in items:
        if i.key in ("efeitos", "estouro", "agc"):
            first.append(i.todo[0] if i.todo else i.title)
    first = first[:3]

    score = verdict.score_after
    problems = [i for i in items if i.key != "alternativa"]
    if not problems:
        headline = f"{ctx.name}, não achei nenhum problema sério: seu áudio ficou {score.text} de 10."
    else:
        main = problems[0]
        headline = (f"{ctx.name}, o seu áudio ficou {score.text} de 10. O principal problema: "
                    f"{main.title[0].lower() + main.title[1:]}.")
    free = ""
    if any(i.key in ("zumbido", "apitos", "sinal") for i in items):
        free = ("Sem mexer em nenhum equipamento: o Equalizador ao vivo roda em segundo plano e remove o zumbido e "
                "os apitos em todos os programas (Discord, OBS, Zoom). Ele ajuda bastante, mas não substitui "
                "ter o sinal chegando forte no computador.")
    return PlainReport(headline=headline, items=items, first_steps=first, free_fix=free)
