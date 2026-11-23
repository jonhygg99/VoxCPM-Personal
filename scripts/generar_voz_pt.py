#!/usr/bin/env python3
"""generar_voz_pt.py — narra en portugues el txt de Generate y deja solo la voz pt.

Mismo motor y misma cache que generar_voz_3idiomas.py (servidor VoxCPM local,
voz pt = leo_antonio), pero sin pedir los guiones es/en: lee el txt de Generate,
lo divide en escenas (parrafos) y bloques, y escribe unicamente la version
portuguesa.

Salidas (una carpeta por txt, igual que el flujo de tres idiomas):

  Generate/<slug>/pt.wav    voz completa en portugues (-16 LUFS, -1.5 dBTP)
  Generate/<slug>/pt.srt    cues por bloque
  Generate/<slug>/voz/      escenas y bloques (cache compartida con 3idiomas)

El txt debe estar YA escrito en portugues: este script no traduce. Si el
Generate/<slug>/pt.wav alineado ya lo hizo generar_voz_3idiomas.py, este script
lo sobrescribe sin relleno de silencio (duracion natural del portugues).

Uso (a traves del lanzador central, que pasa --vault):
  voxcpm.bat archivo -- --txt "01-tablilla-motin-dioses"
  voxcpm.bat archivo -- --txt "..." --dry-run
  voxcpm.bat archivo -- --txt "..." --force
  voxcpm.bat archivo -- --todos
  voxcpm.bat archivo -- --todos --force
  voxcpm.bat archivo -- --txt "..." --verificar
  voxcpm.bat archivo -- --listar        (menu con semaforo)
  voxcpm.bat archivo -- --nombre 3      (el txt numero 3)

El servidor VoxCPM se arranca solo si esta apagado (ventana aparte, espera hasta
8 min a que responda). Sin argumentos abre el menu con los txt de Generate y su
semaforo: Done en verde, In Progress en naranja, Not Started en rojo.
"""

import argparse
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voz_3idiomas as base  # noqa: E402

# Vault en curso: lo fija main() con --vault (o el cwd si no se pasa).
GENERATE = Path.cwd() / "Generate"
IDIOMA = "pt"
VOZ_DEFAULT = base.VOCES_DEFAULT[IDIOMA]
ESPERA_SERVIDOR_S = 480

# Semaforo del menu: verde = pt.wav hecho, naranja = carpeta a medias, rojo = nada.
COLOR = {"done": "\033[92m", "progress": "\033[38;5;208m", "todo": "\033[91m"}
ETIQUETA = {"done": "Done", "progress": "In Progress", "todo": "Not Started"}
RESET = "\033[0m"
_COLOR = False


def habilitar_color():
    """Activa ANSI en la consola de Windows (solo si hay consola real)."""
    global _COLOR
    if not sys.stdout.isatty():
        return
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            modo = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(modo)):
                kernel32.SetConsoleMode(handle, modo.value | 4)  # 4 = VT processing
        except Exception:
            pass
    _COLOR = True


def pintar(texto, estado):
    return "%s%s%s" % (COLOR[estado], texto, RESET) if _COLOR else texto


def estado_caso(txt):
    out = GENERATE / txt.stem
    if (out / "pt.wav").is_file():
        return "done"
    return "progress" if out.is_dir() else "todo"


def menu(txts):
    """Lista numerada con el semaforo al lado (lo usa el .bat)."""
    habilitar_color()
    ancho = len(str(len(txts)))
    for i, t in enumerate(txts, start=1):
        estado = estado_caso(t)
        print(
            "%*d. %s  %s" % (ancho, i, t.name, pintar(ETIQUETA[estado], estado))
        )


def servidor_arriba(url):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=3) as resp:
            return resp.status == 200
    except OSError:
        return False


