import asyncio
import glob
import json
import logging
import os
import signal
import time as _time
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger("MusicPlayer")
load_dotenv()

SONG_INFO_PATH = "/tmp/highrise-song-info.json"

async def write_song_info(title: str, duration: float, url: str):
    try:
        info = {"title": title, "duration": duration, "url": url, "start_time": _time.time()}
        with open(SONG_INFO_PATH, "w") as f:
            json.dump(info, f)
        logger.info(f"Song-Info geschrieben: {title} ({duration:.0f}s)")
    except Exception as e:
        logger.warning(f"Song-Info schreiben fehlgeschlagen: {e}")


async def get_youtube_metadata(url: str) -> dict:
    import json as _json
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"
    cmd = ["yt-dlp", "--no-playlist", "--dump-single-json", "--no-download", "--no-warnings"]
    js_runtime = _get_available_js_runtime()
    if js_runtime:
        cmd.extend(["--js-runtimes", js_runtime])
    cmd.extend(["--no-check-certificates"])
    if os.path.exists(COOKIE_FILE):
        cmd.extend(["--cookies", COOKIE_FILE])
    cmd.append(url)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            return {}
        data = _json.loads(stdout.decode("utf-8", errors="ignore"))
        return {"title": data.get("title", "Unbekannt"), "duration": float(data.get("duration", 0))}
    except Exception:
        return {}

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

def _get_available_js_runtime():
    for runtime in ["node", "bun", "deno", "quickjs"]:
        if shutil.which(runtime):
            return runtime
    return None

def _ytdlp_extra_args():
    args = []
    js_runtime = _get_available_js_runtime()
    if js_runtime:
        args.extend(["--js-runtimes", js_runtime])
    args.extend(["--no-check-certificates", "--no-warnings"])
    if os.path.exists(COOKIE_FILE):
        args.extend(["--cookies", COOKIE_FILE])
    return args

FIFO_PATH = "/tmp/highrise-audio-pipe"
BYTES_PER_SECOND = 44100 * 2 * 2   # 176400
READ_CHUNK = 17640                   # 100ms
SILENCE_CHUNK = bytes(READ_CHUNK)

_429_BASE_WAIT = 60
_429_MAX_WAIT = 1800

YOUTUBE_CHALLENGE_ERRORS = [
    "n challenge solving failed", "Sign in to confirm",
    "confirm you're not a bot", "403 Forbidden", "410 Gone", "429 Too Many Requests",
]
YOUTUBE_RATELIMIT_ERRORS = [
    "rate-limited by YouTube", "rate limited", "try again later", "429", "Too Many Requests",
]
INTER_SONG_PAUSE = 0.5  # 0.5s Pause zwischen Songs (nur für YouTube Rate-Limit Schutz)
_DOWNLOAD_MIN_INTERVAL = 2.0  # Mindestens 2s zwischen Download-Starts


