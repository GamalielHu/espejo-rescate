# CLAUDE.md — Espejo Rescate

App de escritorio (Python + CustomTkinter) para ver y controlar un Android con
la pantalla rota usando scrcpy. Textos de usuario en español.

## Límites (no negociables)
- Solo equipos en estado `device` (depuración USB ya autorizada por el dueño).
- No implementar bypass de bloqueo, fastboot, EDL ni descubrimiento de PIN.
  `enter_pin` solo escribe el PIN que el dueño teclea.

## Estructura
- `adb_manager.py`: subprocess sobre adb, `DeviceWatcher` (hilo de sondeo).
- `mirror_engine.py`: `ScrcpyMirror`, `WindowEmbedder` (SetParent Win32),
  `CaptureMirror` (screencap), `InputQueue`.
- `gui.py`: los hilos NUNCA tocan widgets; usan `self.events` + `after()`.
- `main.py`: CLI/GUI. `descargar_binarios.py`: llena `bin/` (no se versiona).

## Compilar
GitHub Actions (`.github/workflows/build.yml`) en windows-latest: descarga
scrcpy, PyInstaller `--onefile --add-data bin;bin`, publica Release
(`build-N` prerelease en cada push; `vX.Y` en tags).
