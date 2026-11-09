import numpy as np

from limpa_som import metrics, realtime, synth

FS = 48000

# o config.txt real do Daniel, copiado literalmente
DANIEL_CONFIG = (
    "Preamp: 4.4 dB\n"
    "Include: example.txt\n"
    "GraphicEQ: 25 0; 40 0; 63 0; 100 0; 160 0; 250 0; 400 0; 630 0; 1000 0; 1600 0; 2500 0; 4000 0; 6300 0; "
    "10000 0; 16000 0\n"
    "Include: microfonet.txt\n"
)


def _noise_with_hum_and_tones():
    x = (synth.white(8, FS, -70.0) + synth.hum(8, FS, -52.0, f0=60.0)
         + synth.tone(8, FS, 12006.0, -66.0) + synth.tone(8, FS, 16008.0, -66.0))
    return metrics.analyze_noise(x, FS)


def test_device_pattern_and_guid():
    assert realtime.device_pattern("mi mesa (High Definition Audio Device)") == "mi mesa High Definition Audio Device"
    assert realtime.device_pattern("Microfone (WEBCAM RAZA FHD-02 1080P-PCYES)") == \
        "Microfone WEBCAM RAZA FHD-02 1080P-PCYES"
    assert realtime.endpoint_guid("{0.0.1.00000000}.{C22AA433-D0D7-4CD2-A85E-8805102FC4FB}") == \
        "c22aa433-d0d7-4cd2-a85e-8805102fc4fb"
    assert realtime.endpoint_guid("") is None
    assert realtime.is_attached(None) is None


def test_mic_config_uses_measured_hum_and_tones():
    noise = _noise_with_hum_and_tones()
    text = realtime.build_mic_config("mi mesa High Definition Audio Device", noise, rnnoise=True,
                                     rnnoise_available=(True, True), label="mi mesa")
    lines = text.splitlines()
    assert "Device: mi mesa High Definition Audio Device" in lines
    assert any(l.startswith("Filter: ON HPQ Fc 80 Hz") for l in lines)
    notch_freqs = sorted(int(l.split("Fc ")[1].split(" Hz")[0]) for l in lines if " NO Fc " in l)
    assert 60 in notch_freqs and 120 in notch_freqs and 180 in notch_freqs
    assert 12006 in notch_freqs or any(abs(f - 12006) <= 6 for f in notch_freqs)
    assert any(abs(f - 16008) <= 6 for f in notch_freqs)
    # o plugin vem DEPOIS dos filtros e leva a configuração explícita (porta de voz): sem ela o plugin quase não
    # limpa (medido no RNNoise de verdade: +0,4 dB contra +6 dB com a configuração)
    vst = [i for i, l in enumerate(lines) if l.startswith("VSTPlugin:")]
    last_filter = max(i for i, l in enumerate(lines) if l.startswith("Filter:"))
    assert vst and min(vst) > last_filter
    assert all("ChunkData" in l for l in lines if l.startswith("VSTPlugin:"))
    assert "If: inputChannelCount == 1" in lines and "rnnoise_stereo.dll" in text


def test_mic_config_without_noise_data_or_plugin():
    text = realtime.build_mic_config("Microfone X", None, rnnoise=True, rnnoise_available=(False, False))
    assert "VSTPlugin" not in text and "NO Fc" not in text
    assert "Filter: ON HPQ Fc 80 Hz" in text
    only_mono = realtime.build_mic_config("Microfone X", None, rnnoise_available=(True, False))
    assert "VSTPlugin: Library rnnoise_mono.dll" in only_mono and "If:" not in only_mono


def test_install_isolates_the_microphone_from_general_lines():
    new = realtime.install_include(DANIEL_CONFIG)
    lines = new.splitlines()
    i_if = lines.index(realtime.IF_LINE)
    i_end = lines.index("EndIf:")
    # as 4 linhas originais ficam todas dentro do If, na mesma ordem
    assert lines[i_if + 1:i_end] == DANIEL_CONFIG.splitlines()
    assert lines[-2] == "Include: %s" % realtime.MIC_FILE
    assert realtime.is_installed(new)


def test_install_is_idempotent_and_reversible():
    once = realtime.install_include(DANIEL_CONFIG)
    assert realtime.install_include(once) == once
    assert realtime.uninstall_include(once).rstrip() == DANIEL_CONFIG.rstrip()


def test_install_keeps_device_lines_outside_the_if():
    text = "Preamp: -3 dB\nDevice: Speakers\nFilter: ON PK Fc 100 Hz Gain 3 dB Q 1\n# so comentario\n"
    new = realtime.install_include(text)
    lines = new.splitlines()
    dev = lines.index("Device: Speakers")
    assert lines[dev - 1] == realtime.BLOCK_END and lines[dev - 2] == "EndIf:"
    assert realtime.uninstall_include(new).rstrip() == text.rstrip()


def test_comments_only_config_is_not_wrapped():
    text = "# nada ativo aqui\n\n"
    new = realtime.install_include(text)
    assert realtime.IF_LINE not in new
    assert realtime.uninstall_include(new).strip() == text.strip()


def test_crlf_is_preserved():
    new = realtime.install_include(DANIEL_CONFIG.replace("\n", "\r\n"))
    assert "\r\n" in new and "\n" not in new.replace("\r\n", "")


def test_apply_and_remove_on_a_copy_of_the_config_folder(tmp_path):
    (tmp_path / "config.txt").write_text(DANIEL_CONFIG, encoding="utf-8")
    info = realtime.ApoInfo(tmp_path, tmp_path, True, True, None)
    mic = realtime.build_mic_config("mi mesa High Definition Audio Device", _noise_with_hum_and_tones())

    res = realtime.apply(info, mic)
    assert res.ok and res.config_changed
    assert (tmp_path / realtime.MIC_FILE).read_text(encoding="utf-8") == mic
    backup = tmp_path / realtime.BACKUP_NAME
    assert backup.read_text(encoding="utf-8") == DANIEL_CONFIG

    after_first = (tmp_path / "config.txt").read_text(encoding="utf-8")
    res2 = realtime.apply(info, mic)               # aplicar de novo não duplica nada
    assert res2.ok and not res2.config_changed
    assert (tmp_path / "config.txt").read_text(encoding="utf-8") == after_first
    assert backup.read_text(encoding="utf-8") == DANIEL_CONFIG   # backup guarda sempre o original

    off = realtime.remove(info)
    assert off.ok and off.config_changed
    assert (tmp_path / "config.txt").read_text(encoding="utf-8").rstrip() == DANIEL_CONFIG.rstrip()
    assert not (tmp_path / realtime.MIC_FILE).exists()
    assert not realtime.remove(info).config_changed  # desligar de novo é inofensivo


def test_apply_reports_permission_problem(tmp_path, monkeypatch):
    info = realtime.ApoInfo(tmp_path, tmp_path, True, True, None)
    monkeypatch.setattr(realtime.os, "access", lambda *a, **k: False)
    res = realtime.apply(info, "x")
    assert not res.ok and "administrador" in res.message
