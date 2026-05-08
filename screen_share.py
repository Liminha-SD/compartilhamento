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

import asyncio
import argparse
import sys

# Windows: força SelectorEventLoop (necessário para aiortc/aiohttp funcionarem corretamente)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import json
import logging
import threading

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
    """Captura a tela (com cursor no Windows) e serve como track de vídeo WebRTC."""

    def __init__(self, monitor: int = 1, fps: int = 30):
        super().__init__()
        self._monitor = monitor
        self._fps = fps
        self._sct = None

    async def recv(self) -> av.VideoFrame:
        pts, time_base = await self.next_timestamp()
        loop = asyncio.get_event_loop()
        img = await loop.run_in_executor(None, self._grab)
        frame = av.VideoFrame.from_ndarray(img, format="bgra")
        frame.pts = pts
        frame.time_base = time_base
        return frame

    def _grab(self) -> np.ndarray:
        if self._sct is None:
            self._sct = mss.mss()
        mon = self._sct.monitors[self._monitor]
        if WIN32_OK:
            return _grab_win32_cursor(mon["left"], mon["top"], mon["width"], mon["height"])
        raw = self._sct.grab(mon)
        return np.frombuffer(raw.raw, dtype=np.uint8).reshape(raw.height, raw.width, 4)


try:
    import soundcard as sc

    def _find_loopback_mic():
        """Procura dispositivo de loopback para captura do áudio do sistema."""
        # 1. Loopback do speaker padrão (nome exato)
        try:
            spk = sc.default_speaker()
            mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
            log.info(f"Áudio: loopback de '{spk.name}'")
            return mic
        except Exception as e:
            log.debug(f"Loopback pelo nome falhou: {e}")

        # 2. Qualquer mic marcado como loopback
        try:
            loopbacks = [m for m in sc.all_microphones(include_loopback=True)
                         if getattr(m, "isloopback", False)]
            if loopbacks:
                log.info(f"Áudio: loopback automático '{loopbacks[0].name}'")
                return loopbacks[0]
        except Exception as e:
            log.debug(f"Busca por loopbacks falhou: {e}")

        # 3. Stereo Mix (Windows legado)
        try:
            mic = sc.get_microphone("Stereo Mix")
            log.info("Áudio: usando Stereo Mix")
            return mic
        except Exception:
            pass

        raise RuntimeError(
            "Nenhum dispositivo de loopback encontrado.\n"
            "  Use --list-audio para ver os dispositivos disponíveis.\n"
            "  No Windows: Painel de Som > Gravação > clique direito > "
            "Mostrar dispositivos desativados > ativar 'Stereo Mix'"
        )

    class SystemAudioTrack(AudioStreamTrack):
        """Captura o áudio do sistema (loopback) via soundcard."""

        SAMPLE_RATE = 48000
        CHUNK = 960  # 20 ms @ 48 kHz

        def __init__(self):
            super().__init__()
            self._queue: asyncio.Queue = asyncio.Queue(maxsize=8)
            self._loop = asyncio.get_event_loop()
            threading.Thread(target=self._capture, daemon=True).start()

        def _capture(self):
            try:
                mic = _find_loopback_mic()
                with mic.recorder(samplerate=self.SAMPLE_RATE, channels=2) as rec:
                    while True:
                        data = rec.record(numframes=self.CHUNK)
                        fut = asyncio.run_coroutine_threadsafe(
                            self._queue.put(data), self._loop
                        )
                        try:
                            fut.result(timeout=0.05)
                        except Exception:
                            pass
            except Exception as exc:
                log.error(f"Captura de áudio falhou: {exc}")

        async def recv(self) -> av.AudioFrame:
            pts, time_base = await self.next_timestamp()
            try:
                data = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                data = np.zeros((self.CHUNK, 2), dtype=np.float32)

            pcm = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
            frame = av.AudioFrame.from_ndarray(pcm.T, format="s16", layout="stereo")
            frame.pts = pts
            frame.time_base = time_base
            frame.sample_rate = self.SAMPLE_RATE
            return frame

    AUDIO_OK = True

except ImportError:
    AUDIO_OK = False
    log.warning("soundcard não instalado — áudio desativado")


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

    async function connect() {
      statusEl.textContent = 'Conectando…';
      statusEl.style.display = '';

      const pc     = new RTCPeerConnection({ iceServers: [] });
      const stream = new MediaStream();
      videoEl.srcObject = stream;

      pc.ontrack = e => {
        stream.addTrack(e.track);
        if (e.track.kind === 'video') {
          statusEl.textContent = '● Live';
          setTimeout(() => { statusEl.style.display = 'none'; }, 3000);
        }
      };

      pc.onconnectionstatechange = () => {
        if (['failed', 'disconnected', 'closed'].includes(pc.connectionState)) {
          statusEl.textContent = '⚠ ' + pc.connectionState + ' — reconectando…';
          statusEl.style.display = '';
          pc.close();
          setTimeout(connect, 2000);
        }
      };

      pc.addTransceiver('video', { direction: 'recvonly' });
      pc.addTransceiver('audio', { direction: 'recvonly' });

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);

      // Aguarda coleta de ICE candidates (máx 2s)
      await new Promise(r => {
        if (pc.iceGatheringState === 'complete') return r();
        pc.addEventListener('icegatheringstatechange', () => {
          if (pc.iceGatheringState === 'complete') r();
        });
        setTimeout(r, 2000);
      });

      const res = await fetch('/offer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type }),
      });

      if (!res.ok) throw new Error('Servidor retornou ' + res.status);
      const ans = await res.json();
      await pc.setRemoteDescription(ans);
    }

    connect().catch(e => {
      statusEl.textContent = 'Erro: ' + e.message;
      setTimeout(connect, 3000);
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

    pc.addTrack(ScreenVideoTrack(monitor=monitor, fps=fps))
    if AUDIO_OK:
        pc.addTrack(SystemAudioTrack())

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    # Aplica bitrate no SDP da resposta
    patched_sdp = _patch_sdp_bitrate(pc.localDescription.sdp, bitrate)

    return web.Response(
        content_type="application/json",
        text=json.dumps({"sdp": patched_sdp, "type": pc.localDescription.type}),
    )


async def on_shutdown(app: web.Application) -> None:
    await asyncio.gather(*[pc.close() for pc in pcs])
    pcs.clear()


# ─── Main ─────────────────────────────────────────────────────────────────────

def list_audio():
    if not AUDIO_OK:
        print("soundcard não instalado.")
        return
    print("\nSpeakers (saída de áudio):")
    default_spk = sc.default_speaker()
    for spk in sc.all_speakers():
        marker = " ← padrão" if str(spk.name) == str(default_spk.name) else ""
        print(f"  {spk.name}{marker}")
    print("\nMicrofones / Loopbacks disponíveis:")
    for mic in sc.all_microphones(include_loopback=True):
        lb = " [loopback]" if getattr(mic, "isloopback", False) else ""
        print(f"  {mic.name}{lb}")
    print()


def list_monitors():
    with mss.mss() as sct:
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
    parser.add_argument("--list-monitors", action="store_true", help="Lista monitores disponíveis e sai")
    parser.add_argument("--list-audio",   action="store_true", help="Lista dispositivos de áudio e sai")
    _args = parser.parse_args()

    if _args.list_monitors:
        list_monitors()
        return

    if _args.list_audio:
        list_audio()
        return

    app = web.Application()
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", handle_index)
    app.router.add_post("/offer", handle_offer)

    log.info("─" * 55)
    log.info(f"  Monitor  : {_args.monitor}   FPS: {_args.fps}   Bitrate: {_args.bitrate} kbps")
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
