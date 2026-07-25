import os
import random
import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
import soundfile as sf


@dataclass(frozen=True)
class AudioInfo:
    sample_rate: int
    frames: int
    duration: float


def split_text_blocks(text, max_chars):
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    blocks = []
    current = ""

    for sentence in sentences:
        candidate = (current + " " + sentence).strip() if current else sentence
        if len(candidate) <= max_chars:
            current = candidate
            continue

        if current:
            blocks.append(current)

        if len(sentence) > max_chars:
            parts = re.split(r"(?<=[,;])\s+", sentence)
            sub_block = ""
            for part in parts:
                candidate = (sub_block + " " + part).strip() if sub_block else part
                if len(candidate) <= max_chars:
                    sub_block = candidate
                else:
                    if sub_block:
                        blocks.append(sub_block)
                    sub_block = part
            current = sub_block
        else:
            current = sentence

    if current:
        blocks.append(current)

    return blocks


def read_text(path, encoding="ansi"):
    with open(path, "r", encoding=encoding) as f:
        return f.read()


def read_paragraph_blocks(path, max_chars, encoding="ansi"):
    content = read_text(path, encoding=encoding)
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    blocks = []
    for paragraph in paragraphs:
        blocks.extend(split_text_blocks(paragraph, max_chars))
    return paragraphs, blocks


def parse_structured_shorts(path, encoding="ansi"):
    content = read_text(path, encoding=encoding)

    shorts = _parse_shorts_by_script_sections(content)
    if shorts:
        return shorts

    return _parse_shorts_by_short_headers(content)


def _parse_shorts_by_script_sections(content):
    sections = re.split(r"^---\s*$", content, flags=re.MULTILINE)
    shorts = []

    for section in sections:
        section = section.strip()
        if not section:
            continue
        match = re.search(r"^Script:\s*(.+)", section, re.MULTILINE)
        if match:
            text = match.group(1).strip()
            if text:
                shorts.append(text)

    return shorts


def _parse_shorts_by_short_headers(content):
    """Formato alterno: parrafos 'SHORT N - titulo' seguidos del guion hablado.

    El parrafo de cabecera (el titulo) no se narra; el cuerpo del short es la
    union de los parrafos siguientes hasta la proxima cabecera.
    """
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    header_re = re.compile(r"^SHORT\s+\d+", re.IGNORECASE)

    shorts = []
    current_parts = []
    found_header = False

    for paragraph in paragraphs:
        if header_re.match(paragraph):
            found_header = True
            if current_parts:
                shorts.append(" ".join(current_parts))
                current_parts = []
            continue
        current_parts.append(paragraph)

    if current_parts:
        shorts.append(" ".join(current_parts))

    return shorts if found_header else []


def seconds_to_srt(seconds):
    total_ms = int(round(seconds * 1000))
    h = total_ms // 3_600_000
    total_ms %= 3_600_000
    m = total_ms // 60_000
    total_ms %= 60_000
    sec = total_ms // 1000
    ms = total_ms % 1000
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def build_srt_entries(entries):
    srt_lines = []
    cursor = 0.0

    for idx, (text, duration) in enumerate(entries, start=1):
        start = seconds_to_srt(cursor)
        end = seconds_to_srt(cursor + duration)
        srt_lines.append(f"{idx}\n{start} --> {end}\n{text}\n")
        cursor += duration

    return srt_lines


def write_srt(entries, output_path, encoding="ansi"):
    srt_lines = build_srt_entries(entries)
    with open(output_path, "w", encoding=encoding) as f:
        f.write("\n".join(srt_lines))
    return len(srt_lines)


def get_audio_info(path):
    info = sf.info(path)
    return AudioInfo(sample_rate=info.samplerate, frames=info.frames, duration=info.duration)


def read_wav(path):
    return sf.read(path)


def write_wav_bytes(path, wav_bytes):
    with open(path, "wb") as f:
        f.write(wav_bytes)


def concatenate_wavs(wavs: Iterable[np.ndarray]):
    return np.concatenate(list(wavs))


def build_voxcpm_payload(
    text,
    model_id,
    prompt_text,
    reference_wav,
    cfg_value,
    inference_timesteps,
    normalize,
    seed=None,
):
    payload = {
        "text": text,
        "model_id": model_id,
        "prompt_text": prompt_text,
        "prompt_wav_path": reference_wav,
        "reference_wav_path": reference_wav,
        "cfg_value": cfg_value,
        "inference_timesteps": inference_timesteps,
        "normalize": normalize,
        "denoise": False,
        "trim_silence_vad": False,
    }
    if seed is not None:
        payload["seed"] = seed
    return payload


def ensure_dirs(*paths):
    for path in paths:
        os.makedirs(path, exist_ok=True)


