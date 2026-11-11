import os
import time
import traceback

import soundfile as sf

import perfil_maquina
from audio_qa import AudioQA, QAThresholds
from tts_workflow import (
    GenerationPipeline,
    build_srt_entries,
    build_voxcpm_payload,
    concatenate_wavs,
    ensure_dirs,
    generate_validated_block,
    parse_structured_shorts,
    split_text_blocks,
    write_qa_report,
)
from voices.benjamin import PROMPT_TEXT as BENJAMIN_PROMPT
from voices.david import PROMPT_TEXT as DAVID_PROMPT
from voices.jax import PROMPT_TEXT as JAX_PROMPT
from voices.juan_sanchez import PROMPT_TEXT as JUAN_SANCHEZ_PROMPT
from voices.leo_antonio import PROMPT_TEXT as LEO_ANTONIO_PROMPT
from voxcpm_client import DEFAULT_SERVER_URL, VoxCPMServerError, check_server, generate_wav_bytes_with_metrics

# --- Voces disponibles (prompt + wav de referencia) ---
VOICE_PRESETS = {
    "david": {"wav": r"voices\david.wav", "prompt": DAVID_PROMPT},
    "benjamin": {"wav": r"voices\benjamin.wav", "prompt": BENJAMIN_PROMPT},
    "juan_sanchez": {"wav": r"voices\juan_sanchez.wav", "prompt": JUAN_SANCHEZ_PROMPT},
    "jax": {"wav": r"voices\jax.wav", "prompt": JAX_PROMPT},
    "leo_antonio": {"wav": r"voices\leo_antonio.wav", "prompt": LEO_ANTONIO_PROMPT},
}
DEFAULT_VOICE = "david"

# --- Configura aqui ---
SCRIPT_FILE = r"C:\Users\jonhy\Desktop\script.txt"  # cada linea = un short
REFERENCE_WAV = VOICE_PRESETS[DEFAULT_VOICE]["wav"]
PROMPT_TEXT = VOICE_PRESETS[DEFAULT_VOICE]["prompt"]
OUTPUT_DIR = r"C:\Users\jonhy\Desktop\shorts"  # carpeta de salida
BLOQUES_DIR = r"C:\Users\jonhy\Desktop\shorts_bloques"  # bloques intermedios
SRT_OUTPUT = r"C:\Users\jonhy\Desktop\shorts.srt"
MODEL_ID = "openbmb/VoxCPM2"
SERVER_URL = DEFAULT_SERVER_URL
SCRIPT_ENCODING = "utf-8-sig"
SRT_ENCODING = "utf-8"

MAX_CHARS = 200  # ~20 segundos por bloque
CFG_VALUE = 2.0
INFERENCE_TIMESTEPS = 12
NORMALIZE = False
AUDIT_ONLY = False
METRIC_KEYS = ("cache_seconds", "inference_seconds", "wav_write_seconds", "queue_seconds", "request_seconds")

# Control de calidad de audio (ver audio_qa.py): detecta bloques donde el TTS
# alucino -- p.ej. hablo en otro idioma o repitio/corto el audio.
VALIDATE_AUDIO = True
QA_MODEL = perfil_maquina.cargar()["qa"]["model"]
QA_DEVICE = perfil_maquina.cargar()["qa"]["device"]  # la GPU ya la ocupa voxcpm_server.py
MAX_RETRIES = 2  # reintentos con otra seed antes de poner el bloque en cuarentena

# ----------------------


