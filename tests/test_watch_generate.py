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


def test_manager_pending_watch_detects_interrupted_shorts(tmp_path):
    """Regresion del detector del lanzador: un SRT no vacio y la carpeta
    shorts no alcanzan si falta un WAV; debe coincidir con is_done()."""
    import voxcpm_manager

    watch_dir = tmp_path / "Generate"
    watch_dir.mkdir()
    txt_path = watch_dir / "cine shorts.txt"
    txt_path.write_text(
        "### SHORT 1\nScript:\nUno\n\n---\n### SHORT 2\nScript:\nDos\n",
        encoding="utf-8",
    )

    base_dir = watch_dir / "cine shorts"
    shorts_dir = base_dir / "shorts"
    shorts_dir.mkdir(parents=True)
    (base_dir / "cine shorts_completo.srt").write_text("#Short 1\n", encoding="utf-8")
    (shorts_dir / "short_1.wav").write_bytes(b"RIFF")

    proyecto = voxcpm_manager.Proyecto(alias="test", nombre="Test", vault=tmp_path, modo="watch")
    assert voxcpm_manager.plan_watch(txt_path, watch_dir)["ok"] is False
    assert voxcpm_manager.pendientes_watch(proyecto) == [txt_path]

    (shorts_dir / "short_2.wav").write_bytes(b"RIFF")
    assert voxcpm_manager.plan_watch(txt_path, watch_dir)["ok"] is True
    assert voxcpm_manager.pendientes_watch(proyecto) == []


def test_manager_resumen_voces_incluye_idioma_solo_de_canal(tmp_path):
    """Un idioma que solo aparece en un canal (arcanum) debe verse en el resumen."""
    import json

    import voxcpm_manager

    sensible = tmp_path / "Sensible"
    sensible.mkdir()
    (sensible / "voices.json").write_text(
        json.dumps(
            {
                "default": {"es": "juan_sanchez"},
                "canales": {"arcanum": {"en": "jax", "pt": "leo_antonio"}},
            }
        ),
        encoding="utf-8",
    )
    proyecto = voxcpm_manager.Proyecto(alias="test", nombre="Test", vault=tmp_path, modo="3lang")
    assert voxcpm_manager.resumen_voces(proyecto) == "es=juan_sanchez en=jax pt=leo_antonio"


def test_omitido_txt_con_marcador_done(tmp_path):
    assert watch_generate.omitido(str(tmp_path / "cine script.txt")) is False
    (tmp_path / "cine script.done.md").write_text("", encoding="utf-8")
    assert watch_generate.omitido(str(tmp_path / "cine script.txt")) is True


def test_process_file_omite_txt_marcado_done(tmp_path, monkeypatch):
    """El marcador saca el txt del automatico (scan_once); --txt manual sigue generando."""
    watch_dir = tmp_path
    txt_path = watch_dir / "cine script.txt"
    txt_path.write_text("hola", encoding="utf-8")
    (watch_dir / "cine script.done").write_text("", encoding="utf-8")
    _backdate(txt_path)
    calls = []
    monkeypatch.setattr(watch_generate.generate_script, "run", lambda **kw: calls.append(kw))
    watch_generate.scan_once(str(watch_dir), "http://127.0.0.1:8765", qa=None, validate_audio=False)
    assert calls == []
    watch_generate.process_file(str(txt_path), str(watch_dir), "http://127.0.0.1:8765",
                                qa=None, validate_audio=False)
    assert len(calls) == 1


def test_manager_pending_watch_ignora_txt_done(tmp_path):
    import voxcpm_manager

    watch_dir = tmp_path / "Generate"
    watch_dir.mkdir()
    txt_path = watch_dir / "cine script.txt"
    txt_path.write_text("hola", encoding="utf-8")
    (watch_dir / "cine script.done").write_text("", encoding="utf-8")
    proyecto = voxcpm_manager.Proyecto(alias="test", nombre="Test", vault=tmp_path, modo="watch")
    assert voxcpm_manager.pendientes_watch(proyecto) == []


def test_lineas_proyecto_etiqueta_cabe_en_la_fila(tmp_path, monkeypatch):
    import voxcpm_manager

    monkeypatch.setattr(voxcpm_manager, "resumen_voces", lambda p: "es=juan_sanchez en=jax pt=leo_antonio")
    monkeypatch.setattr(voxcpm_manager, "resumen_faltantes", lambda p, lista: "falta: es,en,pt")
    proyecto = voxcpm_manager.Proyecto(alias="t", nombre="Archivo Clasificado", vault=tmp_path, modo="3lang")

    lineas = voxcpm_manager.lineas_proyecto(5, proyecto, "3lang", ["caso"], "")

    assert len(lineas) == 2
    assert lineas[0].endswith("pend. falta: es,en,pt")
    assert all(len(linea) <= voxcpm_manager.ANCHO_MENU for linea in lineas)


