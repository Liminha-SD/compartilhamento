#!/usr/bin/env python3
"""
Screen Share Server - WebRTC para OBS Browser Source
Interface gráfica PySide6 para configuração e monitoramento.
"""

import sys
print("Carregando módulos...", flush=True)

import asyncio
import fractions
import json
import logging
import queue as _queue
import socket
import threading
import time
from dataclasses import dataclass

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import av
import mss
import numpy as np
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import AudioStreamTrack, VideoStreamTrack

log = logging.getLogger(__name__)

# ─── Cursor Windows ───────────────────────────────────────────────────────────

if sys.platform == "win32":
    try:
        import win32gui, win32ui, win32con
        WIN32_OK = True
    except ImportError:
        WIN32_OK = False
        log.warning("pywin32 não instalado — cursor não será capturado")
else:
    WIN32_OK = False


def _grab_win32_cursor(left: int, top: int, width: int, height: int) -> np.ndarray:
    hdesktop   = win32gui.GetDesktopWindow()
    desktop_dc = win32gui.GetWindowDC(hdesktop)
    src_dc     = win32ui.CreateDCFromHandle(desktop_dc)
    mem_dc     = src_dc.CreateCompatibleDC()
    bmp        = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src_dc, width, height)
    mem_dc.SelectObject(bmp)
    mem_dc.BitBlt((0, 0), (width, height), src_dc, (left, top), win32con.SRCCOPY)
    try:
        flags, hcursor, (cx, cy) = win32gui.GetCursorInfo()
        if flags & 0x1:
            win32gui.DrawIconEx(mem_dc.GetSafeHdc(), cx - left, cy - top,
                                hcursor, 0, 0, 0, None, win32con.DI_NORMAL)
    except Exception:
        pass
    bmpstr = bmp.GetBitmapBits(True)
    img = np.frombuffer(bmpstr, dtype=np.uint8).reshape(height, width, 4).copy()
    mem_dc.DeleteDC()
    win32gui.DeleteObject(bmp.GetHandle())
    win32gui.ReleaseDC(hdesktop, desktop_dc)
    return img


# ─── Video Track ──────────────────────────────────────────────────────────────

class ScreenVideoTrack(VideoStreamTrack):
    """Captura contínua em thread dedicada — frame sempre pronto para WebRTC."""

    _V_CLOCK = 90000  # relógio de vídeo WebRTC padrão: 90 kHz

    def __init__(self, monitor: int = 1, fps: int = 60, scale_width: int = 0):
        super().__init__()
        self._monitor = monitor
        self._fps     = fps
        self._scale_w = scale_width
        self._latest: np.ndarray | None = None
        self._lock      = threading.Lock()
        self._new_frame = asyncio.Event()
        self._loop      = asyncio.get_running_loop()
        self._v_ts:    int   = 0
        self._v_start: float = 0.0
        threading.Thread(target=self._capture_loop, daemon=True).start()

    def _capture_loop(self) -> None:
        with mss.MSS() as sct:
            mon     = sct.monitors[self._monitor]
            left, top, w, h = mon["left"], mon["top"], mon["width"], mon["height"]
            interval = 1.0 / (self._fps + 10)
            sw = self._scale_w
            while True:
                t0 = time.monotonic()
                if WIN32_OK:
                    img = _grab_win32_cursor(left, top, w, h)
                else:
                    raw = sct.grab(mon)
                    img = np.frombuffer(raw.raw, dtype=np.uint8).reshape(h, w, 4)
                if sw and img.shape[1] != sw:
                    sh  = int(h * sw / w) & ~1
                    tmp = av.VideoFrame.from_ndarray(img, format="bgra")
                    img = tmp.reformat(width=sw, height=sh).to_ndarray(format="bgra")
                with self._lock:
                    self._latest = img
                self._loop.call_soon_threadsafe(self._new_frame.set)
                elapsed = time.monotonic() - t0
                if interval - elapsed > 0:
                    time.sleep(interval - elapsed)

    async def recv(self) -> av.VideoFrame:
        ptime = 1.0 / self._fps
        if self._v_start == 0.0:
            # primeiro frame: espera ao menos um frame capturado
            await self._new_frame.wait()
            self._new_frame.clear()
            self._v_start = time.time()
        else:
            # pacing primeiro — depois pega o frame mais recente disponível
            self._v_ts += int(ptime * self._V_CLOCK)
            wait = self._v_start + (self._v_ts / self._V_CLOCK) - time.time()
            if wait > 0:
                await asyncio.sleep(wait)

        with self._lock:
            img = self._latest

        frame = av.VideoFrame.from_ndarray(img, format="bgra")
        frame.pts       = self._v_ts
        frame.time_base = fractions.Fraction(1, self._V_CLOCK)
        return frame


