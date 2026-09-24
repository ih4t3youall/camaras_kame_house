#!/usr/bin/env python3
"""
Visor nativo de cámaras con PySide6 (Qt), pantalla completa.

  - Grilla que ocupa toda la pantalla, dividida según la cantidad de cámaras.
  - Doble clic sobre una cámara -> pantalla completa única. Otro doble clic -> volver.
  - Zoom: botones translúcidos (+ / -), rueda del mouse, y PINCH del trackpad.
  - Arrastrar con el mouse -> moverse por la imagen cuando hay zoom.

Teclas:
  Q / ESC -> salir      F -> pantalla completa on/off      R -> reset zoom
"""
import os
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import sys
import time
import math
import logging
import threading
from datetime import datetime

import cv2
import numpy as np

from PySide6.QtCore import Qt, QTimer, QRect, QEvent, QPointF
from PySide6.QtGui import QImage, QPainter, QColor, QFont
from PySide6.QtWidgets import QApplication, QWidget

from main import CameraServer

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Viewer")

ZOOM_MIN, ZOOM_MAX = 1.0, 8.0
ZOOM_BTN_STEP = 1.35
ZOOM_WHEEL_STEP = 1.0015   # por cada "delta" de rueda (se eleva a angleDelta/8)
BTN = 46                   # tamaño de botón translúcido


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


class CameraStream:
    """Lee un stream RTSP en un hilo y mantiene el último frame (BGR)."""

    def __init__(self, name, url):
        self.name = name
        self.url = url
        self.frame = None
        self.lock = threading.Lock()
        self.running = True
        self.connected = False
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _loop(self):
        while self.running:
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            if not cap.isOpened():
                self.connected = False
                time.sleep(3)
                continue
            self.connected = True
            logger.info(f"{self.name}: conectada")
            fail = 0
            while self.running:
                ok, frame = cap.read()
                if not ok or frame is None:
                    fail += 1
                    if fail > 30:
                        break
                    time.sleep(0.05)
                    continue
                fail = 0
                with self.lock:
                    self.frame = frame
            cap.release()
            self.connected = False
            if self.running:
                logger.warning(f"{self.name}: reconectando...")
                time.sleep(2)

    def read(self):
        with self.lock:
            return None if self.frame is None else self.frame

    def stop(self):
        self.running = False


