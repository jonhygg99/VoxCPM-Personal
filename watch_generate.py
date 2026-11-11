"""Vigilante de carpeta: detecta .txt nuevos y genera su audio con VoxCPM.

Escanea WATCH_DIR cada INTERVAL_SECONDS. Cada .txt se procesa como guion
largo (logica de generate_script.py), salvo que termine en " shorts", que usa
la logica de generate_shorts.py. Sin reglas de naming: cualquier nombre vale.

Cada .txt tiene su propia carpeta de salida, nombrada igual que el .txt.
Ejemplo: "cine script.txt" -> WATCH_DIR/cine script/ (audio final, srt y
bloques intermedios); "cine shorts.txt" -> WATCH_DIR/cine shorts/ (bloques,
srt y la subcarpeta shorts/ con un wav por short).

Requiere que voxcpm_server.py ya este corriendo (el modelo vive ahi, no aqui).
"""

import argparse
import glob
import os
import socket
import time
import traceback
from dataclasses import dataclass
from typing import Optional

import generate_script
import generate_shorts
import perfil_maquina
from audio_qa import AudioQA
from tts_workflow import parse_structured_shorts
from voxcpm_client import DEFAULT_SERVER_URL, VoxCPMServerError, check_server

# Voz por defecto y presets centralizados (única fuente de verdad para ambos vaults).
# generate_script/generate_shorts exponen el mismo VOICE_PRESETS.
VOICE_PRESETS = getattr(generate_script, "VOICE_PRESETS", {})
DEFAULT_VOICE = getattr(generate_script, "DEFAULT_VOICE", "david")

DEFAULT_WATCH_DIR = r"D:\Obsidian Vaults\Chismes MX\Generate"
DEFAULT_INTERVAL_SECONDS = 120
MIN_AGE_SECONDS = 5  # ignora .txt modificados hace menos de esto (puede seguir escribiendose)

# Un solo watcher por maquina. Dos instancias sobre la misma carpeta compiten
# por los mismos bloques y el QA rechaza audio bueno (ver tmp_path_for).
INSTANCE_LOCK_PORT = 8766

SCRIPT_SUFFIX = " script"
SHORTS_SUFFIX = " shorts"
# El vault usa "<iniciales>-<carpeta>-script.txt" (guion); el formato viejo era
# "canal script.txt" (espacio). Se aceptan los tres separadores para que el
# nombre base quede limpio. "transcript" NO matchea: se exige el separador.
SCRIPT_SUFFIXES = (SCRIPT_SUFFIX, "-script", "_script")
SHORTS_SUFFIXES = (SHORTS_SUFFIX, "-shorts", "_shorts")


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


def _base_sin_sufijo(stem, suffixes):
    """Devuelve el stem sin el sufijo reconocido (si lo hay) y sin separadores.

    Solo afecta al campo `base` (nombre logico); las rutas de salida siguen
    colgando del stem completo para no colisionar entre "cine script" y
    "cine-shorts".
    """
    stem_lower = stem.lower()
    for suf in suffixes:
        if stem_lower.endswith(suf):
            return stem[: -len(suf)].strip(" -_\t")
    return stem


def plan_outputs(txt_path, watch_dir):
    """Calcula rutas de salida para un .txt segun las convenciones de nombres.

    Funcion pura (sin I/O) para poder testearla sin tocar disco.
    """
    stem = os.path.splitext(os.path.basename(txt_path))[0]
    stem_lower = stem.lower()

    if any(stem_lower.endswith(suf) for suf in SHORTS_SUFFIXES):
        kind = "shorts"
        base = _base_sin_sufijo(stem, SHORTS_SUFFIXES)
    else:
        # Sin reglas de naming: todo .txt es guion largo, con o sin " script".
        kind = "script"
        base = _base_sin_sufijo(stem, SCRIPT_SUFFIXES)

    base_dir = os.path.join(watch_dir, stem)
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
        shorts_dir = os.path.join(base_dir, "shorts")
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


