"""
mirror_engine.py
================
Motor de espejo y control.

1. ScrcpyMirror  -> modo principal. Lanza el binario scrcpy (video H.264 en
   tiempo real + inyeccion de mouse/teclado nativa) y vigila el proceso.
2. WindowEmbedder -> (solo Windows) mete la ventana de scrcpy DENTRO de la
   ventana de la app con la API Win32 (SetParent), para verlo todo en un
   solo lugar.
3. CaptureMirror -> modo de respaldo si scrcpy no esta disponible: captura la
   pantalla con `adb exec-out screencap` en bucle (1-3 fps) y la GUI la
   dibuja; los clics se traducen a `input tap/swipe`.
4. InputQueue    -> hilo que ejecuta los comandos adb de control en serie
   para que la interfaz nunca se congele.

Todo funciona solo con equipos que ya autorizaron la depuracion USB.
"""

from __future__ import annotations

import io
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from adb_manager import AdbError, AdbManager, _NO_WINDOW, find_scrcpy


# --------------------------------------------------------------------------- #
# Configuracion del espejo
# --------------------------------------------------------------------------- #
CALIDADES = {
    # nombre visible: (max_size, Mbps, fps)
    "Rápida (1024 px, 4 Mbps)": (1024, 4, 60),
    "Equilibrada (1600 px, 8 Mbps)": (1600, 8, 60),
    "Máxima (nativa, 16 Mbps)": (0, 16, 60),
    "Cable lento (800 px, 2 Mbps)": (800, 2, 30),
}
CALIDAD_DEFECTO = "Equilibrada (1600 px, 8 Mbps)"


@dataclass
class MirrorConfig:
    serial: str
    calidad: str = CALIDAD_DEFECTO
    apagar_pantalla: bool = True      # --turn-screen-off: el panel roto no gasta
    mantener_despierto: bool = True   # --stay-awake mientras haya USB
    mostrar_toques: bool = False      # --show-touches
    audio: bool = False               # Android 11+; apagado por defecto
    grabar_en: Optional[str] = None   # ruta .mp4 o None
    siempre_encima: bool = False
    incrustado: bool = False          # sin bordes, para meterlo en la app
    titulo: str = "Espejo"
    extra_args: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# scrcpy
# --------------------------------------------------------------------------- #
_HINTS = [
    (r"unauthorized", "El teléfono no autorizó la depuración USB de esta PC."),
    (r"Could not find any ADB device|no devices",
     "No hay ningún teléfono conectado o el cable solo carga."),
    (r"more than one|Multiple .*devices", "Hay varios equipos; elige uno."),
    (r"adb server version .* doesn't match|version mismatch",
     "Otro programa usa una versión distinta de adb. Ciérralo (o reinicia "
     "el servidor adb desde Herramientas)."),
    (r"Could not open video stream|Demuxer error|Video stream stopped",
     "El teléfono cortó el video (se desconectó o se bloqueó el codificador)."),
    (r"Audio .*(disabled|not supported)",
     "Audio no disponible en este equipo (requiere Android 11+)."),
]


def explain_scrcpy_error(text: str) -> str:
    for pat, msg in _HINTS:
        if re.search(pat, text, re.IGNORECASE):
            return msg
    return ""


def scrcpy_version(path: str) -> tuple[int, int]:
    """Devuelve (mayor, menor) de scrcpy; (0, 0) si no se puede leer."""
    try:
        out = subprocess.run([path, "--version"], capture_output=True,
                             timeout=10, stdin=subprocess.DEVNULL,
                             creationflags=_NO_WINDOW)
        text = (out.stdout or out.stderr).decode("utf-8", "replace")
        m = re.search(r"scrcpy\s+v?(\d+)\.(\d+)", text)
        if m:
            return int(m.group(1)), int(m.group(2))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 0, 0


class ScrcpyMirror:
    """
    Gestiona un proceso scrcpy. `on_log(linea)` recibe la salida y
    `on_exit(codigo, explicacion)` se llama cuando el proceso termina.
    """

    def __init__(self, adb: AdbManager,
                 on_log: Callable[[str], None],
                 on_exit: Callable[[int, str], None],
                 scrcpy_path: Optional[str] = None) -> None:
        self.adb = adb
        self.scrcpy = scrcpy_path or find_scrcpy()
        self.on_log = on_log
        self.on_exit = on_exit
        self.proc: Optional[subprocess.Popen] = None
        self.config: Optional[MirrorConfig] = None
        self._stopping = False
        self.version = scrcpy_version(self.scrcpy) if self.scrcpy else (0, 0)

    @property
    def available(self) -> bool:
        return bool(self.scrcpy)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # ----- construccion del comando -------------------------------------- #
    def build_command(self, cfg: MirrorConfig) -> list[str]:
        assert self.scrcpy
        max_size, mbps, fps = CALIDADES.get(cfg.calidad,
                                            CALIDADES[CALIDAD_DEFECTO])
        major = self.version[0]
        cmd = [self.scrcpy, f"--serial={cfg.serial}",
               f"--window-title={cfg.titulo}"]
        if max_size:
            cmd.append(f"--max-size={max_size}")
        # scrcpy 2.x+ renombro --bit-rate a --video-bit-rate.
        if major >= 2 or major == 0:
            cmd.append(f"--video-bit-rate={mbps}M")
            cmd.append("--video-codec=h264")
            if not cfg.audio:
                cmd.append("--no-audio")
        else:
            cmd.append(f"--bit-rate={mbps}M")
        cmd.append(f"--max-fps={fps}")
        if cfg.apagar_pantalla:
            cmd.append("--turn-screen-off")
        if cfg.mantener_despierto:
            cmd.append("--stay-awake")
        if cfg.mostrar_toques:
            cmd.append("--show-touches")
        if cfg.siempre_encima and not cfg.incrustado:
            cmd.append("--always-on-top")
        if cfg.grabar_en:
            cmd.append(f"--record={cfg.grabar_en}")
        if cfg.incrustado:
            # Sin bordes y fuera de la vista hasta que se incruste.
            cmd += ["--window-borderless", "--window-x=-3000",
                    "--window-y=-3000"]
        cmd += cfg.extra_args
        return cmd

    # ----- ciclo de vida ------------------------------------------------- #
    def start(self, cfg: MirrorConfig) -> subprocess.Popen:
        if not self.scrcpy:
            raise AdbError("scrcpy no está disponible.")
        self.stop()
        self.config = cfg
        self._stopping = False
        cmd = self.build_command(cfg)
        env = dict(os.environ)
        # Que scrcpy use el MISMO adb que la app (evita choque de versiones).
        env["ADB"] = self.adb.adb
        self.on_log("$ " + " ".join(os.path.basename(c) if i == 0 else c
                                    for i, c in enumerate(cmd)))
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env, creationflags=_NO_WINDOW,
            cwd=os.path.dirname(self.scrcpy) or None,
        )
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True,
                         name="scrcpy-log").start()
        return self.proc

    def _pump(self, proc: subprocess.Popen) -> None:
        tail: list[str] = []
        assert proc.stdout
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                tail = (tail + [line])[-30:]
                self.on_log("scrcpy: " + line)
        code = proc.wait()
        if proc is not self.proc:
            return          # ya se lanzo otro espejo; este quedo obsoleto
        hint = "" if self._stopping else explain_scrcpy_error("\n".join(tail))
        self.on_exit(code if not self._stopping else 0, hint)

    def stop(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        self._stopping = True
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()


# --------------------------------------------------------------------------- #
# Incrustar la ventana de scrcpy (Windows)
# --------------------------------------------------------------------------- #
class WindowEmbedder:
    """
    Busca la ventana de scrcpy por su titulo y la vuelve hija de un widget
    Tk (su HWND). En Linux/macOS `supported` es False y scrcpy queda como
    ventana aparte.
    """

    GWL_STYLE = -16
    WS_CHILD = 0x40000000
    WS_POPUP = 0x80000000
    WS_VISIBLE = 0x10000000
    WS_DECOR = 0x00C00000 | 0x00040000 | 0x00080000 | 0x00020000 | 0x00010000
    SWP_FLAGS = 0x0020 | 0x0004 | 0x0010   # FRAMECHANGED|NOZORDER|NOACTIVATE

    def __init__(self) -> None:
        self.supported = os.name == "nt"
        self.hwnd: int = 0
        self.parent: int = 0
        self._orig_style: int = 0
        if self.supported:
            import ctypes
            from ctypes import wintypes
            self._u = ctypes.windll.user32
            self._u.FindWindowW.restype = wintypes.HWND
            self._u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            ptr = ctypes.c_ssize_t
            self._get = getattr(self._u, "GetWindowLongPtrW",
                                self._u.GetWindowLongW)
            self._set = getattr(self._u, "SetWindowLongPtrW",
                                self._u.SetWindowLongW)
            self._get.restype = ptr
            self._get.argtypes = [wintypes.HWND, ctypes.c_int]
            self._set.restype = ptr
            self._set.argtypes = [wintypes.HWND, ctypes.c_int, ptr]
            self._u.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
            self._u.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, wintypes.BOOL]
            self._u.IsWindow.argtypes = [wintypes.HWND]
            self._u.SetFocus.argtypes = [wintypes.HWND]
            self._u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                             ctypes.c_int, ctypes.c_int,
                                             ctypes.c_int, ctypes.c_int,
                                             ctypes.c_uint]

    @property
    def embedded(self) -> bool:
        return bool(self.hwnd) and self.supported and bool(
            self._u.IsWindow(self.hwnd))

    def wait_for_window(self, title: str, proc: subprocess.Popen,
                        timeout: float = 25) -> int:
        """Espera (en un hilo de fondo) a que aparezca la ventana de scrcpy."""
        if not self.supported:
            return 0
        end = time.time() + timeout
        while time.time() < end and proc.poll() is None:
            hwnd = self._u.FindWindowW(None, title)
            if hwnd:
                return int(hwnd)
            time.sleep(0.15)
        return 0

    def embed(self, hwnd: int, parent_hwnd: int, w: int, h: int) -> None:
        if not (self.supported and hwnd):
            return
        self.hwnd, self.parent = hwnd, parent_hwnd
        style = self._get(hwnd, self.GWL_STYLE)
        self._orig_style = style
        style = (style & ~(self.WS_POPUP | self.WS_DECOR)) | self.WS_CHILD \
            | self.WS_VISIBLE
        self._set(hwnd, self.GWL_STYLE, style)
        self._u.SetParent(hwnd, parent_hwnd)
        self._u.SetWindowPos(hwnd, 0, 0, 0, w, h, self.SWP_FLAGS)
        self.resize(w, h)
        self.focus()

    def resize(self, w: int, h: int) -> None:
        # scrcpy conserva la proporcion y rellena con negro: basta con
        # ajustar el rectangulo hijo al tamaño del contenedor.
        if self.embedded:
            self._u.MoveWindow(self.hwnd, 0, 0, max(w, 50), max(h, 50), True)

    def focus(self) -> None:
        if self.embedded:
            self._u.SetFocus(self.hwnd)

    def detach(self) -> None:
        """Devuelve la ventana de scrcpy a ventana normal flotante."""
        if self.embedded:
            self._u.SetParent(self.hwnd, None)
            self._set(self.hwnd, self.GWL_STYLE,
                      self._orig_style | self.WS_DECOR | self.WS_VISIBLE)
            self._u.SetWindowPos(self.hwnd, 0, 80, 80, 480, 960,
                                 0x0020 | 0x0004)
        self.hwnd = 0

    def forget(self) -> None:
        self.hwnd = 0


