"""Asegura que los modulos de app/ y la raiz (voices/) importen en los tests."""

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
for ruta in (RAIZ / "app", RAIZ):
    if str(ruta) not in sys.path:
        sys.path.insert(0, str(ruta))