def run(
    script_file=SCRIPT_FILE,
    shorts_dir=OUTPUT_DIR,
    bloques_dir=BLOQUES_DIR,
    srt_output=SRT_OUTPUT,
    reference_wav=REFERENCE_WAV,
    prompt_text=PROMPT_TEXT,
    model_id=MODEL_ID,
    server_url=SERVER_URL,
    max_chars=MAX_CHARS,
    cfg_value=CFG_VALUE,
    inference_timesteps=INFERENCE_TIMESTEPS,
    normalize=NORMALIZE,
    script_encoding=SCRIPT_ENCODING,
    srt_encoding=SRT_ENCODING,
    audit_only=AUDIT_ONLY,
    validate_audio=VALIDATE_AUDIO,
    qa=None,
    max_retries=MAX_RETRIES,
):
    total_start = time.perf_counter()
    shorts = parse_structured_shorts(script_file, encoding=script_encoding)
    print(f"Se encontraron {len(shorts)} shorts en '{script_file}'")

    if not shorts:
        print(
            f"No se encontraron shorts en '{script_file}' -- revisa el formato "
            "('SHORT N - titulo' + parrafo, o secciones '---' con 'Script:'). "
            "No se genera nada."
        )
        return

    def build_payload(block, seed=None):
        return build_voxcpm_payload(
            text=block,
            model_id=model_id,
            prompt_text=prompt_text,
            reference_wav=reference_wav,
            cfg_value=cfg_value,
            inference_timesteps=inference_timesteps,
            normalize=normalize,
            seed=seed,
        )

    def block_path_for(short_idx, block_idx):
        return os.path.join(bloques_dir, f"short_{short_idx:02d}_bloque_{block_idx:03d}.wav")

    ensure_dirs(shorts_dir, bloques_dir)

    if audit_only:
        total = {"total": 0, "reusable": 0, "missing": 0}
        for short_idx, short_text in enumerate(shorts, start=1):
            blocks = split_text_blocks(short_text, max_chars)
            reusable = sum(
                1 for block_idx in range(1, len(blocks) + 1) if os.path.exists(block_path_for(short_idx, block_idx))
            )
            total["total"] += len(blocks)
            total["reusable"] += reusable
            total["missing"] += len(blocks) - reusable
        print(
            "Auditoria cache: " f"{total['reusable']} reutilizables, {total['missing']} nuevos, {total['total']} total"
        )
        return

    try:
        health = check_server(server_url)
        print(f"\nServidor VoxCPM listo: {health.get('model_id', model_id)}")
        print(f"Caches de voz activas: {health.get('prompt_caches', 0)}")
    except VoxCPMServerError as e:
        print(f"\nERROR: {e}")
        raise SystemExit(1) from e

    if validate_audio and qa is None:
        print(f"Cargando modelo QA ({QA_MODEL} en {QA_DEVICE})...")
        qa = AudioQA(model_name=QA_MODEL, device=QA_DEVICE, thresholds=QAThresholds())
    elif not validate_audio:
        qa = None

    def generate_fn(payload):
        return generate_wav_bytes_with_metrics(payload, server_url)

    # Pipeline QA/generacion: el bloque siguiente se genera en un hilo mientras
    # el QA valida el actual (whisper en CPU), ocultando el coste del QA detras
    # de la generacion. Solo cubre el primer intento y los bloques nuevos.
    to_generate = []
    for short_idx, short_text in enumerate(shorts, start=1):
        for block_idx, block in enumerate(split_text_blocks(short_text, max_chars), start=1):
            block_path = block_path_for(short_idx, block_idx)
            if not os.path.exists(block_path):
                to_generate.append(((short_idx, block_idx), block, block_path))
    pipeline = None
    if qa is not None and to_generate:
        pipeline = GenerationPipeline(to_generate, build_payload, generate_fn)
        print(f"Pipeline QA/generacion activo: {len(to_generate)} bloques nuevos")

    srt_sections = []
    generated_count = 0
    reused_count = 0
    failed_count = 0
    qa_failures = []
    generated_seconds = 0.0
    metric_totals = {key: 0.0 for key in METRIC_KEYS}
    metric_count = 0

    for short_idx, short_text in enumerate(shorts, start=1):
        print(f"\n{'=' * 60}")
        print(f"SHORT {short_idx}/{len(shorts)}: {short_text[:80]}{'...' if len(short_text) > 80 else ''}")
        print(f"{'=' * 60}")

        blocks = split_text_blocks(short_text, max_chars)
        print(f"  {len(blocks)} bloques de max. {max_chars} chars")

        fragments = []
        srt_entries = []
        sample_rate = None

        for block_idx, block in enumerate(blocks, start=1):
            block_path = block_path_for(short_idx, block_idx)

            if os.path.exists(block_path):
                result = generate_validated_block(block, block_path, build_payload, generate_fn, qa=None)
                sample_rate = sample_rate or result.sample_rate
                fragments.append(result.wav)
                srt_entries.append((block, result.duration))
                reused_count += 1
                print(f"  [{block_idx}/{len(blocks)}] Reutilizado ({result.duration:.2f}s), cargando...")
                continue

            print(f"\n  [{block_idx}/{len(blocks)}] Generando ({len(block)} caracteres)...")
            print(f"    -> {block[:80]}{'...' if len(block) > 80 else ''}")

            try:
                result = generate_validated_block(
                    block,
                    block_path,
                    build_payload,
                    generate_fn,
                    qa=qa,
                    max_retries=max_retries,
                    prefetch=(pipeline.prefetch_callable((short_idx, block_idx)) if pipeline is not None else None),
                )
            except Exception as e:
                print(f"    ERROR: {e}")
                traceback.print_exc()
                failed_count += 1
                continue

            generated_seconds += result.elapsed_seconds
            if result.metrics:
                metric_count += 1
                for key in METRIC_KEYS:
                    metric_totals[key] += float(result.metrics.get(key, 0.0))

            if result.status == "quarantined":
                qa_failures.append((f"{short_idx}.{block_idx}", block, result.qa_result))
                print(
                    f"    RECHAZADO bloque {short_idx}.{block_idx} tras {result.attempts} intento(s): "
                    f"{'; '.join(result.qa_result.reasons)}"
                )
                continue

            generated_count += 1
            sample_rate = sample_rate or result.sample_rate
            fragments.append(result.wav)
            srt_entries.append((block, result.duration))
            if result.metrics:
                print(
                    f"    OK Guardado: {block_path} ({result.duration:.2f}s audio, "
                    f"{result.elapsed_seconds:.2f}s local | "
                    f"cache {result.metrics.get('cache_seconds', 0):.2f}s, "
                    f"inferencia {result.metrics.get('inference_seconds', 0):.2f}s, "
                    f"wav {result.metrics.get('wav_write_seconds', 0):.2f}s, "
                    f"cola {result.metrics.get('queue_seconds', 0):.2f}s)"
                )
            else:
                print(
                    f"    OK Guardado: {block_path} ({result.duration:.2f}s audio, "
                    f"{result.elapsed_seconds:.2f}s generacion)"
                )

        if not fragments:
            print(f"  Short {short_idx} sin fragmentos, saltando.")
            continue

        audio_short = concatenate_wavs(fragments)
        short_path = os.path.join(shorts_dir, f"short_{short_idx}.wav")
        sf.write(short_path, audio_short, sample_rate)
        print(f"\n  OK Audio guardado: {short_path}")
        srt_sections.append((f"Short {short_idx}", srt_entries))

    srt_lines = []
    for title, entries in srt_sections:
        srt_lines.append(f"#{title}\n")
        srt_lines.extend(build_srt_entries(entries))

    with open(srt_output, "w", encoding=srt_encoding) as f:
        f.write("\n".join(srt_lines))

    if qa_failures:
        report_path = write_qa_report(qa_failures, bloques_dir)
        print(
            f"\n{len(qa_failures)} bloque(s) no pasaron QA y quedaron en cuarentena "
            f"(ver {report_path}). Vuelve a ejecutar para reintentarlos."
        )

    total_elapsed = time.perf_counter() - total_start
    avg = generated_seconds / generated_count if generated_count else 0.0
    print(f"\nOK SRT guardado en: {srt_output}")
    print(f"OK Audios en: {shorts_dir}")
    print(
        "Resumen: "
        f"{generated_count} generados, {reused_count} reutilizados, "
        f"{failed_count} fallidos, {len(qa_failures)} en cuarentena, "
        f"{total_elapsed:.2f}s total, {avg:.2f}s promedio/bloque nuevo"
    )
    if metric_count:
        print(
            "Cuellos servidor: "
            f"cache {metric_totals['cache_seconds']:.2f}s total "
            f"({metric_totals['cache_seconds'] / metric_count:.2f}s prom), "
            f"inferencia {metric_totals['inference_seconds']:.2f}s total "
            f"({metric_totals['inference_seconds'] / metric_count:.2f}s prom), "
            f"wav {metric_totals['wav_write_seconds']:.2f}s total "
            f"({metric_totals['wav_write_seconds'] / metric_count:.2f}s prom), "
            f"cola {metric_totals['queue_seconds']:.2f}s total "
            f"({metric_totals['queue_seconds'] / metric_count:.2f}s prom), "
            f"request servidor {metric_totals['request_seconds']:.2f}s total"
        )


def main():
    run()


if __name__ == "__main__":
    main()
