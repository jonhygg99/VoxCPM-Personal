import os
import socket
import time

import watch_generate


def _backdate(path, seconds=3600):
    """Sets a file's mtime well in the past so the MIN_AGE_SECONDS guard doesn't skip it."""
    past = time.time() - seconds
    os.utime(path, (past, past))


def test_plan_outputs_classifies_script_file():
    watch_dir = r"D:\Obsidian Vaults\Chismes MX\Generate"
    txt_path = os.path.join(watch_dir, "cine script.txt")

    plan = watch_generate.plan_outputs(txt_path, watch_dir)

    assert plan.kind == "script"
    assert plan.base == "cine"
    assert plan.stem == "cine script"
    assert plan.base_dir == os.path.join(watch_dir, "cine script")
    assert plan.blocks_dir == os.path.join(watch_dir, "cine script", "bloques")
    assert plan.final_output == os.path.join(watch_dir, "cine script", "cine script_completo.wav")
    assert plan.srt_output == os.path.join(watch_dir, "cine script", "cine script_completo.srt")
    assert plan.done_marker == plan.final_output


def test_plan_outputs_classifies_shorts_file():
    watch_dir = r"D:\Obsidian Vaults\Chismes MX\Generate"
    txt_path = os.path.join(watch_dir, "cine shorts.txt")

    plan = watch_generate.plan_outputs(txt_path, watch_dir)

    assert plan.kind == "shorts"
    assert plan.base == "cine"
    assert plan.stem == "cine shorts"
    assert plan.base_dir == os.path.join(watch_dir, "cine shorts")
    assert plan.blocks_dir == os.path.join(watch_dir, "cine shorts", "bloques")
    assert plan.shorts_dir == os.path.join(watch_dir, "cine shorts", "shorts")
    assert plan.srt_output == os.path.join(watch_dir, "cine shorts", "cine shorts_completo.srt")
    assert plan.done_marker == plan.srt_output


def test_plan_outputs_treats_any_other_name_as_script():
    watch_dir = r"D:\Obsidian Vaults\Chismes MX\Generate"
    txt_path = os.path.join(watch_dir, "notas.txt")

    plan = watch_generate.plan_outputs(txt_path, watch_dir)

    assert plan.kind == "script"
    assert plan.base == "notas"


def test_plan_outputs_handles_multi_word_base():
    watch_dir = r"D:\Obsidian Vaults\Chismes MX\Generate"
    txt_path = os.path.join(watch_dir, "el escandalo de la semana script.txt")

    plan = watch_generate.plan_outputs(txt_path, watch_dir)

    assert plan.kind == "script"
    assert plan.base == "el escandalo de la semana"
    assert plan.base_dir == os.path.join(watch_dir, "el escandalo de la semana script")


def test_plan_outputs_trims_dash_and_underscore_suffixes_from_base():
    """El vault nombra "<iniciales>-<carpeta>-script.txt"; el base debe quedar limpio."""
    watch_dir = r"D:\Obsidian Vaults\Chismes MX\Generate"

    dash = watch_generate.plan_outputs(os.path.join(watch_dir, "MX-cine-script.txt"), watch_dir)
    assert dash.kind == "script"
    assert dash.base == "MX-cine"
    assert dash.base_dir == os.path.join(watch_dir, "MX-cine-script")

    underscore = watch_generate.plan_outputs(os.path.join(watch_dir, "MX_cine_script.txt"), watch_dir)
    assert underscore.kind == "script"
    assert underscore.base == "MX_cine"
    assert underscore.base_dir == os.path.join(watch_dir, "MX_cine_script")

    shorts = watch_generate.plan_outputs(os.path.join(watch_dir, "MX-cine-shorts.txt"), watch_dir)
    assert shorts.kind == "shorts"
    assert shorts.base == "MX-cine"
    assert shorts.shorts_dir == os.path.join(watch_dir, "MX-cine-shorts", "shorts")


def test_plan_outputs_does_not_trim_transcript():
    watch_dir = r"D:\Obsidian Vaults\Chismes MX\Generate"

    plan = watch_generate.plan_outputs(os.path.join(watch_dir, "canal transcript.txt"), watch_dir)

    assert plan.kind == "script"
    assert plan.base == "canal transcript"