# ─── Audio Track ──────────────────────────────────────────────────────────────

try:
    import pyaudiowpatch as _pawp
    AUDIO_OK = True
except ImportError:
    _pawp    = None
    AUDIO_OK = False
    log.warning("pyaudiowpatch não instalado — áudio desativado")


def get_loopback_devices() -> dict[str, str]:
    """Retorna {nome_dispositivo: nome_dispositivo} dos dispositivos de áudio."""
    if not AUDIO_OK:
        return {}
    pa      = _pawp.PyAudio()
    devices: dict[str, str] = {}
    try:
        loopback_indices: set[int] = set()
        for lb in pa.get_loopback_device_info_generator():
            loopback_indices.add(int(lb["index"]))
            devices[lb["name"]] = lb["name"]
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if int(info["maxInputChannels"]) > 0 and i not in loopback_indices:
                devices[info["name"]] = info["name"]
    finally:
        pa.terminate()
    return devices


if AUDIO_OK:
    class SystemAudioTrack(AudioStreamTrack):
        CHUNK = 960  # 20 ms @ 48 kHz (Opus standard frame)

        def __init__(self, device_name: str = ""):
            super().__init__()
            self._device_name = device_name
            self._sample_rate = 48000
            self._time_base   = fractions.Fraction(1, 48000)
            self._pts         = 0
            # fila com 3 slots (~60ms) — absorve jitter do encoding H264
            # drop-oldest: descarta o mais antigo quando cheia, nunca o mais novo
            self._queue: asyncio.Queue = asyncio.Queue(maxsize=3)
            self._loop  = asyncio.get_running_loop()
            self._stop  = threading.Event()
            threading.Thread(target=self._capture, daemon=True).start()

        def stop(self):
            self._stop.set()
            super().stop()

        def _open(self, pa):
            """Abre stream loopback WASAPI. Retorna (stream, nome, canais, rate, chunk)."""
            dev = None

            # 1) Tenta encontrar pelo nome selecionado na GUI
            if self._device_name:
                for i in range(pa.get_device_count()):
                    info = pa.get_device_info_by_index(i)
                    if self._device_name in info["name"] or info["name"] in self._device_name:
                        dev = info
                        break

            # 2) Auto-detect: loopback que corresponde à saída padrão WASAPI
            if dev is None:
                try:
                    wasapi  = pa.get_host_api_info_by_type(_pawp.paWASAPI)
                    default = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
                    for lb in pa.get_loopback_device_info_generator():
                        if default["name"] in lb["name"]:
                            dev = lb
                            break
                except Exception:
                    pass

            # 3) Qualquer loopback disponível
            if dev is None:
                for lb in pa.get_loopback_device_info_generator():
                    dev = lb
                    break

            if dev is None:
                raise RuntimeError("Nenhum dispositivo loopback WASAPI encontrado")

            # Usa o número nativo de canais do dispositivo — WASAPI loopback
            # não aceita valor diferente do nativo (pode ser 6, 8, etc.)
            ch_in  = int(dev.get("maxInputChannels") or 0)
            ch_out = int(dev.get("maxOutputChannels") or 0)
            native_ch = ch_in if ch_in > 0 else (ch_out if ch_out > 0 else 2)

            # Força 48 kHz — Opus aceita apenas 8/12/16/24/48 kHz (não 44100 Hz)
            # WASAPI shared-mode faz o resampling automaticamente
            rate  = 48000
            chunk = self.CHUNK  # deve sempre bater com CHUNK para o PTS ficar correto

            stream = pa.open(
                format=_pawp.paFloat32,
                channels=native_ch,
                rate=rate,
                frames_per_buffer=chunk,
                input=True,
                input_device_index=int(dev["index"]),
            )
            return stream, dev["name"], native_ch, chunk

        def _capture(self) -> None:
            def _put_fresh(packed):
                # drop-oldest: descarta o mais antigo quando cheia, mantém o mais novo
                if self._queue.full():
                    try: self._queue.get_nowait()
                    except asyncio.QueueEmpty: pass
                try: self._queue.put_nowait(packed)
                except asyncio.QueueFull: pass

            while not self._stop.is_set():
                pa = stream = None
                try:
                    pa = _pawp.PyAudio()
                    stream, dev_name, ch, chunk = self._open(pa)
                    # descarta chunks acumulados antes do peer conectar
                    while not self._queue.empty():
                        try: self._queue.get_nowait()
                        except Exception: break
                    log.info(f"Áudio WASAPI: {dev_name} @ 48000Hz {ch}ch")

                    while not self._stop.is_set():
                        raw  = stream.read(chunk, exception_on_overflow=False)
                        data = np.frombuffer(raw, dtype=np.float32).reshape(-1, ch)
                        if ch == 1:
                            stereo = np.repeat(data, 2, axis=1)
                        elif ch == 2:
                            stereo = data
                        else:
                            stereo = data[:, :2]
                        pcm    = (np.clip(stereo, -1.0, 1.0) * 32767).astype(np.int16)
                        packed = pcm.flatten().reshape(1, -1).copy()
                        self._loop.call_soon_threadsafe(_put_fresh, packed)

                except Exception as exc:
                    if not self._stop.is_set():
                        log.error(f"Áudio falhou: {exc} — tentando em 2s")
                        time.sleep(2)
                finally:
                    if stream:
                        try: stream.stop_stream(); stream.close()
                        except Exception: pass
                    if pa:
                        try: pa.terminate()
                        except Exception: pass

        async def recv(self) -> av.AudioFrame:
            try:
                packed = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                packed = np.zeros((1, self.CHUNK * 2), dtype=np.int16)

            frame = av.AudioFrame.from_ndarray(packed, format="s16", layout="stereo")
            frame.pts         = self._pts
            frame.time_base   = self._time_base
            frame.sample_rate = self._sample_rate
            self._pts        += self.CHUNK
            return frame


