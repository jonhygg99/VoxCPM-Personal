#!/usr/bin/env python3
"""Lanzador global de VoxCPM para varios vaults/proyectos.

Un solo motor y un solo servidor: el modelo se carga una vez y los proyectos
se encadenan sin reiniciarlo. Cada proyecto declara su vault y el modo se
detecta solo (3lang si hay 02_produccion/**/2_guion.md, watch si no).

Archivos que lee:
  Sensible/proyectos.json   registro local (NO se comparte) con alias -> vault
  <vault>/Sensible/voices.json   voz por idioma y por canal
  <vault>/Sensible/rutas.json    vault, out_dir y rutas de herramientas

Uso:
  voxcpm.bat                          menu principal
  voxcpm.bat --vault "D:/.../vault"   proyecto ad-hoc (sin registro)
  voxcpm.bat listas                   abre ese proyecto
  voxcpm.bat listas --modo watch      fuerza el modo
  voxcpm.bat --auto                   corre pendientes de todos, en orden
  voxcpm.bat --doctor                 valida rutas, voces, venv y servidor
  voxcpm.bat --escanear               agrega vaults nuevos al registro
  voxcpm.bat listas -- --dry-run      pasa argumentos al generador
  voxcpm.bat listas --empalmar -- --stem ... --clip-a ... --clip-b ... --hook "..."
                                      empalma CTAs en un audio ya narrado
  voxcpm.bat archivo --pt -- --txt "..."   solo la voz portuguesa de ese txt
  voxcpm.bat --vault "D:/.../vault" --accion watch   accion directa (shims)

Modos: 3lang (guiones es/en/pt) y watch (VoxCPM local por .txt). La voz se
genera siempre en local; el vault no necesita scripts de voz.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import hashlib
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
SCRIPTS = ROOT / "scripts"
VENV_PY = ROOT / "venv_voxcpm" / "Scripts" / "python.exe"
SERVER_URL = "http://127.0.0.1:8765"
REGISTRY = ROOT / "Sensible" / "proyectos.json"
EJEMPLO = ROOT / "Sensible" / "proyectos.ejemplo.json"
LOGS = ROOT / "logs" / "voz"
WATCH_LOCK_PORT = 8766
ANCHO_MENU = 66
NEGRITA, RESET = "\033[1m", "\033[0m"
ROJO = "\033[91m"
# Paleta amplia para que dos proyectos seguidos no compartan color casi nunca.
COLORES = (
    "\033[96m", "\033[93m", "\033[95m", "\033[92m", "\033[94m", "\033[38;5;208m",
    "\033[38;5;135m", "\033[38;5;51m", "\033[38;5;213m", "\033[38;5;154m",
    "\033[38;5;220m", "\033[91m",
)
PT_SCRIPT = SCRIPTS / "generar_voz_pt.py"
EMPALME_SCRIPT = SCRIPTS / "empalmar_cta.py"

SIN_SERVIDOR = False
IDIOMAS_ORDEN = ("es", "en", "pt")


# ─── Utilidades ──────────────────────────────────────────────────────────────


def python_exe():
    return str(VENV_PY) if VENV_PY.is_file() else sys.executable


def leer_json(ruta):
    with open(ruta, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def escribir_json(ruta, datos):
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def preguntar(texto):
    try:
        return input(texto).strip()
    except EOFError:
        raise SystemExit(0)


def slug(texto):
    limpio = re.sub(r"[^a-z0-9]+", "-", texto.lower()).strip("-")
    return limpio or "proyecto"


def puerto_abierto(puerto):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        return sock.connect_ex(("127.0.0.1", puerto)) == 0
    finally:
        sock.close()


# ─── Registro de proyectos ───────────────────────────────────────────────────


@dataclass
class Proyecto:
    alias: str
    nombre: str
    vault: Path
    modo: str = "auto"
    orden: int = 100
    out_dir: str = "Generate"
    guiones_glob: str = "02_produccion/**/2_guion.md"
    voces_path: Path | None = None
    args: list[str] = field(default_factory=list)

    @property
    def generate(self):
        return self.vault / self.out_dir

    @property
    def voices_json(self):
        return self.voces_path or (self.vault / "Sensible" / "voices.json")


def cargar_registro(crear=False):
    if not REGISTRY.is_file():
        if crear and EJEMPLO.is_file():
            REGISTRY.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(EJEMPLO, REGISTRY)
            print(f"Creado {REGISTRY} desde el ejemplo. Revisa las rutas.")
        else:
            raise SystemExit(
                f"No existe {REGISTRY}.\n"
                "Crea el registro copiando Sensible/proyectos.ejemplo.json "
                "(o corre con --escanear una vez exista al menos un vault)."
            )
    datos = leer_json(REGISTRY)
    proyectos = {}
    for alias, raw in (datos.get("proyectos") or {}).items():
        if not isinstance(raw, dict) or not raw.get("vault"):
            print(f"Aviso: '{alias}' no tiene 'vault'; se ignora.")
            continue
        vault = Path(os.path.expandvars(str(raw["vault"]))).expanduser()
        proyectos[alias.lower()] = Proyecto(
            alias=alias.lower(),
            nombre=raw.get("nombre") or alias,
            vault=vault,
            modo=str(raw.get("modo") or "auto").lower(),
            orden=int(raw.get("orden", 100)),
            out_dir=str(raw.get("out_dir") or raw.get("generate") or "Generate"),
            guiones_glob=str(raw.get("guiones_glob") or "02_produccion/**/2_guion.md"),
            voces_path=Path(raw["voces"]).expanduser() if raw.get("voces") else None,
            args=[str(a) for a in (raw.get("args") or [])],
        )
    return datos, proyectos


def ordenados(proyectos):
    return sorted(proyectos.values(), key=lambda p: (p.orden, p.nombre.lower()))


def buscar_proyecto(proyectos, texto):
    texto = texto.strip().lower()
    if texto in proyectos:
        return proyectos[texto]
    # Numero de la lista que muestran el menu principal y "¿Otro proyecto?".
    if texto.isdigit():
        opciones = ordenados(proyectos)
        indice = int(texto) - 1
        if 0 <= indice < len(opciones):
            return opciones[indice]
        return None
    for p in proyectos.values():
        if texto and (texto in p.nombre.lower() or texto == slug(p.nombre)):
            return p
    return None


# ─── Voces ───────────────────────────────────────────────────────────────────


_VOCES_CACHE = {}


def config_voces(p):
    ruta = p.voices_json
    if not ruta.is_file():
        return {}
    try:
        clave = (str(ruta), ruta.stat().st_mtime)
    except OSError:
        return {}
    if clave in _VOCES_CACHE:
        return _VOCES_CACHE[clave]
    try:
        datos = leer_json(ruta) or {}
    except ValueError as exc:
        print(f"Aviso: {ruta} no es JSON valido ({exc}); se ignora.")
        datos = {}
    _VOCES_CACHE.clear()
    _VOCES_CACHE[clave] = datos
    return datos


def idiomas_proyecto(p):
    cfg = config_voces(p)
    entrada = cfg.get("default")
    if isinstance(entrada, dict):
        idiomas = [i for i in IDIOMAS_ORDEN if entrada.get(i)]
    elif isinstance(entrada, str):
        idiomas = ["es"]
    else:
        idiomas = []
    # Un voices.json con canales en varios idiomas tambien es 3lang.
    for valor in (cfg.get("canales") or {}).values():
        if isinstance(valor, dict):
            for idioma in IDIOMAS_ORDEN:
                if valor.get(idioma) and idioma not in idiomas:
                    idiomas.append(idioma)
    return idiomas or ["es"]


def voz_default(p):
    cfg = config_voces(p)
    entrada = cfg.get("default")
    if isinstance(entrada, str):
        return entrada
    if isinstance(entrada, dict):
        return entrada.get("es") or next(iter(entrada.values()), "david")
    return "david"


def resumen_voces(p):
    cfg = config_voces(p)
    entrada = cfg.get("default")
    if isinstance(entrada, str):
        voces = {"es": entrada}
    elif isinstance(entrada, dict):
        voces = {i: entrada[i] for i in IDIOMAS_ORDEN if entrada.get(i)}
    else:
        return "sin voices.json"
    # Idiomas que solo existen en un canal.
    for valor in (cfg.get("canales") or {}).values():
        if isinstance(valor, dict):
            for idioma in IDIOMAS_ORDEN:
                if valor.get(idioma) and not voces.get(idioma):
                    voces[idioma] = valor[idioma]
    return " ".join(f"{i}={voces[i]}" for i in IDIOMAS_ORDEN if voces.get(i))


def tabla_voces(p):
    cfg = config_voces(p)
    print(f"\nVoces de {p.nombre} ({p.voices_json})")
    entrada = cfg.get("default")
    if isinstance(entrada, str):
        print(f"  default: {entrada}")
    elif isinstance(entrada, dict):
        print("  default: " + ", ".join(f"{i}={entrada[i]}" for i in IDIOMAS_ORDEN if entrada.get(i)))
    else:
        print("  default: (no definido)")
    canales = cfg.get("canales") or {}
    if not canales:
        print("  canales: (ninguno; se usa default)")
        return
    for clave, valor in canales.items():
        if isinstance(valor, str):
            print(f"  {clave}: {valor}")
        elif isinstance(valor, dict):
            print(f"  {clave}: " + ", ".join(f"{i}={valor[i]}" for i in IDIOMAS_ORDEN if valor.get(i)))
    print("\n  El canal se elige por el prefijo del .txt o por la carpeta del caso.")


def validar_voz(idioma, nombre):
    py = ROOT / "voices" / f"{nombre}.py"
    wav = ROOT / "voices" / idioma / f"{nombre}.wav"
    if not wav.is_file():
        candidatos = sorted((ROOT / "voices").glob(f"*/{nombre}.wav"))
        if candidatos:
            wav = candidatos[0]
    return py.is_file(), wav.is_file(), py, wav


# ─── Modo y pendientes ───────────────────────────────────────────────────────


def modulo_voz():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import voz_3idiomas

    return voz_3idiomas


def cargar_voces_en_motor(p):
    """Carga el voices.json del proyecto en el motor 3lang.

    voz_3idiomas guarda la config en globals (VOCES_CFG/VOCES_CLI). Si no se
    recarga por proyecto, de_habilitado() queda con la config anterior y el
    conteo de pendientes mira (o ignora) el aleman equivocado.
    """
    v3 = modulo_voz()
    v3.VOCES_CFG = config_voces(p)
    v3.VOCES_CLI = {}
    return v3


def _hay_casos_3lang(p):
    """True si al menos un caso tiene las tres traducciones (es+en+pt)."""
    base = p.vault / "02_produccion"
    if not base.is_dir():
        return False
    # os.walk y no pathlib.glob: glob aborta si un subdirectorio desaparece a
    # mitad del barrido (carpetas sincronizadas por Syncthing en Salvaje Oeste).
    for raiz, _dirs, ficheros in os.walk(base, onerror=lambda _e: None):
        if "2_guion.md" not in ficheros:
            continue
        caso = Path(raiz)
        if caso.name == "_TEMPLATE":
            continue
        if (caso / "2_guion_en.md").is_file() and (caso / "2_guion_pt.md").is_file():
            return True
    return False


def detectar_modo(p):
    if p.modo in ("3lang", "watch", "servidor"):
        return p.modo
    if _hay_casos_3lang(p):
        return "3lang"
    if len(idiomas_proyecto(p)) > 1:
        return "3lang"
    return "watch"


def casos_3lang(p):
    v3 = modulo_voz()
    return v3.descubrir_casos(p.vault)


def pendientes_3lang(p):
    v3 = cargar_voces_en_motor(p)
    base = p.generate
    pendientes = []
    for caso in casos_3lang(p):
        if v3.caso_omitido(caso):
            continue
        sufijos = ("", "_en", "_pt")
        if not all((caso / f"2_guion{s}.md").is_file() for s in sufijos):
            continue
        idiomas = v3.idiomas_del_caso(caso)
        if not v3.salidas_completas(v3.carpeta_salida(base, caso), idiomas):
            pendientes.append(caso)
    return pendientes


_WATCH_MODULE = None


def modulo_watch():
    global _WATCH_MODULE
    if _WATCH_MODULE is None:
        if str(APP) not in sys.path:
            sys.path.insert(0, str(APP))
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import watch_generate

        _WATCH_MODULE = watch_generate
    return _WATCH_MODULE


def plan_watch(txt_path, watch_dir):
    """Pendiente de watch con la misma regla que usa el vigilante.

    No alcanza con que existan el SRT y la carpeta: un shorts interrumpido
    puede tener el SRT final y solo algunos WAV. Delegar en is_done evita que
    el lanzador y watch_generate se desincronicen.
    """
    wg = modulo_watch()
    plan = wg.plan_outputs(str(txt_path), str(watch_dir))
    return {"kind": plan.kind, "ok": wg.is_done(plan, str(txt_path))}


def pendientes_watch(p):
    watch = p.generate
    if not watch.is_dir():
        return []
    wg = modulo_watch()
    return [
        txt
        for txt in sorted(watch.glob("*.txt"))
        if not wg.omitido(str(txt)) and not plan_watch(txt, watch)["ok"]
    ]


def cmd_pt(p, extra):
    cmd = [python_exe(), str(PT_SCRIPT), "--vault", str(p.vault)]
    return cmd + list(p.args) + list(extra)


def pendientes(p, modo=None):
    modo = modo or detectar_modo(p)
    if modo == "3lang":
        return pendientes_3lang(p)
    if modo == "watch":
        return pendientes_watch(p)
    return []


def color_proyecto(p):
    """Color estable por alias: no cambia al anadir o quitar proyectos."""
    return COLORES[hashlib.sha256(p.alias.encode("utf-8")).digest()[0] % len(COLORES)]


def habilitar_ansi():
    """Activa las secuencias ANSI en la consola de Windows (VT processing)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        modo = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(modo)):
            kernel32.SetConsoleMode(handle, modo.value | 4)
    except Exception:
        pass