def test_process_file_skips_when_done_marker_exists(tmp_path, monkeypatch, capsys):
    watch_dir = tmp_path
    txt_path = watch_dir / "cine script.txt"
    txt_path.write_text("hola", encoding="utf-8")

    base_dir = watch_dir / "cine script"
    base_dir.mkdir()
    (base_dir / "cine script_completo.wav").write_bytes(b"RIFF")

    def fail_run(**kwargs):
        raise AssertionError("no deberia regenerar un txt ya completado")

    monkeypatch.setattr(watch_generate.generate_script, "run", fail_run)

    watch_generate.process_file(str(txt_path), str(watch_dir), "http://127.0.0.1:8765")


def test_process_file_skips_recently_modified_file(tmp_path, monkeypatch):
    watch_dir = tmp_path
    txt_path = watch_dir / "cine script.txt"
    txt_path.write_text("hola", encoding="utf-8")

    def fail_run(**kwargs):
        raise AssertionError("no deberia procesar un archivo recien modificado")

    monkeypatch.setattr(watch_generate.generate_script, "run", fail_run)
    monkeypatch.setattr(watch_generate, "MIN_AGE_SECONDS", 3600)

    watch_generate.process_file(str(txt_path), str(watch_dir), "http://127.0.0.1:8765")


def test_process_file_calls_generate_script_run_with_planned_paths(tmp_path, monkeypatch):
    watch_dir = tmp_path
    txt_path = watch_dir / "cine script.txt"
    txt_path.write_text("hola", encoding="utf-8")
    _backdate(txt_path)

    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(watch_generate.generate_script, "run", fake_run)

    watch_generate.process_file(str(txt_path), str(watch_dir), "http://127.0.0.1:8765")

    assert len(calls) == 1
    call = calls[0]
    assert call["script_file"] == str(txt_path)
    assert call["blocks_dir"] == os.path.join(str(watch_dir), "cine script", "bloques")
    assert call["final_output"] == os.path.join(str(watch_dir), "cine script", "cine script_completo.wav")
    assert call["srt_output"] == os.path.join(str(watch_dir), "cine script", "cine script_completo.srt")
    assert call["server_url"] == "http://127.0.0.1:8765"


def test_process_file_calls_generate_shorts_run_with_planned_paths(tmp_path, monkeypatch):
    watch_dir = tmp_path
    txt_path = watch_dir / "cine shorts.txt"
    txt_path.write_text("hola", encoding="utf-8")
    _backdate(txt_path)

    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(watch_generate.generate_shorts, "run", fake_run)

    watch_generate.process_file(str(txt_path), str(watch_dir), "http://127.0.0.1:8765")

    assert len(calls) == 1
    call = calls[0]
    assert call["script_file"] == str(txt_path)
    assert call["shorts_dir"] == os.path.join(str(watch_dir), "cine shorts", "shorts")
    assert call["bloques_dir"] == os.path.join(str(watch_dir), "cine shorts", "bloques")
    assert call["srt_output"] == os.path.join(str(watch_dir), "cine shorts", "cine shorts_completo.srt")


def test_is_done_for_script_checks_final_wav(tmp_path):
    plan = watch_generate.plan_outputs(str(tmp_path / "cine script.txt"), str(tmp_path))

    assert watch_generate.is_done(plan) is False


def test_is_done_for_shorts_ignores_empty_srt_marker(tmp_path):
    """Regresion: un SRT vacio (misparse -> generate_shorts.run no genero nada)
    no debe contar como 'hecho' aunque exista la carpeta y el done_marker viejo."""
    watch_dir = tmp_path
    plan = watch_generate.plan_outputs(str(watch_dir / "cine shorts.txt"), str(watch_dir))

    base_dir = watch_dir / "cine shorts"
    base_dir.mkdir()
    (base_dir / "bloques").mkdir()
    (base_dir / "shorts").mkdir()
    (base_dir / "cine shorts_completo.srt").write_text("", encoding="utf-8")

    assert watch_generate.is_done(plan) is False


def test_is_done_for_shorts_true_when_wav_present(tmp_path):
    watch_dir = tmp_path
    plan = watch_generate.plan_outputs(str(watch_dir / "cine shorts.txt"), str(watch_dir))

    shorts_dir = watch_dir / "cine shorts" / "shorts"
    shorts_dir.mkdir(parents=True)
    (shorts_dir / "short_1.wav").write_bytes(b"RIFF")

    assert watch_generate.is_done(plan) is False


