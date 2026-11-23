import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from voz_3idiomas import caso_omitido, leer_escenas, marcar_done


def test_leer_escenas_ignora_notas_finales(tmp_path):
    """El guion cierra con '## Notas'; no es una escena."""
    guion = tmp_path / "2_guion.md"
    guion.write_text(
        "# Guion\n\nEscena uno.\n\nEscena dos.\n\n---\n\n## Notas\n\n- Enlil: en-LIL.\n",
        encoding="utf-8",
    )

    assert leer_escenas(guion) == ["Escena uno.", "Escena dos."]


def test_caso_omitido_con_archivo_done(tmp_path):
    caso = tmp_path / "caso"
    caso.mkdir()
    assert caso_omitido(caso) is False
    (caso / "done.md").write_text("", encoding="utf-8")
    assert caso_omitido(caso) is True


def test_caso_omitido_con_status_published(tmp_path):
    caso = tmp_path / "caso"
    caso.mkdir()
    (caso / "5_publicacion.md").write_text("# Pub\n\nTexto.\n\nStatus: Published\n", encoding="utf-8")
    assert caso_omitido(caso) is True
    (caso / "5_publicacion.md").write_text("# Pub\n\nTexto.\n", encoding="utf-8")
    assert caso_omitido(caso) is False


def test_marcar_done_crea_archivo(tmp_path):
    caso = tmp_path / "caso"
    caso.mkdir()
    marcar_done(caso)
    assert (caso / "done").is_file()
    assert caso_omitido(caso) is True
