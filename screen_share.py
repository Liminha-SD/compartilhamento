#!/usr/bin/env python3
"""
Screen Share Server - WebRTC para OBS Browser Source
Captura tela + áudio do sistema e serve via WebRTC (sem delay).

Uso (Windows):
    venv\Scripts\python screen_share.py [--port 8080] [--monitor 1] [--fps 30]

No OBS:
    Sources > Browser Source
    URL: http://localhost:8080       (mesma máquina)
         http://<tailscale-ip>:8080  (outra máquina via Tailscale)

    Marcar: "Control audio via OBS" para capturar o áudio no OBS.

Monitores disponíveis: use --list-monitors para ver os índices.
"""

import sys

print("Carregando módulos...", flush=True)

import asyncio
import argparse

# Windows: força SelectorEventLoop (necessário para aiortc/aiohttp funcionarem corretamente)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import fractions
import json
import logging
import threading
import time

import av
import mss
import numpy as np
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import AudioStreamTrack, VideoStreamTrack

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
log = logging.getLogger(__name__)

# ─── Captura de cursor (Windows) ─────────────────────────────────────────────

if sys.platform == "win32":
    try:
        import win32gui
        import win32ui
        import win32con
        WIN32_OK = True
    except ImportError:
        WIN32_OK = False
        log.warning("pywin32 não instalado — cursor não será capturado")
        log.warning("  Instale com: venv\\Scripts\\pip install pywin32")
else:
    WIN32_OK = False


def _grab_win32_cursor(left: int, top: int, width: int, height: int) -> np.ndarray:
    """Captura tela + cursor usando a win32 API."""
    hdesktop = win32gui.GetDesktopWindow()
    desktop_dc = win32gui.GetWindowDC(hdesktop)
    src_dc = win32ui.CreateDCFromHandle(desktop_dc)
    mem_dc = src_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src_dc, width, height)
    mem_dc.SelectObject(bmp)
    mem_dc.BitBlt((0, 0), (width, height), src_dc, (left, top), win32con.SRCCOPY)
    try:
        flags, hcursor, (cx, cy) = win32gui.GetCursorInfo()
        if flags & 0x1:  # CURSOR_SHOWING
            win32gui.DrawIconEx(
                mem_dc.GetSafeHdc(),
                cx - left, cy - top,
                hcursor, 0, 0, 0, None,
                win32con.DI_NORMAL,
            )
    except Exception:
        pass
    bmpstr = bmp.GetBitmapBits(True)
    img = np.frombuffer(bmpstr, dtype=np.uint8).reshape(height, width, 4).copy()
    mem_dc.DeleteDC()
    win32gui.DeleteObject(bmp.GetHandle())
    win32gui.ReleaseDC(hdesktop, desktop_dc)
    return img


# ─── Tracks ──────────────────────────────────────────────────────────────────

class ScreenVideoTrack(VideoStreamTrack):
    """
    Captura contínua em thread dedicada — o frame mais recente está sempre pronto.
    recv() nunca bloqueia esperando a captura, eliminando jitter.
    """

    def __init__(self, monitor: int = 1, fps: int = 60, scale_width: int = 0):
        super().__init__()
        self._monitor    = monitor
        self._fps        = fps
        self._scale_w    = scale_width
        self._latest: np.ndarray | None = None
        self._lock       = threading.Lock()
        threading.Thread(target=self._capture_loop, daemon=True).start()

    def _capture_loop(self) -> None:
        """Roda em thread própria, capturando continuamente sem depender do event loop."""
        with mss.MSS() as sct:
            mon      = sct.monitors[self._monitor]
            left     = mon["left"]
            top      = mon["top"]
            w, h     = mon["width"], mon["height"]
            interval = 1.0 / (self._fps + 10)   # captura um pouco mais rápido que o FPS alvo

            while True:
                t0 = time.monotonic()

                if WIN32_OK:
                    img = _grab_win32_cursor(left, top, w, h)
                else:
                    raw = sct.grab(mon)
                    img = np.frombuffer(raw.raw, dtype=np.uint8).reshape(h, w, 4)

                with self._lock:
                    self._latest = img

                sleep = interval - (time.monotonic() - t0)
                if sleep > 0:
                    time.sleep(sleep)

    async def recv(self) -> av.VideoFrame:
        pts, time_base = await self.next_timestamp()

        with self._lock:
            img = self._latest

        if img is None:
            # Thread de captura ainda iniciando (raro)
            await asyncio.sleep(0.02)
            with self._lock:
                img = self._latest

        frame = av.VideoFrame.from_ndarray(img, format="bgra")

        if self._scale_w and frame.width != self._scale_w:
            scale_h = int(frame.height * self._scale_w / frame.width) & ~1
            frame   = frame.reformat(width=self._scale_w, height=scale_h)

        frame.pts       = pts
        frame.time_base = time_base
        return frame


