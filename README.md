# Espejo Rescate

Ve y controla desde la PC un Android con la **pantalla rota o negra**, por
cable USB. Usa [scrcpy](https://github.com/Genymobile/scrcpy) como motor de
video (H.264 en tiempo real) y de control (mouse y teclado → toques).

## Requisito indispensable
La **depuración USB debe estar activada y autorizada para esta PC** desde
antes. Si el teléfono aparece como «Sin autorizar», la app explica las formas
legítimas de aceptar el aviso (adaptador USB-C→HDMI o ratón OTG). La app no
salta el bloqueo de pantalla ni usa fastboot/EDL; con el espejo, el dueño
escribe su propio PIN.

## Uso rápido (Windows)
1. Descarga `EspejoRescate.exe` de *Releases* (ya trae scrcpy y adb).
2. Conecta el teléfono. El espejo se abre solo dentro de la ventana.
3. Haz clic en el video para usar el teclado; o usa **🔑 Mi PIN…**.

## Desde el código
```bash
pip install -r requirements.txt
python descargar_binarios.py      # Windows: baja scrcpy + adb a bin/
python main.py                    # interfaz
python main.py diagnostico        # rutas/versiones y equipos
python main.py espejo             # espejo sin interfaz
python main.py pin                # escribir tu PIN a ciegas
```
Linux: `sudo apt install scrcpy adb python3-tk`.

## Funciones
- Detección automática (conectar, autorizar, desconectar) cada segundo.
- Espejo scrcpy incrustado en la ventana (Windows) o en ventana aparte.
- Calidad: rápida / equilibrada / máxima / cable lento.
- Apagar el panel físico, mantener despierto, mostrar toques, audio, grabar `.mp4`.
- Botones a ciegas: despertar, deslizar, mi PIN, encendido, inicio, atrás,
  recientes, avisos, volumen, captura PNG, enviar texto.
- **Modo captura** de respaldo si falta scrcpy o falla 3 veces: imagen por
  `screencap` (1–3 fps) con clic = toque, arrastre = deslizar, teclado.

## Módulos
| Archivo | Qué hace |
|---|---|
| `adb_manager.py` | Localiza adb/scrcpy (bin/ o PATH), detecta equipos, control por `input`. |
| `mirror_engine.py` | scrcpy, incrustado Win32, modo captura, cola de comandos. |
| `gui.py` | Interfaz CustomTkinter. |
| `main.py` | Entrada GUI/CLI. |
| `descargar_binarios.py` | Descarga scrcpy (incluye adb) para Windows. |
