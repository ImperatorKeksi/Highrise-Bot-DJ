#!/usr/bin/env python3
"""
stream_master.py v12.1 - Zero-Latency Passthrough Radio
========================================================
Songs werden nacheinander abgespielt.
0.5s Pause zwischen Songs (in music.py konfiguriert).
Kein Crossfade, kein Marker.

ARCHITEKTUR (v12.1):
- music.py Consumer ist der EINZIGE Pacing-Punkt im gesamten System.
  Er schreibt Chunks mit exakt 100ms Abstand in die FIFO.
- stream_master audio_writer hat KEIN eigenes Pacing mehr.
  Er leitet Chunks SOFORT (zero-latency) an ffmpeg weiter.
- Doppelter Pacing (music.py 100ms + stream_master 100ms) wurde entfernt.
- Queue auf 100 Chunks (~10s) reduziert für geringere Latenz.
"""

import asyncio
import logging
import os
import signal
import sys
import threading
import time as _time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("logs/stream-master.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("StreamMaster")

# ── Konfiguration ────────────────────────────────────────────
ICECAST_HOST = "127.0.0.1"
ICECAST_PORT = 8000
ICECAST_MOUNT = "/stream.mp3"
ICECAST_SOURCE_PASS = os.getenv("ICECAST_SOURCE_PASS", "keksradio2026")
FIFO_PATH = "/tmp/highrise-audio-pipe"
READ_SIZE = 17640                  # 100ms pro Read
WRITE_CHUNK = 17640                # 17640 Bytes = 100ms Audio
SILENCE_CHUNK = bytes(WRITE_CHUNK) # 100ms Stille (0x00)
AUDIO_QUEUE_SIZE = 100             # max ~10s Puffer in Queue (reduziert Latenz)


def create_fifo():
    if os.path.exists(FIFO_PATH):
        try:
            os.unlink(FIFO_PATH)
        except Exception:
            pass
    os.mkfifo(FIFO_PATH)
    os.chmod(FIFO_PATH, 0o666)


_current_ffmpeg = [None]


async def start_ffmpeg():
    """Startet ffmpeg für Icecast-Streaming."""
    icecast_url = (
        f"icecast://source:{ICECAST_SOURCE_PASS}@"
        f"{ICECAST_HOST}:{ICECAST_PORT}{ICECAST_MOUNT}"
    )
    cmd = (
        f'ffmpeg -hide_banner -loglevel warning '
        f'-thread_queue_size 512 '
        f'-max_delay 0 '
        f'-f s16le -ar 44100 -ac 2 -i pipe:0 '
        f'-acodec libmp3lame -b:a 128k -f mp3 '
        f'-content_type audio/mpeg '
        f'-ice_name "Highrise Bot Radio" '
        f'-bufsize 256k '
        f'"{icecast_url}"'
    )
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    _current_ffmpeg[0] = proc
    return proc


async def stop_ffmpeg(ffmpeg):
    if ffmpeg and ffmpeg.returncode is None:
        try:
            if ffmpeg.stdin and not ffmpeg.stdin.is_closing():
                ffmpeg.stdin.close()
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(ffmpeg.pid), signal.SIGTERM)
            await asyncio.wait_for(ffmpeg.wait(), timeout=3)
        except Exception:
            try:
                os.killpg(os.getpgid(ffmpeg.pid), signal.SIGKILL)
            except Exception:
                try:
                    ffmpeg.kill()
                except Exception:
                    pass
    if _current_ffmpeg[0] is ffmpeg:
        _current_ffmpeg[0] = None


def fifo_reader_thread(audio_queue, loop):
    """Liest blockierend aus FIFO, schreibt Chunks in asyncio.Queue."""
    while True:
        fd = None
        try:
            fd = os.open(FIFO_PATH, os.O_RDONLY)
        except Exception:
            _time.sleep(0.2)
            continue

        logger.info("FIFO verbunden - lese Audio-Daten...")

        while True:
            try:
                data = os.read(fd, READ_SIZE * 4)
            except OSError:
                break

            if data:
                offset = 0
                while offset < len(data):
                    chunk = data[offset:offset + READ_SIZE]
                    offset += READ_SIZE
                    if len(chunk) == READ_SIZE:
                        try:
                            asyncio.run_coroutine_threadsafe(
                                audio_queue.put(chunk), loop
                            ).result(timeout=30)
                        except Exception:
                            pass
            else:
                logger.info("FIFO EOF — warte auf neuen Writer...")
                break

        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        _time.sleep(0.05)


