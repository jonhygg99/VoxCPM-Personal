"""Barre los ajustes de ESTA maquina y guarda el ganador en benchmarks/perfil-maquina.json.

Reusa benchmark_voxcpm_inference.py (mismas metricas y comparacion que
docs/optimizacion-inferencia.md): para cada combinacion levanta un server
fresco, mide benchmarks/bench_script.txt con seed fija y se queda con el mejor
ratio mediano inferencia/audio. Despues barre el modelo de QA (faster-whisper)
eligiendo el mas rapido que sigue pasando sus propios umbrales.

Uso:
    python scripts/benchmark_perfil.py                  # barrido completo
    python scripts/benchmark_perfil.py --solo-servidor  # sin barrido de QA
    python scripts/benchmark_perfil.py --dry-run        # solo enseña las combinaciones

Requisitos: GPU libre (ningun server levantado ni procesos pesados) y unos
10-20 minutos, porque cada config arranca un server nuevo (~40 s compilando con
inductor caliente).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import benchmark_voxcpm_inference as bench  # noqa: E402
import perfil_maquina  # noqa: E402
from voxcpm_client import DEFAULT_SERVER_URL, VoxCPMServerError, check_server  # noqa: E402

PERFIL_PATH = RAIZ / "benchmarks" / "perfil-maquina.json"
SCRIPT_FILE = RAIZ / "benchmarks" / "bench_script.txt"
QA_WAV = RAIZ / "voices" / "david.wav"
QA_MODELOS = ["small", "base", "medium"]
SERVER_TIMEOUT = 600.0


def fingerprint():
    import torch

    gpu = "cpu"
    vram_gb = None
    if torch.cuda.is_available():
        props = torch.cuda.get_device_properties(0)
        gpu = props.name
        vram_gb = round(props.total_memory / 1024**3, 1)
    try:
        import triton

        triton_version = triton.__version__
    except Exception:
        triton_version = None
    return {
        "hostname": socket.gethostname(),
        "so": platform.platform(),
        "gpu": gpu,
        "vram_gb": vram_gb,
        "torch": torch.__version__,
        "triton": triton_version,
    }


def combinaciones():
    return [
        {"optimize": optimize, "stop_check_batch": batch}
        for optimize in (False, True)
        for batch in ((1,) if not optimize else (1, 2, 4))
    ]


def etiqueta(config):
    return f"optimize={'true' if config['optimize'] else 'false'} batch={config['stop_check_batch']}"


def esperar_servidor(server_url, proceso, timeout):
    limite = time.time() + timeout
    while time.time() < limite:
        try:
            return check_server(server_url, timeout=5)
        except VoxCPMServerError:
            pass
        if proceso.poll() is not None:
            raise RuntimeError(f"el server termino con codigo {proceso.returncode}")
        time.sleep(2)
    raise TimeoutError(f"el server no estuvo listo en {timeout:.0f}s")


def comando_server(config, puerto):
    """Comando + entorno para levantar el server con una config del barrido."""
    env = dict(os.environ)
    env["VOXCPM_STOP_CHECK_BATCH"] = str(config["stop_check_batch"])
    cmd = [sys.executable, str(RAIZ / "voxcpm_server.py"), "--port", str(puerto)]
    # Explicito en ambos casos: el server lee su default de optimize del perfil
    # de esta maquina, y sin flag heredaria el ganador anterior (barrido sucio).
    cmd.append("--optimize" if config["optimize"] else "--no-optimize")
    return cmd, env


def medir_config(config, server_url, bloques, warmup, seed, timeout):
    puerto = str(urllib.parse.urlsplit(server_url).port or 8765)
    cmd, env = comando_server(config, puerto)
    print(f"\n=== {etiqueta(config)} ===", flush=True)
    proceso = subprocess.Popen(cmd, cwd=RAIZ, env=env)
    try:
        esperar_servidor(server_url, proceso, timeout)
        textos = bench.pick_benchmark_blocks(str(SCRIPT_FILE), bench.MAX_CHARS, bloques + warmup)
        resultados = bench.run_benchmark(textos, server_url, warmup_count=warmup, base_seed=seed)
        resumen = bench.summarize_metrics(resultados)
    finally:
        proceso.terminate()
        try:
            proceso.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proceso.kill()
            proceso.wait(timeout=20)

    medidas = [r for r in resultados if r.get("ok") and r.get("metrics") and not r.get("warmup")]
    return {
        "config": config,
        "median_ratio": resumen["median_ratio"],
        "avg_inference_seconds": resumen["avg_inference_seconds"],
        "avg_audio_seconds": resumen["avg_audio_seconds"],
        "completed": resumen["completed"],
        "failed": resumen["failed"],
        "peak_reserved_mb": max((r["metrics"].get("cuda_reserved_mb", 0) for r in medidas), default=0),
        "hashes": sorted(r["wav_sha256"] for r in medidas if r.get("wav_sha256")),
    }


def elegir_ganador(mediciones):
    """Mejor ratio mediano; empate -> menos VRAM reservada. Ignora configs fallidas."""
    validas = [m for m in mediciones if m["completed"] > 0 and m["median_ratio"] > 0]
    if not validas:
        return None
    return min(validas, key=lambda m: (round(m["median_ratio"], 4), m.get("peak_reserved_mb", 0)))


def elegir_qa_ganador(candidatos):
    aceptables = [c for c in candidatos if c["pasa_qa"]]
    if not aceptables:
        return None
    return min(aceptables, key=lambda c: c["total_s"])


def barrer_qa():
    import soundfile as sf
    from audio_qa import AudioQA, QAThresholds
    from voices.david import PROMPT_TEXT

    duracion = sf.info(str(QA_WAV)).duration
    candidatos = []
    for modelo in QA_MODELOS:
        registro = {"model": modelo, "device": "cpu"}
        try:
            inicio = time.perf_counter()
            qa = AudioQA(model_name=modelo, device="cpu", compute_type="int8", thresholds=QAThresholds())
            registro["carga_s"] = round(time.perf_counter() - inicio, 2)
            inicio = time.perf_counter()
            resultado = qa.validate(PROMPT_TEXT, str(QA_WAV), duracion)
            registro["check_s"] = round(time.perf_counter() - inicio, 2)
            registro["pasa_qa"] = bool(resultado.ok)
            if not resultado.ok:
                registro["motivo"] = "; ".join(resultado.reasons)
        except Exception as exc:
            registro["pasa_qa"] = False
            registro["motivo"] = str(exc)
        registro["total_s"] = round(registro.get("carga_s", 0) + registro.get("check_s", 0), 2)
        print(f"  QA {modelo}: {registro['total_s']}s pasa_qa={registro['pasa_qa']} {registro.get('motivo', '')}")
        candidatos.append(registro)
    return candidatos


def construir_perfil(mediciones, qa, bloques, warmup, seed):
    ganador = elegir_ganador(mediciones.values())
    if ganador is None:
        return None
    return {
        "version": 1,
        "origen": "barrido",
        "fecha": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "maquina": fingerprint(),
        "servidor": dict(ganador["config"]),
        "qa": qa,
        "benchmark": {
            "bloques": bloques,
            "warmup": warmup,
            "seed": seed,
            "guion": "benchmarks/bench_script.txt",
        },
        "metricas": mediciones,
    }


def main():
    parser = argparse.ArgumentParser(description="Barrido de ajustes de rendimiento de esta maquina.")
    parser.add_argument("--server-url", default=DEFAULT_SERVER_URL)
    parser.add_argument("--bloques", type=int, default=perfil_maquina.DEFAULTS["benchmark"]["bloques"])
    parser.add_argument("--warmup", type=int, default=perfil_maquina.DEFAULTS["benchmark"]["warmup"])
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--timeout", type=float, default=SERVER_TIMEOUT, help="Espera maxima al arranque del server.")
    parser.add_argument("--solo-servidor", action="store_true", help="No barre el modelo de QA.")
    parser.add_argument("--dry-run", action="store_true", help="Solo enseña las combinaciones y termina.")
    parser.add_argument("--salida", default=str(PERFIL_PATH))
    args = parser.parse_args()

    if args.dry_run:
        for config in combinaciones():
            print(etiqueta(config))
        return

    if not SCRIPT_FILE.exists():
        raise SystemExit(f"Falta el guion de benchmark: {SCRIPT_FILE}")

    try:
        check_server(args.server_url, timeout=3)
    except VoxCPMServerError:
        pass
    else:
        raise SystemExit(
            "Ya hay un server levantado: sus metricas falsearian el barrido. "
            "Cierra la terminal de voxcpm_server.py y vuelve a ejecutar."
        )

    print(f"Barriendo {len(combinaciones())} combinaciones de servidor...")
    mediciones = {}
    for config in combinaciones():
        mediciones[etiqueta(config)] = medir_config(
            config, args.server_url, args.bloques, args.warmup, args.seed, args.timeout
        )

    qa = {"model": perfil_maquina.DEFAULTS["qa"]["model"], "device": perfil_maquina.DEFAULTS["qa"]["device"]}
    if not args.solo_servidor:
        print("\nBarriendo modelo de QA (faster-whisper en CPU)...")
        candidatos = barrer_qa()
        mejor_qa = elegir_qa_ganador(candidatos)
        qa["candidatos"] = candidatos
        if mejor_qa is not None:
            qa["model"] = mejor_qa["model"]
        else:
            print("  Ningun modelo de QA paso sus umbrales; se mantiene el default del repo.")

    perfil = construir_perfil(mediciones, qa, args.bloques, args.warmup, args.seed)
    if perfil is None:
        raise SystemExit("Ninguna combinacion dio resultados validos; no se guarda perfil.")

    Path(args.salida).write_text(json.dumps(perfil, indent=2, ensure_ascii=False), encoding="utf-8")
    ganador = etiqueta(perfil["servidor"])
    ratio = perfil["metricas"][ganador]["median_ratio"]
    print(f"\nPerfil guardado en {args.salida}")
    print(f"Ganador: {ganador} (ratio mediano {ratio:.2f}x)")
    print("voxcpm_server.py y el flujo de generacion ya usan estos defaults via perfil_maquina.py.")


if __name__ == "__main__":
    main()