def shorts_esperados(txt_path):
    """Cuantos shorts declara el txt (None si no se puede leer o parsear)."""
    try:
        return len(parse_structured_shorts(txt_path, encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def is_done(plan, txt_path=None):
    """True si ya hay audio generado para este plan (no solo carpetas/SRT vacios).

    Para shorts no basta con que haya un wav: una corrida interrumpida deja
    short_1.wav y el vigilante daba el txt por terminado para siempre, sin
    reintentar los shorts que faltaban ni escribir el SRT. Ahora exige SRT no
    vacio y, si se conoce cuantos shorts declara el txt, un wav por short.
    """
    if plan.kind == "script":
        return os.path.exists(plan.final_output)

    if plan.kind == "shorts":
        if not plan.shorts_dir or not os.path.isdir(plan.shorts_dir):
            return False
        if not plan.srt_output or not os.path.isfile(plan.srt_output):
            return False
        if os.path.getsize(plan.srt_output) == 0:
            return False
        esperados = shorts_esperados(txt_path) if txt_path else None
        if esperados is not None:
            wavs = glob.glob(os.path.join(plan.shorts_dir, "*.wav"))
            if len(wavs) < esperados:
                return False
        return True

    return False


def _claim_instance_lock():
    """Reserva el puerto de instancia; None si ya hay otro watcher corriendo.

    Un bind local (sin SO_REUSEADDR) es exclusivo por maquina y no deja archivos
    de lock huerfanos si el proceso muere. El socket se mantiene abierto hasta
    que el proceso termina; no se aceptan conexiones.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", INSTANCE_LOCK_PORT))
    except OSError:
        sock.close()
        return None
    return sock


def _resolve_voice(voice):
    """Devuelve (reference_wav, prompt_text) para la voz pedida."""
    if voice in VOICE_PRESETS:
        cfg = VOICE_PRESETS[voice]
        return cfg["wav"], cfg["prompt"]
    # fallback: david si voz desconocida (compatibilidad)
    cfg = VOICE_PRESETS.get(DEFAULT_VOICE) or VOICE_PRESETS.get("david")
    if cfg:
        print(f"Aviso: voz '{voice}' desconocida, usando '{DEFAULT_VOICE}'.")
        return cfg["wav"], cfg["prompt"]
    return None, None


def process_file(txt_path, watch_dir, server_url, qa=None, validate_audio=True, voice=DEFAULT_VOICE):
    plan = plan_outputs(txt_path, watch_dir)

    if plan.kind is None:
        print(
            f"Saltando '{txt_path}': el nombre no termina en "
            f"'{SCRIPT_SUFFIX.strip()}' ni '{SHORTS_SUFFIX.strip()}'."
        )
        return

    if is_done(plan, txt_path):
        return  # ya generado en una pasada anterior

    mtime = os.path.getmtime(txt_path)
    if time.time() - mtime < MIN_AGE_SECONDS:
        print(f"'{txt_path}' se modifico hace muy poco, se revisa en el siguiente ciclo.")
        return

    reference_wav, prompt_text = _resolve_voice(voice)
    print(f"\n=== Procesando '{txt_path}' ({plan.kind}, voz={voice}) ===")
    if plan.kind == "script":
        generate_script.run(
            script_file=txt_path,
            blocks_dir=plan.blocks_dir,
            final_output=plan.final_output,
            srt_output=plan.srt_output,
            reference_wav=reference_wav,
            prompt_text=prompt_text,
            server_url=server_url,
            validate_audio=validate_audio,
            qa=qa,
        )
    else:
        generate_shorts.run(
            script_file=txt_path,
            shorts_dir=plan.shorts_dir,
            bloques_dir=plan.blocks_dir,
            srt_output=plan.srt_output,
            reference_wav=reference_wav,
            prompt_text=prompt_text,
            server_url=server_url,
            validate_audio=validate_audio,
            qa=qa,
        )


def scan_once(watch_dir, server_url, qa=None, validate_audio=True, voice=DEFAULT_VOICE):
    pattern = os.path.join(watch_dir, "*.txt")
    for txt_path in sorted(glob.glob(pattern)):
        try:
            process_file(txt_path, watch_dir, server_url, qa=qa, validate_audio=validate_audio, voice=voice)
        except Exception as exc:
            print(f"ERROR procesando '{txt_path}': {exc}")
            traceback.print_exc()


def main():
    parser = argparse.ArgumentParser(description="Vigilante de carpeta: genera voz VoxCPM para .txt nuevos.")
    parser.add_argument("--watch-dir", default=DEFAULT_WATCH_DIR)
    parser.add_argument("--txt", default=None,
                        help="Procesa solo este .txt (ruta completa o dentro de --watch-dir, cualquier nombre).")
    parser.add_argument("--force", action="store_true",
                        help="Regenera aunque ya exista el WAV final (solo con --txt).")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--server-url", default=DEFAULT_SERVER_URL)
    parser.add_argument("--once", action="store_true", help="Ejecuta un solo ciclo y termina (util para pruebas).")
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Desactiva el chequeo de calidad de audio (idioma/similitud/duracion, ver audio_qa.py).",
    )
    parser.add_argument(
        "--qa-model",
        default=perfil_maquina.cargar()["qa"]["model"],
        help="Modelo faster-whisper para el chequeo de calidad.",
    )
    parser.add_argument(
        "--qa-device",
        default=perfil_maquina.cargar()["qa"]["device"],
        help="Device para el chequeo de calidad (la GPU la usa el server).",
    )
    parser.add_argument(
        "--voice",
        default=DEFAULT_VOICE,
        choices=sorted(VOICE_PRESETS.keys()) if VOICE_PRESETS else ["david", "benjamin"],
        help="Voz de referencia (default: %(default)s). 'david' para Chismes MX, 'benjamin' para El Salvaje Oeste.",
    )
    args = parser.parse_args()

    instance_lock = _claim_instance_lock()
    if instance_lock is None:
        raise SystemExit(
            "Ya hay un watch_generate corriendo en esta maquina. "
            "Cierra esa ventana antes de abrir otra: dos watchers sobre la misma "
            "carpeta corrompen los bloques y el QA los rechaza."
        )

    print(f"Vigilando '{args.watch_dir}' cada {args.interval:.0f}s (servidor: {args.server_url}, voz={args.voice})")
    print("Deja esta ventana abierta. El modelo debe estar cargado en voxcpm_server.py.")

    validate_audio = not args.no_validate
    qa = None
    if validate_audio:
        print(f"Cargando modelo QA ({args.qa_model} en {args.qa_device})...")
        qa = AudioQA(model_name=args.qa_model, device=args.qa_device)
    else:
        print("Chequeo de calidad de audio DESACTIVADO (--no-validate).")

    if args.txt:
        # Ruta directa: vale cualquier nombre; la carpeta de salida cuelga del
        # --watch-dir (o del padre del txt si no se dio watch-dir explicito).
        from pathlib import Path as _Path
        cand = _Path(args.txt)
        txt_path = str(cand if cand.is_file() else _Path(args.watch_dir) / cand.name)
        if not os.path.isfile(txt_path):
            raise SystemExit(f"watch_generate: no existe '{args.txt}'.")
        if args.watch_dir == DEFAULT_WATCH_DIR:
            args.watch_dir = os.path.dirname(os.path.abspath(txt_path))
        try:
            check_server(args.server_url)
        except VoxCPMServerError as exc:
            print(f"Servidor VoxCPM no disponible: {exc}")
            raise SystemExit(1) from exc
        if args.force:
            plan = plan_outputs(txt_path, args.watch_dir)
            if plan.done_marker and os.path.isfile(plan.done_marker):
                os.remove(plan.done_marker)
        process_file(txt_path, args.watch_dir, args.server_url, qa=qa,
                     validate_audio=validate_audio, voice=args.voice)
        return

    while True:
        try:
            check_server(args.server_url)
        except VoxCPMServerError as exc:
            print(f"Servidor VoxCPM no disponible: {exc}")
        else:
            scan_once(args.watch_dir, args.server_url, qa=qa, validate_audio=validate_audio, voice=args.voice)

        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
