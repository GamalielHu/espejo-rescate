"""
adb_manager.py
==============
Capa sobre `adb`: localiza los binarios (incluidos los empaquetados en
`bin/`), detecta equipos, vigila conexiones/desconexiones y ofrece el control
"a ciegas" por comandos (teclas, toques, texto, captura de pantalla).

Alcance: SOLO opera sobre equipos en estado `device`, es decir, telefonos que
ya autorizaron la depuracion USB de esta PC (el dueño acepto la clave RSA en
el propio telefono). No hay ningun mecanismo para saltar el bloqueo de
pantalla, ni fastboot, ni modo EDL. `input_text` solo escribe lo que el dueño
teclea (por ejemplo, su propio PIN).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

# En Windows, evita que cada llamada a adb abra una consola negra.
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# --------------------------------------------------------------------------- #
# Excepciones
# --------------------------------------------------------------------------- #
class AdbError(RuntimeError):
    """Error generico al invocar adb."""


class AdbNotFound(AdbError):
    """No se encontro adb ni en bin/ ni en el PATH."""


# --------------------------------------------------------------------------- #
# Localizacion de binarios empaquetados
# --------------------------------------------------------------------------- #
def app_dir() -> str:
    """Carpeta de la aplicacion (junto al .py o al .exe)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def bin_dirs() -> list[str]:
    """
    Carpetas donde se buscan adb/scrcpy, en orden de prioridad:
      1. bin/ dentro del paquete PyInstaller (sys._MEIPASS)
      2. bin/ junto al ejecutable o al script
    Si no estan ahi, se recurre al PATH del sistema.
    """
    dirs = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(os.path.join(meipass, "bin"))
    dirs.append(os.path.join(app_dir(), "bin"))
    return [d for d in dirs if os.path.isdir(d)]


def find_binary(name: str) -> Optional[str]:
    """Busca `name` (adb, scrcpy) en bin/ y luego en el PATH."""
    exe = name + (".exe" if os.name == "nt" else "")
    for d in bin_dirs():
        cand = os.path.join(d, exe)
        if os.path.isfile(cand):
            return cand
        # Por si se descomprimio el zip de scrcpy con su subcarpeta.
        for sub in sorted(os.listdir(d)):
            cand = os.path.join(d, sub, exe)
            if os.path.isfile(cand):
                return cand
    return shutil.which(name)


def find_adb() -> str:
    path = find_binary("adb")
    if not path:
        raise AdbNotFound(
            "No se encontro 'adb'. Ejecuta 'python descargar_binarios.py' "
            "o instala Android Platform Tools."
        )
    return path


def find_scrcpy() -> Optional[str]:
    return find_binary("scrcpy")


# --------------------------------------------------------------------------- #
# Modelo de dispositivo
# --------------------------------------------------------------------------- #
ESTADOS = {
    "device": "Autorizado",
    "unauthorized": "Sin autorizar",
    "offline": "Desconectado (offline)",
    "recovery": "Modo recovery",
    "sideload": "Modo sideload",
    "no permissions": "Sin permisos USB",
}


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    model: str = ""
    product: str = ""

    @property
    def authorized(self) -> bool:
        return self.state == "device"

    @property
    def label(self) -> str:
        name = (self.model or self.product or "Android").replace("_", " ")
        return f"{name}  [{self.serial}]"

    @property
    def state_text(self) -> str:
        return ESTADOS.get(self.state, self.state)


