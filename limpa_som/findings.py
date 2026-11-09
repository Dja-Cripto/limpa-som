"""Transforma medições em conclusões: o que foi corrigido, o que é configuração e o que é hardware."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .evidence import chain_evidence
from .metrics import Measurement, NoiseReport
from .scoring import Score, compute_score
from .tuning import Settings, SweepPoint

BUCKETS = ["corrigido", "config", "hardware", "ambiente", "ok"]
BUCKET_TITLES = {
    "corrigido": "CORRIGI POR VOCÊ",
    "config": "FALTA VOCÊ AJUSTAR (é configuração)",
    "hardware": "NÃO É CONFIGURAÇÃO (hardware ou instalação)",
    "ambiente": "VEM DE FORA DO COMPUTADOR (microfone, mesa, cabos, sala)",
    "ok": "ESTÁ BOM",
}
EXCELLENT_SNR_DB = 48.0


def num(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}f}".replace(".", ",")


@dataclass
class DeviceInfo:
    name: str
    connection: str = "desconhecida"   # analogica | usb | bluetooth | virtual | desconhecida
    chip: str = ""
    samplerate: int = 48000
    simulated: bool = False

    @property
    def connection_text(self) -> str:
        chip = f" {self.chip}" if self.chip else ""
        return {
            "analogica": f"entrada analógica da placa de som{chip}",
            "usb": "dispositivo USB",
            "bluetooth": "Bluetooth",
            "virtual": "dispositivo virtual (software)",
        }.get(self.connection, "ligação não identificada")


@dataclass
class Finding:
    key: str
    bucket: str
    severity: int
    title: str
    detail: str = ""
    actions: List[str] = field(default_factory=list)
    opens_sound_panel: bool = False


@dataclass
class Context:
    name: str
    device: DeviceInfo
    before: Measurement
    after: Optional[Measurement] = None
    settings_before: Optional[Settings] = None
    settings_after: Optional[Settings] = None
    sweep: List[SweepPoint] = field(default_factory=list)
    isolation: Optional[NoiseReport] = None
    exclusive: Optional[NoiseReport] = None
    has_control: bool = False
    reverted: bool = False
    revert_reason: str = ""             # "pior" (o ajuste piorou) | "sem_ganho" (não adiantou)
    wiring: Optional[str] = None        # mesa | direto | interface | usb_mic | headset
    gain_control: bool = True           # o Windows controla o ganho deste microfone?
    alternatives: List[str] = field(default_factory=list)   # outros microfones USB encontrados
    channel_note: Optional[str] = None

    @property
    def final(self) -> Measurement:
        return self.after or self.before


@dataclass
class Verdict:
    score_before: Score
    score_after: Score
    findings: List[Finding]
    headline: str
    kind: str   # excelente | bom | hardware | config | ambiente
    plain: object = None   # PlainReport: o mesmo diagnóstico em linguagem simples

    def bucket(self, name: str) -> List[Finding]:
        return [f for f in self.findings if f.bucket == name]


def _snr_text(m: Measurement) -> str:
    if m.speech is None:
        return f"piso de ruído {num(m.noise.rms_a_db, 0)} dBFS(A)"
    return (f"piso de ruído {num(m.noise.rms_a_db, 0)} dBFS(A), voz {num(m.speech.level_a_db, 0)} dBFS(A): "
            f"relação sinal/ruído de {num(m.snr_db, 0)} dB (meta: acima de {EXCELLENT_SNR_DB:.0f} dB)")


def _changed(ctx: Context) -> bool:
    a, b = ctx.settings_before, ctx.settings_after
    if a is None or b is None:
        return False
    return (abs((a.boost_db or 0) - (b.boost_db or 0)) > 0.4 or abs(a.volume_db - b.volume_db) > 0.4
            or a.muted != b.muted)


def _noise_floor_findings(ctx: Context, score: Score) -> List[Finding]:
    m = ctx.final
    n = m.noise
    out: List[Finding] = []
    if m.snr_db is not None and m.snr_db >= EXCELLENT_SNR_DB and score.value >= 8.5:
        out.append(Finding("ruido_ok", "ok", 0, "Chiado praticamente inaudível", _snr_text(m)))
        return out

    conn = ctx.device.connection
    iso = ctx.isolation
    # com nota alta o que sobrou é refinamento, não problema
    severity = 3 if score.value < 6.0 else 2 if score.value < 8.0 else 1
    hiss_hint = ""
    if n.hum is not None and n.hum_share >= 0.5:
        hiss_hint = (f"A maior parte da energia do ruído é o zumbido de {num(n.hum.f0, 0)} Hz (veja abaixo); "
                     "o chiado de banda larga em si é bem menor. ")
    elif n.hf_share >= 0.4 and n.flatness > 0.15:
        hiss_hint = "O ruído é de banda larga com muita energia nos agudos: é o 'chiado' típico de ganho alto. "
    elif n.lf_share >= 0.55:
        hiss_hint = "O ruído é principalmente grave (ventilador, ar-condicionado, vibração). "

    if iso is None:
        out.append(Finding(
            "ruido_sem_isolamento", "config", 1,
            f"Ainda há chiado (nota {num(score.value)}) e não consegui separar a causa",
            _snr_text(m) + ". " + hiss_hint +
            "Sem o teste de isolamento não dá para saber se o ruído nasce no microfone/mesa ou dentro do computador.",
            ["Rode o teste de novo e faça a etapa de isolamento (silenciar a fonte): é ela que aponta a causa."]))
        return out

    delta = n.rms_a_db - iso.rms_a_db
    evidence = (f"Com a fonte silenciada o ruído foi de {num(iso.rms_a_db, 0)} dBFS(A); com tudo ligado, "
                f"{num(n.rms_a_db, 0)} dBFS(A) (diferença de {num(max(delta, 0.0), 0)} dB). ")
    sev = severity

    ev = chain_evidence(ctx)
    if delta <= 3.0 and ev.weak_signal and ctx.wiring in ("mesa", None):
        out.append(Finding(
            "sinal_fraco", "config", sev,
            "A sua voz chega fraca no computador e o ruído da entrada fica perto dela",
            evidence + f"A voz chega tão fraca que o computador soma cerca de {num(ev.needed_gain_db, 0)} dB de "
            "ganho para ela soar normal, e esse ganho amplifica o ruído da entrada junto. "
            + ("Mexer no ganho do computador não ajuda: em todas as combinações testadas a relação voz/ruído foi "
               "a mesma. " if ev.gain_cannot_help else "")
            + "O caminho é aumentar o sinal ANTES de ele chegar no computador.",
            ["Se o microfone passa por uma mesa: suba o ganho do canal e o master dela até a voz ficar forte, sem "
             "estourar; use a saída principal (Main).",
             "Aproxime o microfone da boca (8 a 12 cm).",
             "Se o microfone liga direto na placa-mãe, essa entrada é o limite: uma interface USB resolve."]))
    elif delta <= 3.0:
        if conn == "analogica":
            acts = [
                "A saída é trocar a entrada: uma interface de áudio USB (ou microfone/mesa com saída USB) "
                "costuma baixar o piso de ruído em 15 a 25 dB e levar a nota para 8-9.",
                "Teste a entrada de áudio traseira da placa-mãe (a do painel frontal capta mais ruído do gabinete) "
                "e um cabo curto e blindado.",
                "Se a mesa tem saída USB, use-a como placa de som em vez da entrada analógica.",
            ]
            what = "da entrada analógica da placa de som do computador (ruído elétrico do PC e do conversor)"
        elif conn == "usb":
            acts = [
                "Troque de porta USB (traseira, direto na placa-mãe, sem hub) e de cabo; afaste de carregadores.",
                "Se não mudar nada, o ruído é do próprio microfone/interface USB: só trocando o equipamento.",
            ]
            what = "do conversor do próprio microfone/interface USB ou da alimentação da porta USB"
        elif conn == "bluetooth":
            acts = ["Use microfone por cabo ou com dongle dedicado: Bluetooth usa um codec de voz de baixa qualidade."]
            what = "do link Bluetooth (codec de voz limitado)"
        else:
            acts = ["Teste outro microfone ou outra entrada para confirmar de onde vem o ruído."]
            what = "do conversor/entrada do computador"
        out.append(Finding(
            "ruido_entrada", "hardware", sev,
            "O chiado que sobrou nasce na própria entrada do computador",
            evidence + f"Como o ruído não some nem isolando a fonte, ele vem {what}. " + hiss_hint +
            "Nenhum volume ou equalizador resolve isso na origem: dá para esconder por software "
            "(veja o áudio limpo), mas o piso do hardware continua o mesmo.", acts))
    elif delta >= 6.0:
        if n.lf_share >= 0.55:
            acts = [
                "Afaste o microfone de ventiladores, do gabinete e do ar-condicionado; use braço com suporte "
                "amortecido (shock mount) para não passar vibração da mesa.",
                "O filtro de graves da limpeza digital remove esse ruído do arquivo.",
            ]
            title = "O ruído que sobrou é grave e vem do ambiente (ventilador, vibração, ar-condicionado)"
        else:
            acts = [
                "Reduza o ganho do canal na mesa/pré do microfone e suba o nível de SAÍDA da mesa (master/fader): "
                "o PC deve receber sinal forte com o mínimo de ganho dele.",
                "Aproxime o microfone da boca (8 a 12 cm): cada vez que a distância dobra, a voz perde 6 dB "
                "em relação ao chiado.",
                "Teste outro cabo e, se possível, outro microfone: se o chiado mudar, o problema é o microfone/cabo.",
            ]
            title = "O chiado vem de antes do computador (microfone, mesa, cabos ou sala), não da entrada do PC"
        out.append(Finding("ruido_fonte", "ambiente", sev, title,
                           evidence + hiss_hint + "A entrada do computador está limpa: o ruído entra pelo lado de "
                           "fora dela, então a melhoria está na fonte, não em configuração do Windows.", acts))
    else:
        out.append(Finding(
            "ruido_misto", "hardware", sev,
            "O chiado vem em parte da fonte (microfone/mesa/sala) e em parte da entrada do computador",
            evidence + hiss_hint + "Cada lado responde por uma parte; resolver só um deixa o outro no teto.",
            ["Primeiro otimize o ganho da mesa (saída alta, ganho de canal baixo, microfone próximo).",
             "Se ainda sobrar chiado, a entrada do PC é o gargalo: uma interface USB resolve."]))
    return out


def diagnose(ctx: Context) -> List[Finding]:
    final = ctx.final
    n = final.noise
    sp = final.speech
    conn = ctx.device.connection
    findings: List[Finding] = []
    score = compute_score(final)

    # --- o que o programa corrigiu ------------------------------------------
    if ctx.reverted and ctx.revert_reason == "sem_ganho":
        ev0 = chain_evidence(ctx)
        cur = ctx.before.snr_db
        best = max((p.snr_pred_db for p in ctx.sweep), default=None)
        gain_txt = (f"O melhor ajuste que encontrei melhora a relação voz/ruído em só "
                    f"{num(max(best - cur, 0.0), 1)} dB (de {num(cur, 0)} para {num(max(best, cur), 0)} dB). "
                    if best is not None and cur is not None else "")
        why = ("Em todas as combinações a diferença entre voz e ruído ficou igual: o ruído entra junto com a "
               "sua voz, antes do ganho do computador, e subir o volume ou o reforço do Windows amplifica os dois "
               "na mesma medida." if ev0.gain_cannot_help else
               "O ganho que você já tinha está no ponto certo para este microfone.")
        findings.append(Finding(
            "ganho_nao_ajuda", "ambiente", 0,
            "Mexer no ganho do computador não ajuda no seu caso: deixei como estava", gain_txt + why))
    elif ctx.reverted:
        findings.append(Finding(
            "reverteu", "config", 1, "Tentei otimizar o ganho, mas o resultado piorou: voltei ao original",
            "As configurações do microfone foram restauradas para como estavam."))
    elif _changed(ctx) and ctx.after is not None:
        a, b = ctx.settings_before, ctx.settings_after
        parts = []
        if a.boost_db is not None and b.boost_db is not None and abs(a.boost_db - b.boost_db) > 0.4:
            parts.append(f"reforço do microfone de {num(a.boost_db, 0)} dB para {num(b.boost_db, 0)} dB")
        if abs(a.volume_db - b.volume_db) > 0.4:
            parts.append(f"volume de entrada do Windows de {num(a.volume_db)} dB para {num(b.volume_db)} dB")
        if a.muted and not b.muted:
            parts.append("tirei o microfone do mudo")
        before_snr = ctx.before.snr_db
        after_snr = ctx.after.snr_db
        gain_txt = ""
        if before_snr is not None and after_snr is not None:
            gain_txt = (f" A relação sinal/ruído foi de {num(before_snr, 0)} dB para {num(after_snr, 0)} dB "
                        f"(piso de ruído de {num(ctx.before.noise.rms_a_db, 0)} para "
                        f"{num(ctx.after.noise.rms_a_db, 0)} dBFS(A)).")
        findings.append(Finding(
            "ganho_ajustado", "corrigido", 1, "Ajustei o ganho do microfone: " + "; ".join(parts),
            "Escolhi a divisão entre reforço e volume que dá menos chiado para o mesmo nível de voz." + gain_txt
            + " O programa guardou as configurações originais para você poder desfazer."))
    if ctx.before.speech and ctx.after and ctx.after.speech:
        if ctx.before.speech.clip_runs > 0 and ctx.after.speech.clip_runs == 0:
            findings.append(Finding("estouro_corrigido", "corrigido", 1,
                                    "Eliminei o estouro de áudio (clipping)",
                                    f"Antes, a voz passava de 0 dBFS em {ctx.before.speech.clip_runs} trechos."))

    # --- níveis -------------------------------------------------------------
    if sp is not None:
        if sp.clip_runs > 0:
            findings.append(Finding(
                "estouro", "config", 3 if sp.clip_runs > 30 else 2,
                f"O áudio está estourando (clipping) em {sp.clip_runs} trechos",
                f"O pico chegou a {num(sp.peak_db)} dBFS. O estouro distorce a voz e não tem como consertar depois.",
                ["Abaixe o ganho até os picos da voz ficarem entre -12 e -6 dBFS: na mesa/pré do microfone, "
                 "ou no volume de entrada do Windows.", "Fale um pouco mais longe do microfone."]))
        elif sp.level_db < -32.0 or sp.peak_db < -20.0:
            findings.append(Finding(
                "voz_baixa", "config", 2,
                f"Sua voz está baixa ({num(sp.level_db, 0)} dBFS)",
                "Voz baixa obriga a aumentar o ganho depois, e isso aumenta o chiado junto.",
                ["Aproxime o microfone (8 a 12 cm).",
                 "Suba o nível de saída da mesa/pré antes de subir o ganho do Windows."]))
        elif -30.0 <= sp.level_db <= -14.0 and sp.peak_db <= -3.0:
            findings.append(Finding("nivel_ok", "ok", 0, "Nível da voz bom",
                                    f"Média de {num(sp.level_db, 0)} dBFS e picos de {num(sp.peak_db)} dBFS, "
                                    "com folga para não estourar."))
        if sp.active_frac < 0.15:
            findings.append(Finding(
                "pouca_fala", "config", 2, "Quase não detectei voz na gravação de fala",
                "Os números de relação sinal/ruído podem estar errados.",
                ["Refaça o teste lendo o texto em voz normal, mais perto do microfone."]))
        excess = sp.pause_noise_a_db - n.rms_a_db
        if excess >= 6.0:
            findings.append(Finding(
                "ruido_sobe_falando", "ambiente", 1,
                f"O ruído de fundo sobe {num(excess, 0)} dB quando você fala",
                "Pode ser respiração, eco da sala, teclado/mouse ou ganho automático.",
                ["Fale mais perto e com menos força; use sala com menos eco (cortinas, tapete)."]))

    # --- ruído / chiado -----------------------------------------------------
    findings.extend(_noise_floor_findings(ctx, score))

    # --- efeitos do Windows (modo exclusivo x compartilhado) -----------------
    if ctx.exclusive is not None:
        d = n.rms_a_db - ctx.exclusive.rms_a_db
        if d >= 3.0:
            findings.append(Finding(
                "efeitos_windows", "config", 3 if d >= 6 else 2,
                f"Os efeitos de áudio do Windows/driver estão somando {num(d, 0)} dB de chiado",
                f"No modo exclusivo (que ignora os efeitos) o ruído é de {num(ctx.exclusive.rms_a_db, 0)} dBFS(A); "
                f"no modo normal, {num(n.rms_a_db, 0)} dBFS(A). Isso é configuração e dá para resolver.",
                ["Painel de Som (abrirei para você) > aba Gravação > seu microfone > Propriedades > "
                 "Avançado/Melhorias: marque 'Desativar todos os efeitos' ou desligue AGC e supressão de ruído.",
                 "Se usa SteelSeries Sonar, Nahimic, Voicemod ou Realtek Audio Console, desligue os efeitos de "
                 "microfone neles também."], opens_sound_panel=True))
        elif d <= -3.0:
            findings.append(Finding(
                "filtro_tempo_real", "ok", 0,
                f"Um filtro em tempo real (provavelmente o Equalizer APO) já reduz o ruído em {num(-d, 0)} dB",
                f"No modo normal o ruído é de {num(n.rms_a_db, 0)} dBFS(A); no modo exclusivo, que ignora filtros, "
                f"{num(ctx.exclusive.rms_a_db, 0)} dBFS(A). Isso vale para os apps que usam o microfone em modo "
                "normal (Discord, OBS, Zoom). A causa física do ruído continua lá: o filtro só a esconde."))
        elif d < 1.5:
            findings.append(Finding(
                "sem_efeitos", "ok", 0, "O Windows não está acrescentando efeitos nem ruído",
                "O ruído no modo normal é igual ao do modo exclusivo (que ignora todos os efeitos)."))

    # --- zumbido, tons, graves ----------------------------------------------
    if n.hum is not None and n.hum.prominence_db >= 8.0:
        if conn == "analogica":
            acts = ["Use um isolador de loop de terra (barato) entre a mesa e o PC, ou cabo balanceado.",
                    "Ligue mesa e PC na mesma régua/tomada; teste o notebook na bateria para confirmar a causa."]
        else:
            acts = ["Troque de porta USB e de cabo; afaste de carregadores, fontes e reatores de luz.",
                    "Teste o notebook na bateria: se o zumbido sumir, a causa é a tomada/fonte."]
        acts.append("A limpeza digital remove o zumbido do arquivo, mas na origem só resolve fisicamente.")
        findings.append(Finding(
            "zumbido", "hardware", 3 if n.hum.prominence_db >= 18 else 2,
            f"Zumbido da rede elétrica ({num(n.hum.f0, 0)} Hz), típico de loop de terra",
            f"Há um tom de {num(n.hum.f0, 0)} Hz com {n.hum.harmonics} harmônicos, até {num(n.hum.prominence_db, 0)} dB "
            "acima do ruído ao redor. Não é configuração: é ligação elétrica.", acts))
    strong = [t for t in n.tones if t.prominence_db >= 15.0 and t.freq >= 300.0]
    if strong:
        t = strong[0]
        findings.append(Finding(
            "tom", "hardware", 3 if t.prominence_db >= 25 else 2,
            f"Apito/tom de interferência em {num(t.freq, 0)} Hz",
            f"Um tom estreito {num(t.prominence_db, 0)} dB acima do ruído: costuma ser interferência de USB, placa de "
            "vídeo, fonte ou carregador. Não é configuração.",
            ["Afaste cabos de áudio dos de energia e do gabinete; troque de porta USB.",
             "Observe se o apito muda quando a placa de vídeo trabalha (jogo, vídeo): se mudar, é ela."]))
    if n.lf_share >= 0.55 and n.bands.get("sub", -120) > -68.0 and not any(f.key == "ruido_fonte" for f in findings):
        findings.append(Finding(
            "grave", "ambiente", 1, "Muito ruído grave (ventilador, ar-condicionado ou vibração da mesa)",
            "Esse tipo de ruído é fácil de remover por filtro de graves.",
            ["Use suporte amortecido (shock mount) e afaste o microfone do gabinete."]))

    # --- ganho automático, gate, banda, DC, estalos ---------------------------
    if abs(n.drift_db) >= 5.0 and n.drift_r2 >= 0.5:
        direction = "sobe" if n.drift_db > 0 else "desce"
        findings.append(Finding(
            "agc", "config", 2,
            f"O ganho automático (AGC) mexe no volume sozinho: o ruído {direction} {num(abs(n.drift_db), 0)} dB no silêncio",
            "Quando você para de falar, o driver ou o app aumenta o ganho e o chiado aparece.",
            ["Desative 'controle automático de ganho' / melhorias de áudio no painel de som e nos apps "
             "(Discord, Zoom, Teams, Meet)."], opens_sound_panel=True))
    if n.digital_silence_frac >= 0.2:
        findings.append(Finding(
            "gate", "config", 1, "Algo está zerando o som quando você fica quieto (gate/supressor de ruído)",
            "Isso esconde o chiado real e deixa as medições enganosas.",
            ["Desligue, só para o teste, a supressão de ruído do driver, do Sonar ou do NVIDIA Broadcast."]))
    if n.cutoff_hz is not None and n.cutoff_hz < 13000:
        if conn == "bluetooth":
            findings.append(Finding(
                "banda_bluetooth", "hardware", 3,
                f"Áudio cortado em {num(n.cutoff_hz / 1000)} kHz: é o modo de voz do Bluetooth",
                "O perfil Mãos-Livres (HFP) do Bluetooth usa banda estreita. Não tem configuração que resolva.",
                ["Use microfone por cabo ou dongle com microfone dedicado."]))
        else:
            findings.append(Finding(
                "banda", "config", 2 if n.cutoff_hz < 9000 else 1,
                f"O áudio está sendo cortado em {num(n.cutoff_hz / 1000)} kHz",
                "O dispositivo está trabalhando em taxa de amostragem baixa.",
                ["Painel de Som > Gravação > seu microfone > Propriedades > Avançado: escolha "
                 "'24 bits, 48000 Hz (qualidade de estúdio)'."], opens_sound_panel=True))
    if abs(n.dc) > 0.02:
        findings.append(Finding("dc", "hardware", 1, "Deslocamento DC no sinal",
                                f"Média de {num(n.dc * 100, 1)}% do fundo de escala: defeito leve da entrada. "
                                "A limpeza digital corrige no arquivo."))
    if n.transients >= 3:
        findings.append(Finding(
            "estalos", "hardware", 1, f"{n.transients} estalos ou cliques no silêncio",
            "Podem vir de interferência em cabos, porta USB ou mau contato.",
            ["Confira o encaixe dos plugues e troque de porta/cabo."]))
    if ctx.channel_note:
        findings.append(Finding("canais", "ok", 0, "Canais do microfone", ctx.channel_note.capitalize() + "."))

    order = {b: i for i, b in enumerate(BUCKETS)}
    findings.sort(key=lambda f: (order[f.bucket], -f.severity))
    return findings


def build_verdict(ctx: Context) -> Verdict:
    before = compute_score(ctx.before)
    after = compute_score(ctx.final)
    findings = diagnose(ctx)
    name = ctx.name
    hw = [f for f in findings if f.bucket == "hardware" and f.severity >= 2]
    cfg = [f for f in findings if f.bucket == "config" and f.severity >= 1]
    amb = [f for f in findings if f.bucket == "ambiente" and f.severity >= 1]
    delta = after.value - before.value
    progress = ""
    if ctx.after is not None and abs(delta) >= 0.1 and not ctx.reverted:
        progress = f" (antes: {before.text})"

    if after.value >= 9.0:
        kind = "excelente"
        headline = f"{name}, seu áudio está excelente: {after.text} de 10{progress}."
    elif after.value >= 8.0 and not hw:
        kind = "bom"
        tail = " Para passar de 9 ainda dá para refinar um pouco (veja abaixo)." if (cfg or amb or findings) else ""
        headline = f"{name}, seu áudio ficou {after.text} de 10{progress}: {after.label.lower()}.{tail}"
    elif any(f.key == "sinal_fraco" and f.severity >= 2 for f in findings):
        kind = "config"
        headline = (f"{name}, seu áudio ficou {after.text} de 10{progress}. O que mais pesa: a sua voz chega fraca "
                    "no computador, e isso dá para melhorar na mesa (é configuração).")
    elif hw:
        kind = "hardware"
        headline = (f"{name}, seu áudio ficou {after.text} de 10{progress}. O que ainda atrapalha NÃO é configuração: "
                    f"{hw[0].title[0].lower() + hw[0].title[1:]}.")
    elif cfg:
        kind = "config"
        headline = (f"{name}, seu áudio ficou {after.text} de 10{progress}, e ainda dá para melhorar: "
                    f"faltam {len(cfg)} ajuste(s) de configuração que só você pode fazer.")
    elif amb:
        kind = "ambiente"
        headline = (f"{name}, seu áudio ficou {after.text} de 10{progress}. O que sobrou vem de fora do computador "
                    "(microfone, mesa, cabos ou sala), não da entrada do PC.")
    else:
        kind = "bom"
        headline = f"{name}, seu áudio ficou {after.text} de 10{progress}: {after.label.lower()}."
    verdict = Verdict(score_before=before, score_after=after, findings=findings, headline=headline, kind=kind)
    from .plain import explain          # importado aqui porque plain usa os tipos deste módulo
    verdict.plain = explain(ctx, verdict)
    return verdict