class MusicPlayer:
    def __init__(self, bot_instance=None):
        self.bot = bot_instance
        self.queue = []
        self.current_process = None
        self._ytdlp_process = None
        self.song_playing = False
        self.playing = False
        self.current_track = None
        self.skip_flag = False
        self._fifo_fd = None
        self._last_error_429 = False
        self._last_error_challenge = False
        self._consecutive_429 = 0
        self._consecutive_rapid_failures = 0
        self._consecutive_challenge_failures = 0
        self._song_start_time = None
        self._song_duration = 0
        self._metadata_cache = {}
        self._prefetch_task = None
        # v11.1: Pre-fetch PCM file
        self._next_pcm_file = None
        self._next_track_meta = None
        self._next_pcm_ready = asyncio.Event()

    def start(self):
        # Startup-Cleanup: alte highrise-Dateien in /tmp löschen (Crash-Überbleibsel)
        _cleaned = 0
        for _pattern in ["highrise-song-*.pcm", "highrise-audio-*.audio",
                         "highrise-audio-*.part", "highrise-audio-*.temp.audio"]:
            for _f in glob.glob(f"/tmp/{_pattern}"):
                try:
                    os.unlink(_f)
                    _cleaned += 1
                except Exception:
                    pass
        if _cleaned:
            logger.info(f"Startup-Cleanup: {_cleaned} alte /tmp/-Dateien gelöscht")

        if self.bot and hasattr(self.bot, "db"):
            # NEUSTART: Index aus DB laden (Crash-Recovery)
            saved_index = self.bot.db.get("auto_playlist_index", 0)
            self.auto_playlist_index = saved_index
            # played_urls aus DB laden
            saved_played = self.bot.db.get("played_urls", [])
            self._played_urls = set(saved_played) if saved_played else set()
            logger.info(f"Neustart: Auto-Playlist Index={saved_index}, gespielt={len(self._played_urls)}")
        else:
            self.auto_playlist_index = 0
            self._played_urls = set()
        self.ensure_play_loop()

    def ensure_play_loop(self):
        if not self.playing:
            self.playing = True
            asyncio.create_task(self._play_loop())

    @property
    def resolved_auto_playlist(self):
        if not self.bot:
            return []
        if hasattr(self.bot, "db"):
            db_tracks = self.bot.db.get("auto_playlist_tracks")
            if db_tracks:
                self.bot.config["autoPlaylistUrls"] = db_tracks
                del self.bot.db["auto_playlist_tracks"]
                self._save_db()
                self._save_config()
        if not self.bot.config.get("autoPlaylistEnabled", True):
            return []
        urls = self.bot.config.get("autoPlaylistUrls", [])
        playlist = []
        for item in urls:
            if isinstance(item, dict):
                playlist.append({"title": item.get("title", "Auto-Song"), "url": item.get("url", ""), "requested_by": "Auto"})
            else:
                playlist.append({"title": "Auto-Song", "url": item, "requested_by": "Auto"})
        return [t for t in playlist if t["url"]]

    def _save_db(self):
        """Schreibt DB atomisch (write-to-temp + rename) gegen Race Conditions."""
        if not self.bot or not hasattr(self.bot, "db"):
            return
        import tempfile
        db_path = Path("database.json")
        try:
            fd, tmp_path = tempfile.mkstemp(dir=str(db_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.bot.db, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, str(db_path))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                raise
        except Exception:
            try:
                with open(db_path, "w", encoding="utf-8") as f:
                    json.dump(self.bot.db, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

    def _save_config(self):
        if self.bot and hasattr(self.bot, "config"):
            try:
                import tempfile
                from pathlib import Path
                cfg_path = Path("config.json")
                fd, tmp_path = tempfile.mkstemp(dir=str(cfg_path.parent), suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(self.bot.config, f, indent=4, ensure_ascii=False)
                    os.replace(tmp_path, str(cfg_path))
                except Exception:
                    try: os.unlink(tmp_path)
                    except Exception: pass
                    raise
            except Exception:
                pass

    async def set_auto_playlist_from_youtube(self, playlist_url: str) -> dict:
        result = {"count": 0, "playlist_title": "", "playlist_uploader": "", "total_seconds": 0}
        try:
            import json as _json
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["LANG"] = "C.UTF-8"
            env["LC_ALL"] = "C.UTF-8"
            cmd = ["yt-dlp", "--flat-playlist", "--dump-single-json", "--ignore-errors", "--no-abort-on-error", "--no-warnings"] + _ytdlp_extra_args()
            cmd.append(playlist_url)
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
            stdout, stderr = await proc.communicate()
        except Exception as e:
            logger.error(f"!playlist: yt-dlp Fehler: {e}")
            return result

        raw_output = stdout.decode("utf-8", errors="ignore").strip()
        if not raw_output:
            return result
        try:
            data = _json.loads(raw_output)
        except _json.JSONDecodeError:
            return result

        entries = data.get("entries") or []
        tracks = []
        total_seconds = 0
        playlist_title = data.get("title") or data.get("playlist_title") or ""
        playlist_uploader = data.get("uploader") or data.get("playlist_uploader") or ""

        for entry in entries:
            if not entry:
                continue
            video_id = entry.get("id") or entry.get("url", "")
            if not video_id:
                continue
            title = (entry.get("title") or "Auto-Song")[:80]
            try:
                dur = float(entry.get("duration") or 0)
                if dur > 0:
                    total_seconds += int(dur)
            except (ValueError, TypeError):
                dur = 0
            if video_id and title:
                url = f"https://www.youtube.com/watch?v={video_id}" if len(video_id) == 11 else video_id
                tracks.append({"title": title, "url": url})

        if tracks:
            self.bot.config["autoPlaylistUrls"] = tracks
            self.bot.config["autoPlaylistUrl"] = playlist_url  # URL speichern für Refresh
            self._save_config()
            result["count"] = len(tracks)
            result["playlist_title"] = playlist_title
            result["playlist_uploader"] = playlist_uploader
            result["total_seconds"] = total_seconds
        return result

    async def refresh_playlist_if_changed(self) -> bool:
        """Prüft ob sich die YouTube-Playlist geändert hat und aktualisiert sie.
        Gibt True zurück wenn sich etwas geändert hat."""
        playlist_url = self.bot.config.get("autoPlaylistUrl", "")
        if not playlist_url:
            return False
        try:
            result = await self.set_auto_playlist_from_youtube(playlist_url)
            return result["count"] > 0
        except Exception as e:
            logger.warning(f"Playlist-Refresh fehlgeschlagen: {e}")
            return False

    def _create_fifo(self):
        try:
            if not os.path.exists(FIFO_PATH):
                os.mkfifo(FIFO_PATH)
        except FileExistsError:
            pass
        except Exception as e:
            logger.error(f"FIFO-Fehler: {e}")

    async def _open_fifo_async(self, timeout: float = 60.0):
        self._fifo_fd = None
        if not os.path.exists(FIFO_PATH):
            logger.warning(f"FIFO {FIFO_PATH} existiert nicht")
            return
        loop = asyncio.get_event_loop()
        try:
            fd = await asyncio.wait_for(loop.run_in_executor(None, lambda: os.open(FIFO_PATH, os.O_WRONLY)), timeout=timeout)
            self._fifo_fd = fd
            logger.info("FIFO geoeffnet")
        except asyncio.TimeoutError:
            logger.warning(f"FIFO nach {timeout}s nicht verfuegbar")
            self._fifo_fd = None
        except Exception as e:
            logger.warning(f"FIFO-Open Fehler: {e}")
            self._fifo_fd = None

    def _close_fifo(self):
        if self._fifo_fd is not None:
            try:
                os.close(self._fifo_fd)
                self._fifo_fd = None
            except Exception:
                pass

    async def add_to_queue(self, url: str, title: str, requested_by: str = "Unknown") -> bool:
        """Fügt einen Song zur User-Queue hinzu.
        Der aktuelle Song läuft weiter (wird NICHT abgebrochen).
        Der User-Song wird im Hintergrund vorgeladen.
        Wenn der aktuelle Song fertig ist → User-Song ist schon da → sofort abspielen.
        """
        self.queue.append({"url": url, "title": title, "requested_by": requested_by})
        self._save_state()
        self.ensure_play_loop()

        # Pre-Fetch neu starten damit der User-Song VOR dem Auto-Playlist Song geladen wird
        if requested_by != "Auto":
            logger.info(f"User-Song '{title[:40]}' eingereiht → starte Pre-Fetch")
            # Alten Pre-Fetch abbrechen (der evtl. Auto-Playlist Song lädt)
            if self._prefetch_task:
                self._prefetch_task.cancel()
                self._prefetch_task = None
            # Altes Pre-fetch PCM löschen (war evtl. Auto-Playlist Song)
            if self._next_pcm_file:
                try:
                    os.unlink(self._next_pcm_file)
                except Exception:
                    pass
                self._next_pcm_file = None
                self._next_track_meta = None
            # Neuen Pre-Fetch starten → lädt User-Queue[0] (User-Song hat Vorrang)
            self._prefetch_task = asyncio.create_task(self._download_next_song())

        return True

    def _get_next_track(self) -> dict:
        # User-Queue hat immer Vorrag — Songs werden nach Reihenfolge abgespielt
        if self.queue:
            track = self.queue.pop(0)
            track_url = track.get("url", "").strip().lower()
            if track_url:
                if not hasattr(self, "_played_urls"):
                    self._played_urls = set()
                self._played_urls.add(track_url)
            self._save_state()
            return track

        # Auto-Playlist: Überspringe Songs die bereits gespielt wurden
        if self.resolved_auto_playlist:
            idx = getattr(self, "auto_playlist_index", 0)
            tracks = self.resolved_auto_playlist
            played = getattr(self, "_played_urls", set())

            # Wenn alle Songs gespielt wurden → zurücksetzen für neuen Durchlauf
            total_tracks = len(tracks)
            if len(played) >= total_tracks:
                played.clear()
                self.auto_playlist_index = 0
                idx = 0
                logger.info("Alle Auto-Playlist Songs gespielt → neuer Durchlauf")

            for attempt in range(total_tracks):
                if idx >= total_tracks:
                    idx = 0
                track = tracks[idx]
                track_url = track.get("url", "").strip().lower()
                if track_url and track_url in played:
                    idx = (idx + 1) % total_tracks
                    continue
                # Index AKTUALISIEREN: nächster Song nach diesem
                self.auto_playlist_index = (idx + 1) % total_tracks
                if track_url:
                    played.add(track_url)
                # URL als gespielt markieren
                self._save_state()
                return track

        return None

    def _save_state(self):
        """Speichert den aktuellen Bot-Zustand in die DB (fuer Crash-Recovery).
        Wird NACH jedem Song-Wechsel aufgerufen."""
        if not self.bot or not hasattr(self.bot, "db"):
            return
        try:
            # Queue speichern
            self.bot.db["music_queue"] = [
                {"url": t.get("url", ""), "title": t.get("title", ""), "requested_by": t.get("requested_by", "Auto")}
                for t in self.queue
            ]
            # Auto-Playlist-Index speichern (zeigt auf NÄCHSTEN Song)
            self.bot.db["auto_playlist_index"] = getattr(self, "auto_playlist_index", 0)
            # Gespielte URLs speichern
            played = list(getattr(self, "_played_urls", set()))
            self.bot.db["played_urls"] = played[-200:]  # Max 200
            self._save_db()
        except Exception as e:
            logger.warning(f"State speichern fehlgeschlagen: {e}")

    async def skip_song(self):
        """Hartes Skip: Song stoppt SOFORT, dann warte auf Pre-fetch für nächsten Song."""
        if not self.song_playing and not self._ytdlp_process:
            return False

        logger.info("Skip: Song wird sofort gestoppt...")

        # Aktuelle URL aus played_urls entfernen damit sie nicht als "gespielt" zählt
        if self.current_track:
            current_url = self.current_track.get("url", "").strip().lower()
            if current_url and hasattr(self, "_played_urls"):
                self._played_urls.discard(current_url)

        # Download-Prozess nur killen wenn es ein Auto-Playlist Song ist
        is_user_song = (
            self.current_track
            and self.current_track.get("requested_by", "Auto") != "Auto"
        )
        if not is_user_song:
            if self._ytdlp_process and self._ytdlp_process.returncode is None:
                try:
                    self._ytdlp_process.kill()
                    logger.info("Skip: yt-Download-Prozess gekillt")
                except Exception:
                    pass
                self._ytdlp_process = None
        else:
            logger.info("Skip: User-Song wird nicht gekillt, nur übersprungen")

        # Song sofort stoppen
        self.song_playing = False

        # Wenn der aktuelle Track ein User-Song ist → überspringen (NICHT zurück in Queue)
        if self.current_track and self.current_track.get("requested_by", "Auto") != "Auto":
            logger.info(f"Skip: User-Song '{self.current_track.get('title', '')[:40]}' übersprungen")
            # Prüfen ob der vorhandene Pre-fetch bereits den richtigen nächsten Song hat
            _next_url = self.queue[0].get("url", "").strip().lower() if self.queue else ""
            _prefetch_url = (self._next_track_meta or {}).get("url", "").strip().lower()
            if self._next_pcm_file and _prefetch_url and (_prefetch_url == _next_url or not _next_url):
                # Pre-fetch passt bereits → einfach behalten, kein Neustart nötig
                logger.info("Skip: Pre-fetch bereits fertig für nächsten Song — wird wiederverwendet")
                if self._prefetch_task:
                    self._prefetch_task.cancel()
                    self._prefetch_task = None
            else:
                # Pre-fetch passt nicht oder fehlt → alten wegwerfen, neu starten
                if self._prefetch_task:
                    self._prefetch_task.cancel()
                    self._prefetch_task = None
                if self._next_pcm_file:
                    try:
                        os.unlink(self._next_pcm_file)
                    except Exception:
                        pass
                    self._next_pcm_file = None
                    self._next_track_meta = None
                # post_skip=True: ignore_skip im Download → startet sofort trotz skip_flag
                self._prefetch_task = asyncio.create_task(self._download_next_song(post_skip=True))
            self.skip_flag = True

        # Pre-Fetch neu starten wenn User-Queue nicht leer und Pre-fetch passt nicht
        elif self.queue:
            _next_url = self.queue[0].get("url", "").strip().lower() if self.queue else ""
            _prefetch_url = (self._next_track_meta or {}).get("url", "").strip().lower()
            if self._next_pcm_file and _prefetch_url == _next_url:
                # Pre-fetch passt → behalten
                logger.info("Skip: Pre-fetch bereits fertig für User-Queue Song — wird wiederverwendet")
                if self._prefetch_task:
                    self._prefetch_task.cancel()
                    self._prefetch_task = None
            elif self._prefetch_task:
                self._prefetch_task.cancel()
                self._prefetch_task = None
                if self._next_pcm_file:
                    try:
                        os.unlink(self._next_pcm_file)
                    except Exception:
                        pass
                    self._next_pcm_file = None
                    self._next_track_meta = None
                # post_skip=True: ignore_skip im Download → startet sofort trotz skip_flag
                self._prefetch_task = asyncio.create_task(self._download_next_song(post_skip=True))
                logger.info(f"Skip: Pre-fetch neu gestartet für User-Queue ({len(self.queue)} Songs)")
            self.skip_flag = True

        else:
            # Kein User-Queue-Spezialfall → skip_flag direkt setzen
            self.skip_flag = True

        # State speichern
        self._save_state()

        return True

    def _detect_youtube_challenge_error(self, error_text: str) -> bool:
        for pattern in YOUTUBE_CHALLENGE_ERRORS:
            if pattern.lower() in error_text.lower():
                return True
        return False

    async def _handle_429_backoff(self):
        self._consecutive_429 += 1
        wait = min(_429_BASE_WAIT * (2 ** (self._consecutive_429 - 1)), _429_MAX_WAIT)
        logger.warning(f"YouTube Rate-Limit (#{self._consecutive_429}) – warte {wait}s...")
        if self.bot:
            try:
                mins = int(wait // 60)
                secs = int(wait % 60)
                zeit = f"{mins}m {secs}s" if mins else f"{secs}s"
                await self.bot.highrise.chat(f"YouTube-Limit erreicht, Pause {zeit}...")
            except Exception:
                pass
        await asyncio.sleep(wait)
        self._consecutive_429 = max(0, self._consecutive_429 - 1)

    async def _handle_challenge_backoff(self):
        self._consecutive_challenge_failures += 1
        wait = min(60 * self._consecutive_challenge_failures, 600)
        logger.warning(f"YouTube Challenge (#{self._consecutive_challenge_failures}) – warte {wait}s...")
        if self.bot:
            try:
                mins = int(wait // 60)
                secs = int(wait % 60)
                zeit = f"{mins}m {secs}s" if mins else f"{secs}s"
                await self.bot.highrise.chat(f"YouTube blockiert. Pause {zeit}...")
            except Exception:
                pass
        await asyncio.sleep(wait)
        self._consecutive_challenge_failures = max(0, self._consecutive_challenge_failures - 1)

    # ── v11.1: Stream-Download mit Producer/Consumer (EINZIGER Pacer) ──

    async def _download_song_streaming(self, url: str, title: str, is_prefetch: bool = False, ignore_skip: bool = False) -> tuple[str, str, float]:
        """
        Laedt einen Song herunter und konvertiert ihn zu PCM.
        Gibt (pcm_path, title, duration) oder (None, title, 0) bei Fehler.
        is_prefetch=True: Skip-Flag wird NICHT konsumiert (nur Pre-fetch abbrechen, Flag bleibt fuer Play-Loop).
        ignore_skip=True: Skip-Flag komplett ignorieren (fuer Post-Skip Pre-fetch der den naechsten Song laedt).
        """
        # Skip-Check VOR dem Download
        if self.skip_flag and not ignore_skip:
            if is_prefetch:
                # Pre-fetch abbrechen, aber skip_flag NICHT loeschen —
                # der Play-Loop braucht es noch, um den aktuellen Song zu stoppen
                logger.info("Skip: Pre-fetch abgebrochen (Flag bleibt fuer Play-Loop)")
                return None, title, 0
            logger.info("Skip: Download abgebrochen (Vor-Check)")
            self.skip_flag = False
            return None, title, 0

        # Metadaten VORHER holen (für Duration)
        cached = self._metadata_cache.get(url)
        if cached:
            title = cached.get("title", title)
            duration = cached.get("duration", 0)
            del self._metadata_cache[url]
        else:
            metadata = await get_youtube_metadata(url)
            if metadata:
                title = metadata.get("title", title)
                duration = metadata.get("duration", 0)

        # Temp-Pfade erstellen (Dateien werden von yt-dlp/ffmpeg erstellt)
        _tmp_id = os.urandom(4).hex()
        audio_path = f"/tmp/highrise-audio-{_tmp_id}.audio"
        pcm_path = f"/tmp/highrise-song-{_tmp_id}.pcm"

        env = os.environ.copy()
        env["PATH"] = "/opt/highrise-bot:" + env.get("PATH", "/usr/bin:/bin")

        ytdlp = None
        ffmpeg_proc = None
        try:
            # Schritt 1: yt-dlp -> Audio-Datei (über -o)
            ytdlp_cmd = [
                "yt-dlp",
                "-f", "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
                "-q", "-o", audio_path, "--no-playlist"
            ] + _ytdlp_extra_args()
            ytdlp_cmd.append(url)

            ytdlp = await asyncio.create_subprocess_exec(
                *ytdlp_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self._ytdlp_process = ytdlp
            logger.info(f"yt-dlp gestartet PID={ytdlp.pid} für: {title}")
            _, stderr_data = await asyncio.wait_for(ytdlp.communicate(), timeout=180)
            self._ytdlp_process = None
            stderr_text = stderr_data.decode("utf-8", errors="ignore") if stderr_data else ""
            if stderr_text:
                logger.warning(f"yt-dlp stderr: {stderr_text[:500]}")
            logger.info(f"yt-dlp fertig returncode={ytdlp.returncode} für: {title}")
            
            # YouTube Rate-Limit Erkennung
            _rate_limited = False
            for _err_pat in YOUTUBE_RATELIMIT_ERRORS + YOUTUBE_CHALLENGE_ERRORS:
                if _err_pat.lower() in stderr_text.lower():
                    _rate_limited = True
                    break
            if _rate_limited:
                self._consecutive_429 += 1
                _wait = min(300 * self._consecutive_429, 3600)  # 5min, 10min, ... bis 1h
                logger.warning(f"YouTube Rate-Limit erkannt! Warte {_wait}s...")
                if self.bot:
                    try:
                        await self.bot.highrise.chat(f"⏳ YouTube-Limit — Pause {_wait//60}min...")
                    except Exception:
                        pass
                await asyncio.sleep(_wait)
            else:
                self._consecutive_429 = max(0, self._consecutive_429 - 1)

            # Skip-Check NACH yt-dlp
            if self.skip_flag:
                logger.info("Skip: Download abgebrochen (Nach yt-dlp)")
                self.skip_flag = False
                try:
                    os.unlink(audio_path)
                except Exception:
                    pass
                try:
                    os.unlink(pcm_path)
                except Exception:
                    pass
                return None, title, 0

            # Prüfe ob Audio-Datei existiert und groß genug ist
            if not os.path.exists(audio_path):
                logger.warning(f"Audio-Datei existiert nicht: {audio_path}")
                return None, title, 0

            audio_size = os.path.getsize(audio_path)
            logger.info(f"Audio-Datei Größe: {audio_size} bytes für: {title}")
            if audio_size < 1000:
                logger.warning(f"Audio-Datei zu klein ({audio_size} Bytes): {title}")
                try:
                    os.unlink(audio_path)
                except Exception:
                    pass
                try:
                    os.unlink(pcm_path)
                except Exception:
                    pass
                return None, title, 0

            # Schritt 2: ffmpeg konvertiert Audio -> PCM
            ffmpeg_proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", audio_path, "-vn",
                "-f", "s16le", "-ar", "44100", "-ac", "2",
                pcm_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(ffmpeg_proc.wait(), timeout=300)

            # Skip-Check NACH ffmpeg
            if self.skip_flag:
                logger.info("Skip: Download abgebrochen (Nach ffmpeg)")
                self.skip_flag = False
                try:
                    os.unlink(audio_path)
                except Exception:
                    pass
                try:
                    os.unlink(pcm_path)
                except Exception:
                    pass
                return None, title, 0

            # Audio-Datei löschen (wird nicht mehr gebraucht)
            try:
                os.unlink(audio_path)
            except Exception:
                pass

            if not os.path.exists(pcm_path):
                logger.warning(f"PCM-Datei existiert nicht: {pcm_path}")
                return None, title, 0

            pcm_size = os.path.getsize(pcm_path)
            if pcm_size < BYTES_PER_SECOND * 5:
                logger.warning(f"PCM-Datei zu klein ({pcm_size} Bytes): {title}")
                try:
                    os.unlink(pcm_path)
                except Exception:
                    pass
                return None, title, 0

            # Duration aus PCM-Größe berechnen (falls nicht aus Metadaten)
            if duration <= 0 and pcm_size > 0:
                duration = pcm_size / BYTES_PER_SECOND
                logger.info(f"Duration aus PCM berechnet: {duration:.0f}s")

            logger.info(f"Download fertig: {title} ({pcm_size} Bytes, {duration:.0f}s)")
            return pcm_path, title, duration

        except asyncio.TimeoutError:
            logger.warning(f"Download Timeout fuer: {title}")
            for p in [ffmpeg_proc, ytdlp]:
                if p and p.returncode is None:
                    try:
                        p.kill()
                    except Exception:
                        pass
            for path in [audio_path, pcm_path]:
                try:
                    if path:
                        os.unlink(path)
                except Exception:
                    pass
            return None, title, 0
        except Exception as e:
            logger.error(f"Download Fehler: {e}")
            for path in [audio_path, pcm_path]:
                try:
                    if path:
                        os.unlink(path)
                except Exception:
                    pass
            return None, title, 0

    async def _play_pcm_file(self, pcm_path: str) -> int:
        """Spielt eine PCM-Datei mit exaktem Pacing in die FIFO.
        Hartes Skip: skip_flag → Schleife bricht sofort beim nächsten Chunk ab (max. 100ms).
        """
        if self._fifo_fd is None:
            await self._open_fifo_async(timeout=30.0)
            if self._fifo_fd is None:
                return 0

        bytes_sent = 0
        chunk_duration = 0.1  # 100ms pro Chunk
        start_time = _time.monotonic()
        chunk_num = 0

        try:
            with open(pcm_path, "rb") as f:
                while not self.skip_flag:
                    data = f.read(READ_CHUNK)
                    if not data:
                        break

                    # Exaktes Pacing: Warte bis es Zeit ist den nächsten Chunk zu schreiben
                    target_time = start_time + (chunk_num * chunk_duration)
                    now = _time.monotonic()
                    wait_time = target_time - now

                    if wait_time > 0.001:
                        await asyncio.sleep(wait_time)

                    # Chunk in FIFO schreiben
                    try:
                        if self._fifo_fd is not None:
                            os.write(self._fifo_fd, data)
                            bytes_sent += len(data)
                            chunk_num += 1
                        else:
                            self._close_fifo()
                            await self._open_fifo_async(timeout=10.0)
                            if self._fifo_fd is None:
                                break
                            os.write(self._fifo_fd, data)
                            bytes_sent += len(data)
                            chunk_num += 1
                    except (BrokenPipeError, OSError):
                        self._close_fifo()
                        await self._open_fifo_async(timeout=10.0)
                        if self._fifo_fd is None:
                            break
        except Exception as e:
            logger.error(f"PCM Play Fehler: {e}")

        return bytes_sent

    async def _download_next_song(self, post_skip: bool = False):
        """Lädt den nächsten Song im Hintergrund herunter.
        Wird während des Abspielens des aktiven Songs aufgerufen.
        PRIORITÄT: User-Queue > Auto-Playlist
        post_skip=True: Dieser Pre-fetch wurde durch einen Skip ausgelöst —
        skip_flag ignorieren, da dieser Download genau der nächste Song nach dem Skip ist.
        """
        try:
            _next_track = None
            # 1. User-Queue hat IMMER Vorrang
            if self.queue:
                _next_track = self.queue[0]
            # 2. Auto-Playlist nur wenn User-Queue leer
            elif self.resolved_auto_playlist:
                _idx = getattr(self, "auto_playlist_index", 0)
                _played = getattr(self, "_played_urls", set())
                for _i in range(len(self.resolved_auto_playlist)):
                    _ci = (_idx + _i) % len(self.resolved_auto_playlist)
                    _ct = self.resolved_auto_playlist[_ci]
                    _cu = _ct.get("url", "").strip().lower()
                    if not _cu or _cu not in _played:
                        _next_track = _ct
                        break

            if not _next_track:
                return

            _url = _next_track.get("url", "")
            _title = _next_track.get("title", "Unbekannt")
            if not _url:
                return

            # Verhindere mehrfaches Pre-fetching
            if self._next_pcm_file:
                return

            logger.info(f"Pre-fetch: Lade nächsten Song herunter: {_title}...")
            _pcm_path, _dl_title, _dl_duration = await self._download_song_streaming(_url, _title, is_prefetch=True, ignore_skip=post_skip)
            if _pcm_path:
                self._next_pcm_file = _pcm_path
                self._next_track_meta = {
                    "url": _url,
                    "title": _dl_title or _title,
                    "requested_by": _next_track.get("requested_by", "Auto"),
                    "duration": _dl_duration,
                }
                logger.info(f"Pre-fetch fertig: {_dl_title or _title} ({_dl_duration:.0f}s)")
            else:
                logger.warning(f"Pre-fetch fehlgeschlagen für: {_title}")
        except Exception as _e:
            logger.debug(f"Pre-fetch exception: {_e}")

    async def _play_loop(self):
        """Endloser Play-Stream: IMMER Audio (Song oder Stille), NIE eine Pause.

        Pre-Fetch System:
        - Während Song A abgespielt wird, wird Song B heruntergeladen
        - Wenn Song A fertig ist, ist Song B bereits fertig → sofort abspielen
        - Song A wird nach dem Abspielen gelöscht
        """
        self.playing = True
        try:
            self._create_fifo()
            await self._open_fifo_async(timeout=30.0)

            while True:
                if self._fifo_fd is None:
                    await self._open_fifo_async(timeout=10.0)
                    if self._fifo_fd is None:
                        await asyncio.sleep(1.0)
                        continue

                # Nächsten Track holen
                track = self._get_next_track()
                if not track or not track.get("url"):
                    # Kein Track → Stille füttern
                    if self._fifo_fd is not None:
                        try:
                            os.write(self._fifo_fd, bytes(READ_CHUNK))
                        except (BrokenPipeError, OSError):
                            self._close_fifo()
                            await self._open_fifo_async(timeout=5.0)
                    await asyncio.sleep(0.1)
                    continue

                self.current_track = track
                url = track["url"]
                title = track.get("title", "Unbekannt")
                requested_by = track.get("requested_by", "Auto")

                import time as _time_mod
                self._song_start_time = _time_mod.time()
                self._song_duration = 0

                logger.info(f"Spiele jetzt: {title}")

                if self.bot and requested_by != "Auto":
                    try:
                        await self.bot.highrise.chat(f"♫ {title[:40]} – von {requested_by}")
                    except Exception:
                        pass

                self._last_error_429 = False
                self._last_error_challenge = False
                pcm_path = None
                dl_title = None
                dl_duration = 0

                # ── Schritt 0: Pre-fetch PCM verwenden? ──
                if self._next_pcm_file and self._next_track_meta:
                    _meta = self._next_track_meta
                    _meta_url = _meta.get("url", "").strip().lower()
                    _cur_url = url.strip().lower()
                    if _meta_url == _cur_url:
                        pcm_path = self._next_pcm_file
                        dl_title = _meta.get("title", title)
                        dl_duration = _meta.get("duration", 0)
                        self._next_pcm_file = None
                        self._next_track_meta = None
                        title = dl_title
                        if dl_duration > 0:
                            self.current_track["duration"] = dl_duration
                            self._song_duration = dl_duration
                        await write_song_info(title, dl_duration, url)
                        logger.info(f"Pre-fetch PCM verwendet: {title} ({dl_duration:.0f}s)")
                    else:
                        try:
                            os.unlink(self._next_pcm_file)
                        except Exception:
                            pass
                        self._next_pcm_file = None
                        self._next_track_meta = None

                # ── Schritt 1: Download wenn nötig (kein Pre-fetch verfügbar) ──
                if not pcm_path:
                    if self.skip_flag:
                        self.skip_flag = False
                        self.current_track = None
                        continue

                    logger.info(f"Download (kein Pre-fetch): {title}...")
                    try:
                        pcm_path, dl_title, dl_duration = await self._download_song_streaming(url, title)
                        if dl_title:
                            title = dl_title
                        if dl_duration and dl_duration > 0:
                            self.current_track["duration"] = dl_duration
                            self._song_duration = dl_duration
                        await write_song_info(title, dl_duration, url)
                    except Exception as e:
                        logger.error(f"Download Fehler: {e}")
                        pcm_path = None

                # Skip-Check nach Download
                if self.skip_flag:
                    self.skip_flag = False
                    if pcm_path:
                        try:
                            os.unlink(pcm_path)
                        except Exception:
                            pass
                    pcm_path = None
                    self.current_track = None
                    continue

                if not pcm_path:
                    logger.warning(f"Kein PCM für: {title}")
                    self.current_track = None
                    await asyncio.sleep(0.5)
                    continue

                # ── Schritt 2: PCM abspielen + Pre-fetch NÄCHSTEN Song ──
                self.song_playing = True
                self.skip_flag = False

                # Pre-fetch NÄCHSTEN Song im Hintergrund starten
                # User-Queue hat IMMER Vorrang vor Auto-Playlist
                # Der Pre-Fetch lädt den nächsten Song (User > Auto) damit
                # der Übergang lückenlos ist
                if self._prefetch_task:
                    self._prefetch_task.cancel()
                    self._prefetch_task = None
                # Altes Pre-fetch PCM löschen damit der neue Pre-Fetch
                # den richtigen Song lädt (User-Queue hat Vorrang)
                if self._next_pcm_file:
                    try:
                        os.unlink(self._next_pcm_file)
                    except Exception:
                        pass
                    self._next_pcm_file = None
                    self._next_track_meta = None
                self._prefetch_task = asyncio.create_task(self._download_next_song())

                # PCM-Datei mit Pacing in FIFO schreiben
                bytes_sent = 0
                try:
                    bytes_sent = await self._play_pcm_file(pcm_path)
                except Exception as e:
                    logger.error(f"PCM Play Fehler: {e}")

                # PCM-Datei SOFORT löschen (Platz sparen)
                try:
                    os.unlink(pcm_path)
                except Exception:
                    pass
                pcm_path = None

                self.song_playing = False

                # ── Skip-Check: Wenn skip_flag gesetzt → warte auf Pre-fetch ──
                if self.skip_flag:
                    logger.info("Skip-Flag gesetzt → warte auf Pre-fetch...")
                    self.skip_flag = False
                    self.current_track = None
                    self._save_state()
                    # AUF PRE-FETCH WARTEN (max 30s) — Song muss VOLLSTÄNDIG gedownloaded sein
                    if self._prefetch_task:
                        try:
                            await asyncio.wait_for(self._prefetch_task, timeout=30.0)
                            logger.info("Pre-fetch fertig → nächster Song wird abgespielt")
                        except asyncio.TimeoutError:
                            logger.warning("Pre-fetch Timeout (30s) → trotzdem weiter")
                    continue

                self.current_track = None
                self._save_state()

                # Kurz auf Pre-fetch warten (max 3s)
                if self._prefetch_task:
                    try:
                        await asyncio.wait_for(self._prefetch_task, timeout=3.0)
                    except asyncio.TimeoutError:
                        pass

                # Kurze Pause zwischen Songs (0.5s) — wichtig für YouTube Rate-Limit
                await asyncio.sleep(INTER_SONG_PAUSE)

        except asyncio.CancelledError:
            logger.info("Play-Loop abgebrochen.")
        except Exception as e:
            logger.error(f"Play-Loop Fehler: {e}", exc_info=True)
        finally:
            self.playing = False
            self.current_process = None
            self._ytdlp_process = None
            self.current_track = None