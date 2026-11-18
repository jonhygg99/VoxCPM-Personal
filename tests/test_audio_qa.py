from audio_qa import QAThresholds, duration_check, evaluate, normalize_text, span_similarities, text_similarity


def test_normalize_text_strips_accents_punctuation_and_case():
    assert normalize_text("¡Hola, Mundo!") == "hola mundo"
    assert normalize_text("café  con   leche.") == "cafe con leche"


def test_normalize_text_handles_empty_string():
    assert normalize_text("") == ""


def test_text_similarity_identical_text_is_one():
    assert text_similarity("Hola mundo.", "hola mundo") == 1.0


def test_text_similarity_unrelated_text_is_low():
    assert text_similarity("Hola mundo, como estas hoy", "The quick brown fox jumps") < 0.3


def test_text_similarity_both_empty_is_one():
    assert text_similarity("", "") == 1.0


def test_duration_check_flags_loop():
    # Texto corto pero audio muy largo -> posible loop/repeticion.
    ok, ratio = duration_check("Hola.", duration=10.0, thresholds=QAThresholds())
    assert ok is False
    assert ratio > QAThresholds().max_duration_ratio


def test_duration_check_flags_cutoff():
    # Texto largo pero audio muy corto -> posible corte.
    text = "Esta es una oracion bastante larga que deberia tomar varios segundos en pronunciarse completa."
    ok, ratio = duration_check(text, duration=0.3, thresholds=QAThresholds())
    assert ok is False
    assert ratio < QAThresholds().min_duration_ratio


def test_duration_check_accepts_normal_pace():
    text = "Esta es una oracion de longitud normal para narrar."
    thresholds = QAThresholds(chars_per_second=15.0)
    expected_duration = len(text) / thresholds.chars_per_second
    ok, ratio = duration_check(text, duration=expected_duration, thresholds=thresholds)
    assert ok is True
    assert ratio == 1.0


def test_evaluate_all_checks_pass():
    text = "Hola, esto es una prueba de audio en espanol."
    result = evaluate(
        text=text,
        duration=len(text) / 15.0,
        transcript=text,
        language="es",
        language_prob=0.95,
        thresholds=QAThresholds(),
    )
    assert result.ok is True
    assert result.reasons == ()


def test_evaluate_flags_wrong_language():
    text = "Hola, esto es una prueba de audio en espanol."
    result = evaluate(
        text=text,
        duration=len(text) / 15.0,
        transcript="Hello, this is an audio test.",
        language="en",
        language_prob=0.9,
        thresholds=QAThresholds(),
    )
    assert result.ok is False
    assert any("idioma" in reason for reason in result.reasons)


def test_evaluate_flags_low_similarity_even_when_language_matches():
    text = "El presidente anuncio una nueva reforma economica esta manana."
    result = evaluate(
        text=text,
        duration=len(text) / 15.0,
        transcript="Gato perro casa arbol nube.",
        language="es",
        language_prob=0.95,
        thresholds=QAThresholds(),
    )
    assert result.ok is False
    assert any("similitud" in reason for reason in result.reasons)


def test_evaluate_flags_bad_duration():
    text = "Hola."
    result = evaluate(
        text=text,
        duration=15.0,
        transcript=text,
        language="es",
        language_prob=0.95,
        thresholds=QAThresholds(),
    )
    assert result.ok is False
    assert any("duracion" in reason for reason in result.reasons)


def test_evaluate_low_language_probability_flagged_even_if_language_matches():
    text = "Hola, esto es una prueba."
    result = evaluate(
        text=text,
        duration=len(text) / 15.0,
        transcript=text,
        language="es",
        language_prob=0.1,
        thresholds=QAThresholds(min_language_prob=0.5),
    )
    assert result.ok is False
    assert any("idioma" in reason for reason in result.reasons)


# --- 18/11/2026 (don s3): similitud por tramo sobre el bloque real de 175 chars ---

_TEXTO_DON_S3 = (
    "fundada en 2007, hace contenido de entretenimiento y formatos sin guion. "
    "No es un retiro: seria un cambio de lado, de reportera a productora, "
    "con su hijo de tres anos cerca."
)
# Transcripcion del audio bueno: se come "fundada en 2007" y parte "hace".
_ASR_BUENO = (
    "Quitese contenido de entretenimiento y formato sin guion, no es un retiro, "
    "seria un cambio de lado de reportera productora con su hijo de tres anos cerca."
)
# Mismo bloque con la primera frase alucinada (lo que paso de verdad).
_ASR_ALUCINADO = (
    "Tense identidad de Telzate, ya lo veremos en el video, no es un retiro, "
    "seria un cambio de lado de reportera productora con su hijo de tres anos cerca."
)


def test_span_similarities_no_castiga_palabras_sueltas():
    # El ASR come cuatro palabras seguidas: por debajo del minimo no es tramo.
    assert span_similarities(_TEXTO_DON_S3, _ASR_BUENO) == []


def test_span_similarities_delata_la_frase_alucinada():
    tramos = span_similarities(_TEXTO_DON_S3, _ASR_ALUCINADO)
    assert tramos
    assert min(valor for _tramo, valor in tramos) < QAThresholds().min_span_similarity


def test_span_similarities_delata_lo_dicho_de_mas():
    tramos = span_similarities("Hola mundo", "hola mundo y manana veremos el desenlace de la novela")
    assert tramos
    assert tramos[0][0].startswith("dicho de mas")


def test_evaluate_rechaza_frase_alucinada_que_pasa_el_global():
    # Global 0.69 (por encima del 0.6 de siempre); el tramo es lo que la delata.
    result = evaluate(_TEXTO_DON_S3, len(_TEXTO_DON_S3) / 15.0, _ASR_ALUCINADO, "es", 0.95)
    assert result.ok is False
    assert result.similarity >= QAThresholds().min_similarity
    assert any("tramo con similitud baja" in reason for reason in result.reasons)


def test_evaluate_acepta_el_audio_bueno_con_palabras_comidas():
    # Control positivo: el audio real de hoy, con palabras comidas por el ASR.
    result = evaluate(_TEXTO_DON_S3, len(_TEXTO_DON_S3) / 15.0, _ASR_BUENO, "es", 0.95)
    assert result.ok is True
    assert result.reasons == ()