# --------------------------------------------------------------------------- #
# Gestor adb
# --------------------------------------------------------------------------- #
class AdbManager:
    """Envoltorio sobre adb. Cada metodo de equipo recibe `serial`."""

    def __init__(self, adb_path: Optional[str] = None,
                 logger: Optional[Callable[[str], None]] = None) -> None:
        self.adb = adb_path or find_adb()
        self._log = logger or (lambda _m: None)

    # ----- ejecucion ------------------------------------------------------ #
    def _cmd(self, args: list[str], serial: Optional[str]) -> list[str]:
        cmd = [self.adb]
        if serial:
            cmd += ["-s", serial]
        return cmd + args

    def run(self, args: list[str], serial: Optional[str] = None,
            timeout: float = 20, binary: bool = False,
            quiet: bool = False) -> bytes | str:
        cmd = self._cmd(args, serial)
        if not quiet:
            self._log("$ adb " + " ".join(cmd[1:]))
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                  creationflags=_NO_WINDOW)
        except FileNotFoundError as exc:
            raise AdbNotFound(str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError("Tiempo agotado: adb " + " ".join(args)) from exc
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
            raise AdbError(err or f"adb devolvio {proc.returncode}")
        return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")

    def shell(self, command: str, serial: Optional[str] = None,
              timeout: float = 20, quiet: bool = False) -> str:
        return self.run(["shell", command], serial=serial,
                        timeout=timeout, quiet=quiet)  # type: ignore[return-value]

    # ----- servidor ------------------------------------------------------- #
    def start_server(self) -> None:
        try:
            self.run(["start-server"], timeout=30)
        except AdbError as exc:
            self._log(f"! No se pudo iniciar el servidor adb: {exc}")

    def kill_server(self) -> None:
        try:
            self.run(["kill-server"], timeout=10)
        except AdbError:
            pass

    # ----- deteccion ------------------------------------------------------ #
    def list_devices(self, quiet: bool = True) -> list[Device]:
        out = self.run(["devices", "-l"], quiet=quiet)
        devices: list[Device] = []
        for line in str(out).splitlines()[1:]:
            line = line.strip()
            if not line or line.startswith("*"):
                continue
            # "no permissions" trae espacios: se separa por la primera columna.
            m = re.match(r"(\S+)\s+(no permissions|\S+)(.*)", line)
            if not m:
                continue
            serial, state, rest = m.groups()
            props = dict(re.findall(r"(\w+):(\S+)", rest))
            devices.append(Device(serial, state, props.get("model", ""),
                                  props.get("product", "")))
        return devices

    # ----- informacion del equipo ---------------------------------------- #
    def get_prop(self, prop: str, serial: str) -> str:
        try:
            return self.shell(f"getprop {prop}", serial, quiet=True).strip()
        except AdbError:
            return ""

    def screen_size(self, serial: str) -> tuple[int, int]:
        """Resolucion fisica (ancho, alto) segun `wm size`."""
        out = self.shell("wm size", serial, quiet=True)
        # Si hay "Override size" se usa ese, que es el que recibe input.
        sizes = re.findall(r"(\d+)x(\d+)", out)
        if not sizes:
            raise AdbError("No se pudo leer la resolucion de pantalla.")
        w, h = sizes[-1]
        return int(w), int(h)

    def is_screen_on(self, serial: str) -> Optional[bool]:
        try:
            out = self.shell("dumpsys power", serial, quiet=True)
        except AdbError:
            return None
        m = re.search(r"mWakefulness=(\w+)", out)
        if m:
            return m.group(1) == "Awake"
        m = re.search(r"Display Power: state=(\w+)", out)
        return (m.group(1) == "ON") if m else None

    def is_locked(self, serial: str) -> Optional[bool]:
        """Indica si el keyguard (pantalla de bloqueo) esta visible."""
        try:
            out = self.shell("dumpsys window", serial, quiet=True)
        except AdbError:
            return None
        for pat in (r"mDreamingLockscreen=(\w+)",
                    r"isStatusBarKeyguard=(\w+)",
                    r"mShowingLockscreen=(\w+)"):
            m = re.search(pat, out)
            if m:
                return m.group(1) == "true"
        return None

    # ----- control a ciegas ---------------------------------------------- #
    def keyevent(self, keycode: str | int, serial: str) -> None:
        self.shell(f"input keyevent {keycode}", serial)

    def tap(self, x: int, y: int, serial: str) -> None:
        self.shell(f"input tap {int(x)} {int(y)}", serial, quiet=True)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, serial: str,
              ms: int = 250) -> None:
        self.shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {ms}",
                   serial, quiet=True)

    def input_text(self, text: str, serial: str, quiet: bool = True) -> None:
        """
        Escribe texto tal cual lo teclea el dueño. `input text` no admite
        espacios ni ciertos simbolos de shell: se escapan.
        """
        safe = ""
        for ch in text:
            if ch == " ":
                safe += "%s"
            elif ch.isalnum():
                safe += ch
            else:
                safe += "\\" + ch
        if safe:
            self.shell(f"input text {safe}", serial, quiet=quiet)

    def wake(self, serial: str) -> None:
        """Enciende la pantalla (KEYCODE_WAKEUP no la apaga si ya esta on)."""
        self.keyevent(224, serial)

    def swipe_up_unlock(self, serial: str) -> None:
        """Desliza de abajo hacia arriba para mostrar el teclado del PIN."""
        w, h = self.screen_size(serial)
        self.swipe(w // 2, int(h * 0.85), w // 2, int(h * 0.30), serial, 300)

    def enter_pin(self, pin: str, serial: str) -> None:
        """
        Secuencia completa para que el DUEÑO entre con su propio PIN:
        despertar -> deslizar -> escribir -> Enter. No prueba ni adivina PINs.
        """
        self.wake(serial)
        time.sleep(0.6)
        self.swipe_up_unlock(serial)
        time.sleep(0.8)
        self.input_text(pin, serial)
        time.sleep(0.2)
        self.keyevent(66, serial)  # KEYCODE_ENTER

    def screencap_png(self, serial: str, timeout: float = 10) -> bytes:
        """Captura la pantalla actual como PNG (aunque el panel este muerto)."""
        data = self.run(["exec-out", "screencap", "-p"], serial=serial,
                        timeout=timeout, binary=True, quiet=True)
        assert isinstance(data, bytes)
        if not data.startswith(b"\x89PNG"):
            raise AdbError("La captura no devolvio una imagen PNG valida.")
        return data


# --------------------------------------------------------------------------- #
# Vigilante de conexiones
# --------------------------------------------------------------------------- #
class DeviceWatcher(threading.Thread):
    """
    Hilo que sondea `adb devices` cada `interval` segundos y llama a
    `on_change(lista)` solo cuando la lista o algun estado cambia. Asi la GUI
    se entera al instante de que se conecto, se autorizo o se desconecto un
    equipo, sin bloquear la interfaz.
    """

    def __init__(self, adb: AdbManager,
                 on_change: Callable[[list[Device]], None],
                 on_error: Optional[Callable[[str], None]] = None,
                 interval: float = 1.0) -> None:
        super().__init__(daemon=True, name="DeviceWatcher")
        self.adb = adb
        self.on_change = on_change
        self.on_error = on_error or (lambda _m: None)
        self.interval = interval
        self._stop = threading.Event()
        self._last: Optional[tuple] = None

    def run(self) -> None:
        self.adb.start_server()
        last_err = ""
        while not self._stop.is_set():
            try:
                devs = self.adb.list_devices()
                key = tuple((d.serial, d.state) for d in devs)
                if key != self._last:
                    self._last = key
                    self.on_change(devs)
                last_err = ""
            except AdbError as exc:
                msg = str(exc)
                if msg != last_err:
                    self.on_error(msg)
                    last_err = msg
            self._stop.wait(self.interval)

    def force_refresh(self) -> None:
        self._last = None

    def stop(self) -> None:
        self._stop.set()