def asegurar_servidor(url):
    """Arranca VoxCPM si esta apagado y espera a que responda."""
    if servidor_arriba(url):
        return
    voxcpm = base.resolver_voxcpm()
    venv = voxcpm / "venv_voxcpm" / "Scripts" / "python.exe"
    wrapper = Path(__file__).resolve().parent / "voxcpm_win_launcher.py"
    if not venv.is_file() or not wrapper.is_file():
        raise SystemExit(
            "generar_voz_pt: no encuentro el venv (%s) ni el lanzador (%s) para "
            "arrancar VoxCPM." % (venv, wrapper)
        )
    print("VoxCPM apagado: arrancandolo en otra ventana (el modelo tarda en cargar)...", flush=True)
    subprocess.Popen(
        [str(venv), str(wrapper)],
        cwd=str(voxcpm),
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )
    limite = time.time() + ESPERA_SERVIDOR_S
    while time.time() < limite:
        if servidor_arriba(url):
            print("VoxCPM listo.")
            return
        time.sleep(2)
    raise SystemExit(
        "generar_voz_pt: VoxCPM no respondio en %d s. Revisa la ventana del servidor "
        "(o arrancalo con 'voxcpm.bat' -> [s])." % ESPERA_SERVIDOR_S
    )


def resolver_txt(valor):
    """Ruta completa, o el nombre del txt dentro de Generate/."""
    ruta = Path(valor)
    if ruta.is_file():
        return ruta
    nombre = ruta.name if ruta.suffix.lower() == ".txt" else ruta.name + ".txt"
    candidata = GENERATE / nombre
    if not candidata.is_file():
        raise SystemExit("generar_voz_pt: no existe %s" % candidata)
    return candidata


def listar_txt():
    return sorted(p for p in GENERATE.glob("*.txt") if p.is_file())


def normalizar(ruta):
    """Deja el WAV al nivel del pt.wav de 3idiomas sin cambiar frames."""
    ffmpeg = base.ffmpeg_exe()
    with wave.open(str(ruta), "rb") as w:
        frames, rate, canales = w.getnframes(), w.getframerate(), w.getnchannels()

    entrada = base.medir_loudness(ruta, ffmpeg)
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
    filtro = "loudnorm=I=%.1f:TP=%.1f:LRA=11%s" % (base.TARGET_LUFS, base.TARGET_TP, extra)
    tmp = ruta.with_name(".pt.norm.tmp.wav")
    fijo = ruta.with_name(".pt.norm.fixed.wav")
    r = subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-nostats", "-i", str(ruta),
            "-af", filtro, "-ar", str(rate), "-ac", str(canales),
            "-c:a", "pcm_s16le", str(tmp),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise SystemExit(
            "generar_voz_pt: ffmpeg fallo normalizando %s.\n%s" % (ruta, r.stderr[-1200:])
        )
    base.ajustar_frames(tmp, fijo, frames)
    tmp.unlink(missing_ok=True)
    fijo.replace(ruta)

    salida = base.medir_loudness(ruta, ffmpeg)
    print(
        "  volumen: %.2f -> %.2f LUFS (pico %.2f dBTP)"
        % (float(entrada["input_i"]), float(salida["input_i"]), float(salida["input_tp"]))
    )


def verificar(txt):
    out = GENERATE / txt.stem
    final, srt = out / "pt.wav", out / "pt.srt"
    faltan = [p for p in (final, srt) if not p.is_file()]
    if faltan:
        raise SystemExit(
            "generar_voz_pt: faltan salidas de %s: %s" % (txt.stem, ", ".join(str(p) for p in faltan))
        )
    nivel = float(base.medir_loudness(final, base.ffmpeg_exe())["input_i"])
    cues = sum(1 for l in srt.read_text(encoding="utf-8").splitlines() if l.strip().isdigit())
    print(
        "PASS %s: pt.wav %.2fs, %.2f LUFS, pt.srt %d cues"
        % (txt.stem, base.duracion_wav(final), nivel, cues)
    )


