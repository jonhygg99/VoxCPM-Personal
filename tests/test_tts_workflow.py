import io
import os
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from audio_qa import QAResult
from tts_workflow import (
    GenerationPipeline,
    build_srt_entries,
    build_voxcpm_payload,
    generate_validated_block,
    parse_structured_shorts,
    rejected_path_for,
    seconds_to_srt,
    split_text_blocks,
)


def test_split_text_blocks_keeps_sentence_groups_under_limit():
    text = "Primera frase corta. Segunda frase corta. Tercera frase demasiado larga, con parte uno, parte dos."

    blocks = split_text_blocks(text, max_chars=55)

    assert blocks == [
        "Primera frase corta. Segunda frase corta.",
        "Tercera frase demasiado larga, con parte uno,",
        "parte dos.",
    ]


def test_seconds_to_srt_rounds_to_milliseconds():
    assert seconds_to_srt(61.2345) == "00:01:01,234"


def test_build_srt_entries_uses_accumulated_durations():
    lines = build_srt_entries([("hola", 1.0), ("mundo", 2.5)])

    assert lines == [
        "1\n00:00:00,000 --> 00:00:01,000\nhola\n",
        "2\n00:00:01,000 --> 00:00:03,500\nmundo\n",
    ]


def test_parse_structured_shorts_reads_script_lines(tmp_path):
    input_file = Path(tmp_path) / "shorts.txt"
    input_file.write_text(
        "SHORT 1\nTitulo: A\nScript: primer short\n---\nSHORT 2\nScript: segundo short\n",
        encoding="utf-8",
    )

    assert parse_structured_shorts(str(input_file), encoding="utf-8") == ["primer short", "segundo short"]


def test_parse_structured_shorts_reads_short_n_headers(tmp_path):
    """Formato real usado por el usuario: 'SHORT N - titulo' + parrafo, sin --- ni Script:."""
    input_file = Path(tmp_path) / "shorts.txt"
    input_file.write_text(
        "SHORT 1 — La injusticia que NADIE quiere ver\n\n"
        "Primer guion hablado, parrafo completo.\n\n"
        "SHORT 2 — Univision NO firmo su despido\n\n"
        "Segundo guion hablado.\n",
        encoding="utf-8",
    )

    assert parse_structured_shorts(str(input_file), encoding="utf-8") == [
        "Primer guion hablado, parrafo completo.",
        "Segundo guion hablado.",
    ]


def test_parse_structured_shorts_returns_empty_when_no_recognized_format(tmp_path):
    input_file = Path(tmp_path) / "shorts.txt"
    input_file.write_text("Solo texto plano, sin cabeceras ni marcadores.\n", encoding="utf-8")

    assert parse_structured_shorts(str(input_file), encoding="utf-8") == []


def test_build_voxcpm_payload_preserves_quality_parameters():
    payload = build_voxcpm_payload(
        text="bloque",
        model_id="openbmb/VoxCPM2",
        prompt_text="prompt",
        reference_wav="ref.wav",
        cfg_value=2.0,
        inference_timesteps=12,
        normalize=False,
    )

    assert payload == {
        "text": "bloque",
        "model_id": "openbmb/VoxCPM2",
        "prompt_text": "prompt",
        "prompt_wav_path": "ref.wav",
        "reference_wav_path": "ref.wav",
        "cfg_value": 2.0,
        "inference_timesteps": 12,
        "normalize": False,
        "denoise": False,
        "trim_silence_vad": False,
    }


def _wav_bytes(duration_s=1.0, sr=16000):
    samples = np.zeros(int(duration_s * sr), dtype=np.float32)
    buf = io.BytesIO()
    sf.write(buf, samples, sr, format="WAV")
    return buf.getvalue()


def _build_payload(text, seed=None):
    return {"text": text, "seed": seed}