def _consola_utf8():
    """True si la salida admite UTF-8 (consola en 65001 o salida redirigida)."""
    if not sys.stdout.isatty() or os.name != "nt":
        return True
    try:
        import ctypes

        return ctypes.windll.kernel32.GetConsoleOutputCP() == 65001
    except Exception:
        return False


def bullet(consola_utf8=None):
    """Punto medio si la consola lo pinta; guion ASCII si no (cmd antiguas)."""
    if consola_utf8 is None:
        consola_utf8 = _consola_utf8()
    return "\u00b7" if consola_utf8 else "-"


def colorido(texto, color, activo=None):
    """Aplica un color ANSI solo si la salida es una terminal."""
    if activo is None:
        activo = sys.stdout.isatty()
    return f"{color}{texto}{RESET}" if activo and color else texto


def resaltar_proyecto(linea, p, color=""):
    """Pone en negrita y color el nombre del proyecto (columna fija de 26)."""
    campo = f"{p.nombre:<26}"
    return linea.replace(campo, colorido(campo, NEGRITA + color), 1)


def resaltar_pendiente(linea, pend):
    """Marca en rojo el recuento cuando hay pendientes."""
    marca = f"{str(pend):>4} pend."
    if not isinstance(pend, int) or pend <= 0 or marca not in linea:
        return linea
    return linea.replace(marca, colorido(marca, NEGRITA + ROJO), 1)