def write_qa_report(failures, output_dir, filename="qa_report.txt"):
    """Escribe un reporte legible de los bloques que quedaron en cuarentena.

    `failures` es una lista de `(indice, texto, qa_result)`. Devuelve la ruta
    del reporte escrito.
    """
    report_path = os.path.join(output_dir, filename)
    lines = [f"{len(failures)} bloque(s) en cuarentena (no pasaron QA):\n"]
    for index, text, qa_result in failures:
        lines.append(f"--- Bloque {index} ---")
        lines.append(f"Texto pedido: {text}")
        lines.append(f"Transcripcion: {qa_result.transcript}")
        lines.append(f"Idioma detectado: {qa_result.language} (prob={qa_result.language_prob:.2f})")
        lines.append(f"Similitud: {qa_result.similarity:.2f}")
        lines.append(f"Ratio de duracion: {qa_result.duration_ratio:.2f}")
        lines.append(f"Razones: {'; '.join(qa_result.reasons)}")
        lines.append("")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return report_path


@dataclass(frozen=True)
class BlockResult:
    """Resultado de generar (o reutilizar) el audio de un bloque.

    status:
      - "reused": ya existia en `block_path` (paso QA en una corrida previa).
      - "generated": se genero y (si `qa` estaba activo) paso el chequeo.
      - "quarantined": agoto los reintentos sin pasar QA; el intento final
        quedo en `<block_path sin ext>.rejected.wav`, `block_path` NO se
        escribio, y por lo tanto el bloque se regenerara en el proximo rerun.
    """

    status: str
    text: str
    wav: Optional[np.ndarray] = None
    sample_rate: Optional[int] = None
    duration: float = 0.0
    qa_result: object = None
    attempts: int = 0
    elapsed_seconds: float = 0.0
    metrics: Optional[dict] = None


def rejected_path_for(block_path):
    root, _ext = os.path.splitext(block_path)
    return f"{root}.rejected.wav"


def generate_validated_block(
    text,
    block_path,
    build_payload,
    generate_fn,
    qa=None,
    max_retries=0,
    log=print,
):
    """Genera el audio de un bloque, reutilizando `block_path` si ya existe.

    Si `qa` se provee (cualquier objeto con `.validate(text, wav_path,
    duration) -> resultado con .ok y .reasons`), valida cada intento antes de
    aceptarlo. En caso de fallo reintenta hasta `max_retries` veces mas,
    variando la seed via `build_payload(text, seed=...)`. Si sigue fallando,
    el bloque se pone en cuarentena (ver `BlockResult.status`).

    `generate_fn(payload) -> (wav_bytes, metrics)` aisla la llamada HTTP al
    servidor para que esta funcion sea testeable sin red. Errores de
    `generate_fn` (problemas de servidor/red) se propagan de inmediato, sin
    reintentar -- los reintentos aqui son solo para fallos de calidad.
    """
    if os.path.exists(block_path):
        info = get_audio_info(block_path)
        wav, sr = read_wav(block_path)
        return BlockResult(status="reused", text=text, wav=wav, sample_rate=sr, duration=info.duration)

    tmp_path = block_path + ".tmp.wav"
    attempts = 0
    total_elapsed = 0.0
    qa_result = None
    metrics = None

    try:
        while True:
            attempts += 1
            seed = random.randint(0, 2**31 - 1) if attempts > 1 else None
            payload = build_payload(text, seed=seed)

            request_start = time.perf_counter()
            wav_bytes, metrics = generate_fn(payload)
            total_elapsed += time.perf_counter() - request_start
            write_wav_bytes(tmp_path, wav_bytes)
            info = get_audio_info(tmp_path)

            if qa is not None:
                qa_result = qa.validate(text, tmp_path, info.duration)
                if not qa_result.ok:
                    log(f"    QA fallo (intento {attempts}/{max_retries + 1}): {'; '.join(qa_result.reasons)}")
                    if attempts <= max_retries:
                        continue
                    rejected_path = rejected_path_for(block_path)
                    os.replace(tmp_path, rejected_path)
                    return BlockResult(
                        status="quarantined",
                        text=text,
                        duration=info.duration,
                        qa_result=qa_result,
                        attempts=attempts,
                        elapsed_seconds=total_elapsed,
                        metrics=metrics,
                    )

            os.replace(tmp_path, block_path)
            wav, sr = read_wav(block_path)
            return BlockResult(
                status="generated",
                text=text,
                wav=wav,
                sample_rate=sr,
                duration=info.duration,
                qa_result=qa_result,
                attempts=attempts,
                elapsed_seconds=total_elapsed,
                metrics=metrics,
            )
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
