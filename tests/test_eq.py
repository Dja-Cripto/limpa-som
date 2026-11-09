"""Equalizador: os filtros fazem matematicamente o que prometem e o arquivo do APO sai como esperado."""
import base64
import json

import numpy as np
import pytest
from scipy import signal

from limpa_som import eq

FS = 48000


def gain_at(profile, freq):
    """Resposta EXATA na frequência (uma grade não cai no centro de um notch estreito)."""
    _w, h = signal.sosfreqz(eq.profile_sos(profile, FS), worN=[float(freq)], fs=FS)
    return float(20 * np.log10(abs(h[0]) + 1e-12))


def prof(*bands, **kw):
    return eq.Profile(bands=list(bands), **kw)


def test_empty_profile_is_perfectly_flat():
    freqs, db = eq.frequency_response(eq.Profile(), FS)
    assert np.max(np.abs(db)) < 0.01


@pytest.mark.parametrize("kind,fc,gain,q", [("PK", 3000, 6.0, 1.0), ("PK", 300, -9.0, 2.0), ("PK", 120, 5.0, 0.7)])
def test_peak_gives_exactly_the_gain_at_its_center(kind, fc, gain, q):
    p = prof(eq.Band.new(kind, fc, gain, q))
    assert abs(gain_at(p, fc) - gain) < 0.05
    assert abs(gain_at(p, fc * 8)) < abs(gain) / 2 + 0.5             # longe do centro quase não mexe


def test_shelves_move_one_side_and_leave_the_other_alone():
    low = prof(eq.Band.new("LS", 200, 6.0))
    assert abs(gain_at(low, 30) - 6.0) < 0.6 and abs(gain_at(low, 5000)) < 0.3
    high = prof(eq.Band.new("HS", 4000, -9.0))
    assert abs(gain_at(high, 18000) + 9.0) < 0.8 and abs(gain_at(high, 150)) < 0.3


def test_highpass_and_lowpass_cut_where_they_say():
    hp = prof(eq.Band.new("HP", 100.0, q=0.707))
    assert gain_at(hp, 30) < -18 and abs(gain_at(hp, 100) + 3.0) < 0.5 and abs(gain_at(hp, 1000)) < 0.1
    lp = prof(eq.Band.new("LP", 8000.0, q=0.707))
    assert gain_at(lp, 16000) < -9 and abs(gain_at(lp, 8000) + 3.0) < 0.5 and abs(gain_at(lp, 500)) < 0.1


def test_two_lowpass_stages_make_a_steep_butterworth_cut():
    """O corte de agudos do Limpa Som: dois biquads (Q 0,541 e 1,307) = 24 dB por oitava, -3 dB no corte."""
    p = prof(eq.Band.new("LP", 6000.0, q=0.541), eq.Band.new("LP", 6000.0, q=1.307))
    assert abs(gain_at(p, 6000) + 3.0) < 0.3
    assert gain_at(p, 12000) < -22 and abs(gain_at(p, 1000)) < 0.1


def test_notch_removes_a_narrow_frequency_without_touching_neighbours():
    p = prof(*[eq.Band.new("NO", f, q=30.0) for f in (60, 120, 180)])
    for f in (60, 120, 180):
        assert gain_at(p, f) < -60, f
        assert gain_at(p, f * 1.001) < -20, f                       # mesmo com a rede variando 0,1%
    assert gain_at(p, 90) > -3.0 and gain_at(p, 150) > -3.0 and abs(gain_at(p, 1000)) < 0.2


def test_disabled_band_and_disabled_profile_are_bypassed():
    off_band = eq.Band.new("PK", 1000, 9.0)
    off_band.enabled = False
    assert np.max(np.abs(eq.frequency_response(prof(off_band), FS)[1])) < 0.01
    p = prof(eq.Band.new("PK", 1000, 9.0), enabled=False, preamp_db=6.0, rnnoise=1)
    assert np.max(np.abs(eq.frequency_response(p, FS)[1])) < 0.01
    assert np.max(np.abs(eq.frequency_response(p, FS, force_enabled=True)[1])) > 8.0     # a curva "se ligasse"
    text = eq.build_config("mi mesa High Definition Audio Device", p)
    assert "Filter:" not in text and "VSTPlugin" not in text and "Preamp" not in text and "DESLIGADO" in text