def resumen_faltantes(p, casos):
    """Etiqueta agregada de lo que falta en los pendientes ya calculados."""
    if not casos:
        return ""
    cuenta = {}
    for caso in casos:
        for item in falta_caso(p, caso):
            cuenta[item] = cuenta.get(item, 0) + 1
    if not cuenta:
        return ""
    orden = [i for i in ("es", "en", "pt", "volumen") if i in cuenta]
    orden += [i for i in cuenta if i not in orden]
    return "falta: " + ", ".join(f"{i}x{cuenta[i]}" if cuenta[i] > 1 else i for i in orden)


def pendientes_y_error(p, modo=None):
    """Lista de pendientes y, si algo falla, el mensaje en vez de la excepcion."""
    try:
        return pendientes(p, modo), ""
    except SystemExit:
        raise
    except Exception as exc:
        return [], f"error: {exc}"


def lineas_proyecto(i, p, modo, lista, error=""):
    """Filas del menu principal para un proyecto, sin pasar de ANCHO_MENU."""
    pend = error or len(lista)
    fila = f"  [{i}] {p.nombre:<26} {modo:<5} {str(pend):>4} pend."
    voces = f"voces: {resumen_voces(p)}"
    etiqueta = resumen_faltantes(p, lista) if modo == "3lang" and not error else ""
    compacta = etiqueta.replace(", ", ",")
    if not compacta:
        return [fila, f"       {voces}"]
    if len(fila) + 1 + len(compacta) <= ANCHO_MENU:
        return [f"{fila} {compacta}", f"       {voces}"]
    detalle = f"       {etiqueta}"
    if len(detalle) + 3 + len(voces) <= ANCHO_MENU:
        return [fila, f"{detalle} {bullet()} {voces}"]
    return [fila, detalle, f"       {voces}"]


