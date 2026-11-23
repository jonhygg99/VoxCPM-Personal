"""Empalma CTAs nuevos en un audio ya narrado, sin re-generar la voz.

Problema que resuelve: watch_generate cachea los bloques por indice
(bloques/bloque_XXX.wav), no por texto. Si editas el guion, el cache queda
desfasado y una corrida normal reutiliza audio viejo sin avisar. La salida
correcta es generar solo las frases nuevas y empalmarlas aqui.

Uso (version CENTRAL; el vault no necesita copia):
  1. Genera los clips sueltos (carpetas con <algo>_completo.wav y .srt):
       voxcpm.bat <alias> -- --txt "<clip A>"   (y lo mismo con el clip B)
  2. Empalma:
       voxcpm.bat <alias> --empalmar -- --stem <carpeta de Generate> \\
           --clip-a <carpeta del clip A> --clip-b <carpeta del clip B> \\
           --hook "frase final del hook"

Sin --vault usa el cwd como raiz del vault; con --vault "D:/Obsidian Vaults/X"
toma de ese vault Generate/ y herramientas.ffmpeg de Sensible/rutas.json.

El CTA A se inserta justo despues del bloque que contiene --hook; el CTA B
sustituye todo lo que va desde el primer bloque que empieza por
"Si te gusto esta lista" hasta el final.
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUTAS = None  # lo fija main() a partir de --vault
INICIO_CTA = "Si te gustó esta lista"


def ffmpeg():
    import json

    if RUTAS and os.path.isfile(RUTAS):
        with open(RUTAS, encoding="utf-8-sig") as f:
            ruta = (json.load(f).get("herramientas") or {}).get("ffmpeg")
            if ruta and os.path.isfile(ruta):
                return ruta
    encontrado = shutil.which("ffmpeg")
    if encontrado:
        return encontrado
    raise SystemExit(
        "empalmar_cta: no encuentro ffmpeg. Define herramientas.ffmpeg en el "
        "Sensible/rutas.json del vault o dejalo en el PATH."
    )


def parse_srt(path):
    """Devuelve [(inicio_s, fin_s, texto)]."""
    raw = open(path, encoding="utf-8-sig").read().replace("\r\n", "\n")
    out = []
    for bloque in raw.strip().split("\n\n"):
        lineas = [l for l in bloque.split("\n") if l.strip()]
        if len(lineas) < 3:
            continue
        m = re.match(r"(\d+):(\d+):(\d+),(\d+)\s*-->\s*(\d+):(\d+):(\d+),(\d+)", lineas[1])
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        ini = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        fin = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        out.append((ini, fin, " ".join(lineas[2:])))
    return out


def fmt(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(path, entradas):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for i, (ini, fin, texto) in enumerate(entradas, 1):
            f.write(f"{i}\n{fmt(ini)} --> {fmt(fin)}\n{texto}\n\n")


def duracion(wav, exe):
    r = subprocess.run([exe, "-hide_banner", "-i", wav], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+)\.(\d+)", r.stderr)
    if not m:
        raise SystemExit(f"no pude leer la duracion de {wav}")
    h, mi, s, cs = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + cs / 100


def concat(rutas, salida, exe):
    lista = salida + ".lista.txt"
    with open(lista, "w", encoding="utf-8") as f:
        for r in rutas:
            f.write("file '%s'\n" % r.replace("\\", "/"))
    try:
        subprocess.run([exe, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
                        "-safe", "0", "-i", lista, "-c", "copy", salida], check=True)
    finally:
        os.remove(lista)


def clip(carpeta):
    """(wav, srt) del clip generado dentro de su carpeta."""
    wav = glob.glob(os.path.join(carpeta, "*_completo.wav"))
    srt = glob.glob(os.path.join(carpeta, "*_completo.srt"))
    if len(wav) != 1 or len(srt) != 1:
        raise SystemExit(f"{carpeta}: esperaba un *_completo.wav y un *_completo.srt, hay {len(wav)} y {len(srt)}")
    return wav[0], srt[0]


def empalmar(stem, clip_a, clip_b, hook, gen_dir, verificar=True):
    exe = ffmpeg()
    base = os.path.join(gen_dir, stem)
    wav_old = os.path.join(base, stem + "_completo.wav")
    srt_old = os.path.join(base, stem + "_completo.srt")
    bloques = os.path.join(base, "bloques")
    for p in (wav_old, srt_old, bloques):
        if not os.path.exists(p):
            raise SystemExit(f"falta {p}")

    viejo = parse_srt(srt_old)
    wav_a, srt_a = clip(clip_a)
    wav_b, srt_b = clip(clip_b)
    a = parse_srt(srt_a)
    b = parse_srt(srt_b)

    # ultimo bloque del hook (primera aparicion: el payoff suele repetir la frase)
    idx_hook = next((i for i, e in enumerate(viejo) if hook in e[2]), None)
    if idx_hook is None:
        raise SystemExit(f"--hook '{hook}' no aparece en el SRT de {stem}")
    if idx_hook > 10:
        raise SystemExit(f"--hook '{hook}' cae en la entrada {idx_hook + 1}: parece el cuerpo, no el hook")
    idx_cta = next((i for i, e in enumerate(viejo) if e[2].startswith(INICIO_CTA)), None)
    if idx_cta is None:
        raise SystemExit(f"no hay CTA viejo ('{INICIO_CTA}...') en {stem}")
    if idx_cta <= idx_hook:
        raise SystemExit("el CTA viejo aparece antes del hook: revisa el guion")

    ficheros = sorted(glob.glob(os.path.join(bloques, "bloque_*.wav")))
    if len(ficheros) != len(viejo):
        raise SystemExit(f"{stem}: {len(ficheros)} bloques en disco vs {len(viejo)} entradas en el SRT")

    rutas = ficheros[: idx_hook + 1] + [wav_a] + ficheros[idx_hook + 1: idx_cta] + [wav_b]

    respaldo = os.path.join(base, "_respaldo-antes-cta")
    os.makedirs(respaldo, exist_ok=True)
    for p in (wav_old, srt_old):
        shutil.copy2(p, os.path.join(respaldo, os.path.basename(p)))

    tmp = wav_old + ".nuevo.wav"
    concat(rutas, tmp, exe)
    shutil.move(tmp, wav_old)

    # SRT: tiempos absolutos del original, desplazados por la duracion del CTA A
    delta = duracion(wav_a, exe)
    nuevas = list(viejo[: idx_hook + 1])
    cursor = viejo[idx_hook][1]
    for ini, fin, texto in a:
        dur = fin - ini
        nuevas.append((cursor, cursor + dur, texto))
        cursor += dur
    for ini, fin, texto in viejo[idx_hook + 1: idx_cta]:
        dur = fin - ini
        nuevas.append((ini + delta, ini + delta + dur, texto))
    cursor = nuevas[-1][1]
    for ini, fin, texto in b:
        dur = fin - ini
        nuevas.append((cursor, cursor + dur, texto))
        cursor += dur
    write_srt(srt_old, nuevas)

    print(f"{stem}: {len(viejo)} -> {len(nuevas)} entradas | "
          f"{duracion(wav_old, exe):.2f}s | CTA A +{delta:.2f}s")

    if verificar:
        # muestras de 40 ms: el audio nuevo debe estar exactamente donde dice el SRT
        def md5(wav, desde, dur):
            r = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-ss", f"{desde:.3f}",
                                "-t", f"{dur:.3f}", "-i", wav, "-f", "md5", "-"],
                               capture_output=True, text=True)
            return r.stdout.strip()
        puntos = [("CTA A", viejo[idx_hook][1], wav_a, 0.0),
                  ("CTA B", nuevas[len(nuevas) - len(b)][0], wav_b, 0.0)]
        for nombre, t, ref, desde in puntos:
            ok = md5(wav_old, t, 0.04) == md5(ref, desde, 0.04)
            print(f"   {nombre} en {t:8.2f}s: {'OK' if ok else 'NO COINCIDE'}")
            if not ok:
                raise SystemExit("la verificacion fallo: el audio no esta donde dice el SRT")

    # El cache de bloques ya no corresponde al wav final. Se aparta para que una
    # futura corrida regenere en vez de revertir los CTAs en silencio.
    apartado = os.path.join(base, "bloques-pre-cta")
    if os.path.isdir(bloques):
        if os.path.isdir(apartado):
            shutil.rmtree(apartado)
        os.rename(bloques, apartado)
        print(f"   cache de bloques apartado en {os.path.basename(apartado)}/")


def main():
    p = argparse.ArgumentParser(description="Empalma CTAs nuevos en un audio ya narrado.")
    p.add_argument("--vault", default=None,
                   help="raiz del vault (default: cwd); de ahi salen Generate/ y rutas.json")
    p.add_argument("--stem", required=True, help="Nombre de la carpeta dentro de Generate")
    p.add_argument("--clip-a", required=True, help="Carpeta del clip que va tras el hook")
    p.add_argument("--clip-b", required=True, help="Carpeta del clip que sustituye al CTA viejo")
    p.add_argument("--hook", required=True, help="Frase final del hook (ultimo bloque del hook)")
    p.add_argument("--generate-dir", default=None,
                   help="carpeta de salida (default: <vault>/Generate)")
    p.add_argument("--sin-verificar", action="store_true")
    a = p.parse_args()

    global RUTAS
    vault = os.path.abspath(a.vault) if a.vault else os.getcwd()
    RUTAS = os.path.join(vault, "Sensible", "rutas.json")
    if a.generate_dir is None:
        a.generate_dir = os.path.join(vault, "Generate")
    empalmar(a.stem, a.clip_a, a.clip_b, a.hook, a.generate_dir, verificar=not a.sin_verificar)


if __name__ == "__main__":
    main()
