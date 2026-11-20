#!/usr/bin/env python3
"""voz_3idiomas.py — narra el mismo guion en es/en/pt (y aleman si hay 2_guion_de.md) y alinea por escenas.

Cada **escena es un parrafo** del guion (bloques separados por linea en blanco).
En archivos .md se ignoran encabezados, citas (>) y separadores, y se corta en la
seccion de notas. Asi el guion espanol, el ingles y el portugues conservan la
misma rejilla de escenas.

Flujo:
  1. Divide cada escena en bloques de <= --max-chars (cache por bloque).
  2. Sintetiza cada bloque con el servidor VoxCPM local y la voz del idioma.
  3. Concatena los bloques en un WAV por escena.
  4. Alinea las versiones: en cada escena manda la version mas larga y las
     demas rellenan con silencio al final. Resultado: los WAV duran lo mismo
     y cada escena empieza y termina en el mismo segundo en todos los idiomas.

Salidas:
  Generate/<slug>/es.wav, en.wav, pt.wav (y de.wav)   audio alineado (misma duracion)
  Generate/<slug>/es.srt, en.srt, pt.srt   cues por bloque, con los huecos
  Generate/<slug>/escenas.csv              rejilla de escenas
  Generate/<slug>/alineacion.json          detalle reproducible
  Generate/<slug>/volumen.json             niveles LUFS y picos de los WAV
  Generate/<slug>/voz/escenas/<idioma>/...          audio por escena (cache)
  Generate/<slug>/voz/escenas/<idioma>/bloques/...  bloques (cache)

Los finales y la cache van a Generate/<slug>/ (slug = nombre de la carpeta del
caso): los WAV/SRT/CSV/JSON en la raiz y las escenas/bloques en voz/. Asi la voz
aparece en Generate desde la primera escena, sin esperar a la alineacion final.
--out y --cache permiten cambiar cada ruta por separado.

Uso (desde VoxCPM-Personal):
  python scripts/voz_3idiomas.py --vault "D:/Obsidian Vaults/Mi Vault" --caso "02_produccion/.../01-tablilla-motin-dioses"
  python scripts/voz_3idiomas.py --vault "..." --todos
  python scripts/voz_3idiomas.py --vault "..." --es a.txt --en b.txt --pt c.txt [--de d.txt] --out Generate/prueba
  python scripts/voz_3idiomas.py --vault "..." --caso "..." --dry-run
  python scripts/voz_3idiomas.py --vault "..." --caso "..." --solo-alinear
  python scripts/voz_3idiomas.py --vault "..." --caso "..." --solo-normalizar
  python scripts/voz_3idiomas.py --vault "..." --caso "..." --force

--todos recorre los casos con 2_guion.md, _en y _pt; salta los ya verificados y
lista los que solo tienen espanol. El aleman es opcional: entra al lote cuando el
caso tiene 2_guion_de.md. Al cerrar, la normalizacion EBU R128 deja los WAV en
-16 LUFS y -1.5 dBTP sin cambiar los frames ni la rejilla.
El servidor debe estar corriendo (voxcpm.bat lo arranca y reutiliza entre proyectos).
El aleman solo entra si voices.json declara una voz 'de' y el caso tiene 2_guion_de.md.
--out-dir cambia la carpeta raiz de salida (default: Generate). Con --desde-txt el
guion espanol sale del .txt <iniciales>-<slug>.txt de esa carpeta y el lote procesa
solo los casos que tengan ese .txt; el ingles, el portugues y el aleman siguen
saliendo de 2_guion_en.md, 2_guion_pt.md y 2_guion_de.md del caso.
Voces por defecto: joaquin (es), jax_bold (en), leo_antonio_g2 (pt), konrad_klar
(de); se registran en VoxCPM-Personal/voices y se pueden cambiar con
--voz-es/--voz-en/--voz-pt/--voz-de.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

IDIOMAS_ORDEN = ("es", "en", "pt", "de")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
VAULT_ROOT = Path.cwd()
RUTAS_JSON = VAULT_ROOT / "Sensible" / "rutas.json"
VOICES_JSON = VAULT_ROOT / "Sensible" / "voices.json"

IDIOMAS = ["es", "en", "pt"]  # "de" se suma cuando el caso tiene 2_guion_de.md
IDIOMAS_REQUERIDOS = ("es", "en", "pt")
VOCES_DEFAULT = {"es": "joaquin", "en": "jax_bold", "pt": "leo_antonio_g2", "de": "konrad_klar"}
VOCES_CFG = {}
VOCES_CLI = {}
MODEL_ID = "openbmb/VoxCPM2"
SERVER_DEFAULT = "http://127.0.0.1:8765"
MAX_CHARS_DEFAULT = 200
TARGET_LUFS = -16.0
TARGET_TP = -1.5


# ─── Texto y escenas ──────────────────────────────────────────────────────────


def leer_escenas(path):
    """Parrafos narrables del guion, en orden. Una escena = un parrafo."""
    raw = Path(path).read_text(encoding="utf-8-sig")
    lineas = raw.splitlines()

    if Path(path).suffix.lower() in (".md", ".markdown"):
        limpias = []
        for linea in lineas:
            texto = linea.rstrip()
            bajo = texto.strip().lower()
            if bajo.startswith("#") and ("notas" in bajo or "notes" in bajo):
                break
            if not texto.strip():
                limpias.append("")
                continue
            if bajo.startswith("#") or bajo.startswith(">") or bajo == "---":
                continue
            limpias.append(texto)
        lineas = limpias

    escenas, actual = [], []
    for linea in lineas:
        if linea.strip():
            actual.append(linea.strip())
        elif actual:
            escenas.append(" ".join(actual))
            actual = []
    if actual:
        escenas.append(" ".join(actual))
    return escenas


def dividir_bloques(texto, max_chars):
    """Misma division que tts_workflow.py de VoxCPM: oracion y, si hace falta, coma."""
    oraciones = re.split(r"(?<=[.!?])\s+", texto.strip())
    bloques = []
    actual = ""

    for oracion in oraciones:
        candidato = (actual + " " + oracion).strip() if actual else oracion
        if len(candidato) <= max_chars:
            actual = candidato
            continue

        if actual:
            bloques.append(actual)

        if len(oracion) > max_chars:
            partes = re.split(r"(?<=[,;])\s+", oracion)
            sub = ""
            for parte in partes:
                candidato = (sub + " " + parte).strip() if sub else parte
                if len(candidato) <= max_chars:
                    sub = candidato
                else:
                    if sub:
                        bloques.append(sub)
                    sub = parte
            actual = sub
        else:
            actual = oracion

    if actual:
        bloques.append(actual)
    return bloques


def huella(*partes):
    h = hashlib.sha256()
    for parte in partes:
        h.update(str(parte).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


# ─── Rutas, voces y servidor ──────────────────────────────────────────────────


def resolver_voxcpm():
    """VoxCPM-Personal: donde viven voices/ y el servidor."""
    return PROJECT_ROOT


def cargar_voz(voxcpm_dir, nombre):
    """(wav, prompt_text) de VoxCPM-Personal/voices/<nombre> (.py arriba, .wav en la subcarpeta del idioma)."""
    py = voxcpm_dir / "voices" / f"{nombre}.py"
    wav = voxcpm_dir / "voices" / f"{nombre}.wav"
    if not wav.is_file():
        candidatos = sorted((voxcpm_dir / "voices").glob(f"*/{nombre}.wav"))
        if candidatos:
            wav = candidatos[0]
    if not py.is_file():
        raise SystemExit("voz_3idiomas: no existe la voz %s." % py)
    if not wav.is_file():
        raise SystemExit("voz_3idiomas: falta el WAV de la voz %s." % wav)
    namespace = {}
    exec(compile(py.read_text(encoding="utf-8"), str(py), "exec"), namespace)
    prompt = namespace.get("PROMPT_TEXT")
    if not prompt:
        raise SystemExit("voz_3idiomas: %s no define PROMPT_TEXT." % py)
    return wav, prompt


def comprobar_servidor(server_url):
    try:
        peticion = urllib.request.Request(
            server_url.rstrip("/") + "/health", headers={"Connection": "close"}
        )
        with urllib.request.urlopen(peticion, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(
            "voz_3idiomas: servidor VoxCPM no disponible en %s (%s).\n"
            "Arrancalo con voxcpm.bat o desde el lanzador global y reintenta."
            % (server_url, exc)
        )


def pedir_bloque(server_url, texto, voz_wav, prompt_text, args):
    payload = {
        "text": texto,
        "model_id": MODEL_ID,
        "prompt_text": prompt_text,
        "prompt_wav_path": str(voz_wav),
        "reference_wav_path": str(voz_wav),
        "cfg_value": args.cfg,
        "inference_timesteps": args.timesteps,
        "normalize": bool(args.normalize),
        "denoise": False,
        "trim_silence_vad": False,
    }
    datos = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        server_url.rstrip("/") + "/generate",
        data=datos,
        headers={"Content-Type": "application/json", "Connection": "close"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=1800) as resp:
            audio = resp.read()
    except urllib.error.HTTPError as exc:
        detalle = exc.read().decode("utf-8", errors="replace")[:400]
        raise SystemExit("voz_3idiomas: el servidor devolvio HTTP %s: %s" % (exc.code, detalle))
    except OSError as exc:
        raise SystemExit("voz_3idiomas: fallo hablando con VoxCPM (%s)." % exc)
    if not audio:
        raise SystemExit("voz_3idiomas: el servidor devolvio audio vacio.")
    return audio


# ─── WAV ─────────────────────────────────────────────────────────────────────


def params_wav(path):
    with wave.open(str(path), "rb") as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate()


def duracion_wav(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def escribir_wav_atomico(datos, destino):
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(".tmp.wav")
    tmp.write_bytes(datos)
    tmp.replace(destino)


def concatenar_wavs(rutas, destino):
    params = params_wav(rutas[0])
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(".tmp.wav")
    with wave.open(str(tmp), "wb") as salida:
        salida.setnchannels(params[0])
        salida.setsampwidth(params[1])
        salida.setframerate(params[2])
        for ruta in rutas:
            if params_wav(ruta) != params:
                raise SystemExit(
                    "voz_3idiomas: formato WAV inconsistente en %s (%s contra %s)."
                    % (ruta, params_wav(ruta), params)
                )
            with wave.open(str(ruta), "rb") as entrada:
                salida.writeframes(entrada.readframes(entrada.getnframes()))
    tmp.replace(destino)


def escribir_silencio(salida, segundos, params):
    frames = int(round(max(0.0, segundos) * params[2]))
    if frames > 0:
        salida.writeframes(b"\x00" * (frames * params[1] * params[0]))


# ─── Generacion por escena (con cache) ───────────────────────────────────────


def escena_dir(cache, idioma):
    return cache / "escenas" / idioma


def bloques_dir(cache, idioma, indice):
    return escena_dir(cache, idioma) / "bloques" / ("escena_%03d" % indice)


def generar_escena(cache, idioma, indice, texto, voz_nombre, voz_wav, prompt_text, args):
    """Genera (o reutiliza) el WAV de una escena. Devuelve (ruta, nuevos, reutilizados)."""
    base = escena_dir(cache, idioma)
    base.mkdir(parents=True, exist_ok=True)
    destino = base / ("escena_%03d.wav" % indice)
    sello = base / ("escena_%03d.sha256" % indice)
    clave_escena = huella(texto, voz_nombre)
    if destino.is_file() and sello.is_file() and sello.read_text(encoding="utf-8").strip() == clave_escena:
        if not args.force:
            return destino, 0, 0

    bloques = dividir_bloques(texto, args.max_chars)
    rutas = []
    nuevos = reutilizados = 0
    for j, bloque in enumerate(bloques, start=1):
        carpeta = bloques_dir(cache, idioma, indice)
        carpeta.mkdir(parents=True, exist_ok=True)
        ruta = carpeta / ("bloque_%03d.wav" % j)
        sello_bloque = carpeta / ("bloque_%03d.sha256" % j)
        clave = huella(bloque, voz_nombre, args.cfg, args.timesteps, args.normalize)
        if (
            not args.force
            and ruta.is_file()
            and sello_bloque.is_file()
            and sello_bloque.read_text(encoding="utf-8").strip() == clave
        ):
            rutas.append(ruta)
            reutilizados += 1
            continue
        print(
            "  [%s %02d] bloque %d/%d (%d caracteres)..."
            % (idioma, indice, j, len(bloques), len(bloque)),
            flush=True,
        )
        audio = pedir_bloque(args.server_url, bloque, voz_wav, prompt_text, args)
        escribir_wav_atomico(audio, ruta)
        sello_bloque.write_text(clave, encoding="utf-8")
        rutas.append(ruta)
        nuevos += 1

    # Si el texto se acorto, sobran bloques de una corrida anterior: se borran
    # para que el SRT no caiga al cue unico por un cache desincronizado.
    for sobrante in carpeta.glob("bloque_*.wav"):
        try:
            indice_bloque = int(re.search(r"(\d+)", sobrante.stem).group(1))
        except (AttributeError, ValueError):
            continue
        if indice_bloque > len(bloques):
            sobrante.unlink(missing_ok=True)
            sobrante.with_suffix(".sha256").unlink(missing_ok=True)

    concatenar_wavs(rutas, destino)
    sello.write_text(clave_escena, encoding="utf-8")
    return destino, nuevos, reutilizados


# ─── Alineacion ──────────────────────────────────────────────────────────────


def tiempo_srt(segundos):
    ms = int(round(segundos * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def bloques_existentes(cache, idioma, indice):
    carpeta = bloques_dir(cache, idioma, indice)
    if not carpeta.is_dir():
        return []
    return sorted(carpeta.glob("bloque_*.wav"))


def escribir_srt(cache, idioma, textos, targets, escenas, destino, max_chars=MAX_CHARS_DEFAULT):
    inicio = 0.0
    numero = 1
    lineas = []
    for indice, (texto, objetivo) in enumerate(zip(textos, targets), start=1):
        t = inicio
        trozos = dividir_bloques(texto, max_chars)
        rutas = bloques_existentes(cache, idioma, indice)
        if rutas and len(rutas) == len(trozos):
            for ruta, trozo in zip(rutas, trozos):
                dur = duracion_wav(ruta)
                lineas.append(
                    "%d\n%s --> %s\n%s\n" % (numero, tiempo_srt(t), tiempo_srt(t + dur), trozo)
                )
                numero += 1
                t += dur
        else:
            # Sin bloques (o con un cache desincronizado): un cue con la escena entera.
            dur = duracion_wav(escenas[indice - 1]) if escenas else objetivo
            lineas.append("%d\n%s --> %s\n%s\n" % (numero, tiempo_srt(t), tiempo_srt(t + dur), texto))
            numero += 1
        inicio += objetivo

    Path(destino).write_text("\n".join(lineas), encoding="utf-8")


def alinear(out, escenas_por_idioma, targets_por_idioma):
    params = params_wav(escenas_por_idioma["es"][0])
    for idioma in IDIOMAS:
        destino = out / ("%s.wav" % idioma)
        tmp = destino.with_suffix(".tmp.wav")
        with wave.open(str(tmp), "wb") as salida:
            salida.setnchannels(params[0])
            salida.setsampwidth(params[1])
            salida.setframerate(params[2])
            for ruta, objetivo in zip(escenas_por_idioma[idioma], targets_por_idioma[idioma]):
                if params_wav(ruta) != params:
                    raise SystemExit(
                        "voz_3idiomas: WAV inconsistente en %s (%s contra %s)."
                        % (ruta, params_wav(ruta), params)
                    )
                with wave.open(str(ruta), "rb") as entrada:
                    salida.writeframes(entrada.readframes(entrada.getnframes()))
                escribir_silencio(salida, objetivo - duracion_wav(ruta), params)
        tmp.replace(destino)


def ffmpeg_exe():
    try:
        datos = json.loads(RUTAS_JSON.read_text(encoding="utf-8-sig"))
        ruta = datos.get("herramientas", {}).get("ffmpeg")
        if ruta and Path(ruta).is_file():
            return ruta
    except Exception:
        pass
    return "ffmpeg"


def medir_loudness(ruta, ffmpeg):
    filtro = "loudnorm=I=%.1f:TP=%.1f:LRA=11:print_format=json" % (TARGET_LUFS, TARGET_TP)
    r = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(ruta), "-af", filtro, "-f", "null", "-"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    inicio = r.stderr.find("{")
    if inicio < 0:
        raise SystemExit(
            "voz_3idiomas: no pude medir el volumen de %s.\n%s"
            % (ruta, r.stderr[-800:])
        )
    try:
        datos, _ = json.JSONDecoder().raw_decode(r.stderr[inicio:])
    except Exception as exc:
        raise SystemExit("voz_3idiomas: respuesta de loudnorm invalida: %s" % exc)
    return datos


def ajustar_frames(origen, destino, frames):
    """Copia el WAV y lo deja exactamente con el numero de frames pedido."""
    with wave.open(str(origen), "rb") as entrada, wave.open(str(destino), "wb") as salida:
        salida.setnchannels(entrada.getnchannels())
        salida.setsampwidth(entrada.getsampwidth())
        salida.setframerate(entrada.getframerate())
        datos = entrada.readframes(entrada.getnframes())
        bytes_por_frame = entrada.getnchannels() * entrada.getsampwidth()
        necesarios = frames * bytes_por_frame
        if len(datos) < necesarios:
            datos += b"\x00" * (necesarios - len(datos))
        elif len(datos) > necesarios:
            datos = datos[:necesarios]
        salida.writeframes(datos)


def normalizar_volumen(out, dry_run=False):
    """Normaliza todos los idiomas al mismo LUFS sin cambiar la duracion ni el alineado."""
    out = Path(out)
    ffmpeg = ffmpeg_exe()
    finales = {idioma: out / ("%s.wav" % idioma) for idioma in IDIOMAS}
    faltan = [str(p) for p in finales.values() if not p.is_file()]
    if faltan:
        raise SystemExit(
            "voz_3idiomas: faltan WAV para normalizar: %s" % ", ".join(faltan)
        )

    print("\nVolumen: objetivo %.1f LUFS, pico maximo %.1f dBTP." % (TARGET_LUFS, TARGET_TP))
    resultados = {}
    for idioma in IDIOMAS:
        final = finales[idioma]
        with wave.open(str(final), "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate()
            canales = w.getnchannels()

        entrada = medir_loudness(final, ffmpeg)
        entrada_i = float(entrada["input_i"])
        entrada_tp = float(entrada["input_tp"])
        if dry_run:
            print("  %s: %.2f LUFS, %.2f dBTP (sin escribir)" % (idioma, entrada_i, entrada_tp))
            resultados[idioma] = {"entrada_lufs": entrada_i, "entrada_tp": entrada_tp}
            continue

        extra = (
            ":measured_I=%s:measured_TP=%s:measured_LRA=%s:measured_thresh=%s:offset=%s:linear=true"
            % (
                entrada["input_i"],
                entrada["input_tp"],
                entrada["input_lra"],
                entrada["input_thresh"],
                entrada["target_offset"],
            )
        )
        filtro = "loudnorm=I=%.1f:TP=%.1f:LRA=11%s" % (TARGET_LUFS, TARGET_TP, extra)
        tmp = out / (".%s.norm.tmp.wav" % idioma)
        fixed = out / (".%s.norm.fixed.wav" % idioma)
        r = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-nostats",
                "-i",
                str(final),
                "-af",
                filtro,
                "-ar",
                str(rate),
                "-ac",
                str(canales),
                "-c:a",
                "pcm_s16le",
                str(tmp),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise SystemExit(
                "voz_3idiomas: ffmpeg fallo normalizando %s.\n%s"
                % (idioma, r.stderr[-1200:])
            )
        ajustar_frames(tmp, fixed, frames)
        tmp.unlink(missing_ok=True)
        fixed.replace(final)

        salida = medir_loudness(final, ffmpeg)
        salida_i = float(salida["input_i"])
        salida_tp = float(salida["input_tp"])
        resultados[idioma] = {
            "entrada_lufs": entrada_i,
            "entrada_tp": entrada_tp,
            "salida_lufs": salida_i,
            "salida_tp": salida_tp,
        }
        print(
            "  %s: %.2f -> %.2f LUFS (TP %.2f dBTP)"
            % (idioma, entrada_i, salida_i, salida_tp)
        )

    if not dry_run:
        datos = {
            "objetivo_lufs": TARGET_LUFS,
            "pico_max_dbtp": TARGET_TP,
            "generado": time.strftime("%Y-%m-%d %H:%M:%S"),
            "idiomas": resultados,
        }
        (out / "volumen.json").write_text(
            json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return resultados


def escribir_csv(out, targets, habla):
    cabecera = ["escena", "inicio_s", "fin_s", "objetivo_s"]
    cabecera += ["habla_%s_s" % idioma for idioma in IDIOMAS]
    cabecera += ["silencio_%s_s" % idioma for idioma in IDIOMAS]
    lineas = [",".join(cabecera)]
    inicio = 0.0
    for i, objetivo in enumerate(targets, start=1):
        fila = [
            str(i),
            "%.3f" % inicio,
            "%.3f" % (inicio + objetivo),
            "%.3f" % objetivo,
        ]
        for idioma in IDIOMAS:
            fila.append("%.3f" % habla[idioma][i - 1])
        for idioma in IDIOMAS:
            fila.append("%.3f" % max(0.0, objetivo - habla[idioma][i - 1]))
        lineas.append(",".join(fila))
        inicio += objetivo
    (out / "escenas.csv").write_text("\n".join(lineas) + "\n", encoding="utf-8")


def verificar(out):
    """Cierra en verde solo si todos los WAV tienen los mismos frames y hay SRT."""
    faltan = [out / ("%s.wav" % i) for i in IDIOMAS if not (out / ("%s.wav" % i)).is_file()]
    faltan += [out / ("%s.srt" % i) for i in IDIOMAS if not (out / ("%s.srt" % i)).is_file()]
    if faltan:
        raise SystemExit("voz_3idiomas: faltan salidas: %s" % ", ".join(str(p) for p in faltan))

    frames = {}
    for idioma in IDIOMAS:
        with wave.open(str(out / ("%s.wav" % idioma)), "rb") as w:
            frames[idioma] = w.getnframes()
    if len(set(frames.values())) != 1:
        raise SystemExit("voz_3idiomas: duraciones distintas: %s" % frames)

    volumen = out / "volumen.json"
    if not volumen.is_file():
        raise SystemExit(
            "voz_3idiomas: falta volumen.json; el audio no esta normalizado. "
            "Ejecuta --solo-normalizar."
        )
    datos_volumen = json.loads(volumen.read_text(encoding="utf-8"))
    niveles = {}
    picos = {}
    for idioma in IDIOMAS:
        datos_idioma = datos_volumen.get("idiomas", {}).get(idioma, {})
        if "salida_lufs" not in datos_idioma:
            raise SystemExit(
                "voz_3idiomas: volumen.json no tiene el nivel de %s." % idioma
            )
        niveles[idioma] = float(datos_idioma["salida_lufs"])
        picos[idioma] = float(datos_idioma.get("salida_tp", 0.0))
    diferencia = max(niveles.values()) - min(niveles.values())
    if diferencia > 0.8:
        raise SystemExit(
            "voz_3idiomas: volumen distinto entre idiomas: %s (diferencia %.2f dB)."
            % (niveles, diferencia)
        )
    if any(abs(nivel - TARGET_LUFS) > 1.2 for nivel in niveles.values()):
        raise SystemExit(
            "voz_3idiomas: volumen fuera del objetivo %.1f LUFS: %s."
            % (TARGET_LUFS, niveles)
        )

    total = frames["es"] / float(params_wav(out / "es.wav")[2])
    print("PASS: %s = %d frames (%.3fs) cada uno." % ("/".join(IDIOMAS), frames["es"], total))
    for idioma in IDIOMAS:
        srt = out / ("%s.srt" % idioma)
        cues = sum(1 for linea in srt.read_text(encoding="utf-8").splitlines() if linea.strip().isdigit())
        print("  %s.srt: %d cues" % (idioma, cues))
    print(
        "  volumen: %s LUFS; pico max %.1f dBTP"
        % (", ".join("%s %.1f" % (i, niveles[i]) for i in IDIOMAS), max(picos.values()))
    )

    alineacion = out / "alineacion.json"
    if alineacion.is_file():
        datos = json.loads(alineacion.read_text(encoding="utf-8"))
        relleno = {idioma: 0.0 for idioma in IDIOMAS}
        peor = {"escena": 0, "idioma": "", "silencio_s": 0.0}
        for escena in datos.get("escenas", []):
            for idioma in IDIOMAS:
                relleno[idioma] += escena["silencio_s"][idioma]
                if escena["silencio_s"][idioma] > peor["silencio_s"]:
                    peor = {
                        "escena": escena["escena"],
                        "idioma": idioma,
                        "silencio_s": escena["silencio_s"][idioma],
                    }
        print(
            "  relleno total: %s"
            % ", ".join("%s %.1fs" % (i, relleno[i]) for i in IDIOMAS)
        )
        print(
            "  mayor silencio: escena %s (%s) %.1fs"
            % (peor["escena"], peor["idioma"], peor["silencio_s"])
        )
    else:
        print("  aviso: no hay alineacion.json (corrida --sin-alinear).")


def escribir_alineacion(out, cache, targets, habla, textos, rutas, voces):
    inicio = 0.0
    escenas = []
    for i, objetivo in enumerate(targets, start=1):
        escenas.append(
            {
                "escena": i,
                "inicio_s": round(inicio, 3),
                "fin_s": round(inicio + objetivo, 3),
                "objetivo_s": round(objetivo, 3),
                "habla_s": {idioma: round(habla[idioma][i - 1], 3) for idioma in IDIOMAS},
                "silencio_s": {
                    idioma: round(max(0.0, objetivo - habla[idioma][i - 1]), 3) for idioma in IDIOMAS
                },
                "texto": {idioma: textos[idioma][i - 1] for idioma in IDIOMAS},
            }
        )
        inicio += objetivo
    datos = {
        "generado": time.strftime("%Y-%m-%d %H:%M:%S"),
        "vault": str(VAULT_ROOT),
        "cache": str(cache),
        "idiomas": list(IDIOMAS),
        "voces": voces,
        "fuentes": {idioma: str(rutas[idioma]) for idioma in IDIOMAS},
        "total_s": round(sum(targets), 3),
        "escenas": escenas,
    }
    (out / "alineacion.json").write_text(
        json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ─── Main ────────────────────────────────────────────────────────────────────


def resolver(texto, base=None):
    ruta = Path(texto)
    return ruta if ruta.is_absolute() else (base or VAULT_ROOT) / ruta


def descubrir_casos(vault):
    """Todos los casos con 2_guion.md bajo <vault>/02_produccion (sin _TEMPLATE)."""
    base = Path(vault) / "02_produccion"
    if not base.is_dir():
        return []
    return [guion.parent for guion in sorted(base.glob("**/2_guion.md")) if guion.parent.name != "_TEMPLATE"]


def casos_con_tres_guiones(vault=None):
    """Devuelve (casos con es+en+pt, casos que solo tienen espanol o les falta una traduccion)."""
    listos, bloqueados = [], []
    for caso in descubrir_casos(vault or VAULT_ROOT):
        if all((caso / ("2_guion%s.md" % sufijo)).is_file() for sufijo in ("", "_en", "_pt")):
            listos.append(caso)
        else:
            bloqueados.append(caso)
    return listos, bloqueados


def idiomas_del_caso(caso):
    """es/en/pt siempre; de si el caso tiene 2_guion_de.md y voices.json lo habilita."""
    idiomas = list(IDIOMAS_REQUERIDOS)
    if de_habilitado() and (Path(caso) / "2_guion_de.md").is_file():
        idiomas.append("de")
    return idiomas


def _voz_de_entrada(entrada, idioma):
    """Acepta "voz", {"es": "voz"} o {"voz": "es"} y devuelve el nombre."""
    if isinstance(entrada, str):
        return entrada
    if isinstance(entrada, dict):
        valor = entrada.get(idioma)
        if valor:
            return valor
        # {"voz": "x"} sin claves de idioma: vale para cualquiera.
        if not any(clave in entrada for clave in IDIOMAS_ORDEN):
            return next(iter(entrada.values()), None)
    return None


def de_habilitado():
    """El aleman solo entra si voices.json lo declara (o --voz-de lo pide)."""
    if (VOCES_CLI or {}).get("de"):
        return True
    cfg = VOCES_CFG or {}
    if _voz_de_entrada(cfg.get("default"), "de"):
        return True
    return any(_voz_de_entrada(valor, "de") for valor in (cfg.get("canales") or {}).values())


def cargar_voices_json(ruta):
    if not ruta:
        return {}
    ruta = Path(ruta)
    if not ruta.is_file():
        return {}
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise SystemExit("voz_3idiomas: %s no es JSON valido (%s)." % (ruta, exc))
    return datos or {}


def claves_canal(caso):
    """Claves con las que voices.json puede nombrar el canal del caso."""
    caso = Path(caso)
    claves = []
    partes = caso.resolve().parts
    if "02_produccion" in partes:
        idx = partes.index("02_produccion")
        if idx + 1 < len(partes):
            canal = partes[idx + 1]
            claves.append(canal)
            iniciales = "".join(parte[0] for parte in canal.split("-") if parte).lower()
            if iniciales:
                claves.append(iniciales)
    prefijo = caso.name.split("-", 1)[0]
    if prefijo and prefijo != caso.name:
        claves.append(prefijo)
    return claves


def voces_del_caso(caso, cfg=None, overrides=None):
    """Mezcla: builtin < voices.json default < canal < --voz-* de la CLI."""
    voces = dict(VOCES_DEFAULT)
    cfg = cfg or {}
    entrada = cfg.get("default")
    for idioma in IDIOMAS_REQUERIDOS + ("de",):
        valor = _voz_de_entrada(entrada, idioma)
        if valor:
            voces[idioma] = valor
    canales = cfg.get("canales") or {}
    for clave in claves_canal(caso):
        entrada_canal = canales.get(clave) or canales.get(clave.lower())
        if not entrada_canal:
            continue
        for idioma in IDIOMAS_REQUERIDOS + ("de",):
            valor = _voz_de_entrada(entrada_canal, idioma)
            if valor:
                voces[idioma] = valor
        break
    for idioma, valor in (overrides or {}).items():
        if valor:
            voces[idioma] = valor
    return voces


def iniciales_canal(caso):
    """Iniciales del canal ('tragico-clasico' -> 'tc') desde la ruta del caso."""
    partes = caso.resolve().parts
    if "02_produccion" not in partes:
        return None
    idx = partes.index("02_produccion")
    if idx + 1 >= len(partes):
        return None
    canal = partes[idx + 1]
    return "".join(parte[0] for parte in canal.split("-") if parte).lower()


def txt_del_caso(base_out, caso):
    """El .txt de guion que la carpeta de salida tiene para el caso, o None."""
    iniciales = iniciales_canal(caso)
    if not iniciales:
        return None
    ruta = Path(base_out) / ("%s-%s.txt" % (iniciales, caso.name))
    return ruta if ruta.is_file() else None


def salidas_completas(out, idiomas=None):
    return (out / "volumen.json").is_file() and all(
        (out / ("%s.%s" % (idioma, ext))).is_file()
        for idioma in (idiomas or IDIOMAS)
        for ext in ("wav", "srt")
    )


def comando_caso(caso, args, extra=(), es_txt=None, voces=None, idiomas=None):
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--vault",
        str(VAULT_ROOT),
        "--caso",
        str(caso),
    ]
    if VOICES_JSON.is_file():
        cmd += ["--voices-json", str(VOICES_JSON)]
    if es_txt:
        cmd += ["--es", str(es_txt)]
    cmd += [
        "--out-dir", args.out_dir,
        "--max-chars", str(args.max_chars),
        "--cfg", str(args.cfg),
        "--timesteps", str(args.timesteps),
    ]
    if args.normalize:
        cmd.append("--normalize")
    for idioma in (idiomas or IDIOMAS):
        cmd += ["--voz-%s" % idioma, (voces or {}).get(idioma) or getattr(args, "voz_%s" % idioma)]
    cmd += ["--server-url", args.server_url]
    if args.force:
        cmd.append("--force")
    if args.solo_alinear:
        cmd.append("--solo-alinear")
    if args.sin_alinear:
        cmd.append("--sin-alinear")
    if args.sin_normalizar:
        cmd.append("--sin-normalizar")
    cmd += list(extra)
    return cmd


def procesar_todos(args):
    global IDIOMAS
    if any([args.caso, args.es, args.en, args.pt, args.de, args.out, args.cache]):
        raise SystemExit(
            "voz_3idiomas: --todos no se combina con --caso/--es/--en/--pt/--out/--cache."
        )

    base_out = VAULT_ROOT / args.out_dir
    listos, bloqueados = casos_con_tres_guiones(VAULT_ROOT)
    if not listos and not bloqueados:
        print("No hay casos con 02_produccion/**/2_guion.md en %s." % VAULT_ROOT)
        return 0

    txts = {}
    sin_txt = []
    if args.desde_txt:
        for caso in listos:
            txt = txt_del_caso(base_out, caso)
            if txt:
                txts[caso] = txt
            else:
                sin_txt.append(caso)
        listos = [caso for caso in listos if caso in txts]

    print("Casos con es+en+pt: %d" % len(listos))
    print("Casos sin las tres traducciones: %d" % len(bloqueados))
    for caso in bloqueados:
        print("  BLOQUEADO: %s" % caso)
    if args.desde_txt:
        print("TXT de guion en %s/: %d" % (base_out.name, len(txts)))
        print("Casos sin TXT en %s/: %d" % (base_out.name, len(sin_txt)))
        for caso in sin_txt:
            print("  SIN TXT: %s" % caso)
    sys.stdout.flush()

    if args.verificar:
        fallos = 0
        for caso in listos:
            out = base_out / caso.name
            IDIOMAS = idiomas_del_caso(caso)
            print("\n=== VERIFICAR: %s ===" % caso)
            try:
                verificar(out)
            except SystemExit as exc:
                print("FALLO: %s" % exc)
                fallos += 1
        return 1 if fallos else 0

    procesados = omitidos = fallidos = 0
    for n, caso in enumerate(listos, 1):
        out = base_out / caso.name
        idiomas = idiomas_del_caso(caso)
        IDIOMAS = idiomas
        voces = voces_del_caso(caso, VOCES_CFG, VOCES_CLI)
        print("\n=== CASO %d/%d: %s ===" % (n, len(listos), caso))
        print("    voces: %s" % ", ".join("%s=%s" % (i, voces[i]) for i in idiomas))

        if not args.force and salidas_completas(out, idiomas):
            sys.stdout.flush()
            r = subprocess.run(
                comando_caso(caso, args, ["--verificar"], es_txt=txts.get(caso),
                             voces=voces, idiomas=idiomas),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if r.returncode == 0:
                print("SKIP: ya estaba generado y verificado.")
                if r.stdout.strip():
                    print(r.stdout.strip())
                omitidos += 1
                continue
            print("Las salidas existentes no verifican; se regenera.")

        cmd = comando_caso(caso, args, ["--dry-run"] if args.dry_run else [],
                           es_txt=txts.get(caso), voces=voces, idiomas=idiomas)
        sys.stdout.flush()
        r = subprocess.run(cmd, text=True, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            print("FALLO: %s termino con codigo %d." % (caso, r.returncode))
            fallidos += 1
            continue
        if args.dry_run:
            procesados += 1
            continue

        sys.stdout.flush()
        v = subprocess.run(
            comando_caso(caso, args, ["--verificar"], es_txt=txts.get(caso), voces=voces, idiomas=idiomas),
            text=True,
            stdin=subprocess.DEVNULL,
        )
        if v.returncode != 0:
            print("FALLO: la verificacion de %s no paso." % caso)
            fallidos += 1
            continue
        procesados += 1

    print("\n=== RESUMEN LOTE ===")
    print("Procesados: %d" % procesados)
    print("Omitidos:   %d" % omitidos)
    print("Fallidos:   %d" % fallidos)
    print("Bloqueados: %d (faltan 2_guion_en.md y/o 2_guion_pt.md)" % len(bloqueados))
    if sin_txt:
        print("Sin TXT:    %d (no estan en %s/)" % (len(sin_txt), base_out.name))
    if bloqueados:
        print("Para que el lote haga todas las voces, agrega las traducciones a:")
        for caso in bloqueados:
            print("  - %s" % caso)
    return 1 if fallidos else 0


def main():
    global IDIOMAS, VAULT_ROOT, RUTAS_JSON, VOICES_JSON, VOCES_CFG, VOCES_CLI
    ap = argparse.ArgumentParser(
        description="Genera es/en/pt (y de si hay guion aleman) con VoxCPM y alinea las versiones por escenas."
    )
    ap.add_argument("--vault", default=None, help="raiz del vault/proyecto (default: cwd)")
    ap.add_argument(
        "--voices-json",
        default=None,
        help="voices.json con la voz por idioma y por canal (default: <vault>/Sensible/voices.json)",
    )
    ap.add_argument("--caso", default=None, help="carpeta del video; busca 2_guion.md, _en, _pt (y _de si existe)")
    ap.add_argument("--es", default=None, help="guion espanol (.md o .txt)")
    ap.add_argument("--en", default=None, help="guion ingles (.md o .txt)")
    ap.add_argument("--pt", default=None, help="guion portugues (.md o .txt)")
    ap.add_argument("--de", default=None, help="guion aleman (.md o .txt, opcional)")
    ap.add_argument("--out", default=None, help="carpeta de los finales (default: Generate/<slug>)")
    ap.add_argument("--cache", default=None, help="carpeta de escenas y bloques (default: Generate/<slug>/voz)")
    ap.add_argument("--out-dir", default="Generate", help="carpeta raiz de salida cuando no se pasa --out (default: Generate)")
    ap.add_argument("--voz-es", default=None, help="default: voices.json o %s" % VOCES_DEFAULT["es"])
    ap.add_argument("--voz-en", default=None, help="default: voices.json o %s" % VOCES_DEFAULT["en"])
    ap.add_argument("--voz-pt", default=None, help="default: voices.json o %s" % VOCES_DEFAULT["pt"])
    ap.add_argument("--voz-de", default=None, help="default: voices.json o %s" % VOCES_DEFAULT["de"])
    ap.add_argument("--server-url", default=SERVER_DEFAULT)
    ap.add_argument("--max-chars", type=int, default=MAX_CHARS_DEFAULT)
    ap.add_argument("--cfg", type=float, default=2.0)
    ap.add_argument("--timesteps", type=int, default=10)
    ap.add_argument("--normalize", action="store_true")
    ap.add_argument("--force", action="store_true", help="regenera escenas y bloques existentes")
    ap.add_argument("--dry-run", action="store_true", help="solo muestra escenas y bloques")
    ap.add_argument("--solo-alinear", action="store_true", help="realinea escenas ya generadas")
    ap.add_argument("--sin-alinear", action="store_true", help="concatena sin rellenar silencio")
    ap.add_argument("--solo-normalizar", action="store_true", help="normaliza el volumen de WAV ya alineados y termina")
    ap.add_argument("--sin-normalizar", action="store_true", help="no normaliza el volumen")
    ap.add_argument("--verificar", action="store_true", help="comprueba salidas ya generadas y termina")
    ap.add_argument("--todos", action="store_true", help="procesa todos los casos con 2_guion.md, _en y _pt (+_de si existe); salta los ya verificados")
    ap.add_argument("--desde-txt", action="store_true",
                    help="usa los .txt de la carpeta --out-dir como guion espanol y procesa solo los casos que los tengan")
    args = ap.parse_args()

    VAULT_ROOT = Path(args.vault).expanduser().resolve() if args.vault else Path.cwd()
    RUTAS_JSON = VAULT_ROOT / "Sensible" / "rutas.json"
    VOICES_JSON = (
        Path(args.voices_json).expanduser().resolve()
        if args.voices_json
        else VAULT_ROOT / "Sensible" / "voices.json"
    )
    VOCES_CFG = cargar_voices_json(VOICES_JSON)
    VOCES_CLI = {idioma: getattr(args, "voz_%s" % idioma) for idioma in ("es", "en", "pt", "de")}

    if args.todos:
        raise SystemExit(procesar_todos(args))

    if args.caso:
        caso = resolver(args.caso)
        if args.es:
            fuente_es = resolver(args.es)
        else:
            fuente_es = caso / "2_guion.md"
            if args.desde_txt:
                txt = txt_del_caso(VAULT_ROOT / args.out_dir, caso)
                if txt:
                    fuente_es = txt
        fuentes = {
            "es": fuente_es,
            "en": resolver(args.en) if args.en else caso / "2_guion_en.md",
            "pt": resolver(args.pt) if args.pt else caso / "2_guion_pt.md",
        }
        if args.de:
            fuentes["de"] = resolver(args.de)
        elif (caso / "2_guion_de.md").is_file() and de_habilitado():
            fuentes["de"] = caso / "2_guion_de.md"
        IDIOMAS = [i for i in ("es", "en", "pt", "de") if i in fuentes]
        out = resolver(args.out) if args.out else VAULT_ROOT / args.out_dir / caso.name
        cache = resolver(args.cache) if args.cache else VAULT_ROOT / args.out_dir / caso.name / "voz"
    else:
        if not (args.es and args.en and args.pt):
            raise SystemExit("voz_3idiomas: pasa --caso o los tres --es/--en/--pt (--de opcional).")
        fuentes = {"es": resolver(args.es), "en": resolver(args.en), "pt": resolver(args.pt)}
        if args.de:
            fuentes["de"] = resolver(args.de)
        IDIOMAS = [i for i in ("es", "en", "pt", "de") if i in fuentes]
        if not args.out:
            raise SystemExit("voz_3idiomas: con --es/--en/--pt hace falta --out.")
        out = resolver(args.out)
        cache = resolver(args.cache) if args.cache else out / "voz"

    if args.verificar:
        if "de" not in IDIOMAS and (out / "de.wav").is_file():
            IDIOMAS = IDIOMAS + ["de"]
        verificar(out)
        return

    if args.solo_normalizar:
        if "de" not in IDIOMAS and (out / "de.wav").is_file():
            IDIOMAS = IDIOMAS + ["de"]
        normalizar_volumen(out)
        return

    for idioma in IDIOMAS:
        if not fuentes[idioma].is_file():
            raise SystemExit("voz_3idiomas: no existe el guion %s: %s" % (idioma, fuentes[idioma]))

    textos = {idioma: leer_escenas(fuentes[idioma]) for idioma in IDIOMAS}
    conteos = {idioma: len(textos[idioma]) for idioma in IDIOMAS}
    if len(set(conteos.values())) != 1:
        detalle = ", ".join("%s=%d" % (i, conteos[i]) for i in IDIOMAS)
        raise SystemExit(
            "voz_3idiomas: las escenas no coinciden (%s).\n"
            "Cada parrafo es una escena: iguala el numero de parrafos de los guiones."
            % detalle
        )
    if conteos["es"] == 0:
        raise SystemExit("voz_3idiomas: los guiones no tienen texto narrable.")

    voxcpm_dir = resolver_voxcpm()
    base_voces = resolver(args.caso) if args.caso else out
    resolucion = voces_del_caso(base_voces, VOCES_CFG, VOCES_CLI)
    voces = {idioma: resolucion[idioma] for idioma in IDIOMAS}
    voz_info = {idioma: cargar_voz(voxcpm_dir, voces[idioma]) for idioma in IDIOMAS}

    print("Guiones:")
    for idioma in IDIOMAS:
        bloques = sum(len(dividir_bloques(t, args.max_chars)) for t in textos[idioma])
        print(
            "  %s: %d escenas, %d caracteres, %d bloques, voz=%s"
            % (
                idioma,
                conteos[idioma],
                sum(len(t) for t in textos[idioma]),
                bloques,
                voces[idioma],
            )
        )
    print("Finales: %s" % out)
    print("Cache:   %s" % cache)

    if args.dry_run:
        print("Dry-run: no se genera ni se escribe nada.")
        return

    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    escenas = {idioma: [] for idioma in IDIOMAS}

    if args.solo_alinear:
        print("\nSolo alinear: se reutilizan las escenas ya generadas.")
        for idioma in IDIOMAS:
            for i in range(1, conteos[idioma] + 1):
                ruta = escena_dir(cache, idioma) / ("escena_%03d.wav" % i)
                if not ruta.is_file():
                    raise SystemExit("voz_3idiomas: falta la escena %s." % ruta)
                escenas[idioma].append(ruta)
    else:
        comprobar_servidor(args.server_url)
        inicio = time.time()
        for idioma in IDIOMAS:
            voz_wav, prompt_text = voz_info[idioma]
            print("\n=== %s: %s ===" % (idioma, voces[idioma]))
            for i, texto in enumerate(textos[idioma], start=1):
                ruta, nuevos, reutilizados = generar_escena(
                    cache,
                    idioma,
                    i,
                    texto,
                    voces[idioma],
                    voz_wav,
                    prompt_text,
                    args,
                )
                escenas[idioma].append(ruta)
                print(
                    "  escena %02d/%02d: %.2fs (%d nuevos, %d reutilizados)"
                    % (i, conteos[idioma], duracion_wav(ruta), nuevos, reutilizados),
                    flush=True,
                )
        print("\nGeneracion terminada en %.1fs." % (time.time() - inicio))

    habla = {
        idioma: [duracion_wav(ruta) for ruta in escenas[idioma]] for idioma in IDIOMAS
    }
    maximos = [max(habla[idioma][i] for idioma in IDIOMAS) for i in range(conteos[IDIOMAS[0]])]

    if args.sin_alinear:
        targets = {idioma: habla[idioma] for idioma in IDIOMAS}
        print("\nSin alinear: cada idioma conserva su duracion natural.")
    else:
        targets = {idioma: maximos for idioma in IDIOMAS}

    alinear(out, escenas, targets)
    for idioma in IDIOMAS:
        escribir_srt(
            cache,
            idioma,
            textos[idioma],
            targets[idioma],
            escenas[idioma],
            out / ("%s.srt" % idioma),
            max_chars=args.max_chars,
        )
        print(
            "%s.wav: %.2fs (habla %.2fs, %d escenas)"
            % (idioma, duracion_wav(out / ("%s.wav" % idioma)), sum(habla[idioma]), conteos[idioma])
        )

    if not args.sin_alinear:
        escribir_csv(out, maximos, habla)
        escribir_alineacion(out, cache, maximos, habla, textos, fuentes, voces)
        print("\nTotal alineado: %.2fs. Rejilla: %s" % (sum(maximos), out / "escenas.csv"))
        relleno = {
            idioma: sum(max(0.0, maximos[i] - habla[idioma][i]) for i in range(len(maximos)))
            for idioma in IDIOMAS
        }
        print(
            "Silencio de relleno: %s"
            % ", ".join("%s %.1fs" % (i, relleno[i]) for i in IDIOMAS)
        )
        if not args.sin_normalizar:
            normalizar_volumen(out)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