def test_config_writes_every_band_type_in_apo_syntax_and_orders_the_ai_last():
    p = prof(eq.Band.new("HP", 90.0, why="Corta o ronco"), eq.Band.new("NO", 60.0, q=30.0),
             eq.Band.new("PK", 3000.0, 3.0, 1.0), eq.Band.new("LS", 120.0, 2.0, 0.71),
             eq.Band.new("HS", 9000.0, -6.0, 0.71), eq.Band.new("LP", 12000.0, q=0.541),
             preamp_db=6.5, rnnoise=1, rnnoise_gate=0.45)
    lines = eq.build_config("mi mesa High Definition Audio Device", p, rnnoise_available=(True, True)).splitlines()
    assert lines[3] == "Device: mi mesa High Definition Audio Device"
    assert "Preamp: 6.5 dB" in lines
    assert "Filter: ON HPQ Fc 90 Hz Q 0.710" in lines
    assert "Filter: ON NO Fc 60 Hz Q 30.0" in lines
    assert "Filter: ON PK Fc 3000 Hz Gain 3.0 dB Q 1.00" in lines
    assert "Filter: ON LSC Fc 120 Hz Gain 2.0 dB Q 0.71" in lines
    assert "Filter: ON HSC Fc 9000 Hz Gain -6.0 dB Q 0.71" in lines
    assert "Filter: ON LPQ Fc 12000 Hz Q 0.541" in lines
    assert "# Corta o ronco" in lines                              # o porquê de cada filtro vai como comentário
    assert lines.index("Preamp: 6.5 dB") < min(i for i, l in enumerate(lines) if l.startswith("Filter:"))
    vst = [i for i, l in enumerate(lines) if l.startswith("VSTPlugin:")]
    last_filter = max(i for i, l in enumerate(lines) if l.startswith("Filter:"))
    assert vst and min(vst) > last_filter                           # a IA vem depois de todos os filtros
    assert all("ChunkData" in lines[i] for i in vst)


def test_config_is_plain_ascii_even_with_accents_in_the_explanations():
    p = prof(eq.Band.new("NO", 60.0, why="Zumbido da rede elétrica: ação não é fácil"))
    text = eq.build_config("mi mesa", p, label="mi mesa (áudio)")
    text.encode("ascii")                                            # o APO lê o arquivo: nada de acento nos comentários


def test_ai_passes_repeat_the_plugin_and_missing_plugin_is_skipped():
    two = eq.build_config("X", eq.Profile(rnnoise=2), rnnoise_available=(True, False))
    assert two.count("VSTPlugin: Library rnnoise_mono.dll") == 2 and "If:" not in two
    none = eq.build_config("X", eq.Profile(rnnoise=1), rnnoise_available=(False, False))
    assert "VSTPlugin" not in none
    off = eq.build_config("X", eq.Profile(rnnoise=0), rnnoise_available=(True, True))
    assert "VSTPlugin" not in off


def test_ai_settings_chunk_is_byte_identical_to_the_one_the_apo_itself_wrote():
    """O ChunkData que o Daniel já tinha (gravado pelo próprio Equalizer APO) para a porta de voz 0,48."""
    his = ("VkMyIdIAAAA8P3htbCB2ZXJzaW9uPSIxLjAiIGVuY29kaW5nPSJVVEYtOCI/PiA8Uk5Ob2lzZT48UEFSQU0gaWQ9InZhZF9ncmFjZV9wZXJp"
           "b2QiIHZhbHVlPSIxOC4wIi8+PFBBUkFNIGlkPSJ2YWRfcmV0cm9hY3RpdmVfZ3JhY2VfcGVyaW9kIiB2YWx1ZT0iMTAuMCIvPjxQQVJBTSBp"
           "ZD0idmFkX3RocmVzaG9sZCIgdmFsdWU9IjAuNDc5OTk5OTg5MjcxMTYzOSIvPjwvUk5Ob2lzZT4A")
    assert eq.rnnoise_chunk(0.48) == his
    assert b"vad_threshold" in base64.b64decode(eq.rnnoise_chunk(0.7))
    assert eq.rnnoise_chunk(0.7) != eq.rnnoise_chunk(0.3)


