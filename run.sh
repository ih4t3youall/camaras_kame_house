#!/usr/bin/env bash
#
# run.sh - Levanta el servidor de cámara (MediaMTX + FFmpeg) para la Dahua.
# Hace todo lo necesario: crea el venv, verifica dependencias y arranca main.py.
#
set -euo pipefail

# Movernos al directorio del script (para que las rutas relativas funcionen)
cd "$(dirname "$0")"

VENV_DIR="venv"

log()  { printf '\033[1;32m[run]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[run]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[run]\033[0m %s\n' "$*" >&2; }

# 1. Verificar python3
if ! command -v python3 >/dev/null 2>&1; then
    err "python3 no está instalado. Instalalo (p. ej. 'brew install python') y volvé a intentar."
    exit 1
fi
log "Usando $(python3 --version)"

# 2. Crear el entorno virtual si no existe
if [ ! -d "$VENV_DIR" ] || [ ! -f "$VENV_DIR/bin/activate" ]; then
    log "Creando entorno virtual en ./$VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
else
    log "Entorno virtual ya existe."
fi

# 3. Activar el venv
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# 4. Actualizar pip e instalar dependencias (main.py usa solo la stdlib,
#    pero respetamos requirements.txt si existe)
log "Actualizando pip..."
python3 -m pip install --upgrade pip >/dev/null

if [ -f "requirements.txt" ]; then
    log "Instalando dependencias de requirements.txt ..."
    python3 -m pip install -r requirements.txt
else
    log "No hay requirements.txt (main.py solo usa la librería estándar)."
fi

# 5. Verificar FFmpeg (necesario para grabar los segmentos .mp4)
if ! command -v ffmpeg >/dev/null 2>&1; then
    warn "FFmpeg NO está instalado. Es necesario para grabar."
    if command -v brew >/dev/null 2>&1; then
        warn "Instalalo con:  brew install ffmpeg"
    else
        warn "Instalá FFmpeg desde https://ffmpeg.org/download.html"
    fi
    err "Abortando: instalá FFmpeg y volvé a correr ./run.sh"
    exit 1
fi
log "FFmpeg OK -> $(ffmpeg -version | head -n1)"

# 6. Arrancar el visor nativo (ventana con la grilla de cámaras)
log "Iniciando visor de cámaras... (q o ESC para salir, f pantalla completa)"
exec python3 viewer.py