# --------------------------------------------------------------------------- #
# Modo respaldo: capturas por adb
# --------------------------------------------------------------------------- #
class CaptureMirror(threading.Thread):
    """
    Bucle de capturas `screencap -p`. Entrega cada cuadro como
    `PIL.Image` a `on_frame`. Es lento (1-3 fps) pero no necesita scrcpy y
    funciona aunque el panel fisico este muerto, porque Android sigue
    componiendo la imagen en memoria.
    """

    def __init__(self, adb: AdbManager, serial: str,
                 on_frame: Callable[[object], None],
                 on_error: Callable[[str], None],
                 max_fps: float = 3.0) -> None:
        super().__init__(daemon=True, name="CaptureMirror")
        self.adb = adb
        self.serial = serial
        self.on_frame = on_frame
        self.on_error = on_error
        self.min_period = 1.0 / max_fps
        self._stop = threading.Event()

    def run(self) -> None:
        from PIL import Image      # importacion perezosa: solo en este modo
        fails = 0
        while not self._stop.is_set():
            t0 = time.time()
            try:
                png = self.adb.screencap_png(self.serial)
                img = Image.open(io.BytesIO(png))
                img.load()
                self.on_frame(img)
                fails = 0
            except Exception as exc:          # noqa: BLE001 - se informa
                fails += 1
                if fails in (1, 5) or fails % 20 == 0:
                    self.on_error(f"Captura fallida ({fails}): {exc}")
                self._stop.wait(min(5.0, 0.5 * fails))
                continue
            self._stop.wait(max(0.0, self.min_period - (time.time() - t0)))

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- #
# Cola de comandos de control
# --------------------------------------------------------------------------- #
class InputQueue(threading.Thread):
    """Ejecuta en orden las acciones (toques, teclas, PIN) fuera de la GUI."""

    def __init__(self, on_error: Callable[[str], None]) -> None:
        super().__init__(daemon=True, name="InputQueue")
        self._q: queue.Queue = queue.Queue()
        self.on_error = on_error

    def put(self, fn: Callable[[], None], descripcion: str = "") -> None:
        self._q.put((fn, descripcion))

    def run(self) -> None:
        while True:
            fn, desc = self._q.get()
            if fn is None:
                return
            try:
                fn()
            except Exception as exc:          # noqa: BLE001 - se informa
                self.on_error(f"{desc or 'Acción'} falló: {exc}")

    def stop(self) -> None:
        self._q.put((None, ""))
