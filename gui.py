"""
gui.py
======
Interfaz CustomTkinter de Espejo Rescate.

- Panel izquierdo: equipo detectado, calidad y opciones del espejo, botones
  de control a ciegas (despertar, desbloquear, PIN, inicio, atras...).
- Zona derecha: aqui se incrusta la ventana de scrcpy (Windows) o se dibujan
  las capturas del modo respaldo, con clic = toque.
- Consola inferior con el registro.

Hilos: DeviceWatcher, scrcpy (lector de salida), CaptureMirror e InputQueue
nunca tocan widgets; mandan eventos por `self.events` (queue.Queue) y la GUI
los procesa con `after()`.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from typing import Optional

import customtkinter as ctk

from adb_manager import (AdbError, AdbManager, AdbNotFound, Device,
                         DeviceWatcher, find_scrcpy)
from mirror_engine import (CALIDAD_DEFECTO, CALIDADES, CaptureMirror,
                           InputQueue, MirrorConfig, ScrcpyMirror,
                           WindowEmbedder)

APP_NOMBRE = "Espejo Rescate"
APP_VERSION = "1.0.0"
TITULO_SCRCPY = "EspejoRescate-scrcpy"

VERDE, AMBAR, ROJO, GRIS = "#2ecc71", "#f1c40f", "#e74c3c", "#7f8c8d"

AYUDA_SIN_AUTORIZAR = (
    "El teléfono está conectado pero NO autorizó la depuración USB de esta PC.\n\n"
    "Android exige aceptar el aviso «¿Permitir depuración USB?» en el propio "
    "teléfono; esta app no puede (ni intenta) saltarse ese permiso.\n\n"
    "Formas legítimas de aceptarlo con la pantalla rota:\n"
    "  • Adaptador USB-C a HDMI: muchos equipos (Samsung S/Note/A5x, varios "
    "gama alta) envían imagen a un monitor o TV.\n"
    "  • Ratón USB por OTG: aparece un cursor; toca «Permitir» (marca "
    "«Permitir siempre»).\n"
    "  • Si el táctil todavía responde, el aviso suele estar centrado y el "
    "botón «Permitir» abajo a la derecha.\n\n"
    "Después vuelve a conectar el cable a esta PC."
)

AYUDA_SIN_EQUIPO = (
    "Conecta el teléfono por USB.\n\n"
    "Requisitos: depuración USB activada y esta PC autorizada antes de que "
    "se rompiera la pantalla.\n"
    "Usa un cable de datos (no solo de carga).\n"
    "En Windows, si no aparece, instala el driver USB del fabricante."
)

ATAJOS_SCRCPY = (
    "Dentro del espejo: clic = toque · clic derecho = Atrás · rueda = "
    "desplazar · teclado = escribir · Alt+H Inicio · Alt+B Atrás · "
    "Alt+S Recientes · Alt+O apagar panel · Alt+Shift+O encender panel"
)

# Teclas del PC -> KEYCODE de Android (modo captura)
TECLAS = {
    "BackSpace": 67, "Return": 66, "KP_Enter": 66, "Escape": 4,
    "Tab": 61, "Delete": 112, "Home": 3, "Left": 21, "Right": 22,
    "Up": 19, "Down": 20, "Prior": 92, "Next": 93,
}


# --------------------------------------------------------------------------- #
# Configuracion persistente
# --------------------------------------------------------------------------- #
def config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), ".config")
    folder = os.path.join(base, "EspejoRescate")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "config.json")


DEFAULTS = {
    "calidad": CALIDAD_DEFECTO,
    "auto": True,
    "incrustar": os.name == "nt",
    "apagar_pantalla": True,
    "mantener_despierto": True,
    "mostrar_toques": False,
    "audio": False,
    "grabar": False,
    "carpeta": os.path.join(os.path.expanduser("~"), "Videos", "EspejoRescate"),
    "tema": "dark",
}


def load_config() -> dict:
    data = dict(DEFAULTS)
    try:
        with open(config_path(), encoding="utf-8") as fh:
            data.update(json.load(fh))
    except (OSError, ValueError):
        pass
    if data.get("calidad") not in CALIDADES:
        data["calidad"] = CALIDAD_DEFECTO
    return data


def save_config(data: dict) -> None:
    try:
        with open(config_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Dialogo para el PIN del dueño
# --------------------------------------------------------------------------- #
class PinDialog(ctk.CTkToplevel):
    """Pide el PIN/contraseña que el dueño ya conoce (texto oculto)."""

    def __init__(self, master: ctk.CTk) -> None:
        super().__init__(master)
        self.title("Escribir mi PIN")
        self.geometry("360x230")
        self.resizable(False, False)
        self.result: Optional[str] = None
        ctk.CTkLabel(self, text="PIN o contraseña de TU teléfono",
                     font=ctk.CTkFont(size=15, weight="bold")).pack(pady=(18, 4))
        ctk.CTkLabel(self, text="Se despierta la pantalla, se desliza hacia\n"
                                "arriba, se escribe y se pulsa Enter.",
                     text_color=GRIS).pack()
        self.entry = ctk.CTkEntry(self, show="•", width=240, justify="center",
                                  font=ctk.CTkFont(size=18))
        self.entry.pack(pady=12)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack()
        ctk.CTkButton(row, text="Cancelar", width=110, fg_color=GRIS,
                      command=self.destroy).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Enviar", width=110,
                      command=self._ok).pack(side="left", padx=6)
        self.entry.bind("<Return>", lambda _e: self._ok())
        self.after(100, self._grab)

    def _grab(self) -> None:
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.entry.focus_set()

    def _ok(self) -> None:
        self.result = self.entry.get()
        self.destroy()


# --------------------------------------------------------------------------- #
# Ventana principal
# --------------------------------------------------------------------------- #
class App(ctk.CTk):

    def __init__(self) -> None:
        self.cfg = load_config()
        ctk.set_appearance_mode(self.cfg.get("tema", "dark"))
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.title(f"{APP_NOMBRE} {APP_VERSION}")
        self.geometry("1180x800")
        self.minsize(900, 600)

        self.events: queue.Queue = queue.Queue()
        self.devices: list[Device] = []
        self.serial: Optional[str] = None
        self.mode = "idle"          # idle | scrcpy | captura
        self.fallos_seguidos = 0
        self._photo = None          # referencia viva de la imagen (captura)
        self._map = (0.0, 0, 0, 1, 1)   # escala, ox, oy, ancho, alto del cuadro
        self._press: Optional[tuple[int, int, float]] = None
        self._texto_pendiente = ""
        self._texto_job: Optional[str] = None
        self.capture: Optional[CaptureMirror] = None

        self._build_ui()

        # Motor
        try:
            self.adb = AdbManager(logger=lambda m: self.events.put(("log", m)))
        except AdbNotFound as exc:
            self.adb = None
            self._log(f"✖ {exc}")
            self._set_status(ROJO, "Falta adb",
                             "Ejecuta «python descargar_binarios.py» o "
                             "instala Android Platform Tools.")
        self.embedder = WindowEmbedder()
        self.inputs = InputQueue(on_error=lambda m: self.events.put(("log", "! " + m)))
        self.inputs.start()
        self.mirror: Optional[ScrcpyMirror] = None
        self.watcher: Optional[DeviceWatcher] = None

        if self.adb:
            self.mirror = ScrcpyMirror(
                self.adb,
                on_log=lambda m: self.events.put(("log", m)),
                on_exit=lambda c, h: self.events.put(("scrcpy_exit", (c, h))),
            )
            self._log(f"adb: {self.adb.adb}")
            if self.mirror.available:
                v = ".".join(map(str, self.mirror.version))
                self._log(f"scrcpy {v}: {self.mirror.scrcpy}")
            else:
                self._log("! scrcpy no encontrado: se usará el modo captura "
                          "(lento). Ejecuta «python descargar_binarios.py».")
            if not self.embedder.supported:
                self.sw_incrustar.deselect()
                self.sw_incrustar.configure(state="disabled")
            self.watcher = DeviceWatcher(
                self.adb,
                on_change=lambda d: self.events.put(("devices", d)),
                on_error=lambda m: self.events.put(("log", "! adb: " + m)),
            )
            self.watcher.start()
            self._set_status(GRIS, "Buscando teléfono…", AYUDA_SIN_EQUIPO)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._drain_events)

    # ================================================================== #
    # Construccion de la interfaz
    # ================================================================== #
    def _build_ui(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ----- barra lateral --------------------------------------------- #
        side = ctk.CTkScrollableFrame(self, width=290, corner_radius=0)
        side.grid(row=0, column=0, rowspan=2, sticky="nsew")
        bold = ctk.CTkFont(size=14, weight="bold")

        ctk.CTkLabel(side, text="📱 " + APP_NOMBRE,
                     font=ctk.CTkFont(size=20, weight="bold")).pack(pady=(14, 2))
        ctk.CTkLabel(side, text="Ver y controlar un celular con la pantalla rota",
                     text_color=GRIS, wraplength=260).pack(pady=(0, 10))

        # Equipo
        ctk.CTkLabel(side, text="Teléfono", font=bold).pack(anchor="w", padx=12)
        self.cb_device = ctk.CTkOptionMenu(side, values=["(ninguno)"],
                                           command=self._on_pick_device,
                                           width=260)
        self.cb_device.pack(padx=12, pady=4)
        self.lbl_estado = ctk.CTkLabel(side, text="", anchor="w",
                                       wraplength=260, justify="left")
        self.lbl_estado.pack(fill="x", padx=12)

        # Espejo
        ctk.CTkLabel(side, text="Espejo", font=bold).pack(anchor="w", padx=12,
                                                          pady=(12, 0))
        self.btn_espejo = ctk.CTkButton(side, text="▶ Iniciar espejo",
                                        height=40, font=bold,
                                        command=self._toggle_mirror)
        self.btn_espejo.pack(fill="x", padx=12, pady=6)
        self.btn_separar = ctk.CTkButton(side, text="Separar / incrustar ventana",
                                         fg_color="transparent", border_width=1,
                                         command=self._toggle_embed)
        self.btn_separar.pack(fill="x", padx=12, pady=(0, 6))

        self.om_calidad = ctk.CTkOptionMenu(side, values=list(CALIDADES),
                                            width=260, command=self._save_opts)
        self.om_calidad.set(self.cfg["calidad"])
        self.om_calidad.pack(padx=12, pady=4)

        def switch(texto: str, clave: str) -> ctk.CTkSwitch:
            sw = ctk.CTkSwitch(side, text=texto, command=self._save_opts)
            if self.cfg.get(clave):
                sw.select()
            sw.pack(anchor="w", padx=14, pady=3)
            return sw

        self.sw_auto = switch("Iniciar solo al conectar", "auto")
        self.sw_incrustar = switch("Mostrar dentro de esta ventana", "incrustar")
        self.sw_apagar = switch("Apagar panel del teléfono", "apagar_pantalla")
        self.sw_despierto = switch("Mantener despierto", "mantener_despierto")
        self.sw_toques = switch("Mostrar toques", "mostrar_toques")
        self.sw_audio = switch("Audio (Android 11+)", "audio")
        self.sw_grabar = switch("Grabar video .mp4", "grabar")

        # Control a ciegas
        ctk.CTkLabel(side, text="Control", font=bold).pack(anchor="w", padx=12,
                                                           pady=(12, 0))
        grid = ctk.CTkFrame(side, fg_color="transparent")
        grid.pack(padx=8, pady=4)
        botones = [
            ("☀ Despertar", lambda: self._key(224, "Despertar")),
            ("⬆ Deslizar", self._swipe_unlock),
            ("🔑 Mi PIN…", self._pin),
            ("⏻ Encendido", lambda: self._key(26, "Encendido")),
            ("⌂ Inicio", lambda: self._key(3, "Inicio")),
            ("◀ Atrás", lambda: self._key(4, "Atrás")),
            ("▣ Recientes", lambda: self._key(187, "Recientes")),
            ("🔔 Avisos", self._notificaciones),
            ("🔊 Vol +", lambda: self._key(24, "Vol +")),
            ("🔉 Vol −", lambda: self._key(25, "Vol −")),
            ("📷 Captura", self._screenshot),
            ("⌨ Texto…", self._send_text),
        ]
        for i, (txt, fn) in enumerate(botones):
            ctk.CTkButton(grid, text=txt, width=126, height=32,
                          command=fn).grid(row=i // 2, column=i % 2,
                                           padx=3, pady=3)

        ctk.CTkLabel(side, text="Herramientas", font=bold).pack(anchor="w",
                                                                padx=12,
                                                                pady=(12, 0))
        ctk.CTkButton(side, text="Reiniciar servidor adb",
                      fg_color="transparent", border_width=1,
                      command=self._restart_adb).pack(fill="x", padx=12, pady=3)
        ctk.CTkButton(side, text="Abrir carpeta de capturas",
                      fg_color="transparent", border_width=1,
                      command=self._open_folder).pack(fill="x", padx=12, pady=3)
        self.om_tema = ctk.CTkOptionMenu(side, values=["dark", "light", "system"],
                                         command=self._set_theme, width=260)
        self.om_tema.set(self.cfg.get("tema", "dark"))
        self.om_tema.pack(padx=12, pady=(3, 16))

        # ----- zona principal --------------------------------------------- #
        main = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(main, height=44)
        head.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        self.led = ctk.CTkLabel(head, text="●", text_color=GRIS,
                                font=ctk.CTkFont(size=22))
        self.led.pack(side="left", padx=(10, 4))
        self.lbl_titulo = ctk.CTkLabel(head, text="Iniciando…", font=bold)
        self.lbl_titulo.pack(side="left")
        self.lbl_info = ctk.CTkLabel(head, text="", text_color=GRIS)
        self.lbl_info.pack(side="right", padx=10)

        # Contenedor negro: aqui vive scrcpy incrustado o el lienzo de captura.
        # Debe ser un tk.Frame "puro" (no CTkFrame) para tener un HWND limpio.
        self.host = tk.Frame(main, bg="#000000", highlightthickness=0, bd=0)
        self.host.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        self.host.bind("<Configure>", self._on_host_resize)

        self.canvas = tk.Canvas(self.host, bg="#000000", highlightthickness=0,
                                bd=0, cursor="hand2", takefocus=1)
        self.canvas.bind("<ButtonPress-1>", self._c_press)
        self.canvas.bind("<ButtonRelease-1>", self._c_release)
        self.canvas.bind("<ButtonRelease-3>", lambda _e: self._key(4, "Atrás"))
        self.canvas.bind("<MouseWheel>", self._c_wheel)
        self.canvas.bind("<Button-4>", lambda e: self._c_wheel(e, 120))
        self.canvas.bind("<Button-5>", lambda e: self._c_wheel(e, -120))
        self.canvas.bind("<Key>", self._c_key)

        self.placeholder = ctk.CTkLabel(self.host, text="", text_color="#d0d0d0",
                                        fg_color="#000000", justify="left",
                                        wraplength=560,
                                        font=ctk.CTkFont(size=14))
        self.placeholder.place(relx=0.5, rely=0.5, anchor="center")

        self.lbl_atajos = ctk.CTkLabel(main, text=ATAJOS_SCRCPY, text_color=GRIS,
                                       wraplength=860, justify="left")
        self.lbl_atajos.grid(row=2, column=0, sticky="w", padx=12)

        self.console = ctk.CTkTextbox(main, height=120,
                                      font=ctk.CTkFont(family="Consolas", size=11))
        self.console.grid(row=3, column=0, sticky="ew", padx=8, pady=(4, 8))
        self.console.configure(state="disabled")

    # ================================================================== #
    # Utilidades de interfaz
    # ================================================================== #
    def _log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.console.configure(state="normal")
        self.console.insert("end", f"[{stamp}] {msg}\n")
        lines = int(self.console.index("end-1c").split(".")[0])
        if lines > 800:
            self.console.delete("1.0", f"{lines - 800}.0")
        self.console.see("end")
        self.console.configure(state="disabled")

    def _set_status(self, color: str, titulo: str, ayuda: str = "") -> None:
        self.led.configure(text_color=color)
        self.lbl_titulo.configure(text=titulo)
        self.placeholder.configure(text=ayuda)
        if ayuda and self.mode == "idle":
            self.placeholder.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.placeholder.place_forget()

    def _save_opts(self, *_a) -> None:
        self.cfg.update(
            calidad=self.om_calidad.get(),
            auto=bool(self.sw_auto.get()),
            incrustar=bool(self.sw_incrustar.get()),
            apagar_pantalla=bool(self.sw_apagar.get()),
            mantener_despierto=bool(self.sw_despierto.get()),
            mostrar_toques=bool(self.sw_toques.get()),
            audio=bool(self.sw_audio.get()),
            grabar=bool(self.sw_grabar.get()),
        )
        save_config(self.cfg)

    def _set_theme(self, tema: str) -> None:
        ctk.set_appearance_mode(tema)
        self.cfg["tema"] = tema
        save_config(self.cfg)

    def _current_device(self) -> Optional[Device]:
        return next((d for d in self.devices if d.serial == self.serial), None)

    def _require_device(self) -> Optional[str]:
        dev = self._current_device()
        if not self.adb or not dev:
            self._log("! No hay teléfono seleccionado.")
            return None
        if not dev.authorized:
            self._log("! El teléfono no autorizó la depuración USB.")
            return None
        return dev.serial

    # ================================================================== #
    # Eventos de los hilos
    # ================================================================== #
    def _drain_events(self) -> None:
        try:
            for _ in range(200):
                kind, data = self.events.get_nowait()
                if kind == "log":
                    self._log(data)
                elif kind == "devices":
                    self._on_devices(data)
                elif kind == "scrcpy_exit":
                    self._on_scrcpy_exit(*data)
                elif kind == "scrcpy_window":
                    self._on_scrcpy_window(data)
                elif kind == "frame":
                    self._draw_frame(data)
                elif kind == "info":
                    self.lbl_info.configure(text=data)
        except queue.Empty:
            pass
        self.after(30, self._drain_events)

    # ----- conexiones ------------------------------------------------------ #
    def _on_devices(self, devs: list[Device]) -> None:
        self.devices = devs
        if not devs:
            self.cb_device.configure(values=["(ninguno)"])
            self.cb_device.set("(ninguno)")
            self.lbl_estado.configure(text="Sin equipo conectado", text_color=GRIS)
            if self.serial:
                self._log("Teléfono desconectado.")
            self.serial = None
            self._stop_mirror(silencioso=True)
            self._set_status(GRIS, "Esperando teléfono…", AYUDA_SIN_EQUIPO)
            return

        labels = [d.label for d in devs]
        self.cb_device.configure(values=labels)
        if self._current_device() is None:
            # Preferir el primero autorizado.
            pick = next((d for d in devs if d.authorized), devs[0])
            self.serial = pick.serial
        dev = self._current_device()
        assert dev
        self.cb_device.set(dev.label)
        self._log(f"Detectado: {dev.label} → {dev.state_text}")
        self._refresh_device_state()

    def _on_pick_device(self, label: str) -> None:
        dev = next((d for d in self.devices if d.label == label), None)
        if not dev or dev.serial == self.serial:
            return
        self._stop_mirror(silencioso=True)
        self.serial = dev.serial
        self._refresh_device_state()

    def _refresh_device_state(self) -> None:
        dev = self._current_device()
        if not dev:
            return
        if dev.authorized:
            self.lbl_estado.configure(text=f"● {dev.state_text}",
                                      text_color=VERDE)
            if self.mode == "idle":
                self._set_status(AMBAR, f"{dev.label} listo",
                                 "Pulsa «Iniciar espejo».")
                threading.Thread(target=self._read_info, args=(dev.serial,),
                                 daemon=True).start()
                if self.sw_auto.get():
                    self.fallos_seguidos = 0
                    self._start_mirror()
        elif dev.state == "unauthorized":
            self.lbl_estado.configure(text="● Sin autorizar", text_color=ROJO)
            self._stop_mirror(silencioso=True)
            self._set_status(ROJO, "Depuración USB sin autorizar",
                             AYUDA_SIN_AUTORIZAR)
        else:
            self.lbl_estado.configure(text=f"● {dev.state_text}",
                                      text_color=AMBAR)
            self._stop_mirror(silencioso=True)
            self._set_status(AMBAR, dev.state_text,
                             "El equipo no está en modo normal (device). "
                             "Reinícialo o reconecta el cable.")

    def _read_info(self, serial: str) -> None:
        assert self.adb
        ver = self.adb.get_prop("ro.build.version.release", serial)
        bat = ""
        try:
            out = self.adb.shell("dumpsys battery", serial, quiet=True)
            for line in out.splitlines():
                if "level:" in line:
                    bat = line.split(":")[1].strip() + "%"
                    break
        except AdbError:
            pass
        locked = self.adb.is_locked(serial)
        lock = {True: "bloqueado", False: "desbloqueado"}.get(locked, "")
        parts = [p for p in (f"Android {ver}" if ver else "",
                             f"batería {bat}" if bat else "", lock) if p]
        self.events.put(("info", " · ".join(parts)))

    # ================================================================== #
    # Espejo
    # ================================================================== #
    def _toggle_mirror(self) -> None:
        if self.mode == "idle":
            self.fallos_seguidos = 0
            self._start_mirror()
        else:
            self._stop_mirror()

    def _start_mirror(self) -> None:
        serial = self._require_device()
        if not serial:
            return
        assert self.mirror
        if not self.mirror.available:
            self._start_capture(serial)
            return

        self._save_opts()
        grabar = None
        if self.cfg["grabar"]:
            os.makedirs(self.cfg["carpeta"], exist_ok=True)
            grabar = os.path.join(
                self.cfg["carpeta"],
                f"espejo_{datetime.now():%Y%m%d_%H%M%S}.mp4")
            self._log(f"Grabando en {grabar}")
        incrustar = bool(self.sw_incrustar.get()) and self.embedder.supported
        cfg = MirrorConfig(
            serial=serial, calidad=self.cfg["calidad"],
            apagar_pantalla=self.cfg["apagar_pantalla"],
            mantener_despierto=self.cfg["mantener_despierto"],
            mostrar_toques=self.cfg["mostrar_toques"],
            audio=self.cfg["audio"], grabar_en=grabar,
            incrustado=incrustar, titulo=TITULO_SCRCPY,
        )
        # Despertar primero: algunos equipos no codifican con la pantalla apagada.
        assert self.adb
        self.inputs.put(lambda: self.adb.wake(serial), "Despertar")
        try:
            proc = self.mirror.start(cfg)
        except (OSError, AdbError) as exc:
            self._log(f"✖ No se pudo lanzar scrcpy: {exc}")
            self._start_capture(serial)
            return
        self.mode = "scrcpy"
        self.placeholder.place_forget()
        self.btn_espejo.configure(text="■ Detener espejo", fg_color=ROJO,
                                  hover_color="#c0392b")
        self._set_status(VERDE, "Espejo activo (scrcpy)")
        if incrustar:
            self.placeholder.configure(text="Abriendo video…")
            self.placeholder.place(relx=0.5, rely=0.5, anchor="center")
            threading.Thread(target=self._wait_window, args=(proc,),
                             daemon=True).start()

    def _wait_window(self, proc) -> None:
        hwnd = self.embedder.wait_for_window(TITULO_SCRCPY, proc)
        self.events.put(("scrcpy_window", hwnd))

    def _on_scrcpy_window(self, hwnd: int) -> None:
        self.placeholder.place_forget()
        if not hwnd or self.mode != "scrcpy":
            return
        self.host.update_idletasks()
        self.embedder.embed(hwnd, self.host.winfo_id(),
                            self.host.winfo_width(), self.host.winfo_height())
        self._log("Video incrustado en la ventana. Haz clic en él para "
                  "usar el teclado.")

    def _toggle_embed(self) -> None:
        if self.mode != "scrcpy" or not self.embedder.supported:
            self._log("Solo disponible con el espejo scrcpy activo en Windows.")
            return
        if self.embedder.embedded:
            self.embedder.detach()
            self._set_status(VERDE, "Espejo activo (ventana aparte)",
                             )
            self.placeholder.configure(text="El espejo está en otra ventana.")
            self.placeholder.place(relx=0.5, rely=0.5, anchor="center")
        else:
            assert self.mirror and self.mirror.proc
            threading.Thread(target=self._wait_window,
                             args=(self.mirror.proc,), daemon=True).start()

    def _on_host_resize(self, event) -> None:
        if self.mode == "scrcpy":
            self.embedder.resize(event.width, event.height)
        elif self.mode == "captura" and self.capture and self._photo:
            pass  # el siguiente cuadro ya se dibuja al nuevo tamaño

    def _on_scrcpy_exit(self, code: int, hint: str) -> None:
        if self.mode != "scrcpy":
            return
        self.embedder.forget()
        self._reset_idle()
        if code == 0:
            self._log("Espejo cerrado.")
            self._refresh_status_idle()
            return
        self.fallos_seguidos += 1
        self._log(f"✖ scrcpy terminó (código {code}). {hint}")
        self._set_status(ROJO, "El espejo se cerró",
                         hint or "Revisa el registro de abajo.")
        dev = self._current_device()
        if self.sw_auto.get() and dev and dev.authorized:
            if self.fallos_seguidos < 3:
                self._log("Reintentando en 3 s…")
                self.after(3000, lambda: self.mode == "idle" and
                           self._start_mirror())
            else:
                self._log("Tres fallos seguidos: cambio al modo captura.")
                self._start_capture(dev.serial)

    def _stop_mirror(self, silencioso: bool = False) -> None:
        if self.mode == "scrcpy" and self.mirror:
            self.embedder.forget()
            self.mirror.stop()
        if self.mode == "captura" and self.capture:
            self.capture.stop()
            self.capture = None
            self.canvas.place_forget()
        if self.mode != "idle":
            self._reset_idle()
            if not silencioso:
                self._log("Espejo detenido.")
            self._refresh_status_idle()

    def _reset_idle(self) -> None:
        self.mode = "idle"
        self.btn_espejo.configure(text="▶ Iniciar espejo",
                                  fg_color=("#3B8ED0", "#1F6AA5"),
                                  hover_color=("#36719F", "#144870"))

    def _refresh_status_idle(self) -> None:
        dev = self._current_device()
        if dev and dev.authorized:
            self._set_status(AMBAR, f"{dev.label} listo",
                             "Pulsa «Iniciar espejo».")
        elif not dev:
            self._set_status(GRIS, "Esperando teléfono…", AYUDA_SIN_EQUIPO)

    # ----- modo captura (sin scrcpy) -------------------------------------- #
    def _start_capture(self, serial: str) -> None:
        assert self.adb
        try:
            import PIL.ImageTk  # noqa: F401
        except ImportError:
            self._log("✖ Falta Pillow (pip install pillow) para el modo captura.")
            return
        self.inputs.put(lambda: self.adb.wake(serial), "Despertar")
        self.capture = CaptureMirror(
            self.adb, serial,
            on_frame=lambda img: self.events.put(("frame", img)),
            on_error=lambda m: self.events.put(("log", "! " + m)),
        )
        self.capture.start()
        self.mode = "captura"
        self.placeholder.place_forget()
        self.canvas.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.canvas.focus_set()
        self.btn_espejo.configure(text="■ Detener espejo", fg_color=ROJO,
                                  hover_color="#c0392b")
        self._set_status(AMBAR, "Modo captura (lento, sin scrcpy)")
        self._log("Modo captura: clic = toque, arrastrar = deslizar, "
                  "clic derecho = Atrás, teclado = escribir.")

    def _draw_frame(self, img) -> None:
        if self.mode != "captura":
            return
        from PIL import Image, ImageTk
        cw = max(self.canvas.winfo_width(), 50)
        ch = max(self.canvas.winfo_height(), 50)
        iw, ih = img.size
        scale = min(cw / iw, ch / ih)
        w, h = max(1, int(iw * scale)), max(1, int(ih * scale))
        ox, oy = (cw - w) // 2, (ch - h) // 2
        self._photo = ImageTk.PhotoImage(img.resize((w, h), Image.BILINEAR))
        self.canvas.delete("all")
        self.canvas.create_image(ox, oy, anchor="nw", image=self._photo)
        self._map = (scale, ox, oy, iw, ih)

    def _to_device(self, x: int, y: int) -> Optional[tuple[int, int]]:
        scale, ox, oy, iw, ih = self._map
        if scale <= 0:
            return None
        dx, dy = (x - ox) / scale, (y - oy) / scale
        if 0 <= dx < iw and 0 <= dy < ih:
            return int(dx), int(dy)
        return None

    def _c_press(self, e) -> None:
        self.canvas.focus_set()
        self._press = (e.x, e.y, time.time())

    def _c_release(self, e) -> None:
        if not self._press or not self.capture:
            return
        x0, y0, t0 = self._press
        self._press = None
        a, b = self._to_device(x0, y0), self._to_device(e.x, e.y)
        if not a:
            return
        b = b or a
        ms = int((time.time() - t0) * 1000)
        serial = self.capture.serial
        assert self.adb
        adb = self.adb
        if abs(e.x - x0) < 8 and abs(e.y - y0) < 8:
            if ms > 500:     # pulsacion larga
                self.inputs.put(lambda: adb.swipe(*a, *a, serial, ms), "Toque largo")
            else:
                self.inputs.put(lambda: adb.tap(*a, serial), "Toque")
        else:
            self.inputs.put(lambda: adb.swipe(*a, *b, serial, max(120, ms)),
                            "Deslizar")

    def _c_wheel(self, e, delta: Optional[int] = None) -> None:
        if not self.capture:
            return
        p = self._to_device(e.x, e.y)
        if not p:
            return
        d = delta if delta is not None else e.delta
        dist = int(self._map[4] * 0.25) * (1 if d > 0 else -1)
        x, y = p
        assert self.adb
        adb, serial = self.adb, self.capture.serial
        self.inputs.put(lambda: adb.swipe(x, y, x, y + dist, serial, 200),
                        "Desplazar")

    def _c_key(self, e) -> None:
        if not self.capture:
            return
        assert self.adb
        adb, serial = self.adb, self.capture.serial
        if e.keysym in TECLAS:
            self._flush_text()
            code = TECLAS[e.keysym]
            self.inputs.put(lambda: adb.keyevent(code, serial), e.keysym)
        elif e.char and e.char.isprintable():
            # Agrupar letras para no lanzar un adb por cada tecla.
            self._texto_pendiente += e.char
            if self._texto_job:
                self.after_cancel(self._texto_job)
            self._texto_job = self.after(250, self._flush_text)

    def _flush_text(self) -> None:
        self._texto_job = None
        txt, self._texto_pendiente = self._texto_pendiente, ""
        if txt and self.capture and self.adb:
            adb, serial = self.adb, self.capture.serial
            self.inputs.put(lambda: adb.input_text(txt, serial), "Texto")

    # ================================================================== #
    # Botones de control
    # ================================================================== #
    def _key(self, code: int, nombre: str) -> None:
        serial = self._require_device()
        if serial:
            adb = self.adb
            assert adb
            self._log(f"→ {nombre}")
            self.inputs.put(lambda: adb.keyevent(code, serial), nombre)

    def _swipe_unlock(self) -> None:
        serial = self._require_device()
        if serial:
            adb = self.adb
            assert adb

            def job() -> None:
                adb.wake(serial)
                time.sleep(0.5)
                adb.swipe_up_unlock(serial)
            self._log("→ Despertar y deslizar hacia arriba")
            self.inputs.put(job, "Deslizar")

    def _pin(self) -> None:
        serial = self._require_device()
        if not serial:
            return
        dlg = PinDialog(self)
        self.wait_window(dlg)
        if dlg.result:
            adb, pin = self.adb, dlg.result
            assert adb
            self._log("→ Escribiendo tu PIN (oculto)")
            self.inputs.put(lambda: adb.enter_pin(pin, serial), "PIN")

    def _send_text(self) -> None:
        serial = self._require_device()
        if not serial:
            return
        dlg = ctk.CTkInputDialog(title="Enviar texto",
                                 text="Texto a escribir en el teléfono:")
        txt = dlg.get_input()
        if txt:
            adb = self.adb
            assert adb
            self.inputs.put(lambda: adb.input_text(txt, serial), "Texto")

    def _notificaciones(self) -> None:
        serial = self._require_device()
        if serial:
            adb = self.adb
            assert adb
            self.inputs.put(
                lambda: adb.shell("cmd statusbar expand-notifications", serial),
                "Avisos")

    def _screenshot(self) -> None:
        serial = self._require_device()
        if not serial:
            return
        adb, folder = self.adb, self.cfg["carpeta"]
        assert adb

        def job() -> None:
            png = adb.screencap_png(serial)
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder,
                                f"captura_{datetime.now():%Y%m%d_%H%M%S}.png")
            with open(path, "wb") as fh:
                fh.write(png)
            self.events.put(("log", f"Captura guardada: {path}"))
        self.inputs.put(job, "Captura")

    def _restart_adb(self) -> None:
        if not self.adb:
            return
        self._stop_mirror()
        adb = self.adb

        def job() -> None:
            adb.kill_server()
            adb.start_server()
            if self.watcher:
                self.watcher.force_refresh()
            self.events.put(("log", "Servidor adb reiniciado."))
        self.inputs.put(job, "Reiniciar adb")

    def _open_folder(self) -> None:
        folder = self.cfg["carpeta"]
        os.makedirs(folder, exist_ok=True)
        if os.name == "nt":
            os.startfile(folder)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f'open "{folder}"')
        else:
            os.system(f'xdg-open "{folder}" >/dev/null 2>&1 &')

    # ================================================================== #
    # Cierre
    # ================================================================== #
    def _on_close(self) -> None:
        self._save_opts()
        try:
            self._stop_mirror(silencioso=True)
        finally:
            if self.watcher:
                self.watcher.stop()
            self.inputs.stop()
            self.destroy()


def run() -> None:
    App().mainloop()
