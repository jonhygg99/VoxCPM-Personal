"""Control de calidad para el audio generado por VoxCPM.

Chequea cada bloque en 3 capas antes de dejarlo entrar al audio final:
  1. Duracion (sin modelo): detecta loops/repeticiones o cortes.
  2. Idioma (ASR): el bug real que motivo este modulo -- el TTS puede
     "hablar en varios idiomas" en un bloque suelto.
  3. Similitud (ASR): compara la transcripcion contra el texto pedido,
     atrapa gibberish/alucinacion aunque el idioma detectado sea correcto.

Las funciones de evaluacion son puras (no requieren whisper) para poder
testearlas sin cargar ningun modelo. `AudioQA` es la unica pieza con estado:
envuelve faster-whisper de forma perezosa, igual que StableTSAligner envuelve
stable-ts en `voxcpm.timestamps.stable_ts`.
"""

import difflib
import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class QAThresholds:
    expected_lang: str = "es"
    min_language_prob: float = 0.5
    min_similarity: float = 0.6
    max_duration_ratio: float = 1.8
    min_duration_ratio: float = 0.5
    chars_per_second: float = 15.0


@dataclass(frozen=True)
class QAResult:
    ok: bool
    reasons: tuple = ()
    transcript: str = ""
    language: str = ""
    language_prob: float = 0.0
    similarity: float = 0.0
    duration_ratio: float = 0.0


def normalize_text(text):
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def text_similarity(expected, actual):
    a = normalize_text(expected)
    b = normalize_text(actual)
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(a=a, b=b).ratio()


def duration_check(text, duration, thresholds=QAThresholds()):
    expected = max(len(text) / thresholds.chars_per_second, 0.01)
    ratio = duration / expected
    ok = thresholds.min_duration_ratio <= ratio <= thresholds.max_duration_ratio
    return ok, ratio


def evaluate(text, duration, transcript, language, language_prob, thresholds=QAThresholds()):
    reasons = []

    duration_ok, ratio = duration_check(text, duration, thresholds)
    if not duration_ok:
        reasons.append(
            f"duracion sospechosa (ratio={ratio:.2f}, fuera de "
            f"[{thresholds.min_duration_ratio}, {thresholds.max_duration_ratio}])"
        )

    language_ok = language == thresholds.expected_lang and language_prob >= thresholds.min_language_prob
    if not language_ok:
        reasons.append(
            f"idioma inesperado (detectado='{language}' prob={language_prob:.2f}, "
            f"se esperaba '{thresholds.expected_lang}')"
        )

    similarity = text_similarity(text, transcript)
    similarity_ok = similarity >= thresholds.min_similarity
    if not similarity_ok:
        reasons.append(f"similitud baja con el texto pedido ({similarity:.2f} < {thresholds.min_similarity})")

    return QAResult(
        ok=not reasons,
        reasons=tuple(reasons),
        transcript=transcript,
        language=language,
        language_prob=language_prob,
        similarity=similarity,
        duration_ratio=ratio,
    )


class AudioQAError(RuntimeError):
    pass


class AudioQA:
    """Envoltorio con estado sobre faster-whisper: carga el modelo una vez y
    valida bloques de audio contra el texto que se les pidio."""

    def __init__(self, model_name="small", device="cpu", compute_type="int8", thresholds=None):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ImportError(
                "faster-whisper es necesario para el chequeo de audio. " 'Instala con: pip install "voxcpm[qa]"'
            ) from exc

        self.model = WhisperModel(model_name, device=device, compute_type=compute_type)
        self.model_name = model_name
        self.device = device
        self.thresholds = thresholds or QAThresholds()

    def transcribe(self, wav_path):
        segments, info = self.model.transcribe(wav_path, language=None)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return text, info.language, float(info.language_probability)

    def validate(self, text, wav_path, duration):
        transcript, language, language_prob = self.transcribe(wav_path)
        return evaluate(text, duration, transcript, language, language_prob, self.thresholds)
