from audio_qa import QAThresholds, duration_check, evaluate, normalize_text, text_similarity


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