def procesar(txt, args):
    out = GENERATE / txt.stem
    cache = out / "voz"
    final, srt = out / "pt.wav", out / "pt.srt"

    if not args.force and final.is_file() and srt.is_file():
        print("SKIP %s: pt.wav y pt.srt ya existen (--force para rehacer)." % txt.stem)
        return True

    textos = base.leer_escenas(txt)
    if not textos:
        print("AVISO %s: sin texto narrable; se salta." % txt.stem)
        return False

    bloques = sum(len(base.dividir_bloques(t, args.max_chars)) for t in textos)
    print(
        "\n=== %s: %d escenas, %d caracteres, %d bloques ==="
        % (txt.stem, len(textos), sum(len(t) for t in textos), bloques)
    )
    if args.dry_run:
        return True

    asegurar_servidor(args.server_url)
    print("Voz pt: %s" % args.voz)
    voz_wav, prompt_text = base.cargar_voz(base.resolver_voxcpm(), args.voz)
    if not out.is_dir():
        print(
            "  aviso: %s no existia; se crea con el slug del txt "
            "(nombra el txt igual que la carpeta del caso)." % out.name
        )
    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    rutas = []
    for i, texto in enumerate(textos, start=1):
        ruta, nuevos, reutilizados = base.generar_escena(
            cache, IDIOMA, i, texto, args.voz, voz_wav, prompt_text, args
        )
        rutas.append(ruta)
        print(
            "  escena %02d/%02d: %.2fs (%d nuevos, %d reutilizados)"
            % (i, len(textos), base.duracion_wav(ruta), nuevos, reutilizados),
            flush=True,
        )

    base.concatenar_wavs(rutas, final)
    habla = [base.duracion_wav(r) for r in rutas]
    base.escribir_srt(cache, IDIOMA, textos, habla, rutas, srt, max_chars=args.max_chars)
    print("pt.wav: %.2fs (%d escenas) -> %s" % (base.duracion_wav(final), len(textos), final))
    if not args.sin_normalizar:
        normalizar(final)
    return True


def main():
    ap = argparse.ArgumentParser(
        description="Narra en portugues el txt de Generate (solo la voz pt)."
    )
    ap.add_argument("--vault", default=None,
                    help="raiz del vault (default: cwd); el lanzador central siempre la pasa")
    ap.add_argument("--txt", default=None, help="txt de Generate ('slug', 'slug.txt' o ruta)")
    ap.add_argument("--todos", action="store_true", help="todos los Generate/*.txt sin voz pt, uno tras otro")
    ap.add_argument("--voz", default=VOZ_DEFAULT, help="voz pt de VoxCPM (default: %s)" % VOZ_DEFAULT)
    ap.add_argument("--server-url", default=base.SERVER_DEFAULT)
    ap.add_argument("--max-chars", type=int, default=base.MAX_CHARS_DEFAULT)
    ap.add_argument("--cfg", type=float, default=2.0)
    ap.add_argument("--timesteps", type=int, default=10)
    ap.add_argument("--normalize", action="store_true", help="normalizacion de texto de VoxCPM")
    ap.add_argument("--force", action="store_true", help="regenera escenas y bloques existentes")
    ap.add_argument("--dry-run", action="store_true", help="solo cuenta escenas y bloques")
    ap.add_argument("--sin-normalizar", action="store_true", help="no ajusta el volumen a -16 LUFS")
    ap.add_argument("--verificar", action="store_true", help="comprueba pt.wav y pt.srt ya generados")
    ap.add_argument("--listar", action="store_true", help="menu numerado con el semaforo (uso del .bat)")
    ap.add_argument("--nombre", type=int, default=None, help="imprime el txt numero N de --listar")
    args = ap.parse_args()

    global GENERATE
    vault = Path(args.vault).expanduser().resolve() if args.vault else Path.cwd()
    GENERATE = vault / "Generate"

    if args.listar:
        menu(listar_txt())
        return

    if args.nombre is not None:
        txts = listar_txt()
        if not 1 <= args.nombre <= len(txts):
            raise SystemExit("generar_voz_pt: no hay txt numero %d." % args.nombre)
        print(txts[args.nombre - 1].name)
        return

    if args.txt and args.todos:
        raise SystemExit("generar_voz_pt: elige --txt o --todos, no los dos.")

    if args.txt:
        txts = [resolver_txt(args.txt)]
    elif args.todos:
        txts = listar_txt()
        if not txts:
            raise SystemExit("generar_voz_pt: no hay Generate/*.txt.")
        if not args.force:
            txts = [t for t in txts if estado_caso(t) != "done"]
        if not txts:
            print("Nada pendiente: todos los txt ya tienen pt.wav (--force para rehacer).")
            return
        print("Se procesaran %d txt sin voz pt:" % len(txts))
        for t in txts:
            print("  %s" % t.name)
    else:
        print("Uso: --txt <slug> para uno, o --todos para todo Generate/*.txt.")
        print("Txt disponibles:")
        menu(listar_txt())
        raise SystemExit(2)

    if args.verificar:
        for txt in txts:
            verificar(txt)
        return

    fallidos = 0
    for txt in txts:
        if not procesar(txt, args):
            fallidos += 1
    if fallidos:
        raise SystemExit("generar_voz_pt: %d txt sin generar." % fallidos)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