try:
    import pyaudiowpatch as pyaudio

    def _find_wasapi_loopback(hint: int = -1):
        """
        Retorna (device_info, pa_instance) para loopback WASAPI.
        hint >= 0 força um índice específico (via --audio-device).
        """
        pa = pyaudio.PyAudio()

        if hint >= 0:
            d = pa.get_device_info_by_index(hint)
            return d, pa

        # Dispositivo de saída padrão WASAPI
        try:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])

            # Procura a versão loopback do dispositivo padrão
            for lb in pa.get_loopback_device_info_generator():
                if default["name"] in lb["name"]:
                    log.info(f"Áudio WASAPI loopback: {lb['name']}")
                    return lb, pa

            # Se não encontrou versão loopback por nome, usa o padrão direto
            log.info(f"Áudio WASAPI loopback (direto): {default['name']}")
            return default, pa

        except Exception as e:
            raise RuntimeError(
                f"WASAPI loopback não encontrado: {e}\n"
                "  Use --list-audio para ver dispositivos disponíveis."
            )

    class SystemAudioTrack(AudioStreamTrack):
        """Captura todo o áudio do sistema via WASAPI loopback (pyaudiowpatch)."""

        CHUNK = 960   # 20 ms @ 48 kHz

        def __init__(self, device: int = -1):
            super().__init__()
            self._device    = device
            self._pts       = 0
            self._time_base = fractions.Fraction(1, self.SAMPLE_RATE)
            self._queue: asyncio.Queue = asyncio.Queue(maxsize=8)
            self._loop = asyncio.get_event_loop()
            threading.Thread(target=self._capture, daemon=True).start()

        def _capture(self):
            pa = None
            stream = None
            try:
                dev, pa = _find_wasapi_loopback(self._device)
                rate     = int(dev["defaultSampleRate"])
                channels = min(int(dev["maxInputChannels"]), 2)

                stream = pa.open(
                    format=pyaudio.paFloat32,
                    channels=channels,
                    rate=rate,
                    frames_per_buffer=self.CHUNK,
                    input=True,
                    input_device_index=dev["index"],
                )

                log.info(f"Áudio capturando: {dev['name']} @ {rate}Hz {channels}ch")

                while True:
                    raw  = stream.read(self.CHUNK, exception_on_overflow=False)
                    data = np.frombuffer(raw, dtype=np.float32).reshape(-1, channels)
                    if channels == 1:
                        data = np.repeat(data, 2, axis=1)
                    fut = asyncio.run_coroutine_threadsafe(
                        self._queue.put(data[:, :2]), self._loop
                    )
                    try:
                        fut.result(timeout=0.05)
                    except Exception:
                        pass

            except Exception as exc:
                log.error(f"Captura de áudio falhou: {exc}")
                log.error("  Use --list-audio para ver dispositivos disponíveis")
            finally:
                if stream:
                    try: stream.stop_stream(); stream.close()
                    except Exception: pass
                if pa:
                    try: pa.terminate()
                    except Exception: pass

        async def recv(self) -> av.AudioFrame:
            try:
                data = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                data = np.zeros((self.CHUNK, 2), dtype=np.float32)

            pcm   = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
            frame = av.AudioFrame.from_ndarray(pcm.T, format="s16", layout="stereo")
            frame.pts         = self._pts
            frame.time_base   = self._time_base
            frame.sample_rate = self.SAMPLE_RATE
            self._pts        += self.CHUNK   # avança 960 samples = 20 ms
            return frame

    AUDIO_OK = True

except ImportError:
    AUDIO_OK = False
    log.warning("pyaudiowpatch não instalado — áudio desativado")


