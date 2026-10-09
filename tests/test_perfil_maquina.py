import importlib.util
import json
from pathlib import Path

import perfil_maquina

ROOT = Path(__file__).resolve().parents[1]
PERFIL_SCRIPT = ROOT / "scripts" / "benchmark_perfil.py"
EJEMPLO = ROOT / "benchmarks" / "perfil-maquina.ejemplo.json"

spec = importlib.util.spec_from_file_location("benchmark_perfil", PERFIL_SCRIPT)
benchmark_perfil = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(benchmark_perfil)


def test_cargar_sin_fichero_devuelve_defaults(tmp_path):
    assert perfil_maquina.cargar(tmp_path / "no-existe.json") == perfil_maquina.DEFAULTS


def test_cargar_con_json_corrupto_devuelve_defaults(tmp_path):
    roto = tmp_path / "perfil.json"
    roto.write_text("{no es json", encoding="utf-8")

    assert perfil_maquina.cargar(roto) == perfil_maquina.DEFAULTS


def test_cargar_mezcla_secciones_y_conserva_extras(tmp_path):
    perfil_json = tmp_path / "perfil.json"
    perfil_json.write_text(
        json.dumps({"servidor": {"optimize": False}, "maquina": {"gpu": "RTX 3070"}}),
        encoding="utf-8",
    )

    perfil = perfil_maquina.cargar(perfil_json)

    assert perfil["servidor"] == {"optimize": False, "stop_check_batch": 2}
    assert perfil["qa"] == perfil_maquina.DEFAULTS["qa"]
    assert perfil["maquina"]["gpu"] == "RTX 3070"


def test_ajuste_devuelve_default_si_falta_la_ruta():
    # Solo rutas inexistentes: el valor de claves reales depende del perfil de
    # la maquina donde corra el test (benchmarks/perfil-maquina.json).
    assert perfil_maquina.ajuste("servidor", "no_existe", default="x") == "x"
    assert perfil_maquina.ajuste("seccion_inexistente", "clave", default="x") == "x"


def test_ejemplo_commiteado_es_valido_para_el_loader():
    data = json.loads(EJEMPLO.read_text(encoding="utf-8"))

    perfil = perfil_maquina.cargar(EJEMPLO)

    assert data["version"] == 1
    assert perfil["servidor"]["optimize"] is True
    assert perfil["servidor"]["stop_check_batch"] == 2
    assert perfil["qa"]["model"]
    assert perfil["qa"]["device"]
    assert perfil["benchmark"]["bloques"] > 0
    assert perfil["maquina"]["gpu"]


def test_elegir_ganador_queda_con_el_mejor_ratio_mediano():
    mediciones = [
        {"completed": 8, "median_ratio": 2.06, "peak_reserved_mb": 5782},
        {"completed": 8, "median_ratio": 1.04, "peak_reserved_mb": 6360},
        {"completed": 8, "median_ratio": 1.5, "peak_reserved_mb": 6200},
    ]

    ganador = benchmark_perfil.elegir_ganador(mediciones)

    assert ganador["median_ratio"] == 1.04


def test_elegir_ganador_ignora_configs_fallidas_y_empata_por_vram():
    mediciones = [
        {"completed": 0, "median_ratio": 0.0, "peak_reserved_mb": 0},
        {"completed": 8, "median_ratio": 1.04, "peak_reserved_mb": 6360},
        {"completed": 8, "median_ratio": 1.04, "peak_reserved_mb": 6300},
    ]

    ganador = benchmark_perfil.elegir_ganador(mediciones)

    assert ganador is not None
    assert ganador["peak_reserved_mb"] == 6300


def test_elegir_ganador_devuelve_none_si_todo_fallo():
    assert benchmark_perfil.elegir_ganador([{"completed": 0, "median_ratio": 0.0}]) is None


def test_elegir_qa_ganador_elige_el_mas_rapido_que_pasa_sus_umbrales():
    candidatos = [
        {"model": "tiny", "total_s": 2.0, "pasa_qa": False},
        {"model": "small", "total_s": 5.4, "pasa_qa": True},
        {"model": "base", "total_s": 4.1, "pasa_qa": True},
    ]

    assert benchmark_perfil.elegir_qa_ganador(candidatos)["model"] == "base"


def test_comando_server_pasa_optimize_explicito_en_ambos_casos():
    cmd_eager, env_eager = benchmark_perfil.comando_server({"optimize": False, "stop_check_batch": 1}, 8765)
    cmd_opt, env_opt = benchmark_perfil.comando_server({"optimize": True, "stop_check_batch": 2}, 8765)

    # Sin --no-optimize explicito, el server heredaria el default del perfil y
    # la config "eager" mediria en realidad con compile (barrido sucio).
    assert "--no-optimize" in cmd_eager
    assert "--optimize" in cmd_opt
    assert env_eager["VOXCPM_STOP_CHECK_BATCH"] == "1"
    assert env_opt["VOXCPM_STOP_CHECK_BATCH"] == "2"


def test_combinaciones_cubre_eager_y_los_batches_de_optimize():
    configs = benchmark_perfil.combinaciones()

    assert {"optimize": False, "stop_check_batch": 1} in configs
    assert {"optimize": True, "stop_check_batch": 1} in configs
    assert {"optimize": True, "stop_check_batch": 2} in configs
    assert {"optimize": True, "stop_check_batch": 4} in configs
    assert len(configs) == 4


def test_construir_perfil_guarda_el_config_ganador_y_su_origen(monkeypatch):
    monkeypatch.setattr(benchmark_perfil, "fingerprint", lambda: {"gpu": "GPU-de-test"})
    ganador = {"config": {"optimize": True, "stop_check_batch": 2}, "completed": 8, "median_ratio": 1.04}
    medicoes = {
        "optimize=false batch=1": {
            "config": {"optimize": False, "stop_check_batch": 1},
            "completed": 8,
            "median_ratio": 2.06,
        }
    }

    perfil = benchmark_perfil.construir_perfil(
        medicoes | {"optimize=true batch=2": ganador}, {"model": "small"}, 8, 2, 1234
    )

    assert perfil["origen"] == "barrido"
    assert perfil["servidor"] == {"optimize": True, "stop_check_batch": 2}
    assert perfil["maquina"] == {"gpu": "GPU-de-test"}
    assert perfil["benchmark"]["seed"] == 1234
    assert "optimize=true batch=2" in perfil["metricas"]


def test_construir_perfil_devuelve_none_si_no_hay_ganador():
    assert benchmark_perfil.construir_perfil({"a": {"completed": 0, "median_ratio": 0.0}}, {}, 8, 2, 1234) is None