def test_is_done_for_shorts_requires_srt_and_every_short(tmp_path):
    """Un wav suelto no cuenta: la corrida pudo cortarse a mitad (bug real)."""
    watch_dir = tmp_path
    txt_path = watch_dir / "cine shorts.txt"
    txt_path.write_text(
        "### SHORT 1\nScript:\nUno\n\n---\n### SHORT 2\nScript:\nDos\n\n---\n"
        "### SHORT 3\nScript:\nTres\n",
        encoding="utf-8",
    )
    plan = watch_generate.plan_outputs(str(txt_path), str(watch_dir))

    base_dir = watch_dir / "cine shorts"
    shorts_dir = base_dir / "shorts"
    shorts_dir.mkdir(parents=True)
    (shorts_dir / "short_1.wav").write_bytes(b"RIFF")

    # Sin SRT: la corrida no llego al final -> hay que reintentar.
    assert watch_generate.is_done(plan, str(txt_path)) is False

    # SRT no vacio, pero el txt declara 3 shorts y solo hay 1 wav.
    (base_dir / "cine shorts_completo.srt").write_text("#Short 1\n", encoding="utf-8")
    assert watch_generate.is_done(plan, str(txt_path)) is False

    # Con los tres wav, ya esta completo.
    for idx in (2, 3):
        (shorts_dir / f"short_{idx}.wav").write_bytes(b"RIFF")
    assert watch_generate.is_done(plan, str(txt_path)) is True


def test_process_file_retries_shorts_after_interrupted_run(tmp_path, monkeypatch):
    """Regresion del bug: short_1.wav sin SRT (corrida cortada) debe reintentarse."""
    watch_dir = tmp_path
    txt_path = watch_dir / "cine shorts.txt"
    txt_path.write_text("hola", encoding="utf-8")
    _backdate(txt_path)

    shorts_dir = watch_dir / "cine shorts" / "shorts"
    shorts_dir.mkdir(parents=True)
    (shorts_dir / "short_1.wav").write_bytes(b"RIFF")

    calls = []

    monkeypatch.setattr(watch_generate.generate_shorts, "run", lambda **kw: calls.append(kw))

    watch_generate.process_file(str(txt_path), str(watch_dir), "http://127.0.0.1:8765")

    assert len(calls) == 1


def test_process_file_regenerates_shorts_with_empty_srt_marker(tmp_path, monkeypatch):
    """El escenario real del bug: carpetas creadas + srt vacio de una corrida
    anterior con formato mal parseado. La siguiente pasada debe reintentar."""
    watch_dir = tmp_path
    txt_path = watch_dir / "cine shorts.txt"
    txt_path.write_text("hola", encoding="utf-8")
    _backdate(txt_path)

    base_dir = watch_dir / "cine shorts"
    (base_dir / "bloques").mkdir(parents=True)
    (base_dir / "shorts").mkdir()
    (base_dir / "cine shorts_completo.srt").write_text("", encoding="utf-8")

    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(watch_generate.generate_shorts, "run", fake_run)

    watch_generate.process_file(str(txt_path), str(watch_dir), "http://127.0.0.1:8765")

    assert len(calls) == 1


def test_scan_once_processes_script_and_shorts_of_same_channel_separately(tmp_path, monkeypatch):
    watch_dir = tmp_path
    script_txt = watch_dir / "cine script.txt"
    shorts_txt = watch_dir / "cine shorts.txt"
    script_txt.write_text("hola", encoding="utf-8")
    shorts_txt.write_text("hola", encoding="utf-8")
    _backdate(script_txt)
    _backdate(shorts_txt)

    script_calls = []
    shorts_calls = []

    monkeypatch.setattr(watch_generate.generate_script, "run", lambda **kw: script_calls.append(kw))
    monkeypatch.setattr(watch_generate.generate_shorts, "run", lambda **kw: shorts_calls.append(kw))

    watch_generate.scan_once(str(watch_dir), "http://127.0.0.1:8765")

    assert len(script_calls) == 1
    assert len(shorts_calls) == 1
    assert script_calls[0]["blocks_dir"] == os.path.join(str(watch_dir), "cine script", "bloques")
    assert shorts_calls[0]["bloques_dir"] == os.path.join(str(watch_dir), "cine shorts", "bloques")
    assert script_calls[0]["blocks_dir"] != shorts_calls[0]["bloques_dir"]

def test_claim_instance_lock_is_exclusive(monkeypatch):
    """Un segundo watcher en la misma maquina no debe arrancar."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setattr(watch_generate, "INSTANCE_LOCK_PORT", port)

    first = watch_generate._claim_instance_lock()
    try:
        assert first is not None
        assert watch_generate._claim_instance_lock() is None
    finally:
        first.close()

    again = watch_generate._claim_instance_lock()
    try:
        assert again is not None
    finally:
        again.close()