# ─── HTML embutido ────────────────────────────────────────────────────────────

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

    let pc             = null;
    let reconnectTimer = null;
    let isConnecting   = false;

    function status(msg, color) {
      statusEl.textContent = msg;
      statusEl.style.color = color || '#0f0';
      statusEl.style.display = '';
    }

    function scheduleReconnect(ms) {
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(() => {
        isConnecting = false;
        connect().catch(err => {
          status('Erro: ' + err.message, '#f44');
          scheduleReconnect(3000);
        });
      }, ms);
    }

    async function connect() {
      if (isConnecting) return;
      isConnecting = true;
      clearTimeout(reconnectTimer);
      status('Conectando…', '#ff0');

      if (pc) { try { pc.close(); } catch (_) {} pc = null; }

      pc = new RTCPeerConnection({
        iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
      });

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
        if (s === 'connected') {
          clearTimeout(reconnectTimer);
          isConnecting = false;
        } else if (s === 'failed') {
          // Falhou de vez — reconecta em 1.5s
          status('⚠ falhou — reconectando…', '#f44');
          scheduleReconnect(1500);
        } else if (s === 'disconnected') {
          // Transitório — aguarda 8s antes de reconectar (costuma se recuperar sozinho)
          status('⚠ desconectado — aguardando…', '#fa0');
          scheduleReconnect(8000);
        }
      };

      pc.addTransceiver('video', { direction: 'recvonly' });
      pc.addTransceiver('audio', { direction: 'recvonly' });

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);

      // Aguarda ICE candidates (máx 5s — necessário para Tailscale)
      await new Promise(r => {
        if (pc.iceGatheringState === 'complete') return r();
        pc.addEventListener('icegatheringstatechange', () => {
          if (pc.iceGatheringState === 'complete') r();
        });
        setTimeout(r, 5000);
      });

      const res = await fetch('/offer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type }),
      });

      if (!res.ok) throw new Error('HTTP ' + res.status);
      const ans = await res.json();
      await pc.setRemoteDescription(ans);
      isConnecting = false;
    }

    connect().catch(err => {
      status('Erro: ' + err.message, '#f44');
      scheduleReconnect(3000);
    });
  })();
  </script>
