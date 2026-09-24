# camaras_kame_house

Visor nativo de cámaras de un NVR Dahua (probado con **DHI-NVR1108HS-8P-S3/H**), en Python + PySide6.

- Detecta automáticamente qué canales del NVR tienen cámara viva (vía snapshot CGI).
- Muestra todas las cámaras en una grilla a pantalla completa.
- Doble clic para ampliar una cámara / volver.
- Zoom con botones translúcidos, rueda del mouse y pinch del trackpad.
- Arrastrar para desplazarse por la imagen cuando hay zoom.
- Reloj (fecha y hora) sobre cada cámara.

## Uso

1. Configurá las credenciales del NVR:

   ```bash
   cp config.example.py config.py
   # editá config.py con la IP, usuario y contraseña de tu NVR
   ```

2. Levantá todo (crea el venv, instala dependencias, verifica FFmpeg y abre el visor):

   ```bash
   ./run.sh
   ```

## Teclas

- `Q` / `ESC` — salir
- `F` — pantalla completa on/off
- `R` — reset del zoom

## Requisitos

- Python 3.9+ y `python3` en el PATH
- FFmpeg (incluye `ffprobe`) — en macOS: `brew install ffmpeg`
- Las dependencias Python se instalan solas desde `requirements.txt`
