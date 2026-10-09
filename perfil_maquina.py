"""Perfil de rendimiento de esta maquina.

Guarda los ajustes que mejor rinden en ESTE ordenador, medidos con
scripts/benchmark_perfil.py, en benchmarks/perfil-maquina.json. Los scripts del
proyecto (voxcpm_server.py, watch_generate.py, generate_script.py,
generate_shorts.py, benchmark_voxcpm_inference.py) leen aqui sus defaults.

El fichero es local: esta en benchmarks/.gitignore y la plantilla commiteada es
benchmarks/perfil-maquina.ejemplo.json. Si falta o esta corrupto se usan los
DEFAULTS historicos del repo (medidos en la RTX 3070, ver
docs/optimizacion-inferencia.md).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

PERFIL_PATH = Path(__file__).resolve().parent / "benchmarks" / "perfil-maquina.json"

DEFAULTS = {
    "servidor": {"optimize": True, "stop_check_batch": 2},
    "qa": {"model": "small", "device": "cpu"},
    "benchmark": {"bloques": 8, "warmup": 2},
}


def cargar(path=PERFIL_PATH):
    """Perfil mezclado sobre DEFAULTS. Nunca lanza: si el fichero falta o esta
    corrupto devuelve los defaults."""
    perfil = copy.deepcopy(DEFAULTS)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return perfil
    if not isinstance(data, dict):
        return perfil
    for seccion, valores in data.items():
        if isinstance(valores, dict) and isinstance(perfil.get(seccion), dict):
            perfil[seccion].update(valores)
        else:
            perfil[seccion] = valores
    return perfil


def ajuste(*ruta, default=None):
    """Lee un ajuste por ruta, p. ej. ajuste("servidor", "optimize")."""
    nodo = cargar()
    for clave in ruta:
        if not isinstance(nodo, dict) or clave not in nodo:
            return default
        nodo = nodo[clave]
    return nodo