</body>
</html>"""

# ─── Handlers HTTP ────────────────────────────────────────────────────────────

pcs: set[RTCPeerConnection] = set()
_args = None


async def handle_index(request: web.Request) -> web.Response:
    return web.Response(content_type="text/html", text=HTML)


def _patch_sdp_bitrate(sdp: str, bitrate_kbps: int) -> str:
    """Injeta b=AS no SDP para sugerir bitrate máximo ao encoder."""
    lines = sdp.split("\r\n")
    out, in_video, injected = [], False, False
    for line in lines:
        if line.startswith("m="):
            in_video = line.startswith("m=video")
            injected = False
        if in_video and not injected and line.startswith("a="):
            out.append(f"b=AS:{bitrate_kbps}")
            injected = True
        out.append(line)
    return "\r\n".join(out)


async def handle_offer(request: web.Request) -> web.Response:
    body = await request.json()
    offer = RTCSessionDescription(sdp=body["sdp"], type=body["type"])

    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_state():
        log.info(f"Peer [{id(pc) % 10000:04d}] {pc.connectionState}")
        if pc.connectionState in ("failed", "closed", "disconnected"):
            await pc.close()
            pcs.discard(pc)

    monitor  = int(request.rel_url.query.get("monitor", _args.monitor))
    fps      = int(request.rel_url.query.get("fps",     _args.fps))
    bitrate  = int(request.rel_url.query.get("bitrate", _args.bitrate))
    scale    = int(request.rel_url.query.get("scale",   _args.scale))

    pc.addTrack(ScreenVideoTrack(monitor=monitor, fps=fps, scale_width=scale))
    if AUDIO_OK:
        audio_dev = int(request.rel_url.query.get("audio_device", _args.audio_device))
        pc.addTrack(SystemAudioTrack(device=audio_dev))

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    # Aplica bitrate no SDP da resposta
    patched_sdp = _patch_sdp_bitrate(pc.localDescription.sdp, bitrate)

    return web.Response(
        content_type="application/json",
        text=json.dumps({"sdp": patched_sdp, "type": pc.localDescription.type}),
    )


async def on_startup(app: web.Application) -> None:
    """Pré-aquece captura e áudio para que o primeiro cliente conecte instantaneamente."""
    try:
        with mss.MSS() as sct:
            idx = min(_args.monitor, len(sct.monitors) - 1)
            sct.grab(sct.monitors[idx])
        log.info("Captura de tela aquecida")
    except Exception as e:
        log.debug(f"Warmup de tela falhou: {e}")

    if AUDIO_OK:
        try:
            dev, pa = _find_wasapi_loopback(_args.audio_device)
            pa.terminate()
            log.info(f"Áudio confirmado: [{dev['index']}] {dev['name']}")
        except Exception as e:
            log.warning(f"Áudio: {e}")


async def on_shutdown(app: web.Application) -> None:
    await asyncio.gather(*[pc.close() for pc in pcs])
    pcs.clear()


# ─── Main ─────────────────────────────────────────────────────────────────────

def list_audio():
    if not AUDIO_OK:
        print("pyaudiowpatch não instalado.")
        return
    pa = pyaudio.PyAudio()
    try:
        wasapi     = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = wasapi["defaultOutputDevice"]
        default_in  = wasapi["defaultInputDevice"]

        print(f"\n{'Idx':>4}  {'Ch Out':>6}  {'Ch In':>5}  {'Loopback':>8}  Nome")
        print("─" * 65)
        for i in range(pa.get_device_count()):
            d  = pa.get_device_info_by_index(i)
            lb = "sim" if d.get("isLoopbackDevice") else "não"
            marks = []
            if i == default_out: marks.append("← saída padrão")
            if i == default_in:  marks.append("← entrada padrão")
            note = "  " + ", ".join(marks) if marks else ""
            print(f"{i:>4}  {int(d['maxOutputChannels']):>6}  {int(d['maxInputChannels']):>5}"
                  f"  {lb:>8}  {d['name']}{note}")
        print()
        print("Loopback=sim são os dispositivos que capturam áudio do sistema.")
        print("Use --audio-device <Idx> para selecionar um específico.\n")
    finally:
        pa.terminate()


def list_monitors():
    with mss.MSS() as sct:
        print(f"\n{'Idx':>4}  {'Resolução':>16}  {'Posição'}")
        print("-" * 40)
        for i, m in enumerate(sct.monitors):
            res = f"{m['width']}x{m['height']}"
            pos = f"({m['left']}, {m['top']})"
            note = "  ← todos combinados" if i == 0 else ""
            print(f"{i:>4}  {res:>16}  {pos}{note}")
    print()


def main():
    global _args

    parser = argparse.ArgumentParser(
        description="WebRTC Screen Share — OBS Browser Source",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host",          default="0.0.0.0", help="IP de bind (padrão: 0.0.0.0)")
    parser.add_argument("--port",          type=int, default=1935, help="Porta (padrão: 1935)")
    parser.add_argument("--monitor",       type=int, default=1,    help="Índice do monitor (padrão: 1)")
    parser.add_argument("--fps",           type=int, default=60,    help="FPS alvo (padrão: 60)")
    parser.add_argument("--bitrate",       type=int, default=8000,  help="Bitrate de vídeo em kbps (padrão: 8000)")
    parser.add_argument("--scale",         type=int, default=0,     help="Largura de encoding em px, 0=nativo (ex: 1280 para 720p)")
    parser.add_argument("--audio-device",  type=int, default=-1,    help="Índice do dispositivo de saída para loopback (-1=padrão)")
    parser.add_argument("--list-monitors", action="store_true",     help="Lista monitores disponíveis e sai")
    parser.add_argument("--list-audio",    action="store_true",     help="Lista dispositivos de áudio e sai")
    _args = parser.parse_args()

    if _args.list_monitors:
        list_monitors()
        return

    if _args.list_audio:
        list_audio()
        return

    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", handle_index)
    app.router.add_post("/offer", handle_offer)

    log.info("─" * 55)
    scale_info = f"{_args.scale}px largura" if _args.scale else "nativo"
    log.info(f"  Monitor  : {_args.monitor}   FPS: {_args.fps}   Bitrate: {_args.bitrate} kbps   Scale: {scale_info}")
    log.info(f"  Áudio    : {'loopback (soundcard)' if AUDIO_OK else 'desativado'}")
    log.info(f"  Endereço : http://0.0.0.0:{_args.port}")
    log.info("")
    log.info("  ── SETUP 2 PCs ──────────────────────────────")
    log.info("  Maquina A (esta): rode este servidor")
    log.info("  Maquina B (OBS) : Sources > Browser Source")
    log.info(f"    URL mesma rede : http://<ip-local>:{_args.port}")
    log.info(f"    URL Tailscale  : http://<tailscale-ip>:{_args.port}")
    log.info("    Marcar: 'Control audio via OBS'")
    log.info("─" * 55)

    web.run_app(app, host=_args.host, port=_args.port, print=None)


if __name__ == "__main__":
    main()