def resumen_pendientes(p, modo=None):
    lista, error = pendientes_y_error(p, modo)
    return error or len(lista)


# ─── Servidor ────────────────────────────────────────────────────────────────


def health(timeout=2.0):
    try:
        peticion = urllib.request.Request(SERVER_URL + "/health", headers={"Connection": "close"})
        with urllib.request.urlopen(peticion, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def arrancar_servidor():
    if SIN_SERVIDOR:
        print("(--sin-servidor: no se arranca el servidor)")
        return health() is not None

    estado = health()
    if estado:
        print(f"Servidor VoxCPM ya corriendo: {estado.get('model_id')} (optimize={estado.get('optimize')})")
        return True

    if puerto_abierto(8765):
        print(
            "ERROR: el puerto 8765 esta ocupado por otro proceso (no responde /health como VoxCPM).\n"
            "Buscalo con: netstat -ano | findstr 8765"
        )
        return False

    launcher = SCRIPTS / "voxcpm_win_launcher.py"
    if not VENV_PY.is_file():
        print(f"ERROR: falta el venv: {VENV_PY}\nDesde VoxCPM-Personal: python -m venv venv_voxcpm")
        return False
    if not launcher.is_file():
        print(f"ERROR: falta {launcher}")
        return False

    print("Iniciando VoxCPM en ventana aparte (el modelo tarda en cargar)...")
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        subprocess.Popen(
            [str(VENV_PY), str(launcher)],
            cwd=str(ROOT),
            creationflags=flags,
        )
    except OSError as exc:
        print(f"ERROR al arrancar el servidor: {exc}")
        return False

    for intento in range(1, 241):  # 12 minutos: torch.compile puede tardar
        if health():
            print("Servidor listo.")
            return True
        if intento % 10 == 0:
            print(f"  cargando... ({intento * 3}s)")
        time.sleep(3)
    print("ERROR: VoxCPM no respondio en 12 minutos. Revisa la ventana del servidor.")
    return False


# ─── Ejecucion con log ───────────────────────────────────────────────────────


def ejecutar(p, accion, cmd, cwd=None):
    LOGS.mkdir(parents=True, exist_ok=True)
    log_path = LOGS / f"{time.strftime('%Y%m%d-%H%M%S')}-{p.alias}-{accion}.log"
    entorno = os.environ.copy()
    entorno["PYTHONIOENCODING"] = "utf-8"
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    print(f"  log: {log_path}\n")
    codigo = 1
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(str(c) for c in cmd)}\n\n")
        try:
            proceso = subprocess.Popen(
                [str(c) for c in cmd],
                cwd=str(cwd or ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=entorno,
            )
        except OSError as exc:
            print(f"ERROR al ejecutar: {exc}")
            log.write(f"ERROR al ejecutar: {exc}\n")
            return 1
        try:
            for linea in proceso.stdout:
                print(linea, end="")
                log.write(linea)
                log.flush()
        except KeyboardInterrupt:
            print("\nInterrumpido por el usuario.")
            proceso.terminate()
            try:
                proceso.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proceso.kill()
            codigo = 130
        else:
            codigo = proceso.wait()
        log.write(f"\n[exit {codigo}]\n")
    print(f"\n[exit {codigo}]")
    return codigo


def cmd_3lang(p, extra, voces=None):
    cmd = [python_exe(), str(SCRIPTS / "voz_3idiomas.py"), "--vault", str(p.vault), "--out-dir", p.out_dir]
    if p.voices_json.is_file():
        cmd += ["--voices-json", str(p.voices_json)]
    for idioma, voz in (voces or {}).items():
        cmd += [f"--voz-{idioma}", voz]
    return cmd + list(p.args) + list(extra)


def cmd_empalme(p, extra):
    cmd = [python_exe(), str(EMPALME_SCRIPT), "--vault", str(p.vault)]
    return cmd + list(p.args) + list(extra)


def empalmar_interactivo(p):
    """Empalma CTAs nuevos en un audio ya narrado (no gasta creditos)."""
    if not EMPALME_SCRIPT.is_file():
        print(f"ERROR: falta {EMPALME_SCRIPT}")
        return
    print(f"\nEmpalmar CTAs en un audio de {p.out_dir} (no regenera voz).")
    stem = preguntar("  Carpeta a modificar (Enter = cancelar): ")
    if not stem:
        return
    clip_a = preguntar("  Carpeta del clip A (va tras el hook): ")
    clip_b = preguntar("  Carpeta del clip B (sustituye al CTA viejo): ")
    hook = preguntar("  Frase final del hook: ")
    if not (clip_a and clip_b and hook):
        print("Cancelado: faltan datos.")
        return
    codigo = ejecutar(
        p,
        f"cta-{stem}",
        cmd_empalme(p, ["--stem", stem, "--clip-a", clip_a, "--clip-b", clip_b, "--hook", hook]),
    )
    print(f"Empalme terminado (exit {codigo}).")


def cmd_watch(p, extra):
    cmd = [
        python_exe(),
        str(ROOT / "app" / "watch_generate.py"),
        "--watch-dir",
        str(p.generate),
        "--voice",
        voz_default(p),
    ]
    if p.voices_json.is_file():
        cmd += ["--voices-json", str(p.voices_json)]
    return cmd + list(p.args) + list(extra)


# ─── Seleccion de casos / txt ────────────────────────────────────────────────


def falta_caso(p, caso):
    """Que le falta al caso, segun el mismo criterio que cuenta los pendientes."""
    v3 = cargar_voces_en_motor(p)
    return v3.falta_para(v3.carpeta_salida(p.generate, caso), v3.idiomas_del_caso(caso))


def elegir_caso(p, solo_pendientes=False):
    casos = pendientes_3lang(p) if solo_pendientes else casos_3lang(p)
    if not casos:
        print("No hay casos pendientes." if solo_pendientes else "No hay casos con 2_guion.md.")
        return None
    print("\nCasos:")
    for i, caso in enumerate(casos, 1):
        detalle = ""
        if solo_pendientes:
            detalle = "   falta: " + ", ".join(falta_caso(p, caso))
        print(f"  [{i}] {caso.name}{detalle}")
    sel = preguntar("\nNumero (Enter = cancelar): ")
    if not sel:
        return None
    try:
        indice = int(sel) - 1
    except ValueError:
        print("Seleccion invalida.")
        return None
    if 0 <= indice < len(casos):
        return casos[indice]
    print("Seleccion invalida.")
    return None


def elegir_txt(p, solo_pendientes=False):
    txts = pendientes_watch(p) if solo_pendientes else sorted(p.generate.glob("*.txt"))
    if not txts:
        print("No hay .txt pendientes." if solo_pendientes else "No hay .txt en Generate.")
        return None
    print("\nGuiones:")
    for i, txt in enumerate(txts, 1):
        print(f"  [{i}] {txt.name}")
    sel = preguntar("\nNumero (Enter = cancelar): ")
    if not sel:
        return None
    try:
        indice = int(sel) - 1
    except ValueError:
        print("Seleccion invalida.")
        return None
    if 0 <= indice < len(txts):
        return txts[indice]
    print("Seleccion invalida.")
    return None


def abrir_carpeta(ruta):
    if not ruta.is_dir():
        print(f"No existe: {ruta}")
        return
    try:
        os.startfile(str(ruta))  # type: ignore[attr-defined]
    except Exception:
        print(f"Carpeta: {ruta}")


# ─── Pregunta de encadenado ──────────────────────────────────────────────────


def preguntar_otro(proyectos, actual=None):
    print("\n¿Otro proyecto?")
    opciones = ordenados(proyectos)
    for i, p in enumerate(opciones, 1):
        marca = " (actual)" if actual and p.alias == actual else ""
        print(f"  [{i}] {p.nombre}{marca}")
    print("  [Enter] quedarse / volver al menu")
    sel = preguntar("> ")
    if not sel:
        return None
    return buscar_proyecto(proyectos, sel)


def tras_accion(p, proyectos):
    """Pregunta si encadenar otro proyecto despues de una corrida."""
    otro = preguntar_otro(proyectos, p.alias)
    return otro.alias if otro and otro.alias != p.alias else None


# ─── Submenu de proyecto ─────────────────────────────────────────────────────


def menu_proyecto(p, proyectos):
    modo = detectar_modo(p)
    while True:
        print("\n" + "=" * ANCHO_MENU)
        print(f"  {colorido(p.nombre, NEGRITA + color_proyecto(p))}  [{modo}]")
        print(f"  vault: {p.vault}")
        print(f"  voces: {resumen_voces(p)}")
        lista, error = pendientes_y_error(p, modo)
        pend = error or len(lista)
        print(f"  pendientes: {colorido(str(pend), ROJO if isinstance(pend, int) and pend else '')}")
        for caso in lista[:5]:
            detalle = ""
            if modo == "3lang":
                detalle = "   falta: " + ", ".join(falta_caso(p, caso))
            print(f"    {bullet()} {Path(caso).stem}{detalle}")
        if len(lista) > 5:
            print(f"    {bullet()} y {len(lista) - 5} mas")
        estado = health()
        print(f"  servidor: {'listo en 8765' if estado else 'parado'}")
        print("=" * ANCHO_MENU)
        if modo == "3lang":
            print("  [1] Generar pendientes (--todos)")
            print("  [2] Generar un caso concreto")
            print("  [3] Verificar duraciones de todos")
            print("  [4] Solo alinear un caso")
            print("  [5] Normalizar volumen de un caso")
            print("  [6] Dry-run de pendientes")
        else:
            print("  [1] Vigilar carpeta (watch)")
            print("  [2] Procesar pendientes una vez")
            print("  [3] Procesar un .txt concreto")
        print("  [v] Ver voces por canal")
        print("  [s] Arrancar / comprobar servidor")
        print("  [r] Recargar pendientes")
        print("  [c] Empalmar CTAs en un audio ya narrado")
        print("  [o] Abrir carpeta Generate")
        print("  [0] Volver")
        sel = preguntar("\nOpcion: ").lower()

        if sel == "0":
            return None
        if sel == "v":
            tabla_voces(p)
            continue
        if sel == "s":
            arrancar_servidor()
            continue
        if sel == "r":
            print(f"Pendientes: {resumen_pendientes(p, modo)}")
            continue
        if sel == "o":
            abrir_carpeta(p.generate)
            continue

        if sel == "c":
            empalmar_interactivo(p)
            continue

        if modo == "3lang":
            if sel == "1":
                if arrancar_servidor():
                    ejecutar(p, "3lang-todos", cmd_3lang(p, ["--todos"]))
                    destino = tras_accion(p, proyectos)
                    if destino:
                        return destino
            elif sel == "2":
                caso = elegir_caso(p, solo_pendientes=True)
                if caso and arrancar_servidor():
                    ejecutar(p, f"caso-{caso.name}", cmd_3lang(p, ["--caso", str(caso)]))
                    destino = tras_accion(p, proyectos)
                    if destino:
                        return destino
            elif sel == "3":
                ejecutar(p, "verificar", cmd_3lang(p, ["--todos", "--verificar"]))
                destino = tras_accion(p, proyectos)
                if destino:
                    return destino
            elif sel == "4":
                caso = elegir_caso(p)
                if caso:
                    ejecutar(p, f"alinear-{caso.name}", cmd_3lang(p, ["--caso", str(caso), "--solo-alinear"]))
                    destino = tras_accion(p, proyectos)
                    if destino:
                        return destino
            elif sel == "5":
                caso = elegir_caso(p)
                if caso:
                    ejecutar(
                        p,
                        f"normalizar-{caso.name}",
                        cmd_3lang(p, ["--caso", str(caso), "--solo-normalizar"]),
                    )
                    destino = tras_accion(p, proyectos)
                    if destino:
                        return destino
            elif sel == "6":
                ejecutar(p, "dry-run", cmd_3lang(p, ["--todos", "--dry-run"]))
                destino = tras_accion(p, proyectos)
                if destino:
                    return destino
            else:
                print("Opcion invalida.")
            continue

        # watch
        if sel == "1":
            if puerto_abierto(WATCH_LOCK_PORT):
                print(
                    f"Ya hay un watch_generate corriendo (puerto {WATCH_LOCK_PORT}). "
                    "Cierra esa ventana antes de abrir otro."
                )
                continue
            if arrancar_servidor():
                codigo = ejecutar(p, "watch", cmd_watch(p, []))
                if codigo not in (0, 130):
                    print(f"watch_generate termino con codigo {codigo}.")
                destino = tras_accion(p, proyectos)
                if destino:
                    return destino
        elif sel == "2":
            if arrancar_servidor():
                ejecutar(p, "watch-once", cmd_watch(p, ["--once"]))
                destino = tras_accion(p, proyectos)
                if destino:
                    return destino
        elif sel == "3":
            txt = elegir_txt(p)
            if txt and arrancar_servidor():
                ejecutar(p, f"txt-{txt.stem}", cmd_watch(p, ["--txt", str(txt)]))
                destino = tras_accion(p, proyectos)
                if destino:
                    return destino
        else:
            print("Opcion invalida.")


# ─── Menu principal ──────────────────────────────────────────────────────────


ACCIONES = ("servidor", "3lang", "3lang-todos", "watch", "watch-once", "pt")


def correr_accion(p, accion, extra):
    """Accion directa sin menu; la usan los shims de los vaults."""
    if accion == "servidor":
        return 0 if arrancar_servidor() else 1
    if accion == "3lang":
        if not arrancar_servidor():
            return 1
        return ejecutar(p, "3lang", cmd_3lang(p, extra))
    if accion == "3lang-todos":
        if not arrancar_servidor():
            return 1
        return ejecutar(p, "3lang-todos", cmd_3lang(p, ["--todos"] + list(extra)))
    if accion in ("watch", "watch-once"):
        if not arrancar_servidor():
            return 1
        args = list(extra) if accion == "watch" else ["--once"] + list(extra)
        return ejecutar(p, accion, cmd_watch(p, args))
    if accion == "pt":
        return ejecutar(p, "pt", cmd_pt(p, list(extra)))
    print(f"Accion desconocida: {accion}")
    return 2


def menu_principal(datos, proyectos):
    actual = datos.get("default")
    while True:
        print("\n" + "=" * ANCHO_MENU)
        print(f"  VoxCPM {bullet()} lanzador global")
        estado = health()
        print(f"  Servidor: {'listo en 8765' if estado else 'parado'}   Registro: {REGISTRY.name}")
        print("=" * ANCHO_MENU)
        opciones = ordenados(proyectos)
        for i, p in enumerate(opciones, 1):
            modo = detectar_modo(p)
            lista, error = pendientes_y_error(p, modo)
            pend = error or len(lista)
            color = color_proyecto(p)
            for linea in lineas_proyecto(i, p, modo, lista, error):
                print(resaltar_pendiente(resaltar_proyecto(linea, p, color), pend))
        print("\n  [a] Automático (todos los pendientes, en orden)")
        print("  [e] Escanear vaults nuevos")
        print("  [d] Doctor (validar todo)")
        print("  [r] Recargar registro")
        print("  [0] Salir")
        sel = preguntar("\nProyecto: ").lower()

        if sel == "0":
            return
        if sel == "r":
            datos, proyectos = cargar_registro(crear=True)
            continue
        if sel == "d":
            doctor(proyectos)
            continue
        if sel == "e":
            datos, proyectos = escanear(datos, proyectos)
            continue
        if sel == "a":
            modo_auto(proyectos)
            continue
        if not sel and actual and actual in proyectos:
            p = proyectos[actual]
        else:
            p = buscar_proyecto(proyectos, sel)
        if p is None:
            print("Proyecto no encontrado.")
            continue
        destino = menu_proyecto(p, proyectos)
        while destino:
            p = proyectos.get(destino)
            if p is None:
                break
            destino = menu_proyecto(p, proyectos)


# ─── Modo automatico ─────────────────────────────────────────────────────────


def modo_auto(proyectos):
    print("\nBuscando pendientes en todos los proyectos...")
    total = 0
    plan = []
    for p in ordenados(proyectos):
        modo = detectar_modo(p)
        try:
            pend = pendientes(p, modo)
        except Exception as exc:
            print(f"  {p.nombre}: error calculando pendientes ({exc})")
            continue
        print(f"  {p.nombre}: {len(pend)} pendientes [{modo}]")
        if pend:
            plan.append((p, modo, pend))
            total += len(pend)

    if not total:
        print("\nNo quedan pendientes en ningun proyecto.")
        otro = preguntar_otro(proyectos)
        if otro:
            menu_proyecto(otro, proyectos)
        return

    if not arrancar_servidor():
        return

    for p, modo, pend in plan:
        print(f"\n### {p.nombre}: {len(pend)} pendientes ###")
        if modo == "3lang":
            codigo = ejecutar(p, "auto-3lang", cmd_3lang(p, ["--todos"]))
        elif modo == "watch":
            codigo = ejecutar(p, "auto-watch", cmd_watch(p, ["--once"]))
        else:
            print("  modo no gestionado; se omite.")
            continue
        print(f"  resultado: exit {codigo}")

    print("\nAuto terminado.")
    otro = preguntar_otro(proyectos)
    if otro:
        menu_proyecto(otro, proyectos)


# ─── Doctor ──────────────────────────────────────────────────────────────────


def doctor(proyectos):
    fallos = 0

    def ok(cond, texto, detalle=""):
        nonlocal fallos
        print(f"  [{'PASS' if cond else 'FAIL'}] {texto}" + (f" — {detalle}" if detalle else ""))
        if not cond:
            fallos += 1

    print(f"\nDoctor {bullet()} {ROOT}")
    ok(REGISTRY.is_file(), "registro de proyectos", str(REGISTRY))
    ok(VENV_PY.is_file(), "python del venv", str(VENV_PY))
    ok((SCRIPTS / "voz_3idiomas.py").is_file(), "generador 3lang", "scripts/voz_3idiomas.py")
    ok((SCRIPTS / "voxcpm_win_launcher.py").is_file(), "lanzador Windows", "scripts/voxcpm_win_launcher.py")
    ok((APP / "watch_generate.py").is_file(), "watch_generate", "app/watch_generate.py")
    ok(PT_SCRIPT.is_file(), "generador pt", "scripts/generar_voz_pt.py")
    ok(EMPALME_SCRIPT.is_file(), "empalme de CTAs", "scripts/empalmar_cta.py")
    estado = health()
    print(
        f"  [{'PASS' if estado else 'INFO'}] servidor 8765 "
        f"— {(estado or {}).get('model_id', 'parado (se arranca al generar)')}"
    )

    for p in ordenados(proyectos):
        print(f"\n  {p.nombre} ({p.alias})")
        ok(p.vault.is_dir(), "vault", str(p.vault))
        ok(p.voices_json.is_file(), "voices.json", str(p.voices_json))
        if p.voices_json.is_file():
            try:
                leer_json(p.voices_json)
                ok(True, "voices.json valido")
            except (OSError, ValueError) as exc:
                ok(False, "voices.json valido", str(exc))
        modo = detectar_modo(p)
        print(f"       modo detectado: {modo}")
        try:
            cfg = config_voces(p)
            revisadas = set()
            entradas = [cfg.get("default")] + list((cfg.get("canales") or {}).values())
            for entrada in entradas:
                if isinstance(entrada, str):
                    pares = [("es", entrada)]
                elif isinstance(entrada, dict):
                    pares = [(i, entrada[i]) for i in IDIOMAS_ORDEN if entrada.get(i)]
                else:
                    continue
                for idioma, nombre in pares:
                    if (idioma, nombre) in revisadas:
                        continue
                    revisadas.add((idioma, nombre))
                    py_ok, wav_ok, py, wav = validar_voz(idioma, nombre)
                    ok(py_ok and wav_ok, f"voz {idioma}={nombre}", str(wav))
        except SystemExit as exc:
            ok(False, "voices.json", str(exc))
        if modo == "3lang":
            try:
                cargar_voces_en_motor(p)
                n = len(pendientes_3lang(p))
                print(f"       casos pendientes: {n}")
            except Exception as exc:
                ok(False, "pendientes 3lang", str(exc))
        elif modo == "watch":
            print(f"       txt pendientes: {len(pendientes_watch(p))}")

    print(f"\nDoctor: {'TODO OK' if not fallos else f'{fallos} fallo(s)'}")
    return 0 if not fallos else 1


# ─── Escaneo de vaults ───────────────────────────────────────────────────────


def escanear(datos, proyectos):
    bases = []
    for p in proyectos.values():
        padre = p.vault.parent
        if padre not in bases:
            bases.append(padre)
    if not bases:
        bases = [Path("D:/Obsidian Vaults")]
    print("\nBuscando vaults con Sensible/rutas.json...")
    encontrados = []
    for base in bases:
        if not base.is_dir():
            continue
        for rutas in sorted(base.glob("*/Sensible/rutas.json")):
            vault = rutas.parent.parent
            if vault.resolve() in {p.vault.resolve() for p in proyectos.values()}:
                continue
            try:
                cfg = leer_json(rutas)
            except (OSError, ValueError):
                continue
            encontrados.append((vault, cfg.get("vault") or str(vault)))

    if not encontrados:
        print("No hay vaults nuevos.")
        return datos, proyectos

    for vault, declarado in encontrados:
        print(f"  {vault.name}  ->  {declarado}")
    if preguntar("\n¿Agregar todos al registro? [s/N] ").lower() != "s":
        return datos, proyectos

    for vault, declarado in encontrados:
        alias = slug(vault.name)
        base_alias = alias
        n = 2
        while alias in (datos.setdefault("proyectos", {})):
            alias = f"{base_alias}-{n}"
            n += 1
        datos["proyectos"][alias] = {
            "nombre": vault.name,
            "vault": str(vault).replace("\\", "/"),
            "modo": "auto",
            "orden": 100,
        }
        print(f"  + {alias}: {vault}")
    escribir_json(REGISTRY, datos)
    return cargar_registro()


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main():
    global SIN_SERVIDOR
    habilitar_ansi()
    argv = sys.argv[1:]
    extra = []
    if "--" in argv:
        corte = argv.index("--")
        argv, extra = argv[:corte], argv[corte + 1 :]

    parser = argparse.ArgumentParser(description="Lanzador global de VoxCPM.")
    parser.add_argument("proyecto", nargs="?", help="alias del proyecto (si falta, menu)")
    parser.add_argument("--vault", default=None, help="ruta del vault para un proyecto ad-hoc")
    parser.add_argument("--modo", choices=["auto", "3lang", "watch"], default=None)
    parser.add_argument("--auto", action="store_true", help="corre los pendientes de todos los proyectos")
    parser.add_argument("--doctor", action="store_true", help="valida rutas, voces, venv y servidor")
    parser.add_argument("--escanear", action="store_true", help="agrega vaults nuevos al registro")
    parser.add_argument("--sin-servidor", action="store_true", help="no arranca el servidor")
    parser.add_argument("--empalmar", action="store_true",
                        help="empalma CTAs con los argumentos pasados tras --")
    parser.add_argument("--pt", action="store_true",
                        help="genera solo la voz portuguesa de un .txt (args tras --)")
    parser.add_argument("--accion", choices=ACCIONES, default=None,
                        help="ejecuta una accion concreta sin pasar por el menu "
                             "(lo usan los shims de Scripts/voice de los vaults)")
    parser.add_argument("--forzar", action="store_true", help="crea proyectos.json desde el ejemplo si falta")
    args = parser.parse_args(argv)
    SIN_SERVIDOR = args.sin_servidor

    datos, proyectos = cargar_registro(crear=args.forzar or args.escanear)

    if args.accion:
        p = None
        if args.vault:
            vault = Path(args.vault).expanduser().resolve()
            p = next((q for q in proyectos.values() if q.vault.resolve() == vault), None)
            if p is None:
                p = Proyecto(alias=slug(vault.name), nombre=vault.name, vault=vault)
        elif args.proyecto:
            p = buscar_proyecto(proyectos, args.proyecto)
        if p is None:
            raise SystemExit("--accion necesita un proyecto conocido o --vault.")
        if args.modo:
            p.modo = args.modo
        if extra:
            p.args = list(p.args) + list(extra)
        raise SystemExit(correr_accion(p, args.accion, []))

    if args.empalmar or args.pt:
        if args.vault:
            vault = Path(args.vault).expanduser().resolve()
            p = next((q for q in proyectos.values() if q.vault.resolve() == vault), None)
            if p is None:
                p = Proyecto(alias=slug(vault.name), nombre=vault.name, vault=vault)
        elif args.proyecto:
            p = buscar_proyecto(proyectos, args.proyecto)
        else:
            raise SystemExit("--empalmar necesita un proyecto (alias) o --vault.")
        if p is None:
            raise SystemExit(f"No existe el proyecto '{args.proyecto}'.")
        if args.pt:
            raise SystemExit(ejecutar(p, "pt", cmd_pt(p, extra)))
        raise SystemExit(ejecutar(p, "empalmar", cmd_empalme(p, extra)))

    if args.doctor:
        raise SystemExit(doctor(proyectos))
    if args.escanear:
        datos, proyectos = escanear(datos, proyectos)

    if args.vault:
        vault = Path(args.vault).expanduser().resolve()
        p = next((q for q in proyectos.values() if q.vault.resolve() == vault), None)
        if p is None:
            alias = slug(vault.name)
            p = Proyecto(alias=alias, nombre=vault.name, vault=vault)
        if args.modo:
            p.modo = args.modo
        if extra:
            p.args = list(p.args) + list(extra)
        destino = menu_proyecto(p, proyectos)
        while destino:
            p = proyectos[destino]
            destino = menu_proyecto(p, proyectos)
        return

    if args.proyecto:
        p = buscar_proyecto(proyectos, args.proyecto)
        if p is None:
            raise SystemExit(f"No existe el proyecto '{args.proyecto}'. Alias: {', '.join(sorted(proyectos))}")
        if args.modo:
            p.modo = args.modo
        if extra:
            p.args = list(p.args) + list(extra)
        destino = menu_proyecto(p, proyectos)
        while destino:
            p = proyectos[destino]
            destino = menu_proyecto(p, proyectos)
        return

    if args.auto:
        modo_auto(proyectos)
        return

    menu_principal(datos, proyectos)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
