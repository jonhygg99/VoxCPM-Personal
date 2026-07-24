"""Vigilante de carpeta: detecta .txt nuevos y genera su audio con VoxCPM.

Escanea WATCH_DIR cada INTERVAL_SECONDS. Cada .txt se clasifica por el sufijo
de su nombre (sin extension):
  - termina en " script" -> logica de generate_script.py
  - termina en " shorts" -> logica de generate_shorts.py
  - cualquier otro nombre -> se ignora

Ejemplo: "cine script.txt" -> base "cine". Todo lo generado para esa base vive
en WATCH_DIR/cine/ (audio final, srt, bloques intermedios y, para shorts, la
subcarpeta con un wav por short).

Requiere que voxcpm_server.py ya este corriendo (el modelo vive ahi, no aqui).
"""

import argparse
import glob
import os
import time
import traceback
from dataclasses import dataclass
from typing import Optional

import generate_script
import generate_shorts
from voxcpm_client import DEFAULT_SERVER_URL, VoxCPMServerError, check_server

DEFAULT_WATCH_DIR = r"D:\Obsidian Vaults\Chismes MX\Generate"
DEFAULT_INTERVAL_SECONDS = 120
MIN_AGE_SECONDS = 5  # ignora .txt modificados hace menos de esto (puede seguir escribiendose)

SCRIPT_SUFFIX = " script"
SHORTS_SUFFIX = " shorts"


@dataclass(frozen=True)
class GeneratePlan:
    kind: Optional[str]  # "script", "shorts" o None (no clasificable)
    stem: str
    base: str
    base_dir: str
    blocks_dir: str
    done_marker: str  # si existe, el txt ya esta generado
    final_output: Optional[str] = None  # solo "script"
    srt_output: Optional[str] = None
    shorts_dir: Optional[str] = None  # solo "shorts"


def plan_outputs(txt_path, watch_dir):
    """Calcula rutas de salida para un .txt segun las convenciones de nombres.

    Funcion pura (sin I/O) para poder testearla sin tocar disco.
    """
    stem = os.path.splitext(os.path.basename(txt_path))[0]
    stem_lower = stem.lower()

    if stem_lower.endswith(SCRIPT_SUFFIX):
        kind = "script"
        base = stem[: -len(SCRIPT_SUFFIX)].strip()
    elif stem_lower.endswith(SHORTS_SUFFIX):
        kind = "shorts"
        base = stem[: -len(SHORTS_SUFFIX)].strip()
    else:
        kind = None
        base = stem

    base_dir = os.path.join(watch_dir, base)
    blocks_dir = os.path.join(base_dir, "bloques")

    if kind == "script":
        final_output = os.path.join(base_dir, f"{stem}_completo.wav")
        srt_output = os.path.join(base_dir, f"{stem}_completo.srt")
        return GeneratePlan(
            kind=kind,
            stem=stem,
            base=base,
            base_dir=base_dir,
            blocks_dir=blocks_dir,
            done_marker=final_output,
            final_output=final_output,
            srt_output=srt_output,
        )

    if kind == "shorts":
        shorts_dir = os.path.join(base_dir, stem)
        srt_output = os.path.join(base_dir, f"{stem}_completo.srt")
        return GeneratePlan(
            kind=kind,
            stem=stem,
            base=base,
            base_dir=base_dir,
            blocks_dir=blocks_dir,
            done_marker=srt_output,
            shorts_dir=shorts_dir,
            srt_output=srt_output,
        )

    return GeneratePlan(
        kind=None,
        stem=stem,
        base=base,
        base_dir=base_dir,
        blocks_dir=blocks_dir,
        done_marker="",
    )


def is_done(plan):
    """True si ya hay audio generado para este plan (no solo carpetas/SRT vacios)."""
    if plan.kind == "script":
        return os.path.exists(plan.final_output)

    if plan.kind == "shorts":
        if not plan.shorts_dir or not os.path.isdir(plan.shorts_dir):
            return False
        return bool(glob.glob(os.path.join(plan.shorts_dir, "*.wav")))

    return False


def process_file(txt_path, watch_dir, server_url):
    plan = plan_outputs(txt_path, watch_dir)

    if plan.kind is None:
        print(
            f"Saltando '{txt_path}': el nombre no termina en "
            f"'{SCRIPT_SUFFIX.strip()}' ni '{SHORTS_SUFFIX.strip()}'."
        )
        return

    if is_done(plan):
        return  # ya generado en una pasada anterior

    mtime = os.path.getmtime(txt_path)
    if time.time() - mtime < MIN_AGE_SECONDS:
        print(f"'{txt_path}' se modifico hace muy poco, se revisa en el siguiente ciclo.")
        return

    print(f"\n=== Procesando '{txt_path}' ({plan.kind}) ===")
    if plan.kind == "script":
        generate_script.run(
            script_file=txt_path,
            blocks_dir=plan.blocks_dir,
            final_output=plan.final_output,
            srt_output=plan.srt_output,
            server_url=server_url,
        )
    else:
        generate_shorts.run(
            script_file=txt_path,
            shorts_dir=plan.shorts_dir,
            bloques_dir=plan.blocks_dir,
            srt_output=plan.srt_output,
            server_url=server_url,
        )


def scan_once(watch_dir, server_url):
    pattern = os.path.join(watch_dir, "*.txt")
    for txt_path in sorted(glob.glob(pattern)):
        try:
            process_file(txt_path, watch_dir, server_url)
        except Exception as exc:
            print(f"ERROR procesando '{txt_path}': {exc}")
            traceback.print_exc()


def main():
    parser = argparse.ArgumentParser(description="Vigilante de carpeta: genera voz VoxCPM para .txt nuevos.")
    parser.add_argument("--watch-dir", default=DEFAULT_WATCH_DIR)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--server-url", default=DEFAULT_SERVER_URL)
    parser.add_argument("--once", action="store_true", help="Ejecuta un solo ciclo y termina (util para pruebas).")
    args = parser.parse_args()

    print(f"Vigilando '{args.watch_dir}' cada {args.interval:.0f}s (servidor: {args.server_url})")
    print("Deja esta ventana abierta. El modelo debe estar cargado en voxcpm_server.py.")

    while True:
        try:
            check_server(args.server_url)
        except VoxCPMServerError as exc:
            print(f"Servidor VoxCPM no disponible: {exc}")
        else:
            scan_once(args.watch_dir, args.server_url)

        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