class VideoWall(QWidget):
    def __init__(self, streams):
        super().__init__()
        self.streams = streams
        self.n = len(streams)
        self.cols = math.ceil(math.sqrt(self.n))
        self.rows = math.ceil(self.n / self.cols)
        self.zoom = {i: [1.0, 0.5, 0.5] for i in range(self.n)}  # [z, cx, cy]
        self.mode = "grid"
        self.selected = 0
        self.drag = None
        self.setWindowTitle("Cámaras")
        self.setMouseTracking(True)
        self.setStyleSheet("background:#121214;")
        self.setAttribute(Qt.WA_AcceptTouchEvents, True)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)
        self.timer.start(33)  # ~30 fps

    # ---------------- Layout de celdas ---------------- #
    def _cell_rect(self, i):
        cw = self.width() // self.cols
        ch = self.height() // self.rows
        col, row = i % self.cols, i // self.cols
        return QRect(col * cw, row * ch, cw, ch)

    def _cell_at(self, x, y):
        cw = self.width() // self.cols
        ch = self.height() // self.rows
        if cw == 0 or ch == 0:
            return None
        col, row = x // cw, y // ch
        if col >= self.cols or row >= self.rows:
            return None
        idx = row * self.cols + col
        return idx if idx < self.n else None

    def _view_rect(self, idx):
        """Rectángulo (en píxeles del widget) donde se dibuja la cámara idx."""
        if self.mode == "single":
            return idx == self.selected, QRect(0, 0, self.width(), self.height())
        return True, self._cell_rect(idx)

    def _buttons(self, rect):
        """Botones translúcidos para un rectángulo de vista dado."""
        b = {}
        y = rect.bottom() - BTN - 12
        b["plus"] = QRect(rect.right() - BTN - 12, y, BTN, BTN)
        b["minus"] = QRect(rect.right() - 2 * BTN - 20, y, BTN, BTN)
        if self.mode == "single":
            b["reset"] = QRect(rect.right() - 3 * BTN - 28, y, BTN, BTN)
            b["back"] = QRect(rect.left() + 16, rect.top() + 16, BTN, BTN)
        return b

    # ---------------- Zoom / recorte ---------------- #
    def _crop_geom(self, idx, frame, out_w, out_h):
        z, cx, cy = self.zoom[idx]
        fh, fw = frame.shape[:2]
        out_aspect = out_w / out_h
        if fw / fh > out_aspect:
            base_h, base_w = fh, int(fh * out_aspect)
        else:
            base_w, base_h = fw, int(fw / out_aspect)
        cw = max(16, int(base_w / z))
        ch = max(16, int(base_h / z))
        left = int(clamp(cx * fw - cw / 2, 0, fw - cw))
        top = int(clamp(cy * fh - ch / 2, 0, fh - ch))
        return left, top, cw, ch, fw, fh

    def _zoom_towards(self, idx, factor, u, v, out_w, out_h):
        frame = self.streams[idx].read()
        z, cx, cy = self.zoom[idx]
        if frame is not None:
            left, top, cw, ch, fw, fh = self._crop_geom(idx, frame, out_w, out_h)
            cx = (left + u * cw) / fw
            cy = (top + v * ch) / fh
        new_z = clamp(z * factor, ZOOM_MIN, ZOOM_MAX)
        if new_z <= 1.0:
            self.zoom[idx] = [1.0, 0.5, 0.5]
        else:
            self.zoom[idx] = [new_z, clamp(cx, 0, 1), clamp(cy, 0, 1)]

    def _reset(self, idx):
        self.zoom[idx] = [1.0, 0.5, 0.5]

    # ---------------- Dibujo ---------------- #
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.fillRect(self.rect(), QColor(18, 18, 20))
        indices = [self.selected] if self.mode == "single" else range(self.n)
        for idx in indices:
            _, rect = self._view_rect(idx)
            self._draw_camera(p, idx, rect)
        p.end()

    def _draw_camera(self, p, idx, rect):
        stream = self.streams[idx]
        frame = stream.read()
        if frame is None:
            p.fillRect(rect, QColor(18, 18, 20))
            p.setPen(QColor(150, 150, 160))
            p.setFont(QFont("Helvetica", 16))
            p.drawText(rect, Qt.AlignCenter,
                       "conectando..." if stream.running else "sin señal")
        else:
            left, top, cw, ch, fw, fh = self._crop_geom(idx, frame, rect.width(), rect.height())
            crop = np.ascontiguousarray(frame[top:top + ch, left:left + cw])
            h, w = crop.shape[:2]
            img = QImage(crop.data, w, h, 3 * w, QImage.Format_BGR888).copy()
            p.drawImage(rect, img)

        # Etiqueta
        z = self.zoom[idx][0]
        p.fillRect(QRect(rect.left(), rect.top(), max(160, len(stream.name) * 12 + 60), 30),
                   QColor(0, 0, 0, 150))
        p.setFont(QFont("Helvetica", 12, QFont.Bold))
        p.setPen(QColor(90, 220, 110) if stream.connected else QColor(120, 120, 130))
        label = stream.name + (f"   {z:.1f}x" if z > 1.0 else "")
        p.drawText(rect.left() + 10, rect.top() + 20, label)

        # Reloj (fecha y hora) arriba a la derecha
        now = datetime.now().strftime("%d/%m/%Y  %H:%M:%S")
        big = self.mode == "single"
        p.setFont(QFont("Menlo", 15 if big else 12, QFont.Bold))
        tw = p.fontMetrics().horizontalAdvance(now)
        cw_box = tw + 20
        p.fillRect(QRect(rect.right() - cw_box - 8, rect.top() + 8, cw_box, 26 if big else 24),
                   QColor(0, 0, 0, 150))
        p.setPen(QColor(255, 235, 120))
        p.drawText(rect.right() - cw_box - 8 + 10,
                   rect.top() + (26 if big else 24) - 6 + 8, now)

        # Botones translúcidos
        for name, br in self._buttons(rect).items():
            self._draw_button(p, br, {"plus": "+", "minus": "−", "reset": "R", "back": "‹"}[name])

        # Ayuda
        p.setPen(QColor(180, 180, 190))
        p.setFont(QFont("Helvetica", 11))
        if self.mode == "single" and idx == self.selected:
            p.drawText(rect.left() + 10, rect.bottom() - 12, "doble clic: volver")
        elif self.mode == "grid" and idx == self.n - 1:
            p.drawText(rect.left() + 10, rect.bottom() - 12,
                       "doble clic: ampliar  |  rueda/pinch o +/-: zoom  |  arrastrar: mover")

    @staticmethod
    def _draw_button(p, r, label):
        p.save()
        p.setOpacity(0.55)
        p.setBrush(QColor(45, 45, 52))
        p.setPen(QColor(210, 210, 215))
        p.drawRoundedRect(r, 8, 8)
        p.setOpacity(1.0)
        p.setPen(QColor(240, 240, 245))
        p.setFont(QFont("Helvetica", 22 if len(label) == 1 else 14, QFont.Bold))
        p.drawText(r, Qt.AlignCenter, label)
        p.restore()

    # ---------------- Interacción ---------------- #
    def _target_at(self, pos):
        """Devuelve (idx, rect, u, v) del punto pos, o None."""
        x, y = int(pos.x()), int(pos.y())
        if self.mode == "single":
            idx = self.selected
            rect = QRect(0, 0, self.width(), self.height())
        else:
            idx = self._cell_at(x, y)
            if idx is None:
                return None
            rect = self._cell_rect(idx)
        u = (x - rect.left()) / max(1, rect.width())
        v = (y - rect.top()) / max(1, rect.height())
        return idx, rect, u, v

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        t = self._target_at(e.position())
        if t is None:
            return
        idx, rect, u, v = t
        btns = self._buttons(rect)
        x, y = int(e.position().x()), int(e.position().y())
        for name, br in btns.items():
            if br.contains(x, y):
                if name == "plus":
                    self._zoom_towards(idx, ZOOM_BTN_STEP, 0.5, 0.5, rect.width(), rect.height())
                elif name == "minus":
                    self._zoom_towards(idx, 1 / ZOOM_BTN_STEP, 0.5, 0.5, rect.width(), rect.height())
                elif name == "reset":
                    self._reset(idx)
                elif name == "back":
                    self.mode = "grid"
                    self.drag = None
                return
        # Sin botón: iniciar arrastre si hay zoom
        if self.zoom[idx][0] > 1.0:
            self.drag = {"idx": idx, "x": x, "y": y, "w": rect.width(), "h": rect.height()}

    def mouseMoveEvent(self, e):
        if self.drag is None:
            return
        d = self.drag
        idx = d["idx"]
        frame = self.streams[idx].read()
        if frame is None:
            return
        left, top, cw, ch, fw, fh = self._crop_geom(idx, frame, d["w"], d["h"])
        z, cx, cy = self.zoom[idx]
        dx = int(e.position().x()) - d["x"]
        dy = int(e.position().y()) - d["y"]
        cx -= (dx / d["w"]) * cw / fw
        cy -= (dy / d["h"]) * ch / fh
        self.zoom[idx] = [z, clamp(cx, 0, 1), clamp(cy, 0, 1)]
        d["x"], d["y"] = int(e.position().x()), int(e.position().y())

    def mouseReleaseEvent(self, _):
        self.drag = None

    def mouseDoubleClickEvent(self, e):
        t = self._target_at(e.position())
        if t is None:
            return
        idx, rect, _, _ = t
        # No togglear si el doble clic cayó sobre un botón
        x, y = int(e.position().x()), int(e.position().y())
        if any(br.contains(x, y) for br in self._buttons(rect).values()):
            return
        if self.mode == "grid":
            self.selected = idx
            self.mode = "single"
        else:
            self.mode = "grid"
        self.drag = None

    def wheelEvent(self, e):
        t = self._target_at(e.position())
        if t is None:
            return
        idx, rect, u, v = t
        delta = e.angleDelta().y()
        if delta == 0:
            return
        factor = ZOOM_WHEEL_STEP ** delta
        self._zoom_towards(idx, factor, u, v, rect.width(), rect.height())

    def event(self, e):
        # Pinch del trackpad (gesto nativo de macOS)
        if e.type() == QEvent.NativeGesture and e.gestureType() == Qt.ZoomNativeGesture:
            pos = e.position() if hasattr(e, "position") else QPointF(self.mapFromGlobal(e.globalPosition().toPoint()))
            t = self._target_at(pos)
            if t is not None:
                idx, rect, u, v = t
                self._zoom_towards(idx, 1.0 + e.value(), u, v, rect.width(), rect.height())
            return True
        return super().event(e)

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key_Q, Qt.Key_Escape):
            self.close()
        elif k == Qt.Key_F:
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
        elif k == Qt.Key_R:
            if self.mode == "single":
                self._reset(self.selected)
            else:
                for i in range(self.n):
                    self._reset(i)

    def closeEvent(self, e):
        for s in self.streams:
            s.stop()
        e.accept()


def main():
    server = CameraServer()
    if not server.check_ffmpeg():
        logger.error("Falta FFmpeg/ffprobe.")
        return 1

    channels = server.detect_cameras()
    if not channels:
        logger.error("No se detectó ninguna cámara. Revisá IP/usuario/clave o el rango de canales.")
        return 1

    streams = [
        CameraStream(f"Camara {ch}", server.camera_rtsp_url(ch)).start()
        for ch in channels
    ]

    logger.info(f"Abriendo visor Qt ({len(channels)} cámara/s)... "
                "(doble clic: ampliar | rueda/pinch: zoom | Q/ESC: salir)")
    app = QApplication(sys.argv)
    wall = VideoWall(streams)
    wall.showFullScreen()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
