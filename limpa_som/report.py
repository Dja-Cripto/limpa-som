"""Relatório HTML autocontido (gráfico em SVG, sem dependências externas)."""
from __future__ import annotations

import html
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np

from .findings import BUCKET_TITLES, Context, Finding, Verdict, num
from .metrics import Measurement
from .scoring import Score
from .tuning import SweepPoint

F_MIN, F_MAX = 31.5, 20000.0
DB_MIN, DB_MAX = -110.0, -20.0


@dataclass
class ReportData:
    name: str
    when: datetime
    ctx: Context
    verdict: Verdict
    score_clean: Optional[Score] = None
    m_clean: Optional[Measurement] = None
    chosen: Optional[SweepPoint] = None
    files: Dict[str, str] = field(default_factory=dict)
    history: List[dict] = field(default_factory=list)


def _e(text) -> str:
    return html.escape(str(text))


def _score_color(v: float) -> str:
    if v >= 8.5:
        return "var(--good)"
    if v >= 7.0:
        return "var(--ok)"
    if v >= 5.0:
        return "var(--warn)"
    return "var(--bad)"


def _spectrum_svg(series: List[Tuple[str, np.ndarray, np.ndarray, str]]) -> str:
    w, h, ml, mr, mt, mb = 760, 330, 52, 14, 14, 40
    pw, ph = w - ml - mr, h - mt - mb

    def x_of(f):
        return ml + pw * (math.log10(f) - math.log10(F_MIN)) / (math.log10(F_MAX) - math.log10(F_MIN))

    def y_of(db):
        return mt + ph * (1 - (min(max(db, DB_MIN), DB_MAX) - DB_MIN) / (DB_MAX - DB_MIN))

    parts = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Espectro do ruído e da voz" class="chart">']
    for db in range(int(DB_MIN), int(DB_MAX) + 1, 10):
        y = y_of(db)
        parts.append(f'<line x1="{ml}" x2="{w - mr}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{ml - 6}" y="{y + 4:.1f}" text-anchor="end" class="tick">{db}</text>')
    for f in (50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
        x = x_of(f)
        parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{mt}" y2="{mt + ph}" class="grid"/>')
        label = f"{f // 1000}k" if f >= 1000 else str(f)
        parts.append(f'<text x="{x:.1f}" y="{h - 22}" text-anchor="middle" class="tick">{label}</text>')
    parts.append(f'<text x="{ml + pw / 2}" y="{h - 5}" text-anchor="middle" class="tick">frequência (Hz)</text>')
    parts.append(f'<text x="12" y="{mt + ph / 2}" transform="rotate(-90 12 {mt + ph / 2})" '
                 f'text-anchor="middle" class="tick">nível (dBFS por banda)</text>')
    for label, freqs, dbs, css in series:
        if len(freqs) < 2:
            continue
        pts = " ".join(f"{x_of(max(f, F_MIN)):.1f},{y_of(d):.1f}" for f, d in zip(freqs, dbs))
        parts.append(f'<polyline points="{pts}" class="line {css}"><title>{_e(label)}</title></polyline>')
    parts.append("</svg>")
    return "".join(parts)


def _finding_html(f: Finding) -> str:
    acts = "".join(f"<li>{_e(a)}</li>" for a in f.actions)
    detail = f"<p>{_e(f.detail)}</p>" if f.detail else ""
    return f'<div class="finding sev{f.severity}"><h4>{_e(f.title)}</h4>{detail}' + \
           (f"<ul>{acts}</ul>" if acts else "") + "</div>"


def _plain_html(plain) -> str:
    """O diagnóstico em linguagem simples (de onde vem o problema e por que costuma acontecer)."""
    if plain is None or (not plain.items and not plain.first_steps):
        return ""
    items = []
    for it in plain.items:
        todo = "".join(f"<li>{_e(t)}</li>" for t in it.todo)
        items.append(
            f'<div class="finding sev{it.severity}"><h4>{_e(it.title)}</h4>'
            f'<p><span class="lbl">O que eu vi:</span> {_e(it.saw)}</p>'
            f'<p><span class="lbl">Por que costuma ser assim:</span> {_e(it.why)}</p>'
            + (f'<p class="lbl">O que fazer:</p><ul>{todo}</ul>' if todo else "") + "</div>")
    steps = ""
    if plain.first_steps:
        steps = ('<div class="finding"><h4>Por onde começar</h4><ol>'
                 + "".join(f"<li>{_e(t)}</li>" for t in plain.first_steps) + "</ol></div>")
    free = f'<div class="finding"><p>{_e(plain.free_fix)}</p></div>' if plain.free_fix else ""
    return (f'<div class="card plain"><h2 style="margin-top:0">Em palavras simples</h2>'
            f'<p class="headline">{_e(plain.headline)}</p>{"".join(items)}{steps}{free}</div>')


def _rows(ctx: Context, m_clean: Optional[Measurement]) -> str:
    def cells(m: Optional[Measurement]):
        if m is None:
            return ["-"] * 7
        n, s = m.noise, m.speech
        return [
            f"{num(n.rms_a_db, 1)} dBFS(A)",
            f"{num(s.level_a_db, 1)} dBFS(A)" if s else "-",
            f"{num(m.snr_db, 1)} dB" if s else "-",
            f"{num(s.peak_db, 1)} dBFS" if s else "-",
            str(s.clip_runs) if s else "-",
            f"{n.cutoff_hz / 1000:.1f} kHz" if n.cutoff_hz else "completa",
            f"{n.hum.f0:.0f} Hz" if n.hum else "não",
        ]
    labels = ["Piso de ruído", "Nível da voz", "Relação sinal/ruído", "Pico da voz",
              "Trechos estourados", "Largura de banda", "Zumbido"]
    cols = [cells(ctx.before)]
    heads = ["Antes" if ctx.after is not None else "Medido"]
    if ctx.after is not None:
        cols.append(cells(ctx.after))
        heads.append("Depois do ajuste")
    if m_clean is not None:
        cols.append(cells(m_clean))
        heads.append("Com limpeza digital")
    head = "<thead><tr><th></th>" + "".join(f"<th>{h}</th>" for h in heads) + "</tr></thead>"
    rows = []
    for i, label in enumerate(labels):
        rows.append(f"<tr><th>{label}</th>" + "".join(f"<td>{_e(c[i])}</td>" for c in cols) + "</tr>")
    return head + "<tbody>" + "".join(rows) + "</tbody>"


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2530;--muted:#5d6b7c;--line:#d9dee5;--good:#1f9d55;--ok:#6aa84f;
--warn:#d98a00;--bad:#d64545;--accent:#2563eb;--n1:#d64545;--n2:#e08a00;--n3:#1f9d55;--sp:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#10151c;--card:#18202a;--text:#e6ebf1;--muted:#97a4b4;--line:#2b3644;
--good:#3ecf7b;--ok:#8bcf6a;--warn:#f0a63a;--bad:#ff6b6b;--accent:#6ea0ff;--n1:#ff6b6b;--n2:#f0a63a;--n3:#3ecf7b;--sp:#6ea0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
main{max-width:920px;margin:0 auto;padding:24px 16px 64px}h1{font-size:1.6rem;margin:0 0 4px}
h2{font-size:1.2rem;margin:32px 0 12px}h4{margin:0 0 4px;font-size:1rem}.muted{color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin:12px 0}
.scores{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.score{text-align:center}.score .big{font-size:3rem;font-weight:700;line-height:1.1}.score .lab{font-size:.9rem}
.score .cap{color:var(--muted);font-size:.85rem;margin-top:6px}
.headline{font-size:1.15rem;font-weight:600}
.bucket{border-left:6px solid var(--line)}.bucket.corrigido{border-color:var(--good)}.bucket.config{border-color:var(--warn)}
.bucket.hardware{border-color:var(--bad)}.bucket.ambiente{border-color:var(--accent)}.bucket.ok{border-color:var(--ok)}
.finding{padding:10px 0;border-top:1px solid var(--line)}.finding:first-of-type{border-top:0}.finding p{margin:4px 0;color:var(--muted)}
.finding ul,.finding ol{margin:6px 0 0 18px;padding:0}.lbl{font-weight:600;color:var(--text)}
.plain .finding p{color:var(--text)}
table{width:100%;border-collapse:collapse}th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;font-size:.95rem}
td{font-variant-numeric:tabular-nums}thead th{color:var(--muted);font-weight:600}
.chart{width:100%;height:auto}.grid{stroke:var(--line);stroke-width:1}.tick{fill:var(--muted);font-size:11px}
.line{fill:none;stroke-width:2.2}.n1{stroke:var(--n1)}.n2{stroke:var(--n2)}.n3{stroke:var(--n3)}.sp{stroke:var(--sp)}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:.9rem;margin-top:6px}.legend i{display:inline-block;width:14px;height:4px;
border-radius:2px;margin-right:6px;vertical-align:middle}
audio{width:100%;margin:6px 0 14px}
@media (max-width:600px){.score .big{font-size:2.4rem}th,td{padding:6px 6px;font-size:.85rem}}
"""


def build_html(d: ReportData) -> str:
    ctx, v = d.ctx, d.verdict
    sb, sa = v.score_before, v.score_after
    cards = []
    if ctx.after is not None and not ctx.reverted:
        cards.append(("Antes", sb, "como estava ao começar"))
        cards.append(("Depois do ajuste", sa, "só configuração, sem mexer no áudio"))
    else:
        cards.append(("Seu áudio agora", sa, "microfone + configuração atuais"))
    if d.score_clean is not None:
        cards.append(("Com limpeza digital (estimativa)", d.score_clean,
                      "só no arquivo limpo; o microfone não muda"))
    score_html = "".join(
        f'<div class="card score"><div class="lab">{_e(t)}</div>'
        f'<div class="big" style="color:{_score_color(s.value)}">{s.text}</div>'
        f'<div class="lab">de 10 · {_e(s.label)}</div><div class="cap">{_e(c)}</div></div>'
        for t, s, c in cards)

    buckets_html = ""
    for key in ("corrigido", "config", "hardware", "ambiente", "ok"):
        items = v.bucket(key)
        if items:
            title = BUCKET_TITLES[key]
            title = title[0] + title[1:].lower()
            buckets_html += (f'<div class="card bucket {key}"><h2 style="margin-top:0">{_e(title)}'
                             f'</h2>{"".join(_finding_html(f) for f in items)}</div>')

    series = [("Ruído antes", ctx.before.noise.chart_freqs, ctx.before.noise.chart_db, "n1")]
    if ctx.after is not None:
        series.append(("Ruído depois", ctx.after.noise.chart_freqs, ctx.after.noise.chart_db, "n2"))
    if d.m_clean is not None:
        series.append(("Ruído após limpeza digital", d.m_clean.noise.chart_freqs, d.m_clean.noise.chart_db, "n3"))
    if ctx.final.speech is not None:
        series.append(("Sua voz", ctx.final.speech.chart_freqs, ctx.final.speech.chart_db, "sp"))
    legend = "".join(f'<span><i style="background:var(--{c})"></i>{_e(lab)}</span>' for lab, _, _, c in series)

    sweep_html = ""
    if ctx.sweep:
        rows = "".join(
            f"<tr><td>{'+%.0f dB' % p.settings.boost_db if p.settings.boost_db is not None else '-'}</td>"
            f"<td>{num(p.settings.volume_db)} dB</td><td>{num(p.noise_a_db, 1)} dBFS(A)</td>"
            f"<td>{num(p.snr_pred_db, 1)} dB</td><td>{num(p.peak_pred_db, 1)} dBFS</td>"
            f"<td>{'escolhido' if d.chosen is p else ('volume no limite' if p.clamped else '')}</td></tr>"
            for p in ctx.sweep)
        sweep_html = ('<h2>Teste de ganho</h2><div class="card"><p class="muted">Para o mesmo volume de voz, '
                      'testei cada divisão entre reforço do microfone e volume do Windows.</p><table><thead><tr>'
                      '<th>Reforço</th><th>Volume</th><th>Ruído medido</th><th>Sinal/ruído previsto</th>'
                      f'<th>Pico previsto</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>')

    audio_html = ""
    labels = {"antes": "Antes (gravação original)", "depois": "Depois do ajuste de configuração",
              "limpo": "Com limpeza digital"}
    for key in ("antes", "depois", "limpo"):
        if key in d.files:
            audio_html += f"<div><strong>{labels[key]}</strong><audio controls src=\"{_e(d.files[key])}\"></audio></div>"
    if audio_html:
        audio_html = f'<h2>Ouça você mesmo</h2><div class="card">{audio_html}</div>'

    hist_html = ""
    if d.history:
        rows = "".join(f"<tr><td>{_e(h['quando'])}</td><td>{_e(h['antes'])}</td><td>{_e(h['depois'])}</td>"
                       f"<td>{_e(h.get('config', ''))}</td></tr>" for h in d.history[-6:])
        hist_html = ('<h2>Testes anteriores neste microfone</h2><div class="card"><table><thead><tr><th>Quando</th>'
                     f'<th>Nota antes</th><th>Nota depois</th><th>Configuração</th></tr></thead><tbody>{rows}'
                     '</tbody></table></div>')

    settings_html = ""
    if ctx.settings_before is not None and ctx.settings_after is not None and not ctx.reverted:
        settings_html = (f"<p class='muted'>Configuração antes: {_e(ctx.settings_before.describe())}. "
                         f"Depois: {_e(ctx.settings_after.describe())}. Para desfazer, use a opção "
                         "'Restaurar configurações originais' do programa.</p>")

    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Relatório do microfone</title><style>{CSS}</style></head><body><main>
<h1>Relatório do seu microfone</h1>
<div class="muted">{_e(d.when.strftime('%d/%m/%Y %H:%M'))} · {_e(ctx.device.name)} · {_e(ctx.device.connection_text)}</div>
<div class="card"><div class="headline">{_e(v.headline)}</div>{settings_html}</div>
<div class="scores">{score_html}</div>
{_plain_html(v.plain)}
{buckets_html}
<h2>Medições</h2><div class="card"><table>{_rows(ctx, d.m_clean)}</table>
<p class="muted">dBFS(A): nível digital com ponderação A, parecida com a sensibilidade do ouvido. Quanto menor o piso
de ruído e maior a relação sinal/ruído, menos chiado. Meta: sinal/ruído acima de 48 dB.</p>
<p class="muted">Na coluna da limpeza digital o ruído é medido nas pausas, onde a redução é máxima; durante a fala o
chiado só fica mascarado. Por isso a nota dela é limitada a 2,5 pontos acima da nota real.</p></div>
<h2>Espectro</h2><div class="card">{_spectrum_svg(series)}<div class="legend">{legend}</div>
<p class="muted">Chiado aparece como ruído alto nos agudos (direita). Zumbido aparece como picos nos graves.</p></div>
{sweep_html}{audio_html}{hist_html}
</main></body></html>"""