# ─── HTML ─────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Screen Share</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box }
    html, body { width: 100%; height: 100%; background: #000; overflow: hidden }
    video { width: 100%; height: 100%; object-fit: contain; display: block }
    #status {
      position: fixed; top: 8px; left: 8px;
      color: #0f0; font: 12px monospace;
      background: rgba(0,0,0,.6); padding: 4px 10px; border-radius: 4px;
    }
  </style>
</head>
<body>
  <video id="v" autoplay playsinline></video>
  <div id="status">Conectando…</div>
  <script>
  (async () => {
    const statusEl = document.getElementById('status');
    const videoEl  = document.getElementById('v');
    let pc = null, reconnectTimer = null, isConnecting = false;

    function status(msg, color) {
      statusEl.textContent = msg;
      statusEl.style.color = color || '#0f0';
      statusEl.style.display = '';
    }
    function scheduleReconnect(ms) {
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(() => {
        isConnecting = false;
        connect().catch(err => { status('Erro: ' + err.message, '#f44'); scheduleReconnect(3000); });
      }, ms);
    }
    async function connect() {
      if (isConnecting) return;
      isConnecting = true;
      clearTimeout(reconnectTimer);
      status('Conectando…', '#ff0');
      if (pc) { try { pc.close(); } catch (_) {} pc = null; }
      pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });
      const stream = new MediaStream();
      videoEl.srcObject = stream;
      pc.ontrack = e => {
        stream.addTrack(e.track);
        if (e.track.kind === 'video') {
          status('● Live', '#0f0');
          setTimeout(() => { statusEl.style.display = 'none'; }, 3000);
        }
      };
      pc.onconnectionstatechange = () => {
        const s = pc.connectionState;
        if (s === 'connected') { clearTimeout(reconnectTimer); isConnecting = false; }
        else if (s === 'failed') { status('⚠ falhou', '#f44'); scheduleReconnect(1500); }
        else if (s === 'disconnected') { status('⚠ desconectado', '#fa0'); scheduleReconnect(2000); }
      };
      pc.addTransceiver('video', { direction: 'recvonly' });
      pc.addTransceiver('audio', { direction: 'recvonly' });
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await new Promise(r => {
        if (pc.iceGatheringState === 'complete') return r();
        pc.addEventListener('icegatheringstatechange', () => { if (pc.iceGatheringState === 'complete') r(); });
        setTimeout(r, 2000);
      });
      const res = await fetch('/offer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type }),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      await pc.setRemoteDescription(await res.json());
      isConnecting = false;
    }
    connect().catch(err => { status('Erro: ' + err.message, '#f44'); scheduleReconnect(3000); });
  })();
  </script>
