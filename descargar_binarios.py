"""
descargar_binarios.py
=====================
Descarga los binarios que la app necesita y los deja en ./bin:

  Windows -> el zip oficial de scrcpy (win64), que ya trae adb.exe,
             AdbWinApi.dll, scrcpy-server y las DLL de video (SDL2/FFmpeg).
  Linux / macOS -> no descarga nada: usa el gestor de paquetes
             (apt install scrcpy adb  /  brew install scrcpy android-platform-tools).

Uso:  python descargar_binarios.py [--version v3.3.1] [--forzar]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import urllib.request
import zipfile

REPO = "Genymobile/scrcpy"
VERSION_RESPALDO = "v3.3.1"      # se usa si la API de GitHub no responde
BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "espejo-rescate"})
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def ultima_version() -> str:
    try:
        data = json.loads(_get(f"https://api.github.com/repos/{REPO}/releases/latest"))
        return data["tag_name"]
    except Exception as exc:  # noqa: BLE001
        print(f"  ! No se pudo consultar la última versión ({exc}); "
              f"uso {VERSION_RESPALDO}.")
        return VERSION_RESPALDO


def descargar_windows(version: str, forzar: bool) -> None:
    destino_exe = os.path.join(BIN, "scrcpy.exe")
    if os.path.isfile(destino_exe) and not forzar:
        print(f"Ya existe {destino_exe} (usa --forzar para reemplazar).")
        return
    nombre = f"scrcpy-win64-{version}.zip"
    url = f"https://github.com/{REPO}/releases/download/{version}/{nombre}"
    print(f"Descargando {url} …")
    data = _get(url)
    print(f"  {len(data) / 1e6:.1f} MB descargados. Extrayendo en {BIN}")
    os.makedirs(BIN, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            # Quitar la carpeta raíz "scrcpy-win64-vX.Y/" del zip.
            partes = info.filename.split("/", 1)
            rel = partes[1] if len(partes) == 2 else partes[0]
            if not rel or rel.endswith((".bat", ".vbs")):
                continue
            ruta = os.path.join(BIN, rel)
            os.makedirs(os.path.dirname(ruta), exist_ok=True)
            with zf.open(info) as src, open(ruta, "wb") as dst:
                shutil.copyfileobj(src, dst)
    for req in ("scrcpy.exe", "adb.exe", "scrcpy-server"):
        if not os.path.isfile(os.path.join(BIN, req)):
            sys.exit(f"✖ Falta {req} tras extraer el zip.")
    print("✔ scrcpy + adb listos en bin/")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", help="Etiqueta de scrcpy, p. ej. v3.3.1")
    ap.add_argument("--forzar", action="store_true", help="Reemplazar bin/")
    ap.add_argument("--plataforma", choices=["windows", "auto"], default="auto",
                    help="'windows' fuerza la descarga win64 (para compilar en CI)")
    args = ap.parse_args()

    if args.plataforma == "windows" or os.name == "nt":
        descargar_windows(args.version or ultima_version(), args.forzar)
    elif sys.platform == "darwin":
        print("macOS: brew install scrcpy android-platform-tools")
    else:
        print("Linux: sudo apt install scrcpy adb   (o: snap install scrcpy)")


if __name__ == "__main__":
    main()
