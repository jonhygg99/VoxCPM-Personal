"""
Arranca voxcpm_server.py en Windows con dos parches necesarios en esta maquina:

1. huggingface_hub._create_symlink solo catchea PermissionError, pero sin
   Developer Mode os.symlink lanza OSError(WinError 1314). Se intercepta y se
   hace fallback a copia/move.
2. MIMALLOC_PURGE_DELAY=0 antes de cargar torch (c10.dll): sin esto quedan
   ~11 GB de RAM comprometida tras cargar el modelo.

Es el unico wrapper: los vaults ya no necesitan su propia copia.
"""

import os

# Debe ir ANTES de torch: mimalloc lee la variable al cargar c10.dll.
os.environ.setdefault("MIMALLOC_PURGE_DELAY", "0")

import errno
import pathlib
import shutil
import sys
from pathlib import Path

import huggingface_hub.file_download as _hf_fd

_original_create_symlink = _hf_fd._create_symlink


def _patched_create_symlink(src, dst, new_blob=False):
    try:
        return _original_create_symlink(src, dst, new_blob)
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        if exc.errno != errno.EACCES and winerror != 1314:
            raise
        abs_src = pathlib.Path(src).expanduser().resolve()
        abs_dst = pathlib.Path(dst)
        abs_dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            abs_dst.unlink(missing_ok=True)
        except OSError:
            pass
        if new_blob:
            shutil.move(str(abs_src), str(abs_dst))
        else:
            shutil.copy2(str(abs_src), str(abs_dst))


_hf_fd._create_symlink = _patched_create_symlink


PROJECT_DIR = Path(__file__).resolve().parents[1]
SERVER_SCRIPT = PROJECT_DIR / "voxcpm_server.py"
if not SERVER_SCRIPT.is_file():
    raise SystemExit(f"voxcpm_win_launcher: no existe {SERVER_SCRIPT}.")

extra = list(sys.argv[1:])
if "--optimize" not in extra and "--no-optimize" not in extra:
    # torch.compile + CUDA graphs solo tiene sentido con GPU.
    try:
        import torch

        has_cuda = torch.cuda.is_available()
    except Exception:
        has_cuda = False
    if has_cuda:
        extra.append("--optimize")

sys.argv = [str(SERVER_SCRIPT)] + extra

# exec con __file__ del server para que su PROJECT_DIR y sys.path resuelvan bien.
_server_globals = {
    "__name__": "__main__",
    "__file__": str(SERVER_SCRIPT),
}
exec(compile(SERVER_SCRIPT.read_text(encoding="utf-8"), str(SERVER_SCRIPT), "exec"), _server_globals)
