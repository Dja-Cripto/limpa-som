"""O Limpa Som projeta o equalizador a partir do ruído e da voz medidos (e testa no plugin de IA de verdade)."""
from pathlib import Path

import numpy as np
import pytest

from limpa_som import design, eq, metrics, synth, vsthost, wizard

FS = 48000
APO = Path(r"C:\Program Files\EqualizerAPO")
HAS_PLUGIN = vsthost.plugin_path(APO).exists()


def noise_audio(seconds=8, white=-62.0, hum=-50.0, tones=((12006.0, -64.0), (16008.0, -64.0))):
    x = synth.white(seconds, FS, white)
    if hum is not None:
        x = x + synth.hum(seconds, FS, hum, f0=60.0)
    for f, level in tones:
        x = x + synth.tone(seconds, FS, f, level)
    return x


def reports(voice_cutoff=None, voice_level=-24.0, **noise_kw):
    noise_x = noise_audio(**noise_kw)
    voice = synth.speech_like(14, FS, voice_level)
    if voice_cutoff:
        voice = synth.lowpass(voice, FS, voice_cutoff)
    speech_x = voice + noise_audio(14, **noise_kw)
    noise = metrics.analyze_noise(noise_x, FS)
    return noise, metrics.analyze_speech(speech_x, FS, noise.rms_a_db), speech_x


def by_kind(profile, kind):
    return [b for b in profile.bands if b.kind == kind]


def test_hum_and_whistles_become_notches_exactly_where_they_were_measured():
    noise, speech, _ = reports()
    p = design.design_profile(noise, speech)
    notches = sorted(b.freq for b in by_kind(p, "NO"))
    assert 60.0 in notches and 120.0 in notches and 180.0 in notches
    assert any(abs(f - 12006) < 15 for f in notches) and any(abs(f - 16008) < 15 for f in notches)
    assert all(b.q >= 30 for b in by_kind(p, "NO"))
    assert all(b.why for b in p.bands)                            # cada filtro explica por que existe


def test_low_cut_stays_between_70_and_130_hz():
    noise, speech, _ = reports()
    hp = by_kind(design.design_profile(noise, speech), "HP")
    assert len(hp) == 1 and 70.0 <= hp[0].freq <= 130.0


def test_high_cut_appears_where_the_voice_stops_and_only_noise_remains():
    noise, speech, _ = reports(voice_cutoff=3500.0, white=-52.0, tones=(), hum=None)
    p = design.design_profile(noise, speech)
    lp = by_kind(p, "LP")
    assert len(lp) == 2 and {round(b.q, 3) for b in lp} == {0.541, 1.307}   # dois biquads = 24 dB por oitava
    assert design.LP_MIN_HZ <= lp[0].freq <= design.LP_MAX_HZ and lp[0].freq == lp[1].freq


def test_wiener_curve_cuts_where_noise_matches_the_voice_and_spares_the_voice_band():
    noise, speech, _ = reports(voice_cutoff=3000.0, white=-50.0, tones=(), hum=None)
    grid, gain, margin = design.wiener_curve(noise, speech)
    at = lambda f: float(np.interp(np.log(f), np.log(grid), gain))
    assert at(8000) <= -8.0                                       # sem voz ali: corta forte
    assert at(500) >= -3.0                                        # onde a voz manda, quase não mexe
    assert gain.min() >= -design.WIENER_FLOOR_DB - 1e-6 and gain.max() <= 0.0 + 1e-9
    assert margin[np.argmin(np.abs(grid - 500))] > margin[np.argmin(np.abs(grid - 8000))]


def test_preamp_brings_a_weak_voice_up_but_never_past_minus_4_dbfs_peaks():
    noise, speech, _ = reports(voice_level=-44.0)
    p = design.design_profile(noise, speech)
    assert 0.0 < p.preamp_db <= 12.0
    assert speech.peak_db + p.preamp_db <= -4.0 + 0.5
    loud_noise, loud, _ = reports(voice_level=-10.0)
    assert design.design_profile(loud_noise, loud).preamp_db == 0.0


def test_without_the_voice_only_safe_cuts_are_made_and_the_summary_says_so():
    noise, _speech, _ = reports()
    p = design.design_profile(noise, None)
    assert {b.kind for b in p.bands} == {"HP", "NO"} and p.preamp_db == 0.0
    assert "Sem a sua voz medida" in p.summary and p.rnnoise == 1


def test_ai_is_on_by_default_with_an_explicit_voice_gate_and_can_be_left_out():
    noise, speech, _ = reports()
    p = design.design_profile(noise, speech)
    assert p.rnnoise == 1 and 0.2 <= p.rnnoise_gate <= 0.6
    assert design.design_profile(noise, speech, rnnoise=False).rnnoise == 0


