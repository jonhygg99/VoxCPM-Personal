import os
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
    assert plan.base_dir == os.path.join(watch_dir, "cine")
    assert plan.blocks_dir == os.path.join(watch_dir, "cine", "bloques")
    assert plan.final_output == os.path.join(watch_dir, "cine", "cine script_completo.wav")
    assert plan.srt_output == os.path.join(watch_dir, "cine", "cine script_completo.srt")
    assert plan.done_marker == plan.final_output


def test_plan_outputs_classifies_shorts_file():
    watch_dir = r"D:\Obsidian Vaults\Chismes MX\Generate"
    txt_path = os.path.join(watch_dir, "cine shorts.txt")

    plan = watch_generate.plan_outputs(txt_path, watch_dir)

    assert plan.kind == "shorts"
    assert plan.base == "cine"
    assert plan.stem == "cine shorts"
    assert plan.base_dir == os.path.join(watch_dir, "cine")
    assert plan.blocks_dir == os.path.join(watch_dir, "cine", "bloques")
    assert plan.shorts_dir == os.path.join(watch_dir, "cine", "cine shorts")
    assert plan.srt_output == os.path.join(watch_dir, "cine", "cine shorts_completo.srt")
    assert plan.done_marker == plan.srt_output


def test_plan_outputs_ignores_unrecognized_suffix():
    watch_dir = r"D:\Obsidian Vaults\Chismes MX\Generate"
    txt_path = os.path.join(watch_dir, "notas.txt")

    plan = watch_generate.plan_outputs(txt_path, watch_dir)

    assert plan.kind is None
    assert plan.base == "notas"


def test_plan_outputs_handles_multi_word_base():
    watch_dir = r"D:\Obsidian Vaults\Chismes MX\Generate"
    txt_path = os.path.join(watch_dir, "el escandalo de la semana script.txt")

    plan = watch_generate.plan_outputs(txt_path, watch_dir)

    assert plan.kind == "script"
    assert plan.base == "el escandalo de la semana"
    assert plan.base_dir == os.path.join(watch_dir, "el escandalo de la semana")


def test_process_file_skips_when_done_marker_exists(tmp_path, monkeypatch, capsys):
    watch_dir = tmp_path
    txt_path = watch_dir / "cine script.txt"
    txt_path.write_text("hola", encoding="utf-8")

    base_dir = watch_dir / "cine"
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
    assert call["blocks_dir"] == os.path.join(str(watch_dir), "cine", "bloques")
    assert call["final_output"] == os.path.join(str(watch_dir), "cine", "cine script_completo.wav")
    assert call["srt_output"] == os.path.join(str(watch_dir), "cine", "cine script_completo.srt")
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
    assert call["shorts_dir"] == os.path.join(str(watch_dir), "cine", "cine shorts")
    assert call["bloques_dir"] == os.path.join(str(watch_dir), "cine", "bloques")
    assert call["srt_output"] == os.path.join(str(watch_dir), "cine", "cine shorts_completo.srt")


def test_is_done_for_script_checks_final_wav():
    watch_dir = r"D:\Obsidian Vaults\Chismes MX\Generate"
    plan = watch_generate.plan_outputs(os.path.join(watch_dir, "cine script.txt"), watch_dir)

    assert watch_generate.is_done(plan) is False


def test_is_done_for_shorts_ignores_empty_srt_marker(tmp_path):
    """Regresion: un SRT vacio (misparse -> generate_shorts.run no genero nada)
    no debe contar como 'hecho' aunque exista la carpeta y el done_marker viejo."""
    watch_dir = tmp_path
    plan = watch_generate.plan_outputs(str(watch_dir / "cine shorts.txt"), str(watch_dir))

    base_dir = watch_dir / "cine"
    base_dir.mkdir()
    (base_dir / "bloques").mkdir()
    (base_dir / "cine shorts").mkdir()
    (base_dir / "cine shorts_completo.srt").write_text("", encoding="utf-8")

    assert watch_generate.is_done(plan) is False


def test_is_done_for_shorts_true_when_wav_present(tmp_path):
    watch_dir = tmp_path
    plan = watch_generate.plan_outputs(str(watch_dir / "cine shorts.txt"), str(watch_dir))

    shorts_dir = watch_dir / "cine" / "cine shorts"
    shorts_dir.mkdir(parents=True)
    (shorts_dir / "short_1.wav").write_bytes(b"RIFF")

    assert watch_generate.is_done(plan) is True


def test_process_file_regenerates_shorts_with_empty_srt_marker(tmp_path, monkeypatch):
    """El escenario real del bug: carpetas creadas + srt vacio de una corrida
    anterior con formato mal parseado. La siguiente pasada debe reintentar."""
    watch_dir = tmp_path
    txt_path = watch_dir / "cine shorts.txt"
    txt_path.write_text("hola", encoding="utf-8")
    _backdate(txt_path)

    base_dir = watch_dir / "cine"
    (base_dir / "bloques").mkdir(parents=True)
    (base_dir / "cine shorts").mkdir()
    (base_dir / "cine shorts_completo.srt").write_text("", encoding="utf-8")

    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(watch_generate.generate_shorts, "run", fake_run)

    watch_generate.process_file(str(txt_path), str(watch_dir), "http://127.0.0.1:8765")

    assert len(calls) == 1