async def audio_writer(ffmpeg, audio_queue):
    """Liest Audio aus Queue, schreibt an ffmpeg — SOFORT ohne Pacing.

    v12.1: KEIN eigenes Pacing — music.py ist der einzige Pacer.
    Chunks werden SOFORT an ffmpeg weitergegeben (zero-latency).
    Bei fehlenden Daten: Stille füttern (keine Lücke im Stream).
    """
    total_chunks = 0
    silence_chunks = 0
    last_status_time = _time.monotonic()

    while ffmpeg.returncode is None:
        data = None
        try:
            # Warte max 200ms auf Daten
            data = await asyncio.wait_for(audio_queue.get(), timeout=0.2)
            silence_chunks = 0
            total_chunks += 1
        except asyncio.TimeoutError:
            # Keine Daten → Stille füttern (verhindert Stream-Abbruch)
            silence_chunks += 1
            data = SILENCE_CHUNK
            total_chunks += 1

        if data is None:
            continue

        # SOFORT an ffmpeg schreiben — KEIN Pacing (music.py ist der Pacer)
        try:
            ffmpeg.stdin.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

        now = _time.monotonic()
        if now - last_status_time > 30:
            logger.info(f"📊 Stream: {total_chunks} Chunks ({'Stille' if silence_chunks > 5 else 'Audio'}), Queue: {audio_queue.qsize()}")
            last_status_time = now


async def main_loop():
    create_fifo()

    loop = asyncio.get_event_loop()
    audio_queue = asyncio.Queue(maxsize=AUDIO_QUEUE_SIZE)

    reader_thread = threading.Thread(
        target=fifo_reader_thread,
        args=(audio_queue, loop),
        daemon=True,
    )
    reader_thread.start()

    ffmpeg = None
    consecutive_failures = 0

    while True:
        try:
            if ffmpeg is None or ffmpeg.returncode is not None:
                if ffmpeg is not None:
                    await stop_ffmpeg(ffmpeg)
                    ffmpeg = None

                ffmpeg = await start_ffmpeg()
                consecutive_failures = 0
                logger.info("Icecast-Verbindung hergestellt! Stream laeuft 24/7.")

                while not audio_queue.empty():
                    try:
                        audio_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

            if audio_queue.empty():
                logger.info("Warte auf Bot-Audio...")
                wait_start = _time.monotonic()
                while audio_queue.empty():
                    await asyncio.sleep(0.1)
                    elapsed = _time.monotonic() - wait_start
                    if elapsed > 60:
                        logger.info(f"Warte seit {elapsed:.0f}s auf Bot-Audio...")
                        await asyncio.sleep(1)
                    if elapsed > 300:
                        logger.warning("5 Min ohne Audio — Neustart ffmpeg fuer Icecast-Keepalive")
                        break
                else:
                    logger.info(f"Audio-Queue befuellt ({audio_queue.qsize()} Chunks)")
                    pre_buf_start = _time.monotonic()
                    while audio_queue.qsize() < 5:
                        await asyncio.sleep(0.05)
                        if _time.monotonic() - pre_buf_start > 0.5:
                            break

            await audio_writer(ffmpeg, audio_queue)

            if ffmpeg.returncode is not None:
                logger.warning(f"ffmpeg mit Exit-Code {ffmpeg.returncode} beendet — Neustart")
                consecutive_failures += 1
                wait = min(3 * consecutive_failures, 30)
                logger.warning(f"Neustart ffmpeg in {wait}s (Fehler #{consecutive_failures})...")
                await asyncio.sleep(wait)
            else:
                logger.warning("audio_writer unerwartet beendet — Neustart")
                consecutive_failures += 1
                await asyncio.sleep(3)

        except Exception as e:
            logger.error(f"Fehler: {e}", exc_info=True)
            consecutive_failures += 1
            wait = min(3 * consecutive_failures, 30)
            logger.warning(f"Neustart in {wait}s...")
            await asyncio.sleep(wait)


def handle_signal(sig, frame):
    logger.info("Stream-Master wird beendet...")
    proc = _current_ffmpeg[0]
    if proc and proc.returncode is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    logger.info("=" * 50)
    logger.info("Stream-Master v12.1 gestartet - Zero-Latency Passthrough")
    logger.info(f"  FIFO: {FIFO_PATH}")
    logger.info(f"  Icecast: {ICECAST_HOST}:{ICECAST_PORT}{ICECAST_MOUNT}")
    logger.info(f"  Pacing: KEIN eigenes Pacing (music.py ist der einzige Pacer)")
    logger.info(f"  Queue: {AUDIO_QUEUE_SIZE} Chunks (~{AUDIO_QUEUE_SIZE * 100 / 1000:.0f}s)")
    logger.info(f"  Kein Crossfade — 0.5s Pause zwischen Songs")
    logger.info("=" * 50)
    asyncio.run(main_loop())