def test_lineas_proyecto_etiqueta_larga_baja_junto_a_las_voces(tmp_path, monkeypatch):
    import voxcpm_manager

    monkeypatch.setattr(voxcpm_manager, "resumen_voces", lambda p: "es=david")
    monkeypatch.setattr(voxcpm_manager, "resumen_faltantes", lambda p, lista: "falta: enx2, pt, volumen")
    proyecto = voxcpm_manager.Proyecto(alias="t", nombre="Archivo Clasificado", vault=tmp_path, modo="3lang")

    lineas = voxcpm_manager.lineas_proyecto(1, proyecto, "3lang", ["caso"], "")

    assert len(lineas) == 2
    assert lineas[1] == "       falta: enx2, pt, volumen \u00b7 voces: es=david"
    assert all(len(linea) <= voxcpm_manager.ANCHO_MENU for linea in lineas)


def test_lineas_proyecto_etiqueta_y_voces_largas_en_lineas_separadas(tmp_path, monkeypatch):
    import voxcpm_manager

    monkeypatch.setattr(voxcpm_manager, "resumen_voces", lambda p: "es=juan_sanchez en=jax pt=leo_antonio")
    monkeypatch.setattr(voxcpm_manager, "resumen_faltantes", lambda p, lista: "falta: enx2, pt, volumen")
    proyecto = voxcpm_manager.Proyecto(alias="t", nombre="Archivo Clasificado", vault=tmp_path, modo="3lang")

    lineas = voxcpm_manager.lineas_proyecto(1, proyecto, "3lang", ["caso"], "")

    assert len(lineas) == 3
    assert lineas[1].strip() == "falta: enx2, pt, volumen"
    assert lineas[2].startswith("       voces:")
    assert all(len(linea) <= voxcpm_manager.ANCHO_MENU for linea in lineas)


def test_lineas_proyecto_sin_pendientes_ni_etiqueta(tmp_path, monkeypatch):
    import voxcpm_manager

    monkeypatch.setattr(voxcpm_manager, "resumen_voces", lambda p: "es=david")
    proyecto = voxcpm_manager.Proyecto(alias="t", nombre="Listas", vault=tmp_path, modo="watch")

    lineas = voxcpm_manager.lineas_proyecto(1, proyecto, "watch", [], "")

    assert len(lineas) == 2
    assert "falta:" not in lineas[0]


def test_resaltar_proyecto_no_cambia_el_ancho_visible(tmp_path, monkeypatch):
    import re

    import voxcpm_manager

    monkeypatch.setattr(voxcpm_manager, "resumen_voces", lambda p: "es=david")
    monkeypatch.setattr(voxcpm_manager, "colorido", lambda texto, color, activo=None: f"\033[1m{texto}\033[0m")
    proyecto = voxcpm_manager.Proyecto(alias="t", nombre="Listas de Famosos", vault=tmp_path, modo="watch")

    linea = voxcpm_manager.lineas_proyecto(1, proyecto, "watch", [], "")[0]
    resaltada = voxcpm_manager.resaltar_proyecto(linea, proyecto)

    assert "\033[1m" in resaltada
    assert re.sub(r"\033\[[0-9;]*m", "", resaltada) == linea


def test_resaltar_pendiente_solo_con_pendientes(tmp_path, monkeypatch):
    import voxcpm_manager

    monkeypatch.setattr(voxcpm_manager, "resumen_voces", lambda p: "es=david")
    monkeypatch.setattr(voxcpm_manager, "colorido", lambda texto, color, activo=None: f"\033[1m{texto}\033[0m")
    proyecto = voxcpm_manager.Proyecto(alias="t", nombre="Listas", vault=tmp_path, modo="watch")

    con_pendientes = voxcpm_manager.lineas_proyecto(1, proyecto, "watch", ["a", "b"], "")[0]
    sin_pendientes = voxcpm_manager.lineas_proyecto(1, proyecto, "watch", [], "")[0]

    assert "\033[1m" in voxcpm_manager.resaltar_pendiente(con_pendientes, 2)
    assert voxcpm_manager.resaltar_pendiente(sin_pendientes, 0) == sin_pendientes


def test_color_proyecto_es_estable_por_alias(tmp_path):
    import voxcpm_manager

    def proyecto(alias, nombre):
        return voxcpm_manager.Proyecto(alias=alias, nombre=nombre, vault=tmp_path, modo="auto")

    primero = proyecto("listas", "Listas de Famosos")
    segundo = proyecto("listas", "Otro nombre")
    otro = proyecto("chismes", "Chismes MX")

    assert voxcpm_manager.color_proyecto(primero) == voxcpm_manager.color_proyecto(segundo)
    assert voxcpm_manager.color_proyecto(primero) in voxcpm_manager.COLORES
    assert voxcpm_manager.color_proyecto(otro) in voxcpm_manager.COLORES


def test_bullet_segun_codificacion_de_consola():
    import voxcpm_manager

    assert voxcpm_manager.bullet(consola_utf8=True) == "\u00b7"
    assert voxcpm_manager.bullet(consola_utf8=False) == "-"


def test_colorido_respeta_terminal():
    import voxcpm_manager

    assert voxcpm_manager.colorido("x", "\033[96m", activo=True) == "\033[96mx\033[0m"
    assert voxcpm_manager.colorido("x", "\033[96m", activo=False) == "x"