def test_candidates_are_a_full_design_and_a_lean_one_with_only_the_certain_filters():
    noise, speech, _ = reports(voice_cutoff=3500.0, white=-52.0)
    (name_a, full), (name_b, lean) = design.candidates(noise, speech)
    assert (name_a, name_b) == ("completo", "enxuto")
    assert {b.kind for b in lean.bands} <= {"HP", "NO"} and len(full.bands) > len(lean.bands)


def test_without_plugin_or_voice_the_choice_falls_back_to_the_full_design_untested():
    noise, speech, speech_x = reports()
    p, ev, tried = design.choose_profile(noise, speech, speech_x, FS, None)
    assert ev is None and tried == [] and p.bands
    p2, ev2, _ = design.choose_profile(noise, speech, None, FS, APO)
    assert ev2 is None
    p3, ev3, _ = design.choose_profile(noise, speech, speech_x[: FS * 2], FS, APO)         # gravação curta demais
    assert ev3 is None


def test_chain_levels_and_interference_count_measure_what_they_claim():
    pauses_and_voice = np.concatenate([synth.white(2, FS, -60.0), synth.white(1, FS, -20.0)] * 4)
    voice_db, floor_db = design.chain_levels(pauses_and_voice, FS)
    assert 30.0 < voice_db - floor_db < 50.0
    noisy = noise_audio(12)
    assert design.interference_count(noisy, FS) >= 2
    assert design.interference_count(synth.white(12, FS, -60.0), FS) == 0
    assert design.interference_count(synth.white(1, FS, -60.0), FS) == 0                   # curto: não inventa nada


def test_evaluation_prefers_a_clean_ratio_without_losing_the_voice_or_leaving_whistles():
    def ev(**kw):
        base = dict(name="x", voice_change_db=0.0, pause_change_db=-6.0, pause_silent=False, ratio_before_db=15.0,
                    ratio_after_db=21.0, tones_before=6, tones_after=0, ran_ai=True)
        base.update(kw)
        return design.Evaluation(**base)
    assert ev().score > ev(ratio_after_db=18.0).score
    assert ev().score > ev(voice_change_db=-3.0).score                  # apagar a voz custa caro
    assert ev().score > ev(tones_after=4).score
    assert ev(voice_change_db=-0.4).score == ev().score                 # até -0,5 dB não conta como perda
    assert ev(ratio_after_db=104.0).score == design.RATIO_CAP_DB        # pausa muda: a relação deixa de valer
    assert ev(ratio_after_db=104.0, ratio_before_db=24.0).gain_db == pytest.approx(21.0)


def test_evaluation_sentences_are_plain_and_honest():
    silent = design.Evaluation("a", 0.0, -80.0, True, 24.0, 105.0, 9, 0, True)
    assert "some por completo" in wizard._evaluation_sentence(silent)
    partial = design.Evaluation("a", 0.3, -6.0, False, 15.4, 21.7, 6, 0, True)
    text = wizard._evaluation_sentence(partial)
    assert "cai 6.0 dB" in text and "de 15 para 22 dB" in text


@pytest.mark.skipif(not HAS_PLUGIN, reason="o plugin RNNoise do Equalizer APO não está instalado aqui")
def test_the_real_rnnoise_plugin_runs_isolated_and_changes_the_audio():
    noise, speech, speech_x = reports(white=-48.0)
    out = vsthost.process_isolated(speech_x, FS, APO, gate=0.45, passes=1)
    assert out is not None and len(out) == len(speech_x) and np.all(np.isfinite(out))
    assert float(np.max(np.abs(out - speech_x))) > 1e-3                 # a IA mexeu no som de verdade
    assert vsthost.process_isolated(speech_x, FS, Path(r"C:\nao\existe"), 0.45) is None


@pytest.mark.skipif(not HAS_PLUGIN, reason="o plugin RNNoise do Equalizador APO não está instalado aqui")
def test_choose_profile_tests_both_designs_with_the_real_plugin_and_reports_the_winner():
    noise, speech, speech_x = reports(white=-48.0)
    progress = []
    p, ev, tried = design.choose_profile(noise, speech, speech_x, FS, APO, progress=progress.append)
    assert ev is not None and ev.ran_ai and [n for n, _ in tried] == ["completo", "enxuto"]
    assert len(progress) == 2 and all("Testando" in t for t in progress)
    assert "Testado na sua própria gravação" in p.summary
    assert ev.score == max(e.score for _n, e in tried)
