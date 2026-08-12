#!/usr/bin/env python3
"""Instala TODAS las dependencias del proyecto VoxCPM + scripts personalizados.

Instala el paquete local en modo editable con todos los extras:

  - dev        : pytest, black, flake8, pre-commit        (tests y lint)
  - timestamps : stable-ts                               (generar_srt.py, timestamps)
  - qa         : faster-whisper                          (audio_qa.py, generate_script.py,
                                                          generate_shorts.py, watch_generate.py)
  - compile    : triton-windows en Windows               (voxcpm_server.py --optimize,
                                                          torch.compile + CUDA graphs)

Uso:
  python install_deps.py            # instala en el intérprete Python actual
  python install_deps.py --venv     # usa venv_voxcpm/ (Scripts en Windows, bin en Unix)
  python install_deps.py --check    # solo verifica el entorno, no instala nada
  python install_deps.py --venv --check
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / "venv_voxcpm"
EXTRAS = "dev,timestamps,qa,compile"


def venv_python() -> Path | None:
    """Devuelve el python del venv del proyecto si existe."""
    if not VENV_DIR.is_dir():
        return None
    if sys.platform == "win32":
        p = VENV_DIR / "Scripts" / "python.exe"
    else:
        p = VENV_DIR / "bin" / "python"
    return p if p.is_file() else None


def run(cmd: list[str]) -> int:
    print(f"\n>>> {' '.join(cmd)}\n")
    return subprocess.call(cmd, cwd=str(ROOT))


def check_environment(python: str) -> None:
    """Verifica los componentes clave sin instalar nada."""
    print(f"\n=== Verificación del entorno ({python}) ===")
    checks = [
        ("torch instalado", "import torch; print(torch.__version__)"),
        ("CUDA disponible", "import torch; print(torch.cuda.is_available(), torch.version.cuda)"),
        ("triton (torch.compile)", "import triton; print(triton.__version__)"),
        ("faster-whisper (extra qa)", "import faster_whisper; print(faster_whisper.__version__)"),
        ("stable-ts (extra timestamps)", "import stable_ts; print(getattr(stable_ts, '__version__', 'ok'))"),
        ("voxcpm instalado (editable)", "import voxcpm; print(voxcpm.__file__)"),
    ]
    failed = 0
    for label, code in checks:
        r = subprocess.run(
            [python, "-c", code], capture_output=True, text=True, cwd=str(ROOT)
        )
        if r.returncode == 0:
            print(f"  [OK]   {label}: {r.stdout.strip()}")
        else:
            failed += 1
            err = (r.stderr or r.stdout).strip().splitlines()
            print(f"  [FALTA] {label}: {err[-1] if err else 'desconocido'}")
    print(f"\n  {len(checks) - failed}/{len(checks)} componentes correctos.")
    if failed:
        print("  Ejecuta 'python install_deps.py --venv' para instalarlo todo.")


def main() -> None:
    # Evita caracteres corruptos (acentos) en la consola de Windows.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--venv", action="store_true", help="instalar en venv_voxcpm/ (el venv del proyecto)")
    ap.add_argument("--check", action="store_true", help="solo verificar el entorno, no instalar")
    args = ap.parse_args()

    if args.venv:
        python = venv_python()
        if python is None:
            print(
                f"ERROR: no existe {VENV_DIR} con Python.\n"
                f"Créalo primero con:  python -m venv {VENV_DIR}"
            )
            sys.exit(1)
        python = str(python)
    else:
        python = sys.executable

    print(f"Proyecto: {ROOT}")
    print(f"Python:   {python}")

    if args.check:
        check_environment(python)
        return

    spec = f".[{EXTRAS}]"
    print(f"\nInstalando el paquete local con TODOS los extras: {spec}")
    print("  - dev, timestamps, qa, compile (triton-windows en Windows)")
    rc = run([python, "-m", "pip", "install", "-e", spec])
    if rc != 0:
        print("\nERROR: falló la instalación (código %d)." % rc)
        sys.exit(rc)

    print("\nInstalación completada. Verificando el entorno...")
    check_environment(python)

    print(
        "\nSiguientes pasos:\n"
        "  - Servidor TTS con torch.compile:  python voxcpm_server.py --optimize\n"
        "  - Primera ejecución con --optimize tarda unos minutos compilando.\n"
        "  - Si aparece 'torch.compile disabled - triton is not installed',\n"
        "    vuelve a ejecutar este script (el extra 'compile' lo instala)."
    )


if __name__ == "__main__":
    main()