def test_values_are_clamped_to_safe_limits():
    p = eq.Profile(preamp_db=99.0, rnnoise=7, rnnoise_gate=5.0,
                   bands=[eq.Band("PK", 5.0, 80.0, 0.001), eq.Band("HP", 99999.0, 9.0, 999.0), eq.Band("???", 1, 1, 1)])
    p.clamp()
    assert p.preamp_db == eq.PREAMP_MAX and p.rnnoise == 2 and p.rnnoise_gate == eq.GATE_MAX
    pk, hp, odd = p.bands
    assert pk.freq == eq.FREQ_MIN and pk.gain == eq.GAIN_MAX and pk.q == eq.Q_MIN
    assert hp.freq == eq.FREQ_MAX and hp.gain == 0.0 and hp.q == eq.Q_MAX         # HP não tem ganho
    assert odd.kind == "PK"


def test_profile_survives_json_and_ignores_garbage():
    p = prof(eq.Band.new("NO", 60.0, q=30.0, why="Zumbido"), eq.Band.new("PK", 300.0, -4.0),
             preamp_db=3.0, rnnoise=2, rnnoise_gate=0.6, summary="oi")
    again = eq.Profile.from_json(p.to_json())
    assert again == p
    assert eq.Profile.from_json("isto nao e json") == eq.Profile()
    assert eq.Profile.from_json("[1, 2]") == eq.Profile()
    assert eq.Profile.from_dict({"campo_que_nao_existe": 1, "preamp_db": 2.0, "bands": [{"nada": 1}]}).preamp_db == 2.0


def test_first_version_settings_are_converted_to_bands():
    legacy = {"enabled": True, "highpass_hz": 200.0, "bass_db": -4.0, "body_db": 6.0, "presence_db": 7.0,
              "air_db": -15.0, "remove_hum": True, "remove_tones": True, "rnnoise": True, "hum_f0": 60.0,
              "hum_harmonics": 2, "tones": [12006.0, 16005.0]}
    p = eq.Profile.from_dict(legacy)
    kinds = [(b.kind, round(b.freq)) for b in p.bands]
    assert ("HP", 200) in kinds and ("NO", 60) in kinds and ("NO", 120) in kinds and ("NO", 12006) in kinds
    assert ("PK", 3000) in kinds and ("PK", 10000) in kinds and p.rnnoise == 1
    assert next(b for b in p.bands if b.freq == 10000).gain == -15.0


def test_store_keeps_the_original_when_the_user_edits(tmp_path):
    original = prof(eq.Band.new("HP", 80.0), eq.Band.new("NO", 60.0, q=30.0), rnnoise=1)
    eq.save_stored(tmp_path, "mic A", original, limpa_som=original)
    edited = original.copy()
    edited.bands.append(eq.Band.new("PK", 2000.0, 3.0))
    eq.save_stored(tmp_path, "mic A", edited)                        # edição: o original do Limpa Som fica
    eq.save_stored(tmp_path, "mic B", prof(eq.Band.new("HP", 120.0)))
    a = eq.load_stored(tmp_path, "mic A")
    assert len(a.atual.bands) == 3 and len(a.limpa_som.bands) == 2 and a.criado_em
    b = eq.load_stored(tmp_path, "mic B")
    assert b.limpa_som is None and b.atual.bands[0].freq == 120.0
    assert eq.load_stored(tmp_path, "mic C") is None
    assert eq.load_stored(tmp_path / "nao_existe", "mic A") is None
    again = eq.Profile(bands=[eq.Band.new("HP", 90.0)])
    eq.save_stored(tmp_path, "mic A", again, limpa_som=again)        # nova criação do Limpa Som substitui o original
    assert len(eq.load_stored(tmp_path, "mic A").limpa_som.bands) == 1


def test_store_reads_the_old_flat_format(tmp_path):
    (tmp_path / eq.STORE_NAME).write_text(json.dumps({"mic": {"enabled": False, "highpass_hz": 150.0}}),
                                          encoding="utf-8")
    s = eq.load_stored(tmp_path, "mic")
    assert s.atual.bands[0].kind == "HP" and s.atual.bands[0].freq == 150.0 and s.limpa_som is None


def test_describe_summarises_in_plain_words():
    p = prof(eq.Band.new("HP", 90.0), eq.Band.new("LP", 9000.0, q=0.541), eq.Band.new("NO", 60.0),
             eq.Band.new("PK", 300.0, -3.0), preamp_db=6.0, rnnoise=1, rnnoise_gate=0.45)
    text = " ".join(eq.describe(p))
    assert "abaixo de 90 Hz" in text and "acima de 9000 Hz" in text and "1 notch" in text and "+6.0 dB" in text
    assert "45%" in text
    assert eq.describe(eq.Profile()) == ["Nenhum filtro ativo."]