</body>
</html>"""


# ─── Config & estado global ───────────────────────────────────────────────────

@dataclass
class Config:
    host:         str = "0.0.0.0"
    port:         int = 1935
    monitor:      int = 1
    fps:          int = 60
    bitrate:      int = 8000
    scale:        int = 0
    audio_device: str = ""

_config = Config()
_pcs: set[RTCPeerConnection] = set()


# ─── HTTP handlers ────────────────────────────────────────────────────────────

async def handle_index(request: web.Request) -> web.Response:
    return web.Response(content_type="text/html", text=HTML)


def _patch_sdp_bitrate(sdp: str, kbps: int) -> str:
    """Patch SDP answer: inject video bitrate cap and Opus low-latency params."""
    lines = sdp.split("\r\n")
    out = []
    in_video = in_audio = False
    video_b_injected = False
    for line in lines:
        if line.startswith("m="):
            in_video = line.startswith("m=video")
            in_audio = line.startswith("m=audio")
            video_b_injected = False
        # inject b=AS before first a= line in the video section
        if in_video and not video_b_injected and line.startswith("a="):
            out.append(f"b=AS:{kbps}")
            video_b_injected = True
        # Opus: corrige parâmetros de packet loss e ptime
        # minptime/maxptime=20 → jitter buffer calibrado para nossos pacotes de 20ms
        # useinbandfec=1 → FEC embutido: pacote seguinte carrega cópia do perdido
        # usedtx=0 → nunca pula pacotes no silêncio (evita falso positivo de perda)
        # stereo=1 → habilita stereo no decoder do browser
        if in_audio and line.startswith("a=fmtp:") and "minptime" not in line:
            out.append(line + ";minptime=20;maxptime=20;useinbandfec=1;usedtx=0;stereo=1")
            continue
        out.append(line)
    return "\r\n".join(out)


async def handle_offer(request: web.Request) -> web.Response:
    body  = await request.json()
    offer = RTCSessionDescription(sdp=body["sdp"], type=body["type"])
    pc    = RTCPeerConnection()
    _pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_state():
        log.info(f"Peer [{id(pc) % 10000:04d}] {pc.connectionState}")
        if pc.connectionState in ("failed", "closed", "disconnected"):
            await pc.close(); _pcs.discard(pc)

    pc.addTrack(ScreenVideoTrack(monitor=_config.monitor, fps=_config.fps,
                                  scale_width=_config.scale))
    if AUDIO_OK:
        pc.addTrack(SystemAudioTrack(device_name=_config.audio_device))

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    patched = _patch_sdp_bitrate(pc.localDescription.sdp, _config.bitrate)
    return web.Response(
        content_type="application/json",
        text=json.dumps({"sdp": patched, "type": pc.localDescription.type}),
    )


# ─── GUI PySide6 ──────────────────────────────────────────────────────────────

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QPlainTextEdit,
    QFrame, QScrollArea, QSplitter,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QTextCursor

DARK_STYLE = """
QWidget { background:#1a1a1a; color:#e0e0e0; font-size:13px; }
QMainWindow { background:#1a1a1a; }
QScrollArea, QScrollBar { background:#1a1a1a; border:none; }
QScrollBar:vertical { width:8px; background:#222; }
QScrollBar::handle:vertical { background:#444; border-radius:4px; }
QLabel { color:#e0e0e0; }
QLabel#section { color:#aaa; font-size:11px; margin-top:6px; }
QLabel#title { font-size:16px; font-weight:bold; color:#fff; }
QLabel#status_ok  { color:#2ecc71; font-weight:bold; }
QLabel#status_off { color:#666; }
QLabel#urls { color:#6af; font-family:Courier,monospace; font-size:11px; }
QLineEdit, QComboBox {
    background:#2a2a2a; border:1px solid #444; border-radius:4px;
    padding:4px 8px; color:#e0e0e0;
}
QLineEdit:focus, QComboBox:focus { border-color:#3a7bd5; }
QComboBox::drop-down { border:none; width:22px; }
QComboBox QAbstractItemView { background:#2a2a2a; border:1px solid #444;
    selection-background-color:#3a7bd5; color:#e0e0e0; }
QPushButton {
    background:#2d5a9e; color:#fff; border:none;
    padding:6px 14px; border-radius:5px;
}
QPushButton:hover   { background:#3a6fbf; }
QPushButton:pressed { background:#1e4070; }
QPushButton#stop    { background:#9e2d2d; }
QPushButton#stop:hover { background:#bf3a3a; }
QPushButton#ghost {
    background:transparent; border:1px solid #444; color:#aaa;
    padding:4px 10px; border-radius:4px;
}
QPushButton#ghost:hover { border-color:#888; color:#e0e0e0; }
QPlainTextEdit {
    background:#111; color:#c8c8c8; font-family:Courier,monospace;
    font-size:11px; border:none;
}
QFrame#divider { color:#333; }
QFrame#left_panel { background:#141414; border-right:1px solid #2a2a2a; }
"""


class QueueLogHandler(logging.Handler):
    def __init__(self, q: _queue.Queue):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S"
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait(self.format(record))
        except Exception:
            pass


class MainWindow(QMainWindow):
    def __init__(self, log_queue: _queue.Queue):
        super().__init__()
        self.setWindowTitle("Screen Share Server")
        self.resize(920, 560)
        self.setMinimumSize(700, 440)

        self._log_q             = log_queue
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server_thread: threading.Thread | None = None
        self._stop_event: asyncio.Event | None = None
        self._running           = False
        self._audio_map: dict[str, str] = {}
        self._monitor_map: dict[str, int] = {}

        self._build_ui()
        self._populate_monitors()
        self._populate_audio()
        self._update_urls()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(200)

    # ── Build ─────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(2)

        # ── Left panel ────────────────────────────────────────────────────
        left_outer = QWidget()
        left_outer.setObjectName("left_panel")
        left_outer.setFixedWidth(260)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(left_outer)
        splitter.addWidget(scroll)

        lv = QVBoxLayout(left_outer)
        lv.setContentsMargins(14, 14, 14, 14)
        lv.setSpacing(6)

        self._title_lbl = QLabel("Screen Share")
        self._title_lbl.setObjectName("title")
        lv.addWidget(self._title_lbl)

        self._status_lbl = QLabel("● Parado")
        self._status_lbl.setObjectName("status_off")
        lv.addWidget(self._status_lbl)

        lv.addWidget(self._divider())

        # Monitor
        lv.addWidget(self._section("Monitor"))
        self._monitor_cb = QComboBox()
        lv.addWidget(self._monitor_cb)

        # Numeric fields
        self._fields: dict[str, QLineEdit] = {}
        for label, key, default in [
            ("FPS",            "fps",     "60"),
            ("Bitrate (kbps)", "bitrate", "8000"),
            ("Porta",          "port",    "1935"),
            ("Scale (0=orig)", "scale",   "0"),
        ]:
            lv.addWidget(self._section(label))
            field = QLineEdit(default)
            self._fields[key] = field
            lv.addWidget(field)

        lv.addWidget(self._divider())

        # Audio
        lv.addWidget(self._section("Saída de Áudio"))
        self._audio_cb = QComboBox()
        lv.addWidget(self._audio_cb)

        refresh_btn = QPushButton("↺  Atualizar dispositivos")
        refresh_btn.setObjectName("ghost")
        refresh_btn.clicked.connect(self._populate_audio)
        lv.addWidget(refresh_btn)

        lv.addWidget(self._divider())

        # URLs
        lv.addWidget(self._section("URLs  (OBS Browser Source)"))
        self._urls_lbl = QLabel("─")
        self._urls_lbl.setObjectName("urls")
        self._urls_lbl.setWordWrap(True)
        lv.addWidget(self._urls_lbl)

        lv.addWidget(self._divider())

        # Peers
        self._peers_lbl = QLabel("Peers conectados: 0")
        lv.addWidget(self._peers_lbl)

        lv.addSpacing(8)

        # Start/Stop button
        self._btn = QPushButton("INICIAR")
        self._btn.setMinimumHeight(38)
        self._btn.clicked.connect(self._toggle)
        lv.addWidget(self._btn)

        lv.addStretch()

        # ── Right panel (log) ─────────────────────────────────────────────
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(8, 8, 8, 8)
        rv.setSpacing(4)

        top_bar = QHBoxLayout()
        log_lbl = QLabel("Log do Servidor")
        log_lbl.setFont(QFont("", -1, QFont.Bold))
        clear_btn = QPushButton("Limpar")
        clear_btn.setObjectName("ghost")
        clear_btn.setFixedWidth(70)
        clear_btn.clicked.connect(self._clear_log)
        top_bar.addWidget(log_lbl)
        top_bar.addStretch()
        top_bar.addWidget(clear_btn)
        rv.addLayout(top_bar)

        self._log_box = QPlainTextEdit()
        self._log_box.setReadOnly(True)
        rv.addWidget(self._log_box)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        self.setCentralWidget(splitter)

    def _divider(self) -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.HLine)
        f.setObjectName("divider")
        return f

    def _section(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("section")
        return lbl

    # ── Populate ──────────────────────────────────────────────────────────────
    def _populate_monitors(self) -> None:
        try:
            with mss.MSS() as sct:
                self._monitor_map = {
                    f"Monitor {i}  ({m['width']}×{m['height']})": i
                    for i, m in enumerate(sct.monitors) if i > 0
                }
            self._monitor_cb.clear()
            self._monitor_cb.addItems(list(self._monitor_map.keys()))
        except Exception as e:
            log.warning(f"Monitores: {e}")

    def _populate_audio(self) -> None:
        if not AUDIO_OK:
            self._audio_cb.clear()
            self._audio_cb.addItem("pyaudiowpatch não instalado")
            return
        try:
            devices = get_loopback_devices()
            self._audio_map = devices
            prev = self._audio_cb.currentText()
            self._audio_cb.clear()
            self._audio_cb.addItem("(detectar automático)")
            self._audio_cb.addItems(list(devices.keys()))
            idx = self._audio_cb.findText(prev)
            self._audio_cb.setCurrentIndex(max(0, idx))
            log.info(f"Dispositivos de áudio: {len(devices)} encontrado(s)")
            for name in devices:
                log.info(f"  {name}")
        except Exception as e:
            log.warning(f"Audio devices: {e}")

    def _update_urls(self) -> None:
        try:
            port = int(self._fields["port"].text())
        except Exception:
            port = _config.port
        ips: set[str] = {"localhost"}
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None):
                ip = info[4][0]
                if not ip.startswith("127.") and ":" not in ip:
                    ips.add(ip)
        except Exception:
            pass
        self._urls_lbl.setText(
            "\n".join(f"http://{ip}:{port}" for ip in sorted(ips))
        )

    # ── Server control ────────────────────────────────────────────────────────
    def _toggle(self) -> None:
        if self._running:
            self._stop_server()
        else:
            self._start_server()

    def _start_server(self) -> None:
        try:
            mon_label            = self._monitor_cb.currentText()
            _config.monitor      = self._monitor_map.get(mon_label, 1)
            _config.fps          = int(self._fields["fps"].text())
            _config.bitrate      = int(self._fields["bitrate"].text())
            _config.port         = int(self._fields["port"].text())
            _config.scale        = int(self._fields["scale"].text())
            audio_name           = self._audio_cb.currentText()
            _config.audio_device = self._audio_map.get(audio_name, "")
        except ValueError as e:
            log.error(f"Configuração inválida: {e}"); return

        self._loop    = asyncio.new_event_loop()
        self._running = True
        self._update_status()
        self._update_urls()
        self._set_controls_enabled(False)

        def run() -> None:
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._async_server())

        self._server_thread = threading.Thread(target=run, daemon=True)
        self._server_thread.start()

    def _stop_server(self) -> None:
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        self._running = False
        self._update_status()
        self._set_controls_enabled(True)

    async def _async_server(self) -> None:
        self._stop_event = asyncio.Event()

        try:
            with mss.MSS() as sct:
                sct.grab(sct.monitors[min(_config.monitor, len(sct.monitors) - 1)])
            log.info("Captura de tela aquecida")
        except Exception: pass

        if AUDIO_OK:
            log.info(f"Áudio WASAPI: dispositivo='{_config.audio_device or 'auto'}'")


        app = web.Application()
        app.router.add_get("/", handle_index)
        app.router.add_post("/offer", handle_offer)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, _config.host, _config.port).start()

        log.info(f"Servidor em http://0.0.0.0:{_config.port}")
        log.info(
            f"Monitor {_config.monitor}  |  {_config.fps} FPS  |  "
            f"{_config.bitrate} kbps  |  áudio: {'sim' if AUDIO_OK else 'não'}"
        )

        await self._stop_event.wait()
        await asyncio.gather(*[pc.close() for pc in _pcs])
        _pcs.clear()
        await runner.cleanup()
        log.info("Servidor parado")

    # ── UI helpers ────────────────────────────────────────────────────────────
    def _update_status(self) -> None:
        if self._running:
            self._status_lbl.setText("● Rodando")
            self._status_lbl.setObjectName("status_ok")
            self._btn.setText("PARAR")
            self._btn.setObjectName("stop")
        else:
            self._status_lbl.setText("● Parado")
            self._status_lbl.setObjectName("status_off")
            self._btn.setText("INICIAR")
            self._btn.setObjectName("")
        # Re-polish to apply stylesheet changes
        self._status_lbl.style().unpolish(self._status_lbl)
        self._status_lbl.style().polish(self._status_lbl)
        self._btn.style().unpolish(self._btn)
        self._btn.style().polish(self._btn)

    def _set_controls_enabled(self, enabled: bool) -> None:
        for w in (self._monitor_cb, self._audio_cb):
            w.setEnabled(enabled)
        for f in self._fields.values():
            f.setEnabled(enabled)

    def _clear_log(self) -> None:
        self._log_box.clear()

    def _tick(self) -> None:
        try:
            while True:
                msg = self._log_q.get_nowait()
                self._log_box.appendPlainText(msg)
                self._log_box.moveCursor(QTextCursor.End)
        except _queue.Empty:
            pass
        self._peers_lbl.setText(f"Peers conectados: {len(_pcs)}")

    def closeEvent(self, event):
        if self._running:
            self._stop_server()
        event.accept()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    log_q   = _queue.Queue()
    handler = QueueLogHandler(log_q)
    logging.basicConfig(level=logging.INFO, handlers=[
        handler,
        logging.StreamHandler(),
    ])

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_STYLE)

    window = MainWindow(log_q)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
