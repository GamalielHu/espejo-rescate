"""
main.py
=======
Punto de entrada de Espejo Rescate.

  python main.py                 -> interfaz gráfica (por defecto)
  python main.py dispositivos    -> lista teléfonos y su estado
  python main.py diagnostico     -> rutas y versiones de adb/scrcpy
  python main.py espejo [-s SERIAL] [--calidad N]  -> espejo sin interfaz
  python main.py pin [-s SERIAL] -> escribe TU PIN (lo pide oculto)

Solo funciona con teléfonos que ya autorizaron la depuración USB de esta PC.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time

from adb_manager import AdbError, AdbManager, find_scrcpy


def _elegir(adb: AdbManager, serial: str | None) -> str:
    devs = adb.list_devices()
    if serial:
        dev = next((d for d in devs if d.serial == serial), None)
    else:
        dev = next((d for d in devs if d.authorized), devs[0] if devs else None)
    if not dev:
        sys.exit("✖ No hay teléfono conectado.")
    if not dev.authorized:
        sys.exit(f"✖ {dev.label}: {dev.state_text}. Acepta la depuración USB "
                 "en el teléfono (monitor HDMI o ratón OTG).")
    return dev.serial


def cmd_dispositivos(adb: AdbManager, _a) -> None:
    devs = adb.list_devices()
    if not devs:
        print("No hay teléfonos conectados.")
    for d in devs:
        print(f"{d.serial:<24} {d.state_text:<22} {d.model}")


def cmd_diagnostico(adb: AdbManager, _a) -> None:
    from mirror_engine import scrcpy_version
    print(f"adb    : {adb.adb}")
    ver = str(adb.run(["version"], quiet=True)).splitlines()
    print("         " + (ver[0] if ver else "versión desconocida"))
    sc = find_scrcpy()
    print(f"scrcpy : {sc or 'NO ENCONTRADO (python descargar_binarios.py)'}")
    if sc:
        print("         versión " + ".".join(map(str, scrcpy_version(sc))))
    cmd_dispositivos(adb, _a)


def cmd_espejo(adb: AdbManager, a) -> None:
    from mirror_engine import CALIDADES, MirrorConfig, ScrcpyMirror
    serial = _elegir(adb, a.serial)
    fin = {}
    m = ScrcpyMirror(adb, on_log=print,
                     on_exit=lambda c, h: fin.update(code=c, hint=h))
    if not m.available:
        sys.exit("✖ scrcpy no encontrado. Ejecuta: python descargar_binarios.py")
    calidad = list(CALIDADES)[a.calidad]
    adb.wake(serial)
    m.start(MirrorConfig(serial=serial, calidad=calidad,
                         apagar_pantalla=not a.no_apagar))
    try:
        while "code" not in fin:
            time.sleep(0.3)
    except KeyboardInterrupt:
        m.stop()
    if fin.get("hint"):
        print("✖ " + fin["hint"])
    sys.exit(fin.get("code", 0))


def cmd_pin(adb: AdbManager, a) -> None:
    serial = _elegir(adb, a.serial)
    pin = getpass.getpass("PIN/contraseña de tu teléfono (oculto): ")
    if pin:
        adb.enter_pin(pin, serial)
        print("✔ Enviado.")


def main() -> None:
    # Consola de Windows: permitir ✔/✖ y acentos.
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if len(sys.argv) == 1:
        from gui import run
        run()
        return
    ap = argparse.ArgumentParser(prog="EspejoRescate",
                                 description="Espejo y control de un Android "
                                             "con la pantalla rota (vía scrcpy).")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="Interfaz gráfica")
    sub.add_parser("dispositivos", help="Listar teléfonos")
    sub.add_parser("diagnostico", help="Comprobar adb/scrcpy")
    p = sub.add_parser("espejo", help="Abrir el espejo sin interfaz")
    p.add_argument("-s", "--serial")
    p.add_argument("--calidad", type=int, default=1, choices=range(4),
                   help="0 rápida, 1 equilibrada, 2 máxima, 3 cable lento")
    p.add_argument("--no-apagar", action="store_true",
                   help="No apagar el panel físico del teléfono")
    p = sub.add_parser("pin", help="Escribir tu PIN en el teléfono")
    p.add_argument("-s", "--serial")
    a = ap.parse_args()

    if a.cmd in (None, "gui"):
        from gui import run
        run()
        return
    try:
        adb = AdbManager()
        adb.start_server()
        {"dispositivos": cmd_dispositivos, "diagnostico": cmd_diagnostico,
         "espejo": cmd_espejo, "pin": cmd_pin}[a.cmd](adb, a)
    except AdbError as exc:
        sys.exit(f"✖ {exc}")


if __name__ == "__main__":
    main()