class _StubQA:
    """QA falso: devuelve los resultados de `outcomes` en orden, uno por intento."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def validate(self, text, wav_path, duration):
        self.calls.append((text, wav_path, duration))
        return self.outcomes.pop(0)


def test_generate_validated_block_reuses_existing_file(tmp_path):
    block_path = str(tmp_path / "bloque_001.wav")
    with open(block_path, "wb") as f:
        f.write(_wav_bytes(duration_s=0.5))

    def fail_generate(payload):
        raise AssertionError("no deberia generar si el bloque ya existe")

    result = generate_validated_block("hola", block_path, _build_payload, fail_generate)

    assert result.status == "reused"
    assert result.sample_rate == 16000
    assert result.duration == pytest.approx(0.5, abs=0.01)


def test_generate_validated_block_generates_without_qa(tmp_path):
    block_path = str(tmp_path / "bloque_001.wav")
    calls = []

    def generate_fn(payload):
        calls.append(payload)
        return _wav_bytes(duration_s=0.3), {"inference_seconds": 1.2}

    result = generate_validated_block("hola", block_path, _build_payload, generate_fn, qa=None)

    assert result.status == "generated"
    assert len(calls) == 1
    assert os.path.exists(block_path)
    assert not os.path.exists(block_path + ".tmp.wav")
    assert result.metrics == {"inference_seconds": 1.2}


def test_generate_validated_block_retries_with_new_seed_then_passes_qa(tmp_path):
    block_path = str(tmp_path / "bloque_001.wav")
    seeds_used = []

    def generate_fn(payload):
        seeds_used.append(payload["seed"])
        return _wav_bytes(duration_s=0.3), {}

    qa = _StubQA(
        [
            QAResult(ok=False, reasons=("idioma inesperado",)),
            QAResult(ok=True, reasons=()),
        ]
    )

    result = generate_validated_block("hola", block_path, _build_payload, generate_fn, qa=qa, max_retries=1)

    assert result.status == "generated"
    assert result.attempts == 2
    assert seeds_used[0] is None  # primer intento sin seed forzada
    assert seeds_used[1] is not None  # reintento con seed distinta
    assert os.path.exists(block_path)
    assert not os.path.exists(rejected_path_for(block_path))


def test_generate_validated_block_quarantines_after_exhausting_retries(tmp_path):
    block_path = str(tmp_path / "bloque_001.wav")

    def generate_fn(payload):
        return _wav_bytes(duration_s=0.3), {}

    bad_result = QAResult(ok=False, reasons=("idioma inesperado",))
    qa = _StubQA([bad_result, bad_result])

    result = generate_validated_block("hola", block_path, _build_payload, generate_fn, qa=qa, max_retries=1)

    assert result.status == "quarantined"
    assert result.attempts == 2
    assert not os.path.exists(block_path)
    assert os.path.exists(rejected_path_for(block_path))
    assert not os.path.exists(block_path + ".tmp.wav")


def test_generate_validated_block_uses_prefetch_for_first_attempt(tmp_path):
    block_path = str(tmp_path / "bloque_001.wav")
    calls = []

    def generate_fn(payload):
        calls.append(payload)
        return _wav_bytes(duration_s=0.3), {"inference_seconds": 1.2}

    def prefetch():
        return _wav_bytes(duration_s=0.5), {"inference_seconds": 0.5, "prefetched": True}

    result = generate_validated_block("hola", block_path, _build_payload, generate_fn, qa=None, prefetch=prefetch)

    assert result.status == "generated"
    assert len(calls) == 0  # el primer intento lo sirve el prefetch, sin generate_fn
    assert result.metrics == {"inference_seconds": 0.5, "prefetched": True}


def test_generate_validated_block_prefetch_retries_use_generate_fn_with_new_seed(tmp_path):
    block_path = str(tmp_path / "bloque_001.wav")
    seeds_used = []

    def generate_fn(payload):
        seeds_used.append(payload["seed"])
        return _wav_bytes(duration_s=0.3), {}

    qa = _StubQA(
        [
            QAResult(ok=False, reasons=("idioma inesperado",)),
            QAResult(ok=True, reasons=()),
        ]
    )

    def prefetch():
        return _wav_bytes(duration_s=0.3), {}

    result = generate_validated_block(
        "hola", block_path, _build_payload, generate_fn, qa=qa, max_retries=1, prefetch=prefetch
    )

    assert result.status == "generated"
    assert result.attempts == 2
    assert len(seeds_used) == 1  # solo el reintento toca generate_fn
    assert seeds_used[0] is not None  # y con seed nueva


def test_generation_pipeline_delivers_blocks_in_order(tmp_path):
    items = [
        (1, "bloque uno", str(tmp_path / "b1.wav")),
        (2, "bloque dos", str(tmp_path / "b2.wav")),
    ]
    order = []

    def build_payload(text, seed=None):
        return {"text": text, "seed": seed}

    def generate_fn(payload):
        order.append(payload["text"])
        return _wav_bytes(duration_s=0.3), {}

    pipeline = GenerationPipeline(items, build_payload, generate_fn)

    wav1, _ = pipeline.prefetch_callable(1)()
    wav2, _ = pipeline.prefetch_callable(2)()

    assert len(wav1) > 0 and len(wav2) > 0
    assert order == ["bloque uno", "bloque dos"]  # generacion en orden


def test_generate_validated_block_propagates_generate_fn_errors_without_retry(tmp_path):
    block_path = str(tmp_path / "bloque_001.wav")
    calls = []

    def failing_generate_fn(payload):
        calls.append(payload)
        raise RuntimeError("servidor caido")

    with pytest.raises(RuntimeError, match="servidor caido"):
        generate_validated_block("hola", block_path, _build_payload, failing_generate_fn, qa=None, max_retries=3)

    assert len(calls) == 1  # errores de red no se reintentan, solo fallos de QA
    assert not os.path.exists(block_path)
    assert not os.path.exists(block_path + ".tmp.wav")
