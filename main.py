import subprocess
import os
import signal
import time
import logging
import threading
import webbrowser
import urllib.request
from pathlib import Path
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

# Configuración de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("CameraServer")

# Credenciales del NVR: se leen de config.py (no versionado). Si no existe,
# se lanza un pequeño instalador interactivo que las pide y lo genera.
import sys
import getpass

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")


def _write_config(ip, user, password):
    with open(_CONFIG_PATH, "w") as f:
        f.write(
            "# Configuración local del NVR — NO se sube a git (ver .gitignore).\n"
            f'SERVER_IP = "{ip}"\n'
            f'SERVER_USER = "{user}"\n'
            f'SERVER_PASSWORD = "{password}"\n'
        )
    logger.info(f"Configuración guardada en {_CONFIG_PATH}")


def _run_installer():
    if not (sys.stdin and sys.stdin.isatty()):
        raise SystemExit(
            "Falta config.py y no hay terminal interactiva.\n"
            "Copiá la plantilla y completá tus datos:  cp config.example.py config.py"
        )
    print("\n== Configuración inicial de camaras_kame_house ==")
    print("(se guarda en config.py y no se vuelve a preguntar)\n")
    ip = input("IP del NVR: ").strip()
    while not ip:
        ip = input("La IP no puede quedar vacía. IP del NVR: ").strip()
    user = input("Usuario [admin]: ").strip() or "admin"
    password = getpass.getpass("Contraseña: ")
    _write_config(ip, user, password)
    return ip, user, password


def _load_config():
    try:
        import config as _cfg
        return _cfg.SERVER_IP, _cfg.SERVER_USER, _cfg.SERVER_PASSWORD
    except (ImportError, AttributeError):
        return _run_installer()


_SERVER_IP, _SERVER_USER, _SERVER_PASSWORD = _load_config()


class CameraServer:
    # ---- Datos del servidor/NVR de cámaras (Dahua, modo pull) ----
    SERVER_IP = _SERVER_IP
    SERVER_USER = _SERVER_USER
    SERVER_PASSWORD = _SERVER_PASSWORD
    RTSP_PORT = 554
    HTTP_PORT = 80         # puerto web/CGI del NVR (para detectar cámaras)
    SUBTYPE = 0            # 0 = stream principal, 1 = substream (más liviano)
    MAX_CHANNELS = 8       # canales del NVR (DHI-NVR1108HS = 8 canales)
    PROBE_TIMEOUT = 8      # segundos máximos por canal al detectar

    # ---- Puertos del servidor ----
    DASHBOARD_PORT = 8080  # pantalla web con la grilla de cámaras
    HLS_PORT = 8888        # HLS que sirve MediaMTX

    def __init__(self, config_path="./mediamtx.yml", storage_path="./storage", web_path="./web"):
        self.config_path = config_path
        self.storage_path = storage_path
        self.web_path = web_path
        self.mediamtx_process = None
        self.http_server = None
        self.http_thread = None
        self.cameras = []  # lista de canales detectados como funcionando

        Path(self.storage_path).mkdir(exist_ok=True)
        Path(self.web_path).mkdir(exist_ok=True)

    # ------------------------------------------------------------------ #
    # URLs
    # ------------------------------------------------------------------ #
    def camera_rtsp_url(self, channel):
        """URL RTSP de un canal de la cámara/NVR Dahua."""
        return (
            f"rtsp://{self.SERVER_USER}:{self.SERVER_PASSWORD}"
            f"@{self.SERVER_IP}:{self.RTSP_PORT}"
            f"/cam/realmonitor?channel={channel}&subtype={self.SUBTYPE}"
        )

    def path_name(self, channel):
        return f"camara{channel}"

    # ------------------------------------------------------------------ #
    # Detección de cámaras
    # ------------------------------------------------------------------ #
    def _http_opener(self):
        """Opener HTTP con autenticación Digest (la que usa el NVR Dahua)."""
        base = f"http://{self.SERVER_IP}:{self.HTTP_PORT}"
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, base, self.SERVER_USER, self.SERVER_PASSWORD)
        return urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(mgr),
            urllib.request.HTTPBasicAuthHandler(mgr),
        )

    def probe_channel(self, channel):
        """True si el canal tiene una cámara viva.

        En un NVR Dahua los canales vacíos CUELGAN el RTSP, así que la
        detección se hace con el snapshot CGI por HTTP: solo los canales
        con cámara conectada devuelven una imagen JPEG.
        """
        url = (f"http://{self.SERVER_IP}:{self.HTTP_PORT}"
               f"/cgi-bin/snapshot.cgi?channel={channel}")
        try:
            resp = self._http_opener().open(url, timeout=self.PROBE_TIMEOUT)
            data = resp.read(4)
            ok = resp.status == 200 and data[:2] == b"\xff\xd8"  # cabecera JPEG
            if ok:
                logger.info(f"  Canal {channel}: cámara detectada")
            return ok
        except Exception:
            # 400/404/timeout => canal sin cámara
            return False

    def detect_cameras(self):
        """Escanea los canales 1..MAX_CHANNELS y devuelve los que tienen cámara."""
        logger.info(f"Detectando cámaras en {self.SERVER_IP} (canales 1..{self.MAX_CHANNELS})...")
        working = []
        for ch in range(1, self.MAX_CHANNELS + 1):
            if self.probe_channel(ch):
                working.append(ch)
        self.cameras = working
        if working:
            logger.info(f"Cámaras detectadas en canales: {working}")
        else:
            logger.warning("No se detectó ninguna cámara funcionando.")
        return working

    # ------------------------------------------------------------------ #
    # Configuración de MediaMTX
    # ------------------------------------------------------------------ #
    def generate_config(self):
        """Genera mediamtx.yml con un path por cada cámara detectada."""
        header = f"""
# Configuración de mediamtx generada automáticamente (Dahua, modo pull)
logLevel: info
logDestinations: [stdout]
readTimeout: 20s
writeTimeout: 20s
readBufferCount: 512
api: yes
apiAddress: :9997
metrics: no
pprof: no
rtspAddress: :8554
rtmpAddress: :1935
hlsAddress: :{self.HLS_PORT}
hlsAllowOrigin: '*'
webrtcAddress: :8889

paths:
"""
        paths = ""
        for ch in self.cameras:
            name = self.path_name(ch)
            paths += f"""  {name}:
    source: {self.camera_rtsp_url(ch)}
    sourceProtocol: tcp
    sourceOnDemand: true
    sourceOnDemandStartTimeout: 10s
    # Grabación en segmentos de 1 hora
    runOnReady: ffmpeg -rtsp_transport tcp -i rtsp://localhost:$RTSP_PORT/$MTX_PATH -c copy -f segment -segment_time 3600 -reset_timestamps 1 -strftime 1 {self.storage_path}/{name}_%Y-%m-%d_%H-%M-%S.mp4
    runOnReadyRestart: yes
"""
        with open(self.config_path, 'w') as f:
            f.write(header + paths)
        logger.info(f"Configuración generada en {self.config_path} ({len(self.cameras)} cámara/s)")

    # ------------------------------------------------------------------ #
    # Pantalla / dashboard web
    # ------------------------------------------------------------------ #
    def generate_dashboard(self):
        """Genera el index.html con la grilla de todas las cámaras."""
        cams_js = ",".join(
            f'{{name:"Cámara {ch}", path:"{self.path_name(ch)}"}}' for ch in self.cameras
        )
        html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cámaras</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#0e0e10; color:#e8e8e8;
         font-family:-apple-system,Segoe UI,Roboto,sans-serif; }}
  header {{ padding:14px 20px; background:#17171b; border-bottom:1px solid #262630;
           display:flex; align-items:center; gap:12px; }}
  header h1 {{ font-size:18px; margin:0; font-weight:600; }}
  header .count {{ color:#8a8a94; font-size:13px; }}
  .grid {{ display:grid; gap:12px; padding:16px;
          grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); }}
  .cam {{ background:#000; border:1px solid #262630; border-radius:10px; overflow:hidden;
         position:relative; aspect-ratio:16/9; }}
  .cam video {{ width:100%; height:100%; object-fit:contain; background:#000; display:block; }}
  .cam .label {{ position:absolute; top:8px; left:8px; background:rgba(0,0,0,.6);
               padding:3px 8px; border-radius:6px; font-size:12px; }}
  .cam .status {{ position:absolute; bottom:8px; right:8px; font-size:11px;
                background:rgba(0,0,0,.6); padding:2px 7px; border-radius:6px; color:#9aa; }}
  .empty {{ padding:60px 20px; text-align:center; color:#8a8a94; }}
</style>
</head>
<body>
<header>
  <h1>🎥 Servidor de cámaras</h1>
  <span class="count" id="count"></span>
</header>
<div class="grid" id="grid"></div>
<script>
const CAMERAS = [{cams_js}];
const HLS_PORT = {self.HLS_PORT};
const host = location.hostname || "localhost";
const grid = document.getElementById("grid");
document.getElementById("count").textContent = CAMERAS.length + " cámara(s) detectada(s)";

if (CAMERAS.length === 0) {{
  grid.innerHTML = '<div class="empty">No se detectaron cámaras funcionando.</div>';
}}

CAMERAS.forEach(cam => {{
  const src = `http://${{host}}:${{HLS_PORT}}/${{cam.path}}/index.m3u8`;
  const box = document.createElement("div");
  box.className = "cam";
  box.innerHTML = `<div class="label">${{cam.name}}</div>
                   <div class="status">conectando…</div>
                   <video muted autoplay playsinline></video>`;
  grid.appendChild(box);
  const video = box.querySelector("video");
  const status = box.querySelector(".status");

  function setStatus(t) {{ status.textContent = t; }}

  if (Hls.isSupported()) {{
    const hls = new Hls({{ liveSyncDurationCount: 2, lowLatencyMode: true }});
    hls.loadSource(src);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => {{ setStatus("en vivo"); video.play().catch(()=>{{}}); }});
    hls.on(Hls.Events.ERROR, (e, data) => {{
      if (data.fatal) {{ setStatus("reintentando…"); setTimeout(()=>{{ hls.loadSource(src); }}, 3000); }}
    }});
  }} else if (video.canPlayType("application/vnd.apple.mpegurl")) {{
    // Safari
    video.src = src;
    video.addEventListener("loadedmetadata", () => setStatus("en vivo"));
  }} else {{
    setStatus("navegador sin soporte HLS");
  }}
}});
</script>
</body>
</html>"""
        out = Path(self.web_path) / "index.html"
        out.write_text(html, encoding="utf-8")
        logger.info(f"Dashboard generado en {out}")

    def start_dashboard(self):
        """Sirve la pantalla web en un hilo aparte."""
        self.generate_dashboard()
        handler = partial(SimpleHTTPRequestHandler, directory=self.web_path)
        self.http_server = ThreadingHTTPServer(("0.0.0.0", self.DASHBOARD_PORT), handler)
        self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        self.http_thread.start()
        url = f"http://localhost:{self.DASHBOARD_PORT}"
        logger.info(f"Pantalla disponible en {url}")
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def stop_dashboard(self):
        if self.http_server:
            self.http_server.shutdown()
            logger.info("Pantalla web detenida")

    # ------------------------------------------------------------------ #
    # MediaMTX
    # ------------------------------------------------------------------ #
    def start_mediamtx(self):
        self.generate_config()
        try:
            self.mediamtx_process = subprocess.Popen(
                ["./mediamtx", self.config_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                preexec_fn=os.setsid
            )
            logger.info("Servidor mediamtx iniciado")
            time.sleep(2)
            if self.mediamtx_process.poll() is not None:
                _, stderr = self.mediamtx_process.communicate()
                logger.error(f"Error al iniciar mediamtx: {stderr.decode()}")
                return False
            return True
        except Exception as e:
            logger.error(f"Error al iniciar mediamtx: {e}")
            return False

    def stop_mediamtx(self):
        if self.mediamtx_process:
            try:
                os.killpg(os.getpgid(self.mediamtx_process.pid), signal.SIGTERM)
                logger.info("Servidor mediamtx detenido")
            except Exception as e:
                logger.error(f"Error al detener mediamtx: {e}")

    # ------------------------------------------------------------------ #
    # Utilidades
    # ------------------------------------------------------------------ #
    def check_ffmpeg(self):
        for tool in ("ffmpeg", "ffprobe"):
            try:
                subprocess.run([tool, "-version"], capture_output=True, check=True)
            except (subprocess.CalledProcessError, FileNotFoundError):
                logger.error(f"{tool} no está instalado. Es necesario para procesar/detectar streams.")
                return False
        logger.info("FFmpeg/ffprobe OK")
        return True

    def list_recordings(self):
        recordings = []
        storage_path = Path(self.storage_path)
        if storage_path.exists():
            for file in storage_path.iterdir():
                if file.suffix == '.mp4':
                    recordings.append({
                        'name': file.name, 'path': str(file),
                        'size': file.stat().st_size, 'modified': file.stat().st_mtime,
                    })
        return recordings


def download_mediamtx():
    """Descarga mediamtx si no está presente (macOS ARM64)."""
    import urllib.request
    import tarfile

    if not os.path.exists("mediamtx"):
        logger.info("Descargando mediamtx...")
        try:
            url = "https://github.com/bluenviron/mediamtx/releases/download/v1.12.0/mediamtx_v1.12.0_darwin_arm64.tar.gz"
            urllib.request.urlretrieve(url, "mediamtx.tar.gz")
            with tarfile.open("mediamtx.tar.gz", "r:gz") as tar:
                tar.extractall()
            os.chmod("mediamtx", 0o755)
            logger.info("mediamtx descargado y preparado")
        except Exception as e:
            logger.error(f"Error al descargar mediamtx: {e}")
            return False
    return True


if __name__ == "__main__":
    if not download_mediamtx():
        exit(1)

    server = CameraServer()

    if not server.check_ffmpeg():
        logger.error("Por favor, instala FFmpeg (incluye ffprobe) antes de continuar")
        exit(1)

    # 1. Detectar qué cámaras funcionan
    if not server.detect_cameras():
        logger.error("No hay cámaras para mostrar. Revisá IP/usuario/clave o el rango de canales.")
        exit(1)

    try:
        # 2. Levantar MediaMTX con las cámaras detectadas
        if server.start_mediamtx():
            # 3. Abrir la pantalla web con la grilla
            server.start_dashboard()
            logger.info("Todo listo. Abrí la pantalla en el navegador (Ctrl+C para detener).")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Deteniendo servidor...")
    finally:
        server.stop_dashboard()
        server.stop_mediamtx()
