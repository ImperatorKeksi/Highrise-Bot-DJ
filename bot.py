# =============================================================
# bot.py – Highrise Bot Hauptdatei
# Highrise Python Bot SDK
#
# Refactored: 2025-06
#   - Removed unapproved commands: !gold, !goldgive, !reload,
#     !designer, !undesigner, !owner, !unowner
#   - Kept internal !reload as DEPRECATED (critical restart path)
#   - Added stub handlers for approved but missing commands:
#     !tip, !balance, !checkvip
#   - Added structured per-category loggers (music, payments,
#     vip, moderation, admin, errors, system)
#   - Improved error handling throughout _handle_cmd
#   - Added per-command execution timing logs
#   - All other logic, architecture and DB fields unchanged
# =============================================================
import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from collections import defaultdict

from dotenv import load_dotenv
from highrise import BaseBot, User, Position, CurrencyItem, Item
from highrise.__main__ import BotDefinition, main
from aiohttp import web
from music import MusicPlayer
from emotes_dict import EMOTES, EMOTE_BY_NUM

# ── Emote Config aus DB laden (überschreibt statische Defaults) ──
def load_emotes_config() -> dict:
    """Lädt die Emote-Konfiguration aus emotes_dict.py und gibt sie zurück.
    Format: {name: emote_id}"""
    return dict(EMOTES)

def get_emote_by_num_map() -> dict:
    """Gibt die Nummer-zu-Name Map zurück."""
    return dict(EMOTE_BY_NUM)

load_dotenv()

# ── Log-Verzeichnis anlegen ───────────────────────────────────
Path("logs").mkdir(exist_ok=True)

# ── Formatter (gemeinsam für alle Handler) ────────────────────
_log_fmt = logging.Formatter(
    "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

def _make_logger(name: str, filename: str) -> logging.Logger:
    """Erstellt einen dedizierten Logger der in eine eigene Datei *und*
    in die Konsole schreibt. Jede Kategorie bekommt so eine separate
    Log-Datei unter logs/<filename>."""
    lg = logging.getLogger(name)
    lg.setLevel(logging.DEBUG)
    fh = logging.FileHandler(f"logs/{filename}", encoding="utf-8")
    fh.setFormatter(_log_fmt)
    lg.addHandler(fh)
    return lg

# Root-Logger (bot.log + Konsole) – wie bisher
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("HRBot")

# Kategorie-Logger
log_music      = _make_logger("Music",      "music.log")
log_payments   = _make_logger("Payments",   "payments.log")
log_vip        = _make_logger("VIP",        "vip.log")
log_moderation = _make_logger("Moderation", "moderation.log")
log_admin      = _make_logger("Admin",      "admin.log")
log_errors     = _make_logger("Errors",     "errors.log")
log_system     = _make_logger("System",     "system.log")


# ─────────────────────────────────────────────────────────────
# Config / DB Helpers
# ─────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Lädt Konfiguration aus config.json (Web-Panel) oder .env"""
    cfg_path = Path("config.json")
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            log_system.info("Konfiguration aus config.json geladen")
            return json.load(f)
    log_system.info("Konfiguration aus .env geladen")
    return {
        "bot_token":        os.getenv("BOT_TOKEN", ""),
        "bot_id":           os.getenv("BOT_ID", ""),
        "room_id":          os.getenv("ROOM_ID", ""),
        "owner_id":         os.getenv("OWNER_ID", ""),
        "owner_username":   os.getenv("OWNER_USERNAME", "Owner"),
        "gold_enabled":     os.getenv("GOLD_ENABLED", "false") == "true",
        "gold_amount":      int(os.getenv("GOLD_AMOUNT", "0")),
        "gold_interval":    int(os.getenv("GOLD_INTERVAL_MINUTES", "0")) * 60,
        "gold_mode":        os.getenv("GOLD_INTERVAL_MODE", "fixed"),
        "gold_min":         int(os.getenv("GOLD_INTERVAL_MIN", "0")) * 60,
        "gold_max":         int(os.getenv("GOLD_INTERVAL_MAX", "0")) * 60,
        "music_enabled":    os.getenv("MUSIC_ENABLED", "true") == "true",
        "music_gold_cost":  int(os.getenv("MUSIC_GOLD_COST", "10")),
        "panel_password":   os.getenv("PANEL_PASSWORD", "keksadmin2026"),
        "vip_purchase_price":  int(os.getenv("VIP_PURCHASE_PRICE", "2000")),
    }


def load_db() -> dict:
    """Lädt Datenbank aus database.json.

    Fehlende Top-Level-Keys werden automatisch nachgerüstet (Migration),
    damit ältere database.json-Dateien nicht zu KeyErrors führen
    (z. B. fehlendes "music_credits" -> on_tip()/!balance/!play stürzten
    vorher ohne Fehlermeldung ab und der User bekam keine Antwort).
    """
    db_path = Path("database.json")
    if db_path.exists():
        with open(db_path, encoding="utf-8") as f:
            db = json.load(f)
    else:
        db = {}

    defaults = {
        "players": {},
        "music_credits": {},
        "vip_list": [],
        "vip_expiry": {},
        "vip_pending": {},
        "mod_list": [],
        "owner_list": [],
        "designer_list": [],
        "bot_position": None,
        "music_queue": [],
        "web_accounts": {},
        "recent_logs": [],
        "recent_gold_events": [],
        "played_urls": [],  # URLs der zuletzt gespielten Songs (Doppelten-Check)
        "emotes_config": {},  # Emote-System: {name: {emote_id, num, is_free, duration}}
        "all_emotes": [],  # Alle 307 Standard-Emotes (vom Web-Panel lesbar)
    }
    changed = False
    for key, default in defaults.items():
        if key not in db:
            db[key] = default
            changed = True

    if changed:
        log_system.info("database.json migriert: fehlende Felder ergänzt")
        save_db(db)

    return db


def save_db(db: dict):
    """Schreibt DB atomisch (write-to-temp + rename) mit File-Lock gegen Race Conditions."""
    import tempfile
    db_path = Path("database.json")
    try:
        # Atomic write: in temp file schreiben, dann rename (verhindert korrupte DB bei Crash)
        fd, tmp_path = tempfile.mkstemp(dir=str(db_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(db, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, str(db_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
    except Exception as e:
        # Fallback: direkt schreiben
        try:
            with open(db_path, "w", encoding="utf-8") as f:
                json.dump(db, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

def save_config(config: dict):
    """Schreibt Config atomisch (write-to-temp + rename) mit File-Lock gegen Race Conditions."""
    import tempfile
    cfg_path = Path("config.json")
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(cfg_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
            os.replace(tmp_path, str(cfg_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
    except Exception as e:
        log_system.error(f"Fehler beim Speichern der config.json: {e}")
        try:
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
        except Exception:
            pass


# ── Gold-Bar Helpers ────────────────────────────────────────────
# Highrise's tip_user() akzeptiert nur diese festen Stückelungen.
# Damit trotzdem JEDER beliebige Gold-Betrag funktioniert, wird der
# gewünschte Betrag automatisch in eine Kombination dieser Bars zerlegt
# (größte Stückelung zuerst, "Münzwechsel"-Logik).
GOLD_BAR_MAP = {
    10000: "gold_bar_10k", 5000: "gold_bar_5000", 1000: "gold_bar_1k",
    500: "gold_bar_500", 100: "gold_bar_100", 50: "gold_bar_50",
    10: "gold_bar_10", 5: "gold_bar_5", 1: "gold_bar_1",
}


def gold_to_bars(amount: int) -> list[str]:
    """Zerlegt einen beliebigen Gold-Betrag (>0) in eine Liste von
    Highrise-Gold-Bar-Items, die in Summe genau `amount` ergeben.

    Beispiel: 37 -> [gold_bar_10, gold_bar_10, gold_bar_10, gold_bar_5,
                     gold_bar_1, gold_bar_1]
    """
    remaining = int(amount)
    bars: list[str] = []
    for denom, item in sorted(GOLD_BAR_MAP.items(), reverse=True):
        while remaining >= denom:
            bars.append(item)
            remaining -= denom
    return bars


# ─────────────────────────────────────────────────────────────
# Bot
# ─────────────────────────────────────────────────────────────

class HighriseBot(BaseBot):

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.db = load_db()
        self.wallet_balance = 0
        logger.info(f"Bot initialisiert | Room: {self.config['room_id']}")
        log_system.info(f"Bot initialisiert | Room: {self.config['room_id']}")

        # Einmalige Nachberechnung: Spieler, die schon vor diesem Fix mehrfach
        # kleine Beträge gespendet haben (z.B. 1G x mehrmals), hatten dafür
        # nie Musik-Credits erhalten, weil die alte Logik nur EINZELNE Tips
        # >= music_gold_cost belohnt hat. Hier wird das nachgeholt.
        self._reconcile_music_credits()

        # Emote-System: Bei erstmaligem Start alle 307 Emotes in DB schreiben
        self._init_emotes_db()

        # Stats Cache
        self.start_time = None

        # Memory Cache for Live Web Panel (now backed by DB)
        self.recent_logs = self.db.get("recent_logs", [])
        self.recent_gold_events = self.db.get("recent_gold_events", [])
        self.web_sessions = {}  # token -> username
        self.ws_clients = set()

        # Musik Player
        self.music_player = MusicPlayer(self)

        # Aktive Spieler im Raum (wird vom Heartbeat und OnJoin/OnLeave aktualisiert)
        self.active_users = []  # Liste von {id, name, position}

        # Emote-System: User-ID → Emote-ID (wenn aktiv)
        self.active_emotes = {}  # user_id → emote_id
        self._emote_stop_flags = {}  # user_id → bool (True = stop requested)

        # Gold-Events History
        self._gold_events_buffer = []
        # Abstand feuern und dadurch Raum-Resyncs/doppelte Join-Events auslösen.
        self._last_teleport_ts = 0.0
        self._teleport_min_interval = 15.0  # Sekunden

        # Verhindert doppelte Join-Verarbeitung (z.B. durch Resync-Events),
        # die innerhalb kurzer Zeit für denselben User mehrfach feuern.
        self._recent_joins = {}   # user_id -> timestamp
        self._join_dedup_window = 10.0  # Sekunden

        # Verhindert doppelte Befehlsausführung, wenn Highrise dieselbe
        # Chat-Nachricht mehrfach kurz hintereinander liefert (z.B. bei
        # einem Raum-Resync). Schlüssel: (user_id, nachricht).
        self._recent_cmds = {}    # (user_id, message) -> timestamp
        self._cmd_dedup_window = 10.0  # Sekunden

        # Zählt aufeinanderfolgende Teleport-Fehler. Wenn der Bot intern im
        # "reconnecting"-Zustand feststeckt (verbunden laut PM2, aber alle
        # Teleports schlagen mit "Server error" fehl), erzwingt der Bot nach
        # MAX_TELEPORT_FAILURES einen harten Neustart über sys.exit().
        self._consecutive_teleport_failures = 0
        self._max_teleport_failures = 3

    # ── Einmalige Credit-Nachberechnung ───────────────────────

    def _init_emotes_db(self):
        """Schreibt die 307 Standard-Emotes in die DB falls noch nicht geschehen.
        Wird einmalig beim Bot-Start ausgefuhrt."""
        if self.db.get("all_emotes"):
            return  # Bereits initialisiert
        try:
            from emotes_complete import ALL_EMOTES
            self.db["all_emotes"] = ALL_EMOTES
            save_db(self.db)
            log_system.info(f"Emote-DB initialisiert: {len(ALL_EMOTES)} Emotes")
        except ImportError:
            log_system.warning("emotes_complete.py nicht gefunden — Emote-DB nicht initialisiert")

    def _reconcile_music_credits(self):
        """Gleicht für alle Spieler music_credits mit gold_donated ab.

        Vor diesem Fix wurden Credits nur vergeben, wenn ein EINZELNES
        Trinkgeld >= music_gold_cost war. Spieler, die mehrfach kleinere
        Beträge (z.B. mehrmals 1 Gold) gespendet haben, gingen leer aus,
        obwohl gold_donated korrekt mitgezählt wurde. Diese Funktion holt
        das beim ersten Start mit dem neuen Code einmalig nach.
        """
        db = self.db
        cost = self.config.get("music_gold_cost", 10)
        if cost <= 0:
            return

        changed = False
        for uid, player in db.get("players", {}).items():
            if self._is_vip_plus(uid):
                continue
            donated = player.get("gold_donated", 0)
            already_granted = player.get("credits_granted", 0)
            should_have = donated // cost
            missing = should_have - already_granted
            if missing > 0:
                db.setdefault("music_credits", {})
                db["music_credits"][uid] = db["music_credits"].get(uid, 0) + missing
                player["credits_granted"] = should_have
                player["gold_pending"] = donated % cost
                changed = True
                log_payments.info(
                    f"Nachberechnung Musikcredits: {player.get('username', uid)} "
                    f"+{missing} Credits (gold_donated={donated}G)"
                )
            elif "credits_granted" not in player or "gold_pending" not in player:
                # Felder nachrüsten, damit zukünftige Spenden korrekt verbucht werden
                player["credits_granted"] = already_granted
                player["gold_pending"] = donated % cost
                changed = True

        if changed:
            save_db(db)
            log_system.info("Musikcredits-Nachberechnung abgeschlossen")

    # ── In-Memory Log / Gold-Event Helpers ───────────────────

    def add_log(self, level: str, module: str, message: str, detail: str = ""):
        import random as _rnd
        now = datetime.now()
        entry = {
            "id": f"l{int(now.timestamp()*1000)}_{_rnd.randint(1000,9999)}",
            "timestamp": now.isoformat() + "Z",
            "date": now.strftime("%d.%m.%Y"),
            "time": now.strftime("%H:%M:%S"),
            "level": level,
            "module": module,
            "message": message,
            "detail": detail
        }
        self.recent_logs.insert(0, entry)
        if len(self.recent_logs) > 500:
            self.recent_logs.pop()
        # DB-Flush bei ERROR oder alle 20 Einträge
        if level == "ERROR" or len(self.recent_logs) % 20 == 0:
            self.db["recent_logs"] = self.recent_logs
            save_db(self.db)

    def add_gold_event(self, from_user: str, amount: int, evt_type: str, note: str = ""):
        entry = {
            "id": f"g{int(datetime.now().timestamp()*1000)}_{random.randint(1000,9999)}",
            "timestamp": datetime.now().isoformat() + "Z",
            "fromUser": from_user,
            "amount": amount,
            "type": evt_type,
            "note": note
        }
        self.recent_gold_events.insert(0, entry)
        if len(self.recent_gold_events) > 50:
            self.recent_gold_events.pop()
        # DB-Flush nur alle 10 Einträge oder bei ERROR
        if evt_type == "error" or len(self.recent_gold_events) % 10 == 0:
            self.db["recent_gold_events"] = self.recent_gold_events
            save_db(self.db)

    # ── Web-Panel Auth Helpers ────────────────────────────────

    def get_auth_roles(self, request) -> list:
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return []
        token = auth_header.split(" ")[1]

        # Admin Bypass via Master-Password
        if token == self.config.get("panel_password"):
            return ["owner"]

        username = self.web_sessions.get(token)
        if not username:
            return []

        roles = ["user"]
        for acc_user, acc_data in self.db.get("web_accounts", {}).items():
            if acc_user.lower() == username.lower():
                hid = acc_data.get("highrise_id")
                if hid:
                    if hid == self.config.get("owner_id") or hid in self.db.get("owner_list", []):
                        roles.append("owner")
                    if hid in self.db.get("mod_list", []):
                        roles.append("mod")
                    if hid in self.db.get("vip_list", []):
                        roles.append("vip")
                    if hid in self.db.get("designer_list", []):
                        roles.append("designer")
                break
        return list(set(roles))

    def check_auth(self, request) -> bool:
        return len(self.get_auth_roles(request)) > 0

    # ── Web-Panel API Handlers ────────────────────────────────

    async def api_web_accounts_handler(self, request):
        roles = self.get_auth_roles(request)
        if "owner" not in roles:
            return web.json_response(
                {"error": "Forbidden"}, status=403,
                headers={"Access-Control-Allow-Origin": "*"}
            )
        try:
            data = await request.json()
            action = data.get("action")
            username = data.get("username", "").strip()

            if "web_accounts" not in self.db:
                self.db["web_accounts"] = {}

            if action == "set":
                password = data.get("password")
                highrise_id = data.get("highrise_id")
                if not username or not password:
                    return web.json_response(
                        {"success": False, "error": "Username/Password missing"},
                        status=400, headers={"Access-Control-Allow-Origin": "*"}
                    )
                self.db["web_accounts"][username] = {
                    "password": password,
                    "highrise_id": highrise_id
                }
                save_db(self.db)
                log_admin.info(f"Web-Account erstellt/aktualisiert: {username}")
                self.add_log("SUCCESS", "SYSTEM", f"Web-Account {username} erstellt/aktualisiert", "")
                return web.json_response({"success": True}, headers={"Access-Control-Allow-Origin": "*"})

            elif action == "delete":
                if username in self.db["web_accounts"]:
                    del self.db["web_accounts"][username]
                    save_db(self.db)
                    log_admin.info(f"Web-Account gelöscht: {username}")
                    self.add_log("INFO", "SYSTEM", f"Web-Account {username} gelöscht", "")
                return web.json_response({"success": True}, headers={"Access-Control-Allow-Origin": "*"})

            return web.json_response(
                {"success": False, "error": "Unknown action"},
                status=400, headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            log_errors.exception(f"api_web_accounts_handler: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500, headers={"Access-Control-Allow-Origin": "*"}
            )

    async def api_login_handler(self, request):
        try:
            data = await request.json()
            username = data.get("username", "").strip()
            password = data.get("password", "")

            # Master Admin Fallback
            if (
                username.lower() == "admin"
                or username.lower() == self.config.get("owner_username", "").lower()
            ) and password == self.config.get("panel_password"):
                return web.json_response({
                    "success": True,
                    "token": self.config.get("panel_password"),
                    "username": username,
                    "roles": ["owner"]
                }, headers={"Access-Control-Allow-Origin": "*"})

            # Check Web Accounts
            if "web_accounts" not in self.db:
                self.db["web_accounts"] = {}

            for acc_user, acc_data in self.db["web_accounts"].items():
                if acc_user.lower() == username.lower() and acc_data.get("password") == password:
                    import uuid
                    token = str(uuid.uuid4())
                    self.web_sessions[token] = acc_user

                    hid = acc_data.get("highrise_id")
                    roles = ["user"]
                    if hid:
                        if hid == self.config.get("owner_id") or hid in self.db.get("owner_list", []):
                            roles.append("owner")
                        if hid in self.db.get("mod_list", []):
                            roles.append("mod")
                        if hid in self.db.get("vip_list", []):
                            roles.append("vip")
                        if hid in self.db.get("designer_list", []):
                            roles.append("designer")

                    return web.json_response({
                        "success": True,
                        "token": token,
                        "username": acc_user,
                        "roles": list(set(roles))
                    }, headers={"Access-Control-Allow-Origin": "*"})

            return web.json_response(
                {"success": False, "error": "Falscher Benutzername oder Passwort"},
                status=401, headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            log_errors.exception(f"api_login_handler: {e}")
            return web.json_response(
                {"success": False, "error": "Fehler"},
                status=400, headers={"Access-Control-Allow-Origin": "*"}
            )

    def get_dashboard_data(self):
        import time
        elapsed = 0
        if self.music_player and getattr(self.music_player, "_song_start_time", None):
            elapsed = max(0, time.time() - self.music_player._song_start_time)
            
        music_data = {
            "playing": self.music_player.playing if self.music_player else False,
            "current_track": self.music_player.current_track if self.music_player else None,
            "queue": self.music_player.queue if self.music_player else [],
            "queue_count": len(self.music_player.queue) if self.music_player else 0,
            "song_start_time": getattr(self.music_player, "_song_start_time", None) if self.music_player else None,
            "song_elapsed": elapsed,
            "song_duration": getattr(self.music_player, "_song_duration", 0) if self.music_player else 0,
        }

        # Rollen aufbereiten
        roles = {
            "owner_list": self.db.get("owner_list", []),
            "mod_list": self.db.get("mod_list", []),
            "vip_list": self.db.get("vip_list", []),
        }

        # Befehle aus Config
        commands = []
        for k, v in self.config.items():
            if k.startswith("custom_cmd_") and isinstance(v, dict):
                commands.append(v)
        # Standard-Befehle
        std_cmds = [
            {"cmd": "!help", "description": "Alle Befehle anzeigen", "role": "user"},
            {"cmd": "!emotelist", "description": "Alle Emotes mit Name + Nummer", "role": "user"},
            {"cmd": "!play [Song]", "description": "Song abplayen (kostet Gold)", "role": "user"},
            {"cmd": "!np", "description": "Aktuellen Song anzeigen", "role": "user"},
            {"cmd": "!queue", "description": "Warteschlange anzeigen", "role": "user"},
            {"cmd": "!top", "description": "Top-Spender anzeigen", "role": "user"},
            {"cmd": "!balance", "description": "Eigene Credits anzeigen", "role": "user"},
            {"cmd": "!tip [Betrag]", "description": "Gold spenden", "role": "user"},
            {"cmd": "!purchasevip", "description": "VIP kaufen (100 Gold)", "role": "user"},
            {"cmd": "!checkvip", "description": "VIP-Status prüfen", "role": "user"},
            {"cmd": "!skip", "description": "Song überspringen", "role": "mod"},
            {"cmd": "!stop", "description": "Musik stoppen", "role": "mod"},
            {"cmd": "!setvipprice [Gold]", "description": "VIP-Preis ändern", "role": "mod"},
            {"cmd": "!refund @user", "description": "Gold zurückerstatten", "role": "mod"},
            {"cmd": "!playlist [URL]", "description": "Playlist setzen/ändern", "role": "owner"},
            {"cmd": "!setpos", "description": "Bot-Position setzen", "role": "owner"},
            {"cmd": "!gold", "description": "Gold-Einstellungen anzeigen", "role": "owner"},
            {"cmd": "!gold on/off", "description": "Gold-Verteilung ein/aus", "role": "owner"},
            {"cmd": "!gold now", "description": "Gold sofort verteilen", "role": "owner"},
            {"cmd": "!checkgold", "description": "Gold-Status prüfen", "role": "owner"},
            {"cmd": "!reload", "description": "Config neu laden", "role": "owner"},
            {"cmd": "!restart", "description": "Bot neu starten", "role": "owner"},
            {"cmd": "!vip @user", "description": "VIP vergeben", "role": "owner"},
            {"cmd": "!unvip @user", "description": "VIP entziehen", "role": "owner"},
            {"cmd": "!mod @user", "description": "Moderator ernennen", "role": "owner"},
            {"cmd": "!unmod @user", "description": "Moderator entlassen", "role": "owner"},
        ]
        commands.extend(std_cmds)

        # Logs formatieren (letzte 500)
        logs = []
        for log_entry in getattr(self, "recent_logs", [])[-500:]:
            if isinstance(log_entry, dict):
                logs.append({
                    "id": log_entry.get("id", ""),
                    "timestamp": log_entry.get("timestamp", ""),
                    "date": log_entry.get("date", ""),
                    "time": log_entry.get("time", ""),
                    "level": log_entry.get("level", "info"),
                    "module": log_entry.get("module", ""),
                    "message": log_entry.get("message", str(log_entry)),
                    "detail": log_entry.get("detail", "")
                })
            else:
                logs.append({"id": "", "timestamp": "", "date": "", "time": "", "level": "info", "module": "", "message": str(log_entry), "detail": ""})

        # Spielerliste: Live-Daten aus Heartbeat verwenden (aktuelle Spieler im Raum)
        active_users = getattr(self, 'active_users', [])
        players_list = active_users if active_users else []
        total_gold = getattr(self, "wallet_balance", 0)

        return {
            "config": self.config,
            "db": self.db,
            "uptime": (datetime.now() - self.start_time).total_seconds()
                      if getattr(self, "start_time", None) else 0,
            "online": True,
            "music": music_data,
            "current_song": music_data["current_track"],
            "queue": music_data["queue"],
            "queue_count": music_data["queue_count"],
            "players": len(players_list),
            "players_list": players_list,
            "total_gold": total_gold,
            "roles": roles,
            "commands": commands,
            "web_accounts": self.db.get("web_accounts", {}),
            "logs": logs,
            "gold_events": self.recent_gold_events
        }

    async def api_handler(self, request):
        # Auth-Check: Token muss gültig sein
        # Ausnahme: GET ohne Token erlaubt — Panel ist ohne Login zugänglich
        if not self.check_auth(request):
            # Prüfe ob es ein Master-Password im Query-String ist
            master_pass = request.query.get("key", "")
            if master_pass != self.config.get("panel_password", ""):
                return web.json_response(
                    {"success": False, "error": "Unauthorized"},
                    status=401, headers={"Access-Control-Allow-Origin": "*"}
                )
        data = self.get_dashboard_data()
        return web.json_response(data, headers={"Access-Control-Allow-Origin": "*"})

    async def api_ws_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws_clients.add(ws)
        try:
            async for msg in ws:
                pass
        finally:
            self.ws_clients.discard(ws)
        return ws

    async def ws_broadcast_loop(self):
        while True:
            await asyncio.sleep(1)
            if not getattr(self, "ws_clients", None):
                continue
            try:
                data = self.get_dashboard_data()
                for ws in list(self.ws_clients):
                    try:
                        await ws.send_json(data)
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"WS broadcast error: {e}")

    async def options_handler(self, request):
        return web.Response(headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        })

    async def api_queue_handler(self, request):
        # Auth-Check
        if not self.check_auth(request):
            return web.json_response(
                {"success": False, "error": "Unauthorized"},
                status=401, headers={"Access-Control-Allow-Origin": "*"}
            )
        try:
            data = await request.json()
            action = data.get("action")

            if action == "add":
                query = data.get("query")
                if not query:
                    return web.json_response(
                        {"success": False, "error": "No query provided"},
                        status=400, headers={"Access-Control-Allow-Origin": "*"}
                    )
                try:
                    title = await self.music_player.add_to_queue(query, query[:80], "Web-Panel")
                except ValueError as e:
                    return web.json_response(
                        {"success": False, "error": str(e)},
                        status=400, headers={"Access-Control-Allow-Origin": "*"}
                    )
                log_music.info(f"Web-Panel: Song zur Queue hinzugefügt: {title}")
                return web.json_response({"success": True, "title": title},
                                         headers={"Access-Control-Allow-Origin": "*"})

            return web.json_response(
                {"success": False, "error": "Unknown action"},
                status=400, headers={"Access-Control-Allow-Origin": "*"}
            )

        except Exception as e:
            log_errors.exception(f"api_queue_handler: {e}")
            logger.error(f"API Queue Error: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500, headers={"Access-Control-Allow-Origin": "*"}
            )

    async def api_resolve_playlist_handler(self, request):
        # Auth: Bearer-Token ODER Panel-Key
        if not self.check_auth(request) and not self.panel_key_ok(request):
            return web.json_response(
                {"success": False, "error": "Unauthorized"},
                status=401, headers={"Access-Control-Allow-Origin": "*"}
            )
        try:
            data = await request.json()
            url = data.get("url")
            if not url:
                return web.json_response(
                    {"success": False, "error": "No URL provided"},
                    status=400, headers={"Access-Control-Allow-Origin": "*"}
                )

            proc = await asyncio.create_subprocess_exec(
                "yt-dlp", "--flat-playlist", "--dump-json", url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await proc.communicate()

            resolved = []
            for line in stdout.decode().strip().split("\n"):
                if not line:
                    continue
                import json as _json
                try:
                    track = _json.loads(line)
                    vid_url = track.get("url", "")
                    if not vid_url.startswith("http"):
                        vid_id = track.get("id")
                        if vid_id:
                            vid_url = f"https://www.youtube.com/watch?v={vid_id}"
                    if vid_url:
                        dur = track.get("duration", 0)
                        resolved.append({
                            "title": track.get("title", "Auto-Playlist Song"),
                            "url": vid_url,
                            "duration": dur if dur else 0
                        })
                except Exception:
                    pass

            return web.json_response(
                {"success": True, "tracks": resolved},
                headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            log_errors.exception(f"api_resolve_playlist_handler: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500, headers={"Access-Control-Allow-Origin": "*"}
            )

    async def api_roles_handler(self, request):
        # Auth-Check (nur Owner)
        roles = self.get_auth_roles(request)
        if "owner" not in roles:
            return web.json_response(
                {"success": False, "error": "Unauthorized"},
                status=401, headers={"Access-Control-Allow-Origin": "*"}
            )
        try:
            data = await request.json()
            # Unterstütze beide Formate: user_id+role (String) UND target_id+roles (Array)
            target_id = data.get("user_id") or data.get("target_id")
            role = data.get("role")
            roles = data.get("roles", [])
            
            if not roles and role:
                roles = [role]

            if not target_id:
                return web.json_response(
                    {"success": False, "error": "Missing user_id"},
                    status=400, headers={"Access-Control-Allow-Origin": "*"}
                )

            # Wenn roles explizit leer ist → alle Rollen entfernen
            if not roles and not role:
                roles = []  # Leer = alle entfernen

            # Rollen überschreiben (außer config.owner_id)
            if target_id == self.config.get("owner_id"):
                return web.json_response(
                    {"success": False, "error": "Haupt-Owner kann nicht geändert werden"},
                    status=403, headers={"Access-Control-Allow-Origin": "*"}
                )

            # Designer-Rolle entfernt — nicht mehr unterstützt
            roles = [r for r in roles if r != "designer"]

            # Alle existierenden Listen bereinigen
            for lst in ["owner_list", "mod_list", "vip_list", "designer_list"]:
                if lst not in self.db:
                    self.db[lst] = []
                self.db[lst] = [x for x in self.db[lst] if x != target_id]

            # Neue Rollen setzen
            if "owner" in roles:
                self.db["owner_list"].append(target_id)
            if "mod" in roles:
                self.db["mod_list"].append(target_id)
            if "vip" in roles:
                self.db["vip_list"].append(target_id)
            # Designer wird nicht mehr gesetzt

            save_db(self.db)
            log_admin.info(f"Rollen für {target_id} über Web-Panel gesetzt: {', '.join(roles)}")
            self.add_log(
                "SUCCESS", "ROLES",
                f"Rollen für {target_id} über Web-Panel gespeichert",
                f"Neue Rollen: {', '.join(roles)}"
            )
            return web.json_response({"success": True},
                                     headers={"Access-Control-Allow-Origin": "*"})
        except Exception as e:
            log_errors.exception(f"api_roles_handler: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500, headers={"Access-Control-Allow-Origin": "*"}
            )

    def panel_key_ok(self, request):
        """Prüft ob der Panel-Key im Query-String korrekt ist."""
        key = request.query.get("key", "")
        if key:
            return key == self.config.get("panel_password", "")
        return False

    async def api_config_handler(self, request):
        # Auth: Bearer-Token ODER Panel-Key
        if not self.check_auth(request) and not self.panel_key_ok(request):
            return web.json_response(
                {"success": False, "error": "Unauthorized"},
                status=401, headers={"Access-Control-Allow-Origin": "*"}
            )
        try:
            data = await request.json()
            # Mapping: camelCase (Panel) → snake_case (Bot-intern)
            _key_sync = {
                "goldEnabled": "gold_enabled",
                "goldAmount": "gold_amount",
                "goldIntervalMode": "gold_mode",
                "musicEnabled": "music_enabled",
                "musicGoldCost": "music_gold_cost",
            }
            # Diese Keys sind im Panel in Minuten, aber der Bot braucht Sekunden
            _key_sync_minutes_to_seconds = {
                "goldIntervalMinutes": "gold_interval",
                "goldIntervalMin": "gold_min",
                "goldIntervalMax": "gold_max",
            }
            for k, v in data.items():
                if k != "action":
                    self.config[k] = v
                    # Sync: wenn camelCase reinkommt, auch snake_case setzen
                    if k in _key_sync:
                        self.config[_key_sync[k]] = v
                    elif k in _key_sync_minutes_to_seconds:
                        # Panel sendet Minuten, Bot braucht Sekunden
                        try:
                            self.config[_key_sync_minutes_to_seconds[k]] = int(v) * 60
                        except (ValueError, TypeError):
                            self.config[_key_sync_minutes_to_seconds[k]] = 0
            save_config(self.config)
            
            # Web Panel Log — zeige welche Keys geändert wurden
            changed_keys = [k for k in data.keys() if k != "action"]
            if changed_keys:
                self.add_log("WARNING", "WEB", f"Einstellungen geändert", f"Keys: {', '.join(changed_keys)}")

            log_admin.info(f"config.json via Web-Panel aktualisiert: {list(data.keys())}")

            # Trigger play loop if auto playlist is enabled and bot is idle
            if hasattr(self, "music_player") and self.music_player:
                if self.config.get("autoPlaylistEnabled", True):
                    self.music_player.ensure_play_loop()

            return web.json_response({"success": True},
                                     headers={"Access-Control-Allow-Origin": "*"})
        except Exception as e:
            log_errors.exception(f"api_config_handler: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500, headers={"Access-Control-Allow-Origin": "*"}
            )

    async def api_database_handler(self, request):
        # Auth: Bearer-Token ODER Panel-Key
        if not self.check_auth(request) and not self.panel_key_ok(request):
            return web.json_response(
                {"success": False, "error": "Unauthorized"},
                status=401, headers={"Access-Control-Allow-Origin": "*"}
            )
        try:
            data = await request.json()
            if "data" in data:
                new_db = data["data"]
                if not isinstance(new_db, dict):
                    return web.json_response(
                        {"success": False, "error": "DB muss ein Objekt sein"},
                        status=400, headers={"Access-Control-Allow-Origin": "*"}
                    )
                import shutil, os
                if os.path.exists("database.json"):
                    shutil.copy2("database.json", "database.json.bak")
                self.db.clear()
                self.db.update(new_db)
                save_db(self.db)
                
                # Web Panel Log — DB Änderung protokollieren
                vip_count = len([k for k in new_db.get("vip_list", [])])
                mod_count = len([k for k in new_db.get("mod_list", [])])
                player_count = len([k for k in new_db.get("players", {})])
                self.add_log("WARNING", "WEB", "Datenbank aktualisiert", f"{player_count} Spieler, {vip_count} VIPs, {mod_count} Mods")
                
                log_admin.info(f"database.json via Web-Panel überschrieben ({len(new_db)} keys)")
                return web.json_response({"success": True}, headers={"Access-Control-Allow-Origin": "*"})
            else:
                return web.json_response(
                    {"success": False, "error": "Missing 'data' field"},
                    status=400, headers={"Access-Control-Allow-Origin": "*"}
                )
        except Exception as e:
            log_errors.exception(f"api_database_handler: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500, headers={"Access-Control-Allow-Origin": "*"}
            )

    async def api_restart_handler(self, request):
        """Bot und Stream Master neu starten — mit Speichern & Validieren."""
        # Auth: Bearer-Token ODER Panel-Key
        if not self.check_auth(request) and not self.panel_key_ok(request):
            return web.json_response(
                {"success": False, "error": "Unauthorized"},
                status=401, headers={"Access-Control-Allow-Origin": "*"}
            )
        try:
            import subprocess

            # ── 1. Alle Daten speichern ──
            # DB speichern
            if hasattr(self, "db"):
                save_db(self.db)
                log_admin.info("Web-Panel: DB gespeichert")
            # Config speichern
            if hasattr(self, "config"):
                try:
                    save_config(self.config)
                    log_admin.info("Web-Panel: Config gespeichert")
                except Exception as e:
                    logger.warning(f"Config speichern fehlgeschlagen: {e}")

            # ── 2. Validieren (soft – fehlende Felder werden ergänzt) ──
            try:
                if Path("database.json").exists():
                    with open("database.json", "r", encoding="utf-8") as f:
                        db_check = json.load(f)
                    if "auto_playlist_index" not in db_check:
                        db_check["auto_playlist_index"] = 0
                        with open("database.json", "w", encoding="utf-8") as f:
                            json.dump(db_check, f, indent=2, ensure_ascii=False)
                        log_admin.warning("Web-Panel: auto_playlist_index fehlte → auf 0 gesetzt")
                    log_admin.info("Web-Panel: ✅ DB validiert")
            except Exception as e:
                logger.error(f"Web-Panel: ❌ DB Validierung fehlgeschlagen: {e}")
                # Trotzdem weiter – nicht blockieren
            try:
                if Path("config.json").exists():
                    with open("config.json", "r", encoding="utf-8") as f:
                        cfg_check = json.load(f)
                    if "autoPlaylistUrls" not in cfg_check:
                        cfg_check["autoPlaylistUrls"] = []
                        save_config(cfg_check)
                        log_admin.warning("Web-Panel: autoPlaylistUrls fehlte → leeres Array gesetzt")
                    log_admin.info("Web-Panel: ✅ Config validiert")
            except Exception as e:
                logger.error(f"Web-Panel: ❌ Config Validierung fehlgeschlagen: {e}")
                # Trotzdem weiter – nicht blockieren

            # ── 3. Stream Master neu starten ──
            subprocess.run(
                ["pm2", "restart", "stream-master"],
                capture_output=True, timeout=10
            )
            log_admin.info("Web-Panel: Stream Master neu gestartet")

            # ── 4. Bot neu starten (PM2 managed) ──
            subprocess.Popen(
                ["pm2", "restart", "highrise-bot"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            log_admin.info("Web-Panel: Bot Restart ausgelöst (Save → Validate → Restart)")

            return web.json_response(
                {"success": True, "message": "Neustart eingeleitet (Save → Validate → Restart)"},
                headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            log_errors.exception(f"api_restart_handler: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500, headers={"Access-Control-Allow-Origin": "*"}
            )

    # ── Emote Config API ─────────────────────────────────────

    async def api_emotes_handler(self, request):
        """Gibt die Emote-Konfiguration zuruck (Name → Emote ID + Meta-Daten).
        Kein Auth nötig — Panel ist ohne Login zugänglich."""
        emotes_config = self.db.get("emotes_config", {})
        return web.json_response(
            {"success": True, "emotes": emotes_config},
            headers={"Access-Control-Allow-Origin": "*"}
        )

    async def api_emotes_save_handler(self, request):
        """Speichert oder aktualisiert einen Emote-Eintrag.
        Kein Auth nötig — Panel ist ohne Login zugänglich."""
        try:
            data = await request.json()
            name = data.get("name", "").strip().lower()
            emote_id = data.get("emote_id", "").strip()
            num = data.get("num")
            is_free = data.get("is_free")
            duration = data.get("duration")

            if not name:
                return web.json_response(
                    {"success": False, "error": "Name fehlt"},
                    status=400, headers={"Access-Control-Allow-Origin": "*"}
                )
            if not emote_id:
                return web.json_response(
                    {"success": False, "error": "Emote-ID fehlt"},
                    status=400, headers={"Access-Control-Allow-Origin": "*"}
                )

            # Prüfen ob neues Emote oder Update
            emotes_config = self.db.get("emotes_config", {})
            is_update = name in emotes_config

            # Alten Eintrag löschen falls Umbenennung
            old_name = data.get("old_name")
            old_name_display = None
            if old_name and old_name != name and old_name in emotes_config:
                old_name_display = old_name
                del emotes_config[old_name]

            # Bestehende Werte erhalten (falls nicht überschrieben)
            existing = emotes_config.get(name, {})
            entry = {
                "emote_id": emote_id,
                "num": num if num is not None else existing.get("num")
            }
            if is_free is not None:
                entry["is_free"] = is_free
            else:
                entry["is_free"] = existing.get("is_free")
            if duration is not None:
                entry["duration"] = float(duration)
            else:
                entry["duration"] = existing.get("duration", 4.0)

            emotes_config[name] = entry
            self.db["emotes_config"] = emotes_config
            save_db(self.db)

            # Web Panel Log-Eintrag
            if old_name_display:
                self.add_log("WARNING", "WEB", f"Emote umbenannt: {old_name_display} → {name}", f"ID: {emote_id}")
            elif is_update:
                self.add_log("WARNING", "WEB", f"Emote aktualisiert: {name}", f"ID: {emote_id}")
            else:
                self.add_log("WARNING", "WEB", f"Neues Emote erstellt: {name}", f"ID: {emote_id}")

            log_admin.info(f"Emote gespeichert: {name} → {emote_id}")
            return web.json_response(
                {"success": True, "message": f"Emote '{name}' gespeichert"},
                headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            log_errors.exception(f"api_emotes_save_handler: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500, headers={"Access-Control-Allow-Origin": "*"}
            )

    async def api_emotes_delete_handler(self, request):
        """Löscht einen Emote-Eintrag.
        Kein Auth nötig — Panel ist ohne Login zugänglich."""
        try:
            data = await request.json()
            name = data.get("name", "").strip().lower()

            if not name:
                return web.json_response(
                    {"success": False, "error": "Name fehlt"},
                    status=400, headers={"Access-Control-Allow-Origin": "*"}
                )

            emotes_config = self.db.get("emotes_config", {})
            if name in emotes_config:
                del emotes_config[name]
                self.db["emotes_config"] = emotes_config
                save_db(self.db)
                self.add_log("WARNING", "WEB", f"Emote gelöscht: {name}", "")
                log_admin.info(f"Emote gelöscht: {name}")
                return web.json_response(
                    {"success": True, "message": f"Emote '{name}' gelöscht"},
                    headers={"Access-Control-Allow-Origin": "*"}
                )
            else:
                return web.json_response(
                    {"success": False, "error": f"Emote '{name}' nicht gefunden"},
                    status=404, headers={"Access-Control-Allow-Origin": "*"}
                )
        except Exception as e:
            log_errors.exception(f"api_emotes_delete_handler: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500, headers={"Access-Control-Allow-Origin": "*"}
            )

    async def web_panel_handler(self, request):
        """Liefert das Web-Panel aus (neues v2 Panel)."""
        panel_path = Path("/opt/highrise-web-panel/index.html")
        if panel_path.exists():
            return web.FileResponse(panel_path)
        # Fallback: altes Panel
        panel_path2 = Path("dist/index.html")
        if panel_path2.exists():
            return web.FileResponse(panel_path2)
        return web.Response(
            text="Web-Panel nicht gefunden.",
            status=404
        )

    async def radio_proxy_handler(self, request: web.Request) -> web.StreamResponse:
        """
        Proxyt den Icecast-Stream (/radio) durch den aiohttp-Server,
        damit Cloudflare ihn nicht als langen Request abbricht.
        Setzt Transfer-Encoding: chunked + Cache-Control: no-cache.
        """
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "audio/mpeg",
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "Access-Control-Allow-Origin": "*",
            },
        )
        resp.enable_chunked_encoding()
        await resp.prepare(request)

        import aiohttp as _aiohttp
        try:
            timeout = _aiohttp.ClientTimeout(connect=5, sock_read=None)
            async with _aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get("http://127.0.0.1:8000/stream.mp3") as icecast:
                    async for chunk in icecast.content.iter_chunked(8192):
                        try:
                            await resp.write(chunk)
                        except (ConnectionResetError, BrokenPipeError):
                            break
        except Exception as e:
            logger.debug(f"Radio-Proxy beendet: {e}")

        return resp

    async def start_api(self):
        app = web.Application()
        app.router.add_options('/api/login',             self.options_handler)
        app.router.add_post('/api/login',                self.api_login_handler)
        app.router.add_get('/api/data',                  self.api_handler)
        app.router.add_options('/api/data',              self.options_handler)
        app.router.add_options('/api/queue',             self.options_handler)
        app.router.add_post('/api/queue',                self.api_queue_handler)
        app.router.add_options('/api/resolve_playlist',  self.options_handler)
        app.router.add_post('/api/resolve_playlist',     self.api_resolve_playlist_handler)
        app.router.add_options('/api/roles',             self.options_handler)
        app.router.add_post('/api/roles',                self.api_roles_handler)
        app.router.add_options('/api/web_accounts',      self.options_handler)
        app.router.add_post('/api/web_accounts',         self.api_web_accounts_handler)
        app.router.add_options('/api/config',            self.options_handler)
        app.router.add_post('/api/config',               self.api_config_handler)
        app.router.add_options('/api/restart',            self.options_handler)
        app.router.add_post('/api/restart',               self.api_restart_handler)
        app.router.add_options('/api/database',           self.options_handler)
        app.router.add_post('/api/database',              self.api_database_handler)
        app.router.add_get('/api/ws',                    self.api_ws_handler)
        app.router.add_get('/',                          self.web_panel_handler)
        app.router.add_get('/radio',                      self.radio_proxy_handler)
        app.router.add_options('/api/emotes',             self.options_handler)
        app.router.add_get('/api/emotes',                self.api_emotes_handler)
        app.router.add_post('/api/emotes',               self.api_emotes_save_handler)
        app.router.add_post('/api/emotes/delete',         self.api_emotes_delete_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', 5000)
        try:
            await site.start()
            asyncio.create_task(self.ws_broadcast_loop())
            logger.info("API Server gestartet auf Port 5000")
            log_system.info("API Server gestartet auf Port 5000")
        except Exception as e:
            logger.error(f"Konnte API Server nicht starten: {e}")
            log_errors.exception(f"API Server konnte nicht starten: {e}")

    # ── Lifecycle Hooks ───────────────────────────────────────

    async def on_start(self, session_metadata) -> None:
        self._on_start_called = True
        self.start_time = datetime.now()
        logger.info(f"Bot verbunden | Room: {self.config.get('room_id')}")
        log_system.info(f"Bot verbunden | Room: {self.config.get('room_id')}")

        # API in den Hintergrund
        if not getattr(self, "_api_started", False):
            self._api_started = True
            asyncio.create_task(self.start_api())
        self.music_player.start()
        logger.info("Bot bereit!")

        # Stream-Recovery: Nach Join Stream neu starten
        asyncio.create_task(self._recover_stream_on_join())

        try:
            bot_wallet = await self.highrise.get_wallet()
            if bot_wallet and bot_wallet.content:
                self.wallet_balance = getattr(bot_wallet.content[0], "amount", 0)
                logger.info(f"Start-Guthaben: {self.wallet_balance} Gold")
                log_payments.info(f"Start-Guthaben: {self.wallet_balance} Gold")
        except Exception as e:
            logger.warning(f"Konnte Wallet nicht abrufen: {e}")
            log_errors.warning(f"on_start: Wallet nicht abrufbar: {e}")

        # Warteschlange wiederherstellen (User-Wünsche die nicht gespielt wurden)
        if self.db.get("music_queue"):
            restored_queue = []
            for item in self.db["music_queue"]:
                if isinstance(item, dict) and item.get("url"):
                    restored_queue.append({
                        "url": item.get("url", ""),
                        "title": item.get("title", "Unbekannt"),
                        "requested_by": item.get("requested_by", "Auto"),
                    })
            self.music_player.queue = restored_queue
            logger.info(f"Warteschlange wiederhergestellt ({len(self.music_player.queue)} User-Songs)")
            log_music.info(f"Warteschlange wiederhergestellt ({len(self.music_player.queue)} User-Songs)")
        
        # Auto-Playlist-Index: Bei Neustart aus DB laden (Crash-Recovery)
        if self.music_player:
            saved_index = self.db.get("auto_playlist_index", 0)
            self.music_player.auto_playlist_index = saved_index
            logger.info(f"Auto-Playlist: Fortsetzung bei Index {saved_index}")

        # Zur gespeicherten Position laufen (mit Retry-Logik)
        async def move_and_play():
            # 15s warten: Highrise akzeptiert Teleport-Befehle erst einige Sekunden
            # nach dem Bot-Join – zu frühe Teleports liefern "Server error".
            await asyncio.sleep(15.0)
            await self._teleport_to_saved_position("Start")

        asyncio.create_task(move_and_play())

        # Timer starten – Guard-Flags verhindern doppelte Loops bei Reconnects
        # Alte Tasks cancellen falls vorhanden (bei Reconnect)
        if getattr(self, "_gold_loop_task", None):
            self._gold_loop_task.cancel()
        if getattr(self, "_heartbeat_loop_task", None):
            self._heartbeat_loop_task.cancel()
        if getattr(self, "_emote_loop_task", None):
            self._emote_loop_task.cancel()
        
        self._gold_loop_task = asyncio.create_task(self.gold_loop())
        self._heartbeat_loop_task = asyncio.create_task(self.heartbeat_loop())
        self._emote_loop_task = asyncio.create_task(self._emote_loop())

        # "Bot ist online" nur senden wenn letzte Nachricht >5 Min her (verhindert Spam bei Neustarts)
        last_online_msg = self.db.get("_last_online_msg", 0)
        if (time.time() - last_online_msg) > 300:
            await self.highrise.chat(
                "🤖 Bot ist online! Schreibe !help für alle Befehle."
            )
            self.db["_last_online_msg"] = time.time()
            save_db(self.db)
        self.add_log("SUCCESS", "SYSTEM", "Bot gestartet", f"Room: {self.config.get('room_id')}")
        log_system.info(f"Bot vollständig gestartet | Room: {self.config.get('room_id')}")

    async def heartbeat_loop(self):
        """Sendet alle 60 Sekunden einen Keepalive damit Highrise den Bot nicht
        wegen Inaktivität disconnectet. 
        JEDER Tick (= 1 Min): Emote senden (Wink) für Aktivität.
        Alle 2 Ticks (= 2 Min): Zur gespeicherten Position teleportieren
        um die Position für alle Clients zu synchronisieren.
        
        v11.0: Emote jede Minute, Teleport alle 2 Minuten, Auto-Rejoin wenn rausgeworfen."""
        logger.info("Heartbeat-Loop gestartet")
        await asyncio.sleep(20)  # kurz nach Start warten
        tick = 0
        
        while True:
            tick += 1
            is_bot_in_room = False
            real_users = []
            try:
                # get_room_users hält die WebSocket-Verbindung aktiv
                # Rate-Limit: Max 1x pro 5s, daher Timeout von 10s
                try:
                    users = await asyncio.wait_for(
                        self.highrise.get_room_users(),
                        timeout=10.0
                    )
                except asyncio.TimeoutError:
                    # Rate-Limit erreicht — überspringen
                    logger.debug(f"Heartbeat Tick {tick}: get_room_users Timeout (Rate-Limit)")
                    await asyncio.sleep(30)
                    continue

                # Fehler-Antwort prüfen
                if hasattr(users, 'message') and hasattr(users, 'do_not_reconnect'):
                    # Das ist ein Error-Objekt
                    err_msg = str(getattr(users, 'message', '')).lower()
                    if "rate" in err_msg or "limit" in err_msg:
                        logger.warning(f"Heartbeat Tick {tick}: Rate-Limit, warte 30s")
                        await asyncio.sleep(30)
                        continue
                    logger.warning(f"Heartbeat Tick {tick}: Error: {users.message}")
                    await self._try_rejoin_room()
                    continue

                # Normale Response
                user_list = []
                if hasattr(users, 'content') and users.content:
                    user_list = list(users.content)
                elif hasattr(users, 'users'):
                    user_list = list(users.users)

                # Prüfen ob irgendein Spieler (nicht nur der Bot) im Raum ist
                real_users = [u for u in user_list if u[0].id != self.highrise.my_id] if user_list else []

                # ── Aktive Spieler-Liste aktualisieren (für Web Dashboard) ──
                self.active_users = []
                is_bot_in_room = False
                seen_uids = set()
                for user_obj, position in user_list:
                    if user_obj.id == self.highrise.my_id:
                        is_bot_in_room = True
                        continue
                    uid = user_obj.id
                    # Deduplizieren
                    if uid in seen_uids:
                        continue
                    seen_uids.add(uid)
                    uname = getattr(user_obj, 'username', str(uid))
                    # DB-Daten nachfalls nachladen
                    pdata = self.db.get("players", {}).get(uid, {})
                    self.active_users.append({
                        "id": uid,
                        "name": uname,
                        "gold_donated": pdata.get("gold_donated", 0),
                        "credits": pdata.get("music_credits", 0),
                        "is_vip": uid in self.db.get("vip_list", []),
                        "is_mod": uid in self.db.get("mod_list", []),
                        "is_owner": uid == self.config.get("owner_id") or uid in self.db.get("owner_list", []),
                        "joined_at": pdata.get("joined_at")
                    })

                if not is_bot_in_room:
                    logger.warning(f"Heartbeat Tick {tick}: Bot nicht im Raum!")
                    await self._try_rejoin_room()
                elif not real_users:
                    logger.debug(f"Heartbeat Tick {tick}: Kein Spieler im Raum (nur Bot)")
                else:
                    logger.debug(f"Heartbeat Tick {tick}: {len(real_users)} Spieler im Raum: {[u[0].username for u in real_users]}")

            except Exception as e:
                err_str = str(e).lower()
                # "already added" = Bot ist noch verbunden, kein Rejoin nötig
                if "already added" in err_str or "already in room" in err_str:
                    logger.debug(f"Bot bereits im Raum (kein Rejoin nötig): {e}")
                elif "room" in err_str or "not found" in err_str or "not in room" in err_str:
                    logger.warning(f"Bot nicht mehr im Raum! Versuche neu zu joinen: {e}")
                    await self._try_rejoin_room()
                else:
                    logger.warning(f"Heartbeat fehlgeschlagen (Tick {tick}): {e}")
                    log_errors.warning(f"Heartbeat fehlgeschlagen (Tick {tick}): {e}")
                    await asyncio.sleep(5)
                    
                    # Nach Verbindungsfehler: Prüfen ob Bot noch im Raum ist
                    try:
                        await self.highrise.get_room_users()
                    except Exception as e2:
                        logger.warning(f"Verbindungsversuch fehlgeschlagen: {e2}")
                        log_errors.warning(f"Heartbeat Reconnect fehlgeschlagen: {e2}")
            
            # ── Emote/Teleport: Nur wenn Bot im Raum (is_bot_in_room gesetzt) ──
            if is_bot_in_room:
                # Emote senden (Wink)
                try:
                    wink_id = EMOTES.get("wink") or EMOTES.get("wave") or "emote-wave"
                    await self.highrise.send_emote(wink_id)
                    logger.info(f"Emote gesendet: {wink_id} (Tick {tick})")
                except Exception as e:
                    logger.debug(f"Emote übersprungen (Tick {tick}): {e}")

                # ── Alle 2 Minuten: Zur Home-Position teleportieren ──
                if tick % 2 == 0:
                    now_ts = time.time()
                    if not hasattr(self, '_last_heartbeat_teleport') or (now_ts - self._last_heartbeat_teleport) > 90:
                        try:
                            pos = self.db.get("bot_position")
                            if pos:
                                await self.highrise.teleport(
                                    self.highrise.my_id,
                                    Position(pos["x"], pos["y"], pos["z"], pos.get("facing", "FrontRight"))
                                )
                                logger.info(f"Teleport zur Home-Position: {pos}")
                        except Exception as e:
                            logger.debug(f"Teleport fehlgeschlagen: {e}")
                        self._last_heartbeat_teleport = now_ts

            await self._check_vip_expiry()
            await asyncio.sleep(60)

    async def _emote_loop(self):
        """Hintergrund-Task: Sendet aktiven Spieler immer wieder ihr Emote.
        Wiederholt das Emote kontinuierlich ohne Pause bis der Spieler 'stop' schreibt."""
        logger.info("Emote-Loop gestartet")
        
        async def _safe_send(eid, u_id):
            try:
                await self.highrise.send_emote(eid, u_id)
            except Exception:
                pass

        while True:
            if not self.active_emotes:
                await asyncio.sleep(0.1)
                continue
            active_copy = dict(self.active_emotes)
            for uid, emote_id in active_copy.items():
                if self._emote_stop_flags.get(uid, False):
                    self.active_emotes.pop(uid, None)
                    self._emote_stop_flags.pop(uid, None)
                    continue
                try:
                    # Emote asynchron senden damit der Loop nicht blockiert
                    asyncio.create_task(_safe_send(emote_id, uid))
                except Exception:
                    pass
            # Emote-Dauer-Loop: User wünscht 0.01s für absoluten Spam.
            await asyncio.sleep(1)

    async def _try_rejoin_room(self):
        """Versucht den Bot in den Raum neu zu joinen.
        Wird aufgerufen wenn der Bot erkennt dass er nicht mehr im Raum ist."""
        room_id = self.config.get("room_id")
        if not room_id:
            logger.error("Keine room_id in Config — kann nicht neu joinen")
            return
        
        # Mehrere Versuche mit exponential backoff
        for attempt in range(1, 6):
            try:
                wait_time = min(10 * (2 ** (attempt - 1)), 60)  # 10s, 20s, 40s, 60s, 60s
                logger.info(f"Rejoin-Versuch {attempt}/5 — warte {wait_time}s...")
                await asyncio.sleep(wait_time)
                
                # Versuche zu teleportieren — wenn das fehlschlägt, ist der Bot nicht im Raum
                try:
                    users_check = await self.highrise.get_room_users()
                    if hasattr(users_check, 'message') and hasattr(users_check, 'do_not_reconnect'):
                        # Es ist ein Error-Objekt ("Not in room") -> überspringen
                        logger.debug(f"get_room_users returned error: {users_check.message}")
                    else:
                        # Wenn das klappt und kein Error ist, ist der Bot noch im Raum
                        logger.info("Bot ist noch im Raum — kein Rejoin nötig")
                        return
                except:
                    pass
                
                # Bot ist nicht im Raum — neu joinen über room API
                logger.info(f"Versuche Raum {room_id} neu zu joinen (Versuch {attempt})...")
                
                # Rejoin über die Highrise API
                try:
                    # Methode 1: Über WebSocket reconnect
                    if hasattr(self.highrise, 'join_room'):
                        await self.highrise.join_room(room_id)
                        logger.info(f"✅ Bot erfolgreich in Raum {room_id} gejoint (join_room)")
                        return
                except Exception as e:
                    logger.warning(f"join_room fehlgeschlagen: {e}")
                
                # Methode 2: Über teleport (joint den Bot in den Raum)
                try:
                    await self.highrise.teleport(
                        self.highrise.my_id,
                        Position(0, 0, 0, "FrontRight"),
                        room_id
                    )
                    logger.info(f"✅ Bot erfolgreich in Raum {room_id} gejoint (teleport)")
                    return
                except Exception as e:
                    logger.warning(f"Teleport-Rejoin fehlgeschlagen: {e}")
                    
            except Exception as e:
                logger.warning(f"Rejoin-Versuch {attempt}/5 fehlgeschlagen: {e}")
        
        logger.error("❌ Alle Rejoin-Versuche fehlgeschlagen — Erzwinge Neustart durch PM2!")
        self._save_db_to_disk()
        os._exit(1)

    async def _on_ws_disconnect(self, *args, **kwargs):
        """Wird aufgerufen wenn die WebSocket-Verbindung unterbrochen wird."""
        logger.warning("WebSocket-Verbindung unterbrochen!")
        log_errors.warning("WebSocket disconnect erkannt")

        # Warte kurz — die Highrise-Bibliothek reconnectet oft automatisch
        await asyncio.sleep(10)

        # Prüfe ob der Bot noch im Raum ist (automatischer Reconnect hat funktioniert)
        try:
            users = await self.highrise.get_room_users()
            user_list = list(users.content) if hasattr(users, 'content') else []
            bot_in_room = any(u[0].id == self.highrise.my_id for u in user_list)
            if bot_in_room:
                logger.info("Bot ist noch im Raum (automatischer Reconnect erfolgreich) — kein Rejoin nötig")
                return
        except Exception:
            pass

        # Bot ist nicht im Raum — versuche neu zu joinen
        logger.warning("Bot nicht im Raum nach Disconnect — versuche Rejoin...")
        await self._try_rejoin_room()

    async def _check_vip_expiry(self):
        """VIP ist jetzt unbegrenzt – Ablaufprüfung deaktiviert."""
        return

    async def _recover_stream_on_join(self):
        """Stellt sicher, dass der Stream nach dem Join wieder läuft.
        v8.5: Stream-Master wird NICHT mehr neu gestartet — das würde den
        Radio-Link unterbrechen. Stattdessen wird nur geprüft ob der
        stream-master läuft und wenn nicht, neu gestartet."""
        await asyncio.sleep(25)
        try:
            import subprocess
            result = subprocess.run(["pm2","list"],capture_output=True,text=True,timeout=5)
            if "stream-master" in result.stdout:
                if "online" in result.stdout:
                    logger.info("Stream-Master läuft bereits — kein Neustart nötig (Radio-Link stabil)")
                else:
                    subprocess.run(["pm2","restart","stream-master"],capture_output=True,timeout=10)
                    logger.info("Stream-Master war offline — neu gestartet")
            else:
                logger.warning("Stream-Master nicht in PM2-Liste gefunden")
        except Exception as e:
            logger.warning(f"Stream-Recovery fehlgeschlagen: {e}")

    async def _teleport_to_saved_position(self, reason: str = "", force: bool = False) -> None:
        """Teleportiert den Bot zu seiner gespeicherten Position (mit Retry-Logik)."""
        pos = self.db.get("bot_position")
        if not pos:
            return
        now = time.time()
        if not force and (now - self._last_teleport_ts) < self._teleport_min_interval:
            logger.debug(f"Teleport übersprungen (Throttle) - {reason}")
            return
        if not hasattr(self, "_teleport_lock"):
            self._teleport_lock = asyncio.Lock()
        if self._teleport_lock.locked():
            logger.debug(f"Teleport übersprungen (Lock belegt) - {reason}")
            return
        async with self._teleport_lock:
            for attempt in range(1, 6):
                try:
                    await self.highrise.teleport(
                        self.highrise.my_id,
                        Position(pos["x"], pos["y"], pos["z"], pos.get("facing", "FrontRight"))
                    )
                    self._last_teleport_ts = time.time()
                    self._consecutive_teleport_failures = 0
                    logger.info(f"Zur Position teleportiert: {pos} (Versuch {attempt}) - {reason}")
                    return
                except Exception as e:
                    logger.warning(f"Teleport Versuch {attempt}/5 fehlgeschlagen: {e}")
                    if attempt < 5:
                        await asyncio.sleep(5.0 * attempt)
                    else:
                        self._consecutive_teleport_failures += 1
                        logger.error(f"Teleport nach 5 Versuchen aufgegeben.")
                        # NICHT bei Teleport-Fehlern den Bot beenden – Highrise braucht Zeit nach Join
                        # if self._consecutive_teleport_failures >= self._max_teleport_failures:
                        #     import sys
                        #     sys.exit(1)

    async def on_user_leave(self, user: User) -> None:
        logger.info(f"Verlassen: {user.username}")
        self.add_log("INFO", "BOT", f"{user.username} hat den Raum verlassen")
        if user.id in self.db["players"]:
            joined_str = self.db["players"][user.id].get("joined_at")
            if joined_str:
                try:
                    joined_at = datetime.fromisoformat(joined_str)
                    duration = (datetime.now() - joined_at).total_seconds()
                    self.db["players"][user.id]["time_in_room"] = (
                        self.db["players"][user.id].get("time_in_room", 0) + duration
                    )
                    self.db["players"][user.id]["joined_at"] = None
                except Exception:
                    pass

        self.active_users = [u for u in self.active_users if u["id"] != user.id]

        # Emote-System: Aktives Emote des Users löschen
        self.active_emotes.pop(user.id, None)
        self._emote_stop_flags.pop(user.id, None)

        # Aktive !purchasevip-Session sofort beenden, wenn der Spieler den Raum
        # verlässt. Der bisherige Fortschritt (received) bleibt gespeichert und
        # "eingefroren" – Gold, das er nach dem Wiederbeitreten spendet, zählt
        # dann normal als Musik-Credit, bis er erneut !purchasevip eingibt.
        pending = self.db.get("vip_pending", {}).get(user.id)
        if pending and pending.get("expires_at", 0) >= time.time():
            pending["expires_at"] = 0
            log_vip.info(
                f"VIP-Session beendet (Raum verlassen): {user.username} (uid={user.id}) "
                f"| Fortschritt eingefroren: {pending.get('received', 0)}/{pending.get('amount', 0)}G"
            )

        save_db(self.db)

        # Prüfen ob der Raum jetzt leer ist (nur der Bot selbst noch drin)
        asyncio.create_task(self._keepalive_on_empty_room())

    async def _keepalive_on_empty_room(self):
        """Wird aufgerufen wenn ein Spieler geht – läuft solange der Raum leer ist.
        Sendet häufige Aktionen damit Highrise den Bot nicht wegen Inaktivität rauswirft."""
        await asyncio.sleep(2.0)
        try:
            room = await self.highrise.get_room_users()
            others = [u for u, _ in room.content if u.id != self.highrise.my_id]
            if not others:
                logger.info("Raum ist leer – verstaerkter Keepalive aktiv (alle 15s).")
                tick = 0
                while True:
                    await asyncio.sleep(15)
                    tick += 1
                    try:
                        room = await self.highrise.get_room_users()
                        others = [u for u, _ in room.content if u.id != self.highrise.my_id]
                        logger.debug(f"Empty-Room Keepalive – {len(others)} andere User im Raum")
                        if others:
                            logger.info("Spieler beigetreten – Empty-Room Keepalive beendet.")
                            await self._teleport_to_saved_position("Re-Teleport nach leerem Raum")
                            break

                        # Alle 15s Teleport + Emote als Aktivitäts-Keepalive
                        await self._teleport_to_saved_position("Aktivitaets-Keepalive")
                        try:
                            wave_id = EMOTES.get("wave")
                            if wave_id:
                                await self.highrise.send_emote(wave_id)
                                logger.debug("Aktivitaets-Keepalive: Wave-Emote gesendet")
                        except Exception as e:
                            logger.warning(f"Keepalive-Emote fehlgeschlagen: {e}")
                            
                        # Alle 30s (jeder 2. Tick) zusaetzlich Chat-Nachricht senden
                        if tick % 2 == 0:
                            try:
                                heartbeat_msg = [
                                    "🎵 Radio Highrise – Musik laeuft!",
                                    "📻 Highrise Bot Radio – 24/7 Musik",
                                    "🎶 Radio am Laeuften – Highrise Musik",
                                    "🤖 Highrise Radio Bot – online",
                                ]
                                msg = heartbeat_msg[tick % len(heartbeat_msg)]
                                await self.highrise.chat(msg)
                                logger.debug(f"Keepalive-Chat gesendet: {msg}")
                            except Exception as e:
                                logger.warning(f"Keepalive-Chat fehlgeschlagen: {e}")
                    except Exception as e:
                        err_str = str(e).lower()
                        if "room" in err_str or "not found" in err_str or "not in room" in err_str:
                            logger.warning(f"Bot nicht mehr im Raum!: {e}")
                            await self._try_rejoin_room()
                            break
                        else:
                            logger.warning(f"Empty-Room Keepalive fehlgeschlagen: {e}")
                            break
        except Exception as e:
            logger.warning(f"Raum-Pruefung nach User-Leave fehlgeschlagen: {e}")

    async def _delayed_join_sync(self) -> None:
        """Sorgt dafür, dass neu beigetretene Clients den Bot an der richtigen Position sehen.

        Strategie: Zwei-Schritt-Teleport mit 2s Pause dazwischen.
        Schritt 1 teleportiert zu einer leicht versetzten Zwischenposition,
        Schritt 2 zur exakten Zielposition. Das erzwingt eine sichtbare
        Positionsänderung ohne walk_to (walk_to kann clientseitig fälschlicherweise
        ein on_user_join-Event auslösen → Doppel-Whisper)."""
        pos = self.db.get("bot_position")
        if not pos:
            return

        await asyncio.sleep(4.0)

        # Schritt 1: Zwischenposition
        try:
            mid = Position(
                pos["x"] + 0.5, pos["y"], pos["z"] + 0.5,
                pos.get("facing", "FrontRight")
            )
            await self.highrise.teleport(self.highrise.my_id, mid)
            logger.info("Join-Sync Schritt 1: Zwischenposition")
        except Exception as e:
            logger.warning(f"Join-Sync Schritt 1 fehlgeschlagen: {e}")

        await asyncio.sleep(2.0)

        # Schritt 2: Exakte Zielposition (force=True, da Schritt 1 den Drossel-Timestamp
        # gerade aktualisiert hat und wir sonst blockiert würden)
        await self._teleport_to_saved_position("Join-Sync Schritt 2", force=True)

    async def on_user_join(self, user: User, position) -> None:
        db = self.db

        # Zeitbasierter Dedup
        now = time.time()
        last_join = self._recent_joins.get(user.id, 0)
        is_deduped = (now - last_join) < self._join_dedup_window
        if is_deduped:
            logger.debug(f"on_user_join schnelles Duplikat ignoriert: {user.username}")
            return
        self._recent_joins[user.id] = now

        logger.info(f"Beigetreten: {user.username}")
        self.add_log("INFO", "BOT", f"{user.username} hat den Raum betreten")

        # Verzögerter Sync-Teleport für neue Clients
        asyncio.create_task(self._delayed_join_sync())
        asyncio.create_task(self._keepalive_on_empty_room())

        if user.id not in db["players"]:
            db["players"][user.id] = {
                "username": user.username,
                "gold_donated": 0,
                "songs_added": 0,
                "time_in_room": 0,
                "joined_at": datetime.now().isoformat(),
            }
        else:
            db["players"][user.id]["joined_at"] = datetime.now().isoformat()
            
        if not any(u["id"] == user.id for u in self.active_users):
            pdata = db["players"][user.id]
            self.active_users.append({
                "id": user.id,
                "name": user.username,
                "gold_donated": pdata.get("gold_donated", 0),
                "credits": pdata.get("music_credits", 0),
                "is_vip": user.id in db.get("vip_list", []),
                "is_mod": user.id in db.get("mod_list", []),
                "is_owner": user.id == self.config.get("owner_id") or user.id in db.get("owner_list", []),
                "joined_at": pdata.get("joined_at")
            })

        save_db(db)

        try:
            await self.highrise.send_whisper(
                user.id,
                f"👋 Willkommen {user.username}! Schreibe !help für Befehle."
            )
            logger.info(f"Willkommens-Whisper gesendet an {user.username}")
        except Exception as e:
            logger.warning(f"Whisper fehlgeschlagen für {user.username}: {e}")

        # Willkommen-Emote — der BOT winkt, nicht der Spieler
        try:
            wave_id = EMOTES.get("wave") or "emote-wave"
            await self.highrise.send_emote(wave_id)
            logger.info(f"Willkommen-Emote: Bot winkt für {user.username}")
        except Exception as e:
            logger.debug(f"Willkommen-Emote fehlgeschlagen für {user.username}: {e}")

    async def on_tip(self, sender: User, receiver: User, tip: CurrencyItem | Item) -> None:
        # on_tip feuert je nach SDK/Server-Version möglicherweise nicht zuverlässig.
        # Die eigentliche Verarbeitung läuft über _process_gold_tip(), das auch
        # von on_message (Wallet-Diff-Methode) aufgerufen wird. Dedup via
        # _processed_tip_ids verhindert Doppelverarbeitung falls beide feuern.
        logger.info(
            f"[TIP-DEBUG] on_tip aufgerufen! sender={sender.username}, "
            f"receiver={receiver.username}, amount={getattr(tip, 'amount', 'unknown')}"
        )

        my_id = getattr(self.highrise, "my_id", None) or self.config.get("bot_id", "")
        if my_id and receiver.id != my_id:
            return

        try:
            amount = tip.amount
        except AttributeError:
            amount = 0
            logger.warning("[TIP-DEBUG] tip objekt hat kein .amount Attribut!")

        if amount <= 0:
            return

        # Wallet-Balance aktualisieren damit on_message-Wallet-Diff korrekt bleibt
        try:
            bot_wallet = await self.highrise.get_wallet()
            if bot_wallet and bot_wallet.content:
                self.wallet_balance = getattr(bot_wallet.content[0], "amount", 0)
        except Exception:
            pass

        await self._process_gold_tip(sender.id, sender.username, amount, source="on_tip")

    async def _process_gold_tip(self, sender_id: str, sender_username: str, amount: int, source: str = "unknown") -> None:
        """Gemeinsame Logik für eingehende Gold-Tips.

        Wird sowohl von on_tip (falls SDK das Event liefert) als auch von
        on_message (Wallet-Diff-Methode, falls on_tip nicht feuert) aufgerufen.
        Dedup via _processed_tip_ids verhindert Doppelverarbeitung.
        """
        logger.info(f"Gold empfangen [{source}]: {sender_username} → {amount}G")
        log_payments.info(f"Gold empfangen [{source}]: {sender_username} ({sender_id}) → {amount}G")
        self.add_gold_event(sender_username, amount, "received", f"Spende an Bot [{source}]")
        self.add_log("SUCCESS", "GOLD", "Gold empfangen",
                     f"{sender_username} hat {amount} Gold gespendet [{source}]")

        try:
            db = self.db
            player = db["players"].setdefault(sender_id, {
                "username": sender_username,
                "gold_donated": 0,
                "credits_granted": 0,
            })
            player["username"] = sender_username
            player["gold_donated"] = player.get("gold_donated", 0) + amount
            cost = self.config.get("music_gold_cost", 10)

            # ── VIP-Kauf (Fortschritt bleibt nach Session-Ablauf "eingefroren") ──
            # Nur während einer AKTIVEN !purchasevip-Session (60s) zählt eingehendes
            # Gold zum VIP-Fortschritt. Läuft die Session ab (z.B. weil der Spieler
            # den Raum verlässt/abstürzt), bleibt der bisherige Fortschritt gespeichert,
            # aber neues Gold läuft normal als Musik-Credit-Spende weiter – bis der
            # Spieler erneut !purchasevip eingibt, um den Kauf fortzusetzen.
            vip_pending_entry = db.get("vip_pending", {}).get(sender_id)
            if vip_pending_entry and vip_pending_entry.get("expires_at", 0) >= time.time():
                target_amount = vip_pending_entry.get("amount", 0)
                received = vip_pending_entry.get("received", 0) + amount
                if received >= target_amount:
                    overpay = received - target_amount
                    del db["vip_pending"][sender_id]
                    if sender_id not in db["vip_list"]:
                        db["vip_list"].append(sender_id)
                    save_db(db)
                    await self.highrise.chat(
                        f"⭐ {sender_username} hat VIP gekauft! Songwünsche jetzt dauerhaft ohne Gold-Tipping."
                    )
                    extra = f"\n💰 Überschuss von {overpay}G wurde als Guthaben verbucht." if overpay else ""
                    await self.highrise.send_whisper(
                        sender_id,
                        f"⭐ VIP dauerhaft aktiviert! Du kannst jetzt !play ohne Gold-Tipping nutzen.{extra}"
                    )
                    log_vip.info(
                        f"VIP gekauft: {sender_username} (uid={sender_id}) | "
                        f"{target_amount}G (+{overpay}G Überschuss) | dauerhaft [{source}]"
                    )
                    self.add_log("SUCCESS", "VIP", f"{sender_username} hat VIP gekauft",
                                 "Dauerhaft")
                    if overpay > 0:
                        amount = overpay  # Überschuss als normale Spende weiterverarbeiten
                    else:
                        return
                else:
                    vip_pending_entry["received"] = received
                    save_db(db)
                    remaining = target_amount - received
                    await self.highrise.send_whisper(
                        sender_id,
                        f"⭐ VIP-Kauf: {received}/{target_amount}G erhalten. "
                        f"Noch {remaining}G für dauerhaften VIP. "
                        f"Du hast noch Zeit, weitere Tips zu senden."
                    )
                    log_vip.info(
                        f"VIP-Kauf Teilzahlung: {sender_username} (uid={sender_id}) "
                        f"| {received}/{target_amount}G [{source}]"
                    )
                    return
            # Kein vip_pending oder Session abgelaufen: Gold läuft normal als
            # Musik-Credit-Spende weiter; ein evtl. gespeicherter VIP-Fortschritt
            # bleibt unverändert ("eingefroren"), bis !purchasevip erneut aufgerufen wird.

            # ── Musik-Credits (kumulativ) ─────────────────────────────────────
            if not self._is_vip_plus(sender_id) and self.config.get("music_enabled"):
                gold_acc = player.get("gold_pending", 0) + amount
                new_credits = gold_acc // cost
                gold_acc = gold_acc % cost
                player["gold_pending"] = gold_acc

                if new_credits > 0:
                    player["credits_granted"] = player.get("credits_granted", 0) + new_credits
                    db.setdefault("music_credits", {})
                    db["music_credits"][sender_id] = (
                        db["music_credits"].get(sender_id, 0) + new_credits
                    )
                    log_payments.info(
                        f"Musikcredits vergeben: {sender_username} +{new_credits} Credit(s) "
                        f"(Zahlung: {amount}G, Rest-Guthaben: {gold_acc}G) [{source}]"
                    )
                    new_balance = db["music_credits"][sender_id]
                    await self.highrise.chat(
                        f"💰 Danke {sender_username} für {amount} Gold! "
                        f"Du hast {new_credits} Musikwunsch/Wünsche freigeschaltet "
                        f"(Guthaben: {new_balance} Credits)! "
                        f"Schreibe: !play [Songname - Artist] um deinen Wunsch einzutragen (Picks from YouTube)"
                    )
                    await self.highrise.send_whisper(
                        sender_id,
                        f"🎶 Danke für deine Spende von {amount} Gold!\n"
                        f"Du hast +{new_credits} Musik-Credit(s) erhalten.\n"
                        f"Aktuelles Guthaben: {new_balance} Credit(s).\n"
                        + (f"Dein Gold-Restguthaben: {gold_acc}G (zählt für den nächsten Credit).\n"
                           if gold_acc else "")
                        + "Schreibe !play [Songname - Artist] um deinen Wunsch einzutragen (Picks from YouTube)."
                    )
                else:
                    remaining = cost - gold_acc
                    await self.highrise.chat(
                        f"💖 Danke {sender_username} für {amount} Gold! "
                        f"Noch {remaining}G bis zum nächsten Musik-Credit."
                    )
                    await self.highrise.send_whisper(
                        sender_id,
                        f"💖 Danke für deine Spende von {amount} Gold!\n"
                        f"Dein Gold-Guthaben: {gold_acc}/{cost}G. "
                        f"Noch {remaining}G bis zum nächsten Musik-Credit "
                        f"(weitere Spenden werden automatisch addiert)."
                    )
                    log_payments.info(
                        f"Gold-Guthaben aktualisiert: {sender_username} hat {amount}G gesendet "
                        f"(Gesamt-Guthaben: {gold_acc}/{cost}G, noch kein Credit) [{source}]"
                    )
            else:
                await self.highrise.chat(
                    f"💖 Danke {sender_username} für {amount} Gold!"
                )
                await self.highrise.send_whisper(
                    sender_id, f"💖 Danke für deine Spende von {amount} Gold!"
                )
            save_db(db)
        except Exception as e:
            logger.error(f"_process_gold_tip: Fehler bei der Verarbeitung: {e}")
            log_errors.exception(f"_process_gold_tip [{source}]: {e}")
            self.add_log("ERROR", "GOLD", "Gold-Verarbeitung fehlgeschlagen", str(e))
            try:
                await self.highrise.chat(
                    f"💖 Danke {sender_username} für {amount} Gold! "
                    f"(Hinweis: Credits konnten nicht verbucht werden, bitte melde dich beim Owner.)"
                )
                await self.highrise.send_whisper(
                    sender_id,
                    f"💖 Danke für deine Spende von {amount} Gold! "
                    f"⚠️ Deine Credits konnten leider nicht verbucht werden – bitte melde dich beim Owner."
                )
            except Exception:
                pass

    async def on_message(self, user_id: str, conversation_id: str, is_new_conversation: bool) -> None:
        # on_tip feuert bei dieser SDK/Server-Kombination nicht zuverlässig.
        # Als Fallback: Inbox-Benachrichtigung "Du hast ein Trinkgeld erhalten!"
        # erkennen, Wallet-Differenz zum letzten bekannten Guthaben berechnen
        # und _process_gold_tip() mit dem ermittelten Betrag aufrufen.
        #
        # Dedup: Jede verarbeitete conversation_id wird für 5 Minuten gecacht,
        # damit Doppel-Events (z.B. Resync) nicht doppelt verbucht werden.
        # Falls on_tip doch noch feuert (anderes SDK-Update), wird die
        # wallet_balance dort ebenfalls aktualisiert – ein zweiter Aufruf von
        # _process_gold_tip() für dieselbe Zahlung tritt dann nicht auf, weil
        # on_message nur bei Inbox-Events feuert, on_tip nur bei direkten Tips.

        try:
            messages = await self.highrise.get_messages(conversation_id)
        except Exception as e:
            logger.warning(f"[ON-MESSAGE] get_messages fehlgeschlagen: {e}")
            return

        if not messages or not messages.messages:
            return

        latest = messages.messages[0]
        content = getattr(latest, "content", "") or ""

        # Nur Trinkgeld-Benachrichtigungen verarbeiten
        tip_keywords = ["trinkgeld", "tip", "gold bar", "goldbar"]
        is_tip_notification = any(kw in content.lower() for kw in tip_keywords)
        if not is_tip_notification:
            logger.debug(f"[ON-MESSAGE] Kein Trinkgeld-Event – ignoriert. content={content!r}")
            return

        logger.info(f"[ON-MESSAGE] Trinkgeld-Inbox-Event erkannt: user_id={user_id}, content={content!r}")

        # Wallet-Differenz berechnen
        try:
            bot_wallet = await self.highrise.get_wallet()
            new_balance = 0
            if bot_wallet and bot_wallet.content:
                new_balance = getattr(bot_wallet.content[0], "amount", 0)
        except Exception as e:
            logger.warning(f"[ON-MESSAGE] get_wallet fehlgeschlagen: {e}")
            return

        diff = new_balance - self.wallet_balance
        if diff <= 0:
            # Kein positiver Eingang (z.B. Bot hat gerade Gold gesendet oder Wallet unverändert)
            logger.info(
                f"[ON-MESSAGE] Wallet-Diff nicht positiv (alt={self.wallet_balance}, "
                f"neu={new_balance}, diff={diff}) – übersprungen."
            )
            self.wallet_balance = new_balance
            return

        self.wallet_balance = new_balance
        logger.info(
            f"[ON-MESSAGE] Wallet-Diff: +{diff}G (neu={new_balance}G) von user_id={user_id}"
        )

        # Sender-Username aus DB nachschlagen (user_id aus Inbox-Event)
        sender_username = self.db.get("players", {}).get(user_id, {}).get("username", user_id[:8])

        await self._process_gold_tip(user_id, sender_username, diff, source="on_message")


    # ── Chat / Whisper Routing ────────────────────────────────

    async def on_chat(self, user: User, message: str) -> None:
        text = message.strip().lower()

        # ── Emote-System: Dauerhaftes Emote an den Spieler senden ──
        # !stop oder stop beendet das aktive Emote
        if text in ("stop", "!stop"):
            if user.id in self.active_emotes:
                del self.active_emotes[user.id]
                self._emote_stop_flags[user.id] = True
                try:
                    await self.highrise.send_whisper(user.id, "🛑 Emote gestoppt!")
                except Exception:
                    pass
                logger.info(f"EMOTE STOPPED: {user.username}")
            else:
                try:
                    await self.highrise.send_whisper(user.id, "Du hast kein aktives Emote. Schreibe einen Emote-Namen oder eine Nummer (1-215).")
                except Exception:
                    pass
            return

        if not message.startswith("!"):
            # Emote-Config aus DB laden (oder Fallback auf statische)
            emotes_config = self.db.get("emotes_config", {})
            emote_by_num = {}

            # Nummer-Map aus DB aufbauen
            for name, cfg in emotes_config.items():
                if cfg.get("num") is not None:
                    emote_by_num[str(cfg["num"])] = name

            # Emote-Nummer oder Name erkennen
            emote_id = None
            emote_name = None
            if text.isdigit():
                # Erst in DB-Map suchen, dann in statischer
                num = int(text)
                emote_name = emote_by_num.get(text) or EMOTE_BY_NUM.get(num)
                if emote_name:
                    # Emote-ID aus DB oder statischer Map
                    db_cfg = emotes_config.get(emote_name)
                    emote_id = db_cfg.get("emote_id") if db_cfg else EMOTES.get(emote_name)
            else:
                # Name: erst in DB suchen (Auslöser-Name), dann in statischer
                db_cfg = emotes_config.get(text)
                if db_cfg:
                    # Auslöser-Name aus DB gefunden → emote_id verwenden
                    emote_id = db_cfg.get("emote_id")
                    emote_name = text
                else:
                    # Fallback: statische EMOTES Map
                    static_id = EMOTES.get(text)
                    if static_id:
                        emote_id = static_id
                        emote_name = text

            if emote_id:
                # Vorheriges Emote stoppen falls anderes
                self.active_emotes[user.id] = emote_id
                self._emote_stop_flags[user.id] = False
                try:
                    await self.highrise.send_emote(emote_id, user.id)
                    await self.highrise.send_whisper(
                        user.id,
                        f"🎭 Emote '{emote_name}' aktiv! Schreibe 'stop' zum Beenden."
                    )
                    logger.info(f"EMOTE LOOP STARTED: {user.username} → {emote_name} ({emote_id})")
                except Exception as e:
                    logger.warning(f"Emote fehlgeschlagen für {user.username}: {e}")
                    del self.active_emotes[user.id]
                return

            logger.info(f"[CHAT-DEBUG] {user.username}: {message}")
            return

        logger.info(f"CMD [{user.username}]: {message}")
        self.add_log("INFO", "CMD", f"{user.username}: {message}")
        await self._handle_cmd(user, message)

    async def on_whisper(self, user: User, message: str) -> None:
        logger.info(f"[WHISPER-DEBUG] {user.username}: {message}")
        if not message.startswith("!"):
            return
        self.add_log("INFO", "CMD", f"{user.username} (Whisper): {message}")
        await self._handle_cmd(user, message)

    # ── Command Dispatcher ────────────────────────────────────

    async def _handle_cmd(self, user: User, message: str):
        # Duplikat-Schutz: identische Nachricht desselben Users innerhalb
        # des Dedup-Fensters wird ignoriert (z.B. doppelt zugestelltes
        # Chat-Event bei Raum-Resync).
        now = time.monotonic()
        dedup_key = (user.id, message.strip())
        last_seen = self._recent_cmds.get(dedup_key, 0)
        if (now - last_seen) < self._cmd_dedup_window:
            logger.info(f"[CMD-DEDUP] Doppelter Befehl ignoriert: {user.username}: {message}")
            return
        self._recent_cmds[dedup_key] = now

        # Alte Einträge aufräumen, damit das Dict nicht unbegrenzt wächst
        if len(self._recent_cmds) > 200:
            cutoff = now - self._cmd_dedup_window
            self._recent_cmds = {k: v for k, v in self._recent_cmds.items() if v >= cutoff}

        parts = message.strip().split(None, 2)
        cmd = parts[0].lower()
        args = parts[1:] if len(parts) > 1 else []
        t_start = time.monotonic()

        try:
            await self._dispatch_cmd(user, cmd, args)
        except Exception as e:
            elapsed = (time.monotonic() - t_start) * 1000
            logger.error(f"Unbehandelter Fehler in {cmd} von {user.username}: {e}")
            log_errors.exception(
                f"CMD={cmd} | user={user.username} | uid={user.id} | "
                f"args={args} | elapsed={elapsed:.1f}ms | error={e}"
            )
            self.add_log("ERROR", "CMD", f"Fehler in {cmd}", str(e))
            try:
                await self.highrise.send_whisper(
                    user.id, "❌ Ein interner Fehler ist aufgetreten. Bitte später versuchen."
                )
            except Exception:
                pass
        else:
            elapsed = (time.monotonic() - t_start) * 1000
            logger.debug(
                f"CMD={cmd} | user={user.username} | uid={user.id} | "
                f"args={args} | elapsed={elapsed:.1f}ms | status=OK"
            )

    async def _dispatch_cmd(self, user: User, cmd: str, args: list):
        """
        Dispatches approved commands only.

        Approved user commands   : !np, !play, !tip, !balance, !queue, !top
        Approved mod/admin cmds  : !refund, !skip, !checkvip,
                                   !vip, !unvip, !mod, !unmod, !setpos,
                                   !gold

        DEPRECATED (kept for stability, owner-only):
          !reload  – triggers an immediate process restart via os._exit(0).
                     Required for emergency restarts; PM2 handles the reboot.
                     Removing it would require a manual server login to restart.
        """

        # ── Alle Spieler ──────────────────────────────────────

        def _split_blocks(text):
            """Text in Highrise-sichere Blöcke aufteilen ( Whisper/Chat)"""
            lines = text.split("\n") if isinstance(text, str) else text
            blocks, current = [], []
            for line in lines:
                if line == "" and current:
                    blocks.append("\n".join(current))
                    current = []
                elif line != "":
                    current.append(line)
            if current:
                blocks.append("\n".join(current))
            return blocks

        if cmd == "!help":
            # !help ist nicht in der genehmigten Liste, aber kritisch für UX.
            # Whisper + DM, damit der User die Befehle auch später nachlesen kann.
            # Rollenbasiert: Mods/Owner sehen zusätzliche Befehle.
            is_mod = self._is_mod_plus(user.id)
            is_owner = self._is_owner(user.id)
            cost = self.config.get("music_gold_cost", 10)

            # ── Basis-Hilfe (alle Spieler) ────────────────────
            lines_user = [
                "🤖 BOT – BEFEHLSÜBERSICHT",
                "🎵  MUSIK",
                f"  !play [Songname - Artist]  –  Songwunsch eintragen ({cost} Gold, Picks from YouTube)",
                "  !np                        –  Welcher Song läuft gerade?",
                "  !queue                     –  Warteschlange anzeigen",
                "",
                "💰  GOLD & GUTHABEN",
                "  !balance  –  Deine Credits & gesamt gespendetes Gold",
                f"  !tip      –  Wie bekomme ich Musik-Credits? ({cost} Gold = 1 Credit)",
                "",
                "🏆  RANGLISTE",
                "  !top  –  Top 3 der größten Spender",
                "",
                "⭐  VIP",
                f"  !purchasevip  –  VIP kaufen ({self.config.get('vip_purchase_price', 2000)}G, dauerhaft, Songwünsche ohne Tipping)",
                "",
                "🎭  EMOTES",
                "  [Name]         –  Emote per Name starten (z.B. 'winken')",
                "  [Nummer]       –  Emote per Nummer (z.B. '71' für Wave)",
                "  !emotelist     –  Alle Emotes mit Name + Nummer anzeigen",
                "  stop / !stop   –  Aktives Emote stoppen",
            ]

            # ── Erweiterung für Mods ──────────────────────────
            lines_mod = [
                "",
                "🛡️  MODERATOR-BEFEHLE",
                "  !skip              –  Aktuellen Song überspringen",
                "  !refund [Anzahl] @user  –  Musik-Credits gutschreiben",
                "  !checkvip           –  VIP- & Mod-Liste anzeigen",
            ]

            # ── Erweiterung für Owner ─────────────────────────
            lines_owner = [
                "",
                "👑  OWNER-BEFEHLE",
                "  !vip @user         –  VIP-Status vergeben",
                "  !unvip @user       –  VIP-Status entfernen",
                "  !setvipprice [Gold]  –  !purchasevip-Preis ändern",
                "  !mod @user         –  Moderator ernennen",
                "  !unmod @user       –  Moderator entfernen",
                "  !playlist [Name]   –  Playlist als Auto-Playlist setzen",
                "  !setpos           –  Bot-Position setzen",
                "  !reload           –  Bot neu starten",
                "",
                "🪙  GOLD-VERTEILUNG (nur Owner)",
                "  !checkgold        –  Aktuelles Bot-Guthaben anzeigen",
                "  !gold             –  Aktuelle Einstellungen anzeigen",
                "  !gold on/off      –  Gold-Verteilung ein-/ausschalten",
                "  !gold now         –  Gold sofort an alle verteilen",
                "  !gold [Betrag] [Interval] [Einheit]",
                "    s = Sekunden  |  m = Minuten  |  h = Stunden",
                "    Beispiel: !gold 5 30 m  →  5 Gold alle 30 Min",
            ]

            # Nachricht zusammenbauen
            help_lines = lines_user
            if is_mod:
                help_lines = help_lines + lines_mod
            if is_owner:
                help_lines = help_lines + lines_owner

            help_text = "\n".join(help_lines)

            # Hilfe per Whisper senden (privat, nicht im Chat)
            blocks = _split_blocks(help_lines)
            for block in blocks:
                try:
                    await self.highrise.send_whisper(user.id, block)
                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.warning(f"!help Whisper fehlgeschlagen: {e}")

        elif cmd == "!emotelist":
            # !emotelist — Alle Emotes mit Custom-Anzeigenamen (falls im Panel geändert)
            from emotes_dict import EMOTE_BY_NUM
            emotes_config = self.db.get("emotes_config", {})
            
            final_emotes = {}
            # 1. Standard-Werte befüllen
            for num, name in EMOTE_BY_NUM.items():
                final_emotes[num] = name
                
            # 2. Eigene Einstellungen aus dem Web-Panel anwenden
            for custom_name, cfg in emotes_config.items():
                cnum = cfg.get("num")
                if cnum is not None:
                    final_emotes[cnum] = custom_name
            
            sorted_nums = sorted(final_emotes.keys())
            total = len(sorted_nums)
            
            # Schöne, kategorisierte Ausgabe wie bei !help
            lines = [
                f"🎭 EMOTE-LISTE — {total} Emotes",
                "",
                "📖  SO FUNKTIONIERT'S",
                "  Schreibe den Namen oder die Nummer in den Chat,",
                "  um ein Emote zu starten.",
                "  Schreibe 'stop' zum Beenden.",
                "",
            ]
            
            # Emotes in Gruppen à 25 aufteilen
            group_size = 25
            for i in range(0, total, group_size):
                chunk = sorted_nums[i:i + group_size]
                start_num = chunk[0]
                end_num = chunk[-1]
                lines.append(f"✨  EMOTES {start_num} – {end_num}")
                for num in chunk:
                    name = final_emotes[num]
                    lines.append(f"  {name}  ({num})")
                lines.append("")
            
            lines.append("💡 Tipp: Schreibe !help für alle Bot-Befehle.")
            
            full_text = "\n".join(lines)
            
            # Emote-Liste per Whisper senden (privat, nicht im Chat)
            blocks = _split_blocks(lines)
            for block in blocks:
                try:
                    await self.highrise.send_whisper(user.id, block)
                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.warning(f"!emotelist Whisper fehlgeschlagen: {e}")

        elif cmd == "!np":
            t = getattr(self.music_player, "current_track", None)
            if t:
                title = t.get("title", "Unbekannt")[:30]
                requested_by = t.get("requested_by", "Auto")
                # Song-Zeit berechnen
                song_start = getattr(self.music_player, "_song_start_time", None)
                song_duration = getattr(self.music_player, "_song_duration", 0)
                time_info = ""
                if song_start and song_duration and song_duration > 0:
                    import time as _time_mod
                    elapsed = _time_mod.time() - song_start
                    remaining = max(0, song_duration - elapsed)
                    elapsed_min = int(elapsed) // 60
                    elapsed_sec = int(elapsed) % 60
                    remaining_min = int(remaining) // 60
                    remaining_sec = int(remaining) % 60
                    time_info = f" | ⏱️ {elapsed_min}:{elapsed_sec:02d} / {int(song_duration)//60}:{int(song_duration)%60:02d} (noch {remaining_min}:{remaining_sec:02d})"
                await self.highrise.send_whisper(
                    user.id,
                    f"▶️ Jetzt läuft: {title} (von {requested_by}){time_info}"
                )
            else:
                await self.highrise.send_whisper(user.id, "🎵 Aktuell läuft kein Song.")

        elif cmd == "!play":
            await self._cmd_play(user, args)

        elif cmd == "!queue":
            q = self.music_player.queue
            if q:
                songs = [f"{i+1}. {t['title'][:25]}" for i, t in enumerate(q)]
                msg = "🎵 Queue: " + " | ".join(songs)
                # Highrise Nachrichtenlimit: ~250 Zeichen pro Whisper
                for i in range(0, len(msg), 250):
                    await self.highrise.send_whisper(user.id, msg[i:i+250])
            else:
                await self.highrise.send_whisper(user.id, "🎵 Queue ist leer – Auto-Playlist läuft")

        elif cmd == "!top":
            players = self.db.get("players", {})
            top = sorted(
                players.items(),
                key=lambda x: x[1].get("gold_donated", 0),
                reverse=True
            )[:3]
            if top:
                msg = "🏆 " + " | ".join(
                    f"{i+1}. {p['username']}: {p['gold_donated']}G"
                    for i, (_, p) in enumerate(top)
                )
                await self.highrise.chat(msg)

        elif cmd == "!balance":
            # Zeigt dem User ein "Inventar": Musik-Credits, gespendetes Gold,
            # VIP-Status (inkl. laufendem Kauf-Fortschritt), Zeit im Raum
            # und Anzahl bisher gewünschter Songs.
            db = self.db
            credits = db["music_credits"].get(user.id, 0)
            player = db["players"].get(user.id, {})
            donated = player.get("gold_donated", 0)
            songs_added = player.get("songs_added", 0)

            # Zeit im Raum: gespeicherte Gesamtzeit + ggf. laufende aktuelle Session
            total_seconds = player.get("time_in_room", 0)
            joined_str = player.get("joined_at")
            if joined_str:
                try:
                    joined_at = datetime.fromisoformat(joined_str)
                    total_seconds += (datetime.now() - joined_at).total_seconds()
                except Exception:
                    pass
            total_seconds = int(total_seconds)
            hours, rem = divmod(total_seconds, 3600)
            minutes, _ = divmod(rem, 60)
            time_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"

            # VIP-Status
            if user.id in db.get("vip_list", []):
                vip_line = "⭐ VIP-Status: Aktiv"
            else:
                vip_line = "⭐ VIP-Status: Kein VIP"

            await self.highrise.send_whisper(
                user.id,
                f"📋 Dein Profil ({user.username}):\n"
                f"💰 Musik-Credits: {credits}\n"
                f"💸 Gesamt gespendet: {donated}G\n"
                f"🎵 Songwünsche insgesamt: {songs_added}\n"
                f"⏱️ Zeit im Raum (gesamt): {time_str}\n"
                f"{vip_line}"
            )
            log_vip.info(f"!balance abgefragt von {user.username} (uid={user.id})")

        elif cmd == "!tip":
            # Erklärt dem User wie er Musik-Credits kaufen kann.
            cost = self.config.get("music_gold_cost", 10)
            await self.highrise.send_whisper(
                user.id,
                f"💸 So bekommst du Musik-Credits:\n"
                f"1) Öffne das Geschenk-Menü und tippe dem Bot Gold.\n"
                f"2) {cost} Gold = 1 Musik-Credit (z.B. {cost*5} Gold = 5 Credits).\n"
                f"3) Danach: !play [Songname - Artist] um deinen Wunsch einzutragen (Picks from YouTube).\n"
                f"Dein aktuelles Guthaben siehst du mit !balance."
            )

        # ── Mod+ ──────────────────────────────────────────────

        elif cmd == "!purchasevip":
            price = self.config.get("vip_purchase_price", 2000)
            db = self.db
            db.setdefault("vip_pending", {})

            # Bereits angesammeltes Guthaben aus vorherigen Sessions beibehalten
            existing = db["vip_pending"].get(user.id, {})
            already_received = existing.get("received", 0)

            # Falls bereits genug (oder mehr) im Hintergrund gesammelt wurde,
            # VIP sofort vergeben statt eine neue Session zu starten.
            if already_received >= price:
                overpay = already_received - price
                del db["vip_pending"][user.id]
                if user.id not in db["vip_list"]:
                    db["vip_list"].append(user.id)
                save_db(db)
                extra = f"\n💰 Überschuss von {overpay}G wurde als Guthaben verbucht." if overpay else ""
                await self.highrise.chat(
                    f"⭐ {user.username} hat VIP gekauft! Songwünsche jetzt dauerhaft ohne Gold-Tipping."
                )
                await self.highrise.send_whisper(
                    user.id,
                    f"⭐ VIP dauerhaft aktiviert! Du kannst jetzt !play ohne Gold-Tipping nutzen.{extra}"
                )
                log_vip.info(
                    f"VIP gekauft (via !purchasevip nachgeholt): {user.username} (uid={user.id}) | "
                    f"{price}G (+{overpay}G Überschuss) | dauerhaft"
                )
                self.add_log("SUCCESS", "VIP", f"{user.username} hat VIP gekauft", "Dauerhaft")
                return

            remaining_needed = price - already_received

            db["vip_pending"][user.id] = {
                "amount": price,
                "received": already_received,
                "expires_at": time.time() + 60,  # 1 Minute Zeit pro Session
            }
            save_db(db)

            if already_received > 0:
                await self.highrise.send_whisper(
                    user.id,
                    f"⭐ VIP-Kauf fortgesetzt! Du hast bereits {already_received}/{price}G bezahlt. "
                    f"Sende jetzt noch {remaining_needed}G an den Bot, um dauerhaften VIP zu erhalten. "
                    f"Du hast 1 Minute Zeit."
                )
            else:
                await self.highrise.send_whisper(
                    user.id,
                    f"⭐ VIP-Kauf gestartet! Sende jetzt {price} Gold an den Bot (Tip), "
                    f"um dauerhaften VIP zu erhalten "
                    f"(Songwünsche ohne Gold-Tipping). Du hast 1 Minute Zeit."
                )
            log_vip.info(
                f"!purchasevip gestartet von {user.username} (uid={user.id}) | "
                f"Preis={price}G | bereits_bezahlt={already_received}G"
            )

            # Hintergrund-Task: nach 60s Ablauf-Nachricht senden (Fortschritt bleibt gespeichert)
            async def _vip_session_expired(uid: str, uname: str, session_start_ts: float):
                await asyncio.sleep(61)
                current_entry = self.db.get("vip_pending", {}).get(uid)
                if not current_entry:
                    return  # VIP wurde in der Zwischenzeit bereits gekauft
                # Nur reagieren, wenn es noch dieselbe Session ist (expires_at stimmt überein)
                if abs(current_entry.get("expires_at", 0) - (session_start_ts + 60)) > 2:
                    return  # Eine neue Session wurde gestartet – nicht eingreifen
                # Session abgelaufen: expires_at entfernen, Fortschritt behalten
                received_so_far = current_entry.get("received", 0)
                vip_price = current_entry.get("amount", price)
                if received_so_far > 0:
                    await self.highrise.send_whisper(
                        uid,
                        f"⏰ Deine VIP-Kaufsession ist abgelaufen!\n"
                        f"Dein Fortschritt wurde gespeichert: {received_so_far}/{vip_price}G.\n"
                        f"Schreibe erneut !purchasevip, um weiterzumachen – du musst nicht von vorne anfangen."
                    )
                else:
                    await self.highrise.send_whisper(
                        uid,
                        f"⏰ Deine VIP-Kaufsession ist abgelaufen (kein Gold eingegangen).\n"
                        f"Schreibe erneut !purchasevip, um einen neuen Kauf zu starten."
                    )
                log_vip.info(
                    f"VIP-Session abgelaufen: {uname} (uid={uid}) | "
                    f"Fortschritt gespeichert: {received_so_far}/{vip_price}G"
                )

            asyncio.create_task(
                _vip_session_expired(user.id, user.username, time.time())
            )

        elif cmd == "!checkvip":
            if not self._is_mod_plus(user.id):
                await self.highrise.send_whisper(user.id, "🚫 Nur Mods können die VIP-Liste anzeigen!")
                log_vip.warning(f"!checkvip: Berechtigung verweigert für {user.username} (uid={user.id})")
                return
            vip_ids = self.db.get("vip_list", [])
            mod_ids = self.db.get("mod_list", [])
            owner_ids = self.db.get("owner_list", [])
            # Namen aus players-DB aufschlüsseln
            players = self.db.get("players", {})
            def _name(uid):
                return players.get(uid, {}).get("username", uid[:8])
            vip_names = [_name(v) for v in vip_ids] or ["–"]
            mod_names = [_name(m) for m in mod_ids] or ["–"]
            owner_names = [_name(o) for o in owner_ids] or ["–"]
            await self.highrise.send_whisper(
                user.id,
                f"👑 Owner: {', '.join(owner_names)}\n"
                f"⭐ VIPs: {', '.join(vip_names)}\n"
                f"🛡️ Mods: {', '.join(mod_names)}"
            )
            log_vip.info(f"!checkvip angezeigt von {user.username}")

        elif cmd == "!setvipprice":
            if not self._is_owner(user.id):
                await self.highrise.send_whisper(user.id, "🚫 Nur der Owner kann das!")
                log_admin.warning(f"!setvipprice: Berechtigung verweigert für {user.username}")
                return
            if not args:
                price = self.config.get("vip_purchase_price", 2000)
                await self.highrise.send_whisper(
                    user.id,
                    f"⭐ Aktuell: !purchasevip kostet {price}G (dauerhafter VIP).\n"
                    f"Ändern: !setvipprice [Gold]"
                )
                return
            try:
                new_price = int(args[0])
                if new_price <= 0:
                    raise ValueError
            except (ValueError, IndexError):
                await self.highrise.send_whisper(user.id, "❓ Nutzung: !setvipprice [Gold]")
                return
            self.config["vip_purchase_price"] = new_price
            self._save_config()
            await self.highrise.send_whisper(
                user.id, f"✅ !purchasevip kostet jetzt {new_price}G (dauerhafter VIP)."
            )
            log_admin.info(f"!setvipprice: {new_price}G | Von {user.username}")

        elif cmd in ("!vip", "!unvip", "!mod", "!unmod", "!owner", "!unowner"):
            if not self._is_owner(user.id):
                await self.highrise.send_whisper(user.id, "🚫 Nur der Owner kann das!")
                log_vip.warning(f"{cmd}: Berechtigung verweigert für {user.username}")
                return
            await self._cmd_role(user, cmd, args)

        elif cmd == "!skip":
            if not self._is_mod_plus(user.id):
                await self.highrise.send_whisper(user.id, "🚫 Nur Mods können skippen!")
                log_moderation.warning(f"!skip: Berechtigung verweigert für {user.username}")
                return
            skipped = await self.music_player.skip_song()
            if skipped:
                await self.highrise.chat("⏭️ Song übersprungen!")
                log_music.info(f"Song übersprungen von {user.username} (uid={user.id})")
                self.add_log("INFO", "MUSIC", "Song übersprungen", f"Von {user.username}")
            else:
                await self.highrise.send_whisper(user.id, "Queue ist leer oder es läuft nichts.")

        elif cmd == "!playlist":
            if not self._is_owner(user.id):
                await self.highrise.send_whisper(user.id, "🚫 Nur der Owner kann die Auto-Playlist setzen!")
                log_moderation.warning(f"!playlist: Berechtigung verweigert für {user.username}")
                return
            if not args:
                await self.highrise.send_whisper(user.id, "❓ Nutzung: !playlist [Playlist-Name oder Link]")
                return
            playlist_url = args[0].strip()
            await self.highrise.send_whisper(
                user.id, "⏳ Playlist wird geladen... Das kann bei großen Playlists etwas dauern."
            )
            log_music.info(f"!playlist: Lade Playlist {playlist_url} (von {user.username})")
            result = await self.music_player.set_auto_playlist_from_youtube(playlist_url)
            count = result["count"]
            if count > 0:
                title = result.get("playlist_title") or "Unbenannte Playlist"
                uploader = result.get("playlist_uploader") or "Unbekannt"
                total_seconds = result.get("total_seconds", 0)

                hours, rem = divmod(total_seconds, 3600)
                minutes, _ = divmod(rem, 60)
                if hours > 0:
                    duration_str = f"{hours}h {minutes}min"
                else:
                    duration_str = f"{minutes}min"

                await self.highrise.chat(
                    f"📻 Auto-Playlist gesetzt: \"{title}\" von {uploader} | "
                    f"{count} Songs geladen | Gesamtlänge: ~{duration_str} | "
                    f"Wird der Reihe nach abgespielt."
                )
                log_music.info(
                    f"!playlist: {count} Songs ('{title}' von {uploader}, "
                    f"~{duration_str}) geladen von {user.username}"
                )
                self.add_log("SUCCESS", "MUSIC", "Auto-Playlist gesetzt",
                              f"{title} | {count} Songs | ~{duration_str} | Von {user.username}")
            else:
                await self.highrise.send_whisper(
                    user.id, "❌ Konnte die Playlist nicht laden. Ist der Link korrekt?"
                )
                log_music.warning(f"!playlist: Laden fehlgeschlagen ({playlist_url})")

        elif cmd == "!refresh":
            if not self._is_owner(user.id):
                await self.highrise.send_whisper(user.id, "🚫 Nur der Owner kann die Playlist aktualisieren!")
                return
            if not self.bot.config.get("autoPlaylistUrl"):
                await self.highrise.send_whisper(user.id, "❌ Keine Playlist gesetzt. Nutze !playlist [URL] erst.")
                return
            await self.highrise.send_whisper(user.id, "⏳ Playlist wird aktualisiert...")
            result = await self.music_player.refresh_playlist_if_changed()
            if result:
                count = len(self.bot.config.get("autoPlaylistUrls", []))
                await self.highrise.chat(f"📻 Playlist aktualisiert! {count} Songs geladen.")
                # Index zurücksetzen bei neuem Durchlauf
                self.music_player.auto_playlist_index = 0
                self.db["auto_playlist_index"] = 0
                # ── Speichern → Validieren → Neustart ──
                # 1. DB speichern
                save_db(self.db)
                # 2. Config speichern
                try:
                    with open("config.json", "w", encoding="utf-8") as f:
                        json.dump(self.bot.config, f, indent=4, ensure_ascii=False)
                except Exception as e:
                    logger.warning(f"Config speichern fehlgeschlagen: {e}")
                # 3. Validieren:	DB + Config laden und prüfen
                try:
                    with open("database.json", "r", encoding="utf-8") as f:
                        db_check = json.load(f)
                    assert db_check.get("auto_playlist_index") == 0, "DB Validation: Index nicht 0"
                    assert "auto_playlist_tracks" not in db_check or len(db_check.get("auto_playlist_urls", [])) == 0, "DB Validation: alte Tracks noch vorhanden"
                    logger.info("!refresh: ✅ DB validiert")
                except Exception as e:
                    logger.error(f"!refresh: ❌ DB Validierung fehlgeschlagen: {e}")
                    await self.highrise.send_whisper(user.id, f"⚠️ Validierung fehlgeschlagen: {e}")
                    return
                try:
                    with open("config.json", "r", encoding="utf-8") as f:
                        cfg_check = json.load(f)
                    assert len(cfg_check.get("autoPlaylistUrls", [])) == count, f"Config Validation: Playlist Länge stimmt nicht ({len(cfg_check.get('autoPlaylistUrls', []))} != {count})"
                    logger.info("!refresh: ✅ Config validiert")
                except Exception as e:
                    logger.error(f"!refresh: ❌ Config Validierung fehlgeschlagen: {e}")
                    await self.highrise.send_whisper(user.id, f"⚠️ Validierung fehlgeschlagen: {e}")
                    return
                # 4. Bot neu starten (PM2)
                try:
                    import subprocess
                    subprocess.Popen(
                        ["pm2", "restart", "highrise-bot"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    await self.highrise.chat("🔄 Bot wird neu gestartet...")
                    logger.info(f"!refresh: Playlist aktualisiert, {count} Songs, Bot neu gestartet")
                except Exception as e:
                    logger.error(f"!refresh: ❌ Neustart fehlgeschlagen: {e}")
                    await self.highrise.send_whisper(user.id, f"⚠️ Neustart fehlgeschlagen: {e}")
            else:
                await self.highrise.send_whisper(user.id, "✅ Playlist ist bereits aktuell.")

        elif cmd == "!refund":
            if not self._is_mod_plus(user.id):
                await self.highrise.send_whisper(user.id, "🚫 Keine Berechtigung!")
                log_moderation.warning(f"!refund: Berechtigung verweigert für {user.username}")
                return
            if len(args) < 2:
                await self.highrise.send_whisper(user.id, "❓ Nutzung: !refund [Anzahl Credits] @username")
                return

            amount_str = args[0].lower().replace("g", "")
            if not amount_str.isdigit():
                await self.highrise.send_whisper(user.id, "❌ Ungültige Anzahl!")
                return

            amount = int(amount_str)
            target = args[1].lstrip("@")

            if target.isdigit():
                await self.highrise.send_whisper(
                    user.id,
                    f"⚠️ '{target}' sieht nach einer Zahl aus – !refund braucht "
                    f"[Anzahl Credits] @username. Beispiel: !refund {amount} @Spielername"
                )
                return

            if amount <= 0:
                await self.highrise.send_whisper(user.id, "❌ Anzahl muss größer als 0 sein!")
                return

            db = self.db

            # Zielspieler suchen: zuerst im Raum (für aktuellen Username),
            # sonst in der DB (auch offline möglich, da keine echte
            # Gold-Transaktion mehr nötig ist).
            target_id = None
            try:
                room_users = await self.highrise.get_room_users()
                for u, _ in room_users.content:
                    if u.username.lower() == target.lower():
                        target_id = u.id
                        target = u.username
                        break
            except Exception as e:
                log_errors.error(f"!refund: Raum-User-Abruf fehlgeschlagen: {e}")

            if not target_id:
                for uid, p in db.get("players", {}).items():
                    if p.get("username", "").lower() == target.lower():
                        target_id = uid
                        target = p.get("username", target)
                        break

            if not target_id:
                await self.highrise.send_whisper(user.id, f"❓ Spieler {target} nicht gefunden!")
                return

            db.setdefault("music_credits", {})
            db["music_credits"][target_id] = db["music_credits"].get(target_id, 0) + amount
            new_balance = db["music_credits"][target_id]
            save_db(db)

            await self.highrise.chat(
                f"🎁 {user.username} hat {target} {amount} Musik-Credit(s) gutgeschrieben! "
                f"(Neues Guthaben: {new_balance})"
            )
            logger.info(f"Refund: +{amount} Musik-Credits an {target} durch {user.username}")
            log_payments.info(f"Refund: +{amount} Credits → {target} (uid={target_id}) | Ausgeführt von {user.username}")
            self.add_log("SUCCESS", "MUSIC", f"Refund: +{amount} Credits an {target}", f"Von {user.username}")

        # ── Owner ─────────────────────────────────────────────

        elif cmd == "!setpos":
            if not self._is_owner(user.id):
                await self.highrise.send_whisper(user.id, "🚫 Nur der Owner!")
                log_admin.warning(f"!setpos: Berechtigung verweigert für {user.username}")
                return

            try:
                room_users = await self.highrise.get_room_users()
            except Exception as e:
                await self.highrise.send_whisper(user.id, "❌ Konnte Raum-Daten nicht abrufen!")
                log_errors.error(f"!setpos: get_room_users fehlgeschlagen: {e}")
                return

            user_pos = None
            for u, pos in room_users.content:
                if u.id == user.id:
                    user_pos = pos
                    break

            if user_pos and hasattr(user_pos, "x"):
                facing = getattr(user_pos, "facing", "FrontRight")
                self.db["bot_position"] = {
                    "x": user_pos.x, "y": user_pos.y,
                    "z": user_pos.z, "facing": facing
                }
                save_db(self.db)
                await self.highrise.send_whisper(
                    user.id, "📍 Bot-Position gespeichert! Der Bot wird zukünftig hier spawnen."
                )
                logger.info(f"Bot-Position gesetzt von {user.username} auf {user_pos.x}, {user_pos.y}, {user_pos.z}, {facing}")
                log_admin.info(f"!setpos: Position gesetzt von {user.username}: x={user_pos.x} y={user_pos.y} z={user_pos.z} facing={facing}")
                try:
                    await self.highrise.teleport(self.highrise.my_id, user_pos)
                except Exception as e:
                    logger.warning(f"Sofort-Teleport nach !setpos fehlgeschlagen: {e}")
            else:
                await self.highrise.send_whisper(user.id, "❌ Konnte deine Position nicht finden!")

        elif cmd == "!gold":
            if not self._is_owner(user.id):
                await self.highrise.send_whisper(user.id, "🚫 Nur der Owner!")
                return
            await self._cmd_gold(user, args)

        elif cmd == "!checkgold":
            if not self._is_owner(user.id):
                await self.highrise.send_whisper(user.id, "🚫 Nur der Owner!")
                return
            await self._cmd_checkgold(user)

        # ── DEPRECATED – Behalten aus Stabilitätsgründen ──────
        # !reload ist NICHT in der genehmigten Befehlsliste.
        # Es bleibt als reiner Owner-Notfallbefehl erhalten, da ein Entfernen
        # bedeuten würde, dass der Prozess nur noch per SSH-Zugang neu gestartet
        # werden kann. PM2 startet den Prozess nach os._exit(0) automatisch neu.
        elif cmd == "!reload":
            if not self._is_owner(user.id):
                await self.highrise.send_whisper(user.id, "🚫 Nur der Owner!")
                return
            await self.highrise.chat("✅ Alles wurde gespeichert! Bot startet sich neu...")
            logger.info("Bot-Neustart via !reload angefordert.")
            log_admin.info(f"!reload: Neustart angefordert von {user.username}")
            await asyncio.sleep(1.0)
            os._exit(0)

        # ── Unbekannte Commands ────────────────────────────────
        else:
            # Keine Ausgabe für unbekannte Commands – verhindert Spam
            logger.debug(f"Unbekannter Command: {cmd} von {user.username}")

    # ── Music Command ─────────────────────────────────────────

    async def _cmd_play(self, user: User, args: list):
        if not self.config.get("music_enabled"):
            await self.highrise.send_whisper(user.id, "🎵 Musik ist gerade deaktiviert.")
            return
        if not args:
            await self.highrise.send_whisper(
                user.id, "❓ Nutzung: !play [Songname - Artist] um deinen Wunsch einzutragen (Picks from YouTube)"
            )
            return

        query_check = " ".join(args).lower()
        if "youtube.com" in query_check or "youtu.be" in query_check or "http://" in query_check or "https://" in query_check:
            await self.highrise.send_whisper(
                user.id,
                "🚫 Links sind bei !play nicht erlaubt. Bitte gib nur den Songnamen "
                "und/oder Künstler an, z.B.: !play Aqua - Barbie Girl"
            )
            return

        # Max 3 Songs pro Spieler in der Queue
        user_songs_in_queue = sum(
            1 for t in self.music_player.queue if t.get("requested_by") == user.username
        )
        if user_songs_in_queue >= 3:
            await self.highrise.send_whisper(
                user.id,
                "🚫 Du kannst maximal 3 Songs in der Warteschlange haben! "
                "Warte bitte, bis deine Lieder gespielt wurden, bevor du neue hinzufügst."
            )
            return

        # Credits prüfen (VIP/Mod/Owner zahlen nicht)
        _credit_deducted = False
        if not self._is_vip_plus(user.id):
            credits = self.db["music_credits"].get(user.id, 0)
            if credits <= 0:
                cost = self.config.get("music_gold_cost", 10)
                await self.highrise.send_whisper(
                    user.id,
                    f"💸 Du brauchst {cost} Gold für einen Wunsch! Gib dem Bot Gold!"
                )
                log_payments.info(f"!play: {user.username} hat keine Credits (uid={user.id})")
                return
            self.db["music_credits"][user.id] = credits - 1
            _credit_deducted = True
            log_payments.info(f"!play: Credit verbraucht von {user.username} | verbleibend={credits-1}")

        query = " ".join(args)

        # YouTube-Suche: Suchbegriff -> URL + Titel via yt-dlp
        await self.highrise.send_whisper(user.id, f"🔍 Suche: {query}...")
        try:
            import asyncio as _asyncio
            ytdlp_search = await _asyncio.create_subprocess_exec(
                "yt-dlp", "--js-runtimes", "node", "--no-playlist",
                "--get-id", "--get-title", "-q",
                f"ytsearch1:{query}",
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await _asyncio.wait_for(ytdlp_search.communicate(), timeout=30.0)
            lines = stdout.decode("utf-8", errors="replace").strip().splitlines()
            if len(lines) >= 2:
                song_title = lines[0].strip()
                video_id = lines[1].strip()
                song_url = f"https://www.youtube.com/watch?v={video_id}"
            else:
                await self.highrise.send_whisper(user.id, f"❌ Kein Song gefunden für: {query}")
                if _credit_deducted:
                    self.db["music_credits"][user.id] = self.db["music_credits"].get(user.id, 0) + 1
                    save_db(self.db)
                return
        except Exception as e:
            await self.highrise.send_whisper(user.id, f"❌ Suche fehlgeschlagen: {e}")
            if _credit_deducted:
                self.db["music_credits"][user.id] = self.db["music_credits"].get(user.id, 0) + 1
                save_db(self.db)
            return

        # ── Dauer-Prüfung: max 10 Minuten bei !play ──
        # Livstreams und Videos > 600s werden abgelehnt (Auto-Playlist ist davon nicht betroffen)
        try:
            ytdlp_meta = await _asyncio.create_subprocess_exec(
                "yt-dlp", "--js-runtimes", "node", "--no-playlist",
                "--dump-single-json", "--no-download", "-q",
                song_url,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.DEVNULL,
            )
            meta_out, _ = await _asyncio.wait_for(ytdlp_meta.communicate(), timeout=30.0)
            import json as _json
            meta = _json.loads(meta_out.decode("utf-8", errors="replace"))
            is_live = meta.get("is_live") or meta.get("live_status") in ("is_live", "is_upcoming")
            duration_sec = meta.get("duration") or 0
            if is_live:
                await self.highrise.send_whisper(
                    user.id,
                    "🚫 Livestreams sind bei !play nicht erlaubt! Bitte wähle ein normales Video (max 10 Min)."
                )
                if _credit_deducted:
                    self.db["music_credits"][user.id] = self.db["music_credits"].get(user.id, 0) + 1
                    save_db(self.db)
                return
            if duration_sec > 600:
                dur_min = duration_sec / 60
                await self.highrise.send_whisper(
                    user.id,
                    f"🚫 Video zu lang ({dur_min:.0f} Min)! Maximal 10 Minuten erlaubt. "
                    "Bitte wähne ein kürzeres Video."
                )
                if _credit_deducted:
                    self.db["music_credits"][user.id] = self.db["music_credits"].get(user.id, 0) + 1
                    save_db(self.db)
                return
        except Exception as e:
            logger.warning(f"Dauer-Prüfung fehlgeschlagen für {song_url}: {e}")
            # Bei Fehler: trotzdem erlauben (nicht blockieren)

        # Song zur Queue hinzufügen
        await self.music_player.add_to_queue(song_url, song_title, user.username)

        # Queue persistent speichern (verliert sich nicht bei Crash/Reconnect)
        self.db["music_queue"] = self.music_player.queue.copy()
        save_db(self.db)

        # Spieler-Stats
        if user.id in self.db["players"]:
            self.db["players"][user.id]["songs_added"] = (
                self.db["players"][user.id].get("songs_added", 0) + 1
            )
        save_db(self.db)

        await self.highrise.chat(f"✅ {user.username}'s Wunsch: {song_title[:40]}")
        logger.info(f"Song hinzugefügt: {song_title} von {user.username}")
        log_music.info(f"Song zur Queue hinzugefügt: '{song_title}' | Wunsch von {user.username} (uid={user.id})")
        self.add_log("SUCCESS", "MUSIC", f"Song hinzugefügt: {song_title[:30]}", f"Gewünscht von {user.username}")

    # ── Gold-Config Command ──────────────────────────────────────

    async def _cmd_checkgold(self, user: User):
        """!checkgold – Aktuelles Bot-Wallet-Guthaben live abrufen (nur Owner)."""
        try:
            bot_wallet = await self.highrise.get_wallet()
            if bot_wallet and bot_wallet.content:
                balance = getattr(bot_wallet.content[0], "amount", 0)
                self.wallet_balance = balance
            else:
                balance = 0
        except Exception as e:
            logger.warning(f"!checkgold: Konnte Wallet nicht abrufen: {e}")
            await self.highrise.send_whisper(
                user.id, f"⚠️ Konnte Bot-Guthaben nicht abrufen: {e}"
            )
            return

        gold_enabled = self.config.get("gold_enabled", False)
        gold_amount = self.config.get("gold_amount", 0)

        msg = f"🪙 Bot-Guthaben: {balance} Gold"

        if gold_enabled and gold_amount:
            possible_distributions = balance // gold_amount if gold_amount else 0
            msg += (
                f"\n💰 Gold-Verteilung läuft mit {gold_amount} Gold pro Spieler "
                f"({possible_distributions}x möglich mit aktuellem Guthaben)"
            )
            if possible_distributions <= 1:
                msg += "\n⚠️ Guthaben wird knapp – bitte bald Gold nachfüllen!"

        await self.highrise.send_whisper(user.id, msg)

    async def _cmd_gold(self, user: User, args: list):
        """!gold – Gold-Verteilungs-Einstellungen anzeigen und ändern.

        Nutzung:
          !gold                          – Aktuelle Einstellungen anzeigen
          !gold on                       – Gold-Verteilung aktivieren
          !gold off                      – Gold-Verteilung deaktivieren
          !gold now                      – Gold sofort an alle verteilen
          !gold now [Betrag]             – Gold sofort mit eigenem Betrag verteilen (1-1.000.000)
          !gold [Betrag] [Interval] [Einheit]
            Einheiten: s = Sekunden, m = Minuten, h = Stunden
            Beispiel:  !gold 5 30 m  →  5 Gold alle 30 Minuten
                       !gold 10 2 h  →  10 Gold alle 2 Stunden
                       !gold 1 90 s  →  1 Gold alle 90 Sekunden
        """
        cfg = self.config

        # ── Nur anzeigen ───────────────────────────────────────
        if not args:
            enabled    = cfg.get("gold_enabled", False)
            amount     = cfg.get("gold_amount")
            interval_s = cfg.get("gold_interval")

            status = "✅ AN" if enabled else "❌ AUS"

            # Betrag
            amount_str = f"{amount} Gold" if amount else "nicht gesetzt"

            # Interval lesbar machen
            if not interval_s:
                interval_str = "nicht gesetzt"
            elif interval_s % 3600 == 0:
                interval_str = f"{interval_s // 3600} Stunde(n)"
            elif interval_s % 60 == 0:
                interval_str = f"{interval_s // 60} Minute(n)"
            else:
                interval_str = f"{interval_s} Sekunde(n)"

            await self.highrise.send_whisper(
                user.id,
                f"🪙 Gold-Verteilung: {status}\n"
                f"💰 Betrag pro Spieler: {amount_str}\n"
                f"⏱️ Interval: {interval_str}\n"
                f"\nÄndern: !gold [Betrag] [Interval] [s/m/h]\n"
                f"Beispiel: !gold 5 30 m  (5 Gold alle 30 Min)\n"
                f"Sofort verteilen: !gold now\n"
                f"Ein/Aus: !gold on  /  !gold off"
            )
            return

        # ── on / off ───────────────────────────────────────────
        if args[0].lower() == "on":
            cfg["gold_enabled"] = True
            self._save_config()
            await self.highrise.send_whisper(user.id, "✅ Gold-Verteilung aktiviert!")
            log_admin.info(f"!gold on: aktiviert von {user.username}")
            self.add_log("SUCCESS", "GOLD", "Gold-Verteilung aktiviert", f"Von {user.username}")
            return

        if args[0].lower() == "off":
            cfg["gold_enabled"] = False
            self._save_config()
            await self.highrise.send_whisper(user.id, "❌ Gold-Verteilung deaktiviert!")
            log_admin.info(f"!gold off: deaktiviert von {user.username}")
            self.add_log("INFO", "GOLD", "Gold-Verteilung deaktiviert", f"Von {user.username}")
            return

        # ── now (sofort verteilen) ──────────────────────────────
        if args[0].lower() == "now":
            # Optionaler Betrag: !gold now [Betrag] — beliebige Zahl
            custom_amount = None
            if len(args) >= 2:
                try:
                    custom_amount = int(args[1])
                    if custom_amount < 1:
                        await self.highrise.send_whisper(user.id, "❌ Betrag muss mindestens 1 Gold sein!")
                        return
                except ValueError:
                    await self.highrise.send_whisper(
                        user.id, "❌ Ungültiger Betrag! Beispiel: !gold now 50"
                    )
                    return

            if custom_amount:
                await self.highrise.send_whisper(
                    user.id, f"⚡ Gold wird sofort verteilt ({custom_amount} Gold pro Spieler)..."
                )
                log_admin.info(f"!gold now {custom_amount}: Sofort-Verteilung von {user.username}")
                self.add_log("INFO", "GOLD", f"Sofort-Verteilung {custom_amount} Gold", f"Ausgelöst von {user.username}")
                await self._distribute_gold(amount=custom_amount, forced=True)
            else:
                await self.highrise.send_whisper(user.id, "⚡ Gold wird sofort verteilt...")
                log_admin.info(f"!gold now: Sofort-Verteilung von {user.username}")
                self.add_log("INFO", "GOLD", "Sofort-Verteilung", f"Ausgelöst von {user.username}")
                await self._distribute_gold(forced=True)
            return

        # ── Betrag + Interval ändern ────────────────────────────
        # Unterstützt beide Schreibweisen:
        #   !gold 5 30 m   (mit Leerzeichen)
        #   !gold 5 30m    (Interval und Einheit zusammen)
        #   !gold 5m       (nur Betrag+Einheit → Fehler, aber saubere Meldung)
        import re

        # Alle args zusammenjoinen und neu aufteilen, um "30m" -> "30", "m" zu unterstützen
        raw = " ".join(args)
        # Trenne Zahl+Einheit-Kombinationen auf: "30m" → "30 m", "2h" → "2 h"
        raw = re.sub(r"(\d+)([smhSMH])", r"\1 \2", raw)
        parts = raw.split()

        if len(parts) < 3:
            await self.highrise.send_whisper(
                user.id,
                "❓ Nutzung: !gold [Betrag] [Interval] [Einheit]\n"
                "Einheiten: s = Sekunden, m = Minuten, h = Stunden\n"
                "Beispiel:  !gold 5 30m  oder  !gold 5 30 m"
            )
            return

        try:
            new_amount   = int(parts[0])
            new_interval = int(parts[1])
            unit         = parts[2].lower()
        except ValueError:
            await self.highrise.send_whisper(
                user.id, "❌ Betrag und Interval müssen Zahlen sein!"
            )
            return

        if new_amount < 1 or new_interval < 1:
            await self.highrise.send_whisper(
                user.id, "❌ Betrag und Interval müssen mindestens 1 sein!"
            )
            return

        unit_map = {"s": 1, "m": 60, "h": 3600}
        if unit not in unit_map:
            await self.highrise.send_whisper(
                user.id, "❌ Einheit muss s (Sekunden), m (Minuten) oder h (Stunden) sein!"
            )
            return

        interval_s = new_interval * unit_map[unit]
        unit_label = {"s": "Sekunde(n)", "m": "Minute(n)", "h": "Stunde(n)"}[unit]

        cfg["gold_amount"]   = new_amount
        cfg["gold_interval"] = interval_s
        cfg["gold_mode"]     = "fixed"
        self._save_config()

        await self.highrise.send_whisper(
            user.id,
            f"✅ Gold-Einstellungen gespeichert!\n"
            f"💰 Betrag: {new_amount} Gold pro Spieler\n"
            f"⏱️ Interval: alle {new_interval} {unit_label}"
        )
        log_admin.info(
            f"!gold: Einstellungen geändert von {user.username} | "
            f"amount={new_amount}G, interval={interval_s}s"
        )
        self.add_log(
            "SUCCESS", "GOLD", "Gold-Einstellungen geändert",
            f"{new_amount}G alle {new_interval} {unit_label} | Von {user.username}"
        )

    def _save_config(self):
        """Speichert die aktuelle self.config zurück in config.json."""
        try:
            save_config(self.config)
            log_system.info("config.json gespeichert")
        except Exception as e:
            log_errors.error(f"_save_config: Fehler beim Speichern: {e}")

    # ── Role Management (Mod/Owner) ───────────────────────────

    async def _cmd_role(self, user: User, cmd: str, args: list):
        """
        Verarbeitet Rollenverwaltungs-Commands.

        Unterstützte Commands: !vip, !unvip, !mod, !unmod
        ENTFERNT:  !designer, !undesigner, !owner, !unowner
          – Diese Commands sind nicht mehr per Chat verfügbar.
          – Die internen Listen (designer_list, owner_list) existieren weiterhin
            in der DB und werden von den Web-Panel-Endpoints und den
            _is_*-Hilfsfunktionen verwendet. Nur die Chat-Commands wurden entfernt.
        """
        if not args:
            await self.highrise.send_whisper(user.id, f"Nutzung: {cmd} @username")
            return

        target = args[0].lstrip("@")

        if target.isdigit():
            await self.highrise.send_whisper(
                user.id,
                f"⚠️ '{target}' sieht nach einer Zahl aus – {cmd} braucht einen @username, "
                f"keinen Zahlenwert. Beispiel: {cmd} @Spielername"
            )
            return

        target_id = next(
            (uid for uid, p in self.db["players"].items()
             if p["username"].lower() == target.lower()),
            None
        )
        if not target_id:
            await self.highrise.send_whisper(user.id, f"❓ Spieler {target} nicht gefunden!")
            return

        db = self.db
        if cmd == "!vip":
            if target_id not in db["vip_list"]:
                db["vip_list"].append(target_id)
            save_db(db)
            await self.highrise.chat(f"⭐ {target} ist jetzt ein VIP!")
            logger.info(f"VIP vergeben: {target} von {user.username}")
            log_vip.info(f"VIP vergeben: {target} (uid={target_id}) | Von {user.username}")
            self.add_log("SUCCESS", "VIP", f"{target} ist jetzt VIP", f"Vergeben von {user.username}")

        elif cmd == "!unvip":
            db["vip_list"] = [v for v in db["vip_list"] if v != target_id]
            await self.highrise.chat(f"❌ {target}'s VIP-Status wurde entfernt.")
            log_vip.info(f"VIP entfernt: {target} (uid={target_id}) | Von {user.username}")
            self.add_log("INFO", "VIP", f"{target} ist kein VIP mehr", f"Entfernt von {user.username}")

        elif cmd == "!mod":
            if target_id not in db["mod_list"]:
                db["mod_list"].append(target_id)
            await self.highrise.chat(f"🛡️ {target} ist jetzt ein Moderator!")
            logger.info(f"Mod vergeben: {target} von {user.username}")
            log_moderation.info(f"Mod vergeben: {target} (uid={target_id}) | Von {user.username}")
            self.add_log("SUCCESS", "VIP", f"{target} ist jetzt Mod", f"Vergeben von {user.username}")

        elif cmd == "!unmod":
            db["mod_list"] = [m for m in db["mod_list"] if m != target_id]
            await self.highrise.chat(f"❌ {target}'s Mod-Status entfernt.")
            log_moderation.info(f"Mod entfernt: {target} (uid={target_id}) | Von {user.username}")
            self.add_log("INFO", "VIP", f"{target} ist kein Mod mehr", f"Entfernt von {user.username}")

        elif cmd == "!owner":
            if target_id not in db["owner_list"]:
                db["owner_list"].append(target_id)
            await self.highrise.chat(f"👑 {target} ist jetzt Owner!")
            logger.info(f"Owner vergeben: {target} von {user.username}")
            log_admin.info(f"Owner vergeben: {target} (uid={target_id}) | Von {user.username}")
            self.add_log("SUCCESS", "VIP", f"{target} ist jetzt Owner", f"Vergeben von {user.username}")

        elif cmd == "!unowner":
            db["owner_list"] = [o for o in db["owner_list"] if o != target_id]
            await self.highrise.chat(f"❌ {target}'s Owner-Status entfernt.")
            log_admin.info(f"Owner entfernt: {target} (uid={target_id}) | Von {user.username}")
            self.add_log("INFO", "VIP", f"{target} ist kein Owner mehr", f"Entfernt von {user.username}")

        save_db(db)

    # ── Gold Distribution ─────────────────────────────────────

    async def gold_loop(self):
        logger.info("Gold-Timer gestartet")
        last_distribution = self.db.get("_last_gold_distribution", None)
        if last_distribution is None:
            # Erster Start — erstmaliges Intervall abwarten
            last_distribution = time.time()
        while True:
            # Wenn Gold-Verteilung deaktiviert oder kein Interval gesetzt → 30s warten und erneut prüfen
            if not self.config.get("gold_enabled", False):
                await asyncio.sleep(30)
                continue

            mode = self.config.get("gold_mode", "fixed")
            if mode == "random":
                interval = random.randint(
                    self.config.get("gold_min") or 0,
                    self.config.get("gold_max") or 0
                )
            else:
                interval = self.config.get("gold_interval") or 0

            if interval <= 0:
                logger.debug("Gold-Loop: Kein Interval gesetzt – warte 30s.")
                await asyncio.sleep(30)
                continue

            # Prüfe ob seit letzter Verteilung genug Zeit vergangen ist
            now = time.time()
            elapsed = now - last_distribution
            if elapsed < interval:
                # Noch warten
                wait_time = interval - elapsed
                logger.debug(f"Gold-Loop: Warte noch {wait_time:.0f}s bis zur nächsten Verteilung")
                await asyncio.sleep(min(wait_time, 30))
                continue

            if interval >= 3600:
                logger.info(f"Nächste Gold-Verteilung in {interval // 3600}h {(interval % 3600) // 60}m")
            elif interval >= 60:
                logger.info(f"Nächste Gold-Verteilung in {interval // 60}m {interval % 60}s")
            else:
                logger.info(f"Nächste Gold-Verteilung in {interval}s")
            
            await self._distribute_gold()
            last_distribution = time.time()
            self.db["_last_gold_distribution"] = last_distribution
            save_db(self.db)

    async def _distribute_gold(self, amount: Optional[int] = None, forced: bool = False):
        amount = amount or self.config.get("gold_amount") or 0

        if amount <= 0:
            logger.warning("_distribute_gold: Kein Gold-Betrag gesetzt – Verteilung übersprungen.")
            log_payments.warning("_distribute_gold: Betrag nicht konfiguriert (0 oder None) – übersprungen.")
            return

        try:
            # 1. Spielerliste abrufen und DEDUPPLIZIEREN
            room = await self.highrise.get_room_users()
            seen_ids = set()
            users = []
            for u, _ in room.content:
                if u.id != self.highrise.my_id and u.id not in seen_ids:
                    seen_ids.add(u.id)
                    users.append(u)
            
            if not users:
                logger.info("_distribute_gold: Niemand im Raum.")
                return

            total_needed = amount * len(users)

            # 2. Live-Wallet prüfen
            bot_wallet = await self.highrise.get_wallet()
            current_balance = getattr(bot_wallet.content[0], "amount", 0) if bot_wallet and bot_wallet.content else 0
            self.wallet_balance = current_balance

            if current_balance < total_needed:
                logger.warning(f"Gold-Verteilung abgebrochen! Nicht genug Guthaben: Bot hat {current_balance}G, benötigt {total_needed}G.")
                log_payments.warning(f"Abbruch: {current_balance}G / {total_needed}G benötigt.")
                self.add_log("WARNING", "GOLD", "Nicht genug Gold für Verteilung", f"Brauche {total_needed}G, habe {current_balance}G")
                return

            logger.info(f"Verteile {amount}G an {len(users)} Spieler (Gesamt: {total_needed}G)")
            log_payments.info(f"Gold-Verteilung gestartet: {amount}G × {len(users)} Spieler | forced={forced}")
            total_sent = 0
            for u in users:
                sent, failed = await self._send_gold(u.id, amount)
                total_sent += sent
                if sent < amount:
                    logger.warning(f"Gold-Verteilung an {u.username} unvollständig: {sent}/{amount}G")
                    log_payments.warning(f"Gold-Verteilung unvollständig für {u.username}: {sent}/{amount}G")
                else:
                    log_payments.debug(f"Gold verteilt: {amount}G → {u.username}")
            # Chat-Nachricht nur bei manueller Verteilung, sonst Whisper an Owner
            if forced:
                await self.highrise.chat(f"⚡ {amount} Gold an {len(users)} Spieler verteilt!")
            else:
                owners = set()
                owner_id = self.config.get("owner_id")
                if owner_id:
                    owners.add(owner_id)
                owners.update(self.db.get("owner_list", []))

                for oid in owners:
                    try:
                        await self.highrise.send_whisper(
                            oid,
                            f"🪙 Auto-Gold: {amount}G an {len(users)} Spieler verteilt!"
                        )
                    except Exception as e:
                        logger.debug(f"Konnte Auto-Gold Whisper nicht an Owner {oid} senden: {e}")
            logger.info("Gold-Verteilung abgeschlossen")
            log_payments.info(f"Gold-Verteilung abgeschlossen: {total_sent}G total")
            self.add_gold_event("BOT", total_sent, "distributed",
                                f"{amount}G an {len(users)} Spieler")
            self.add_log("SUCCESS", "GOLD", "Gold verteilt",
                         f"{amount}G an {len(users)} Spieler im Raum")
        except Exception as e:
            logger.error(f"Gold-Verteilung Fehler: {e}")
            log_errors.exception(f"_distribute_gold: {e}")
            self.add_log("ERROR", "GOLD", "Gold-Verteilung fehlgeschlagen", str(e))

    # ── Custom-Gold-Versand (beliebiger Betrag) ───────────────

    async def _send_gold(self, user_id: str, amount: int) -> tuple[int, int]:
        """Sendet einen beliebigen Gold-Betrag an einen Spieler, indem der
        Betrag automatisch in passende Gold-Bars zerlegt wird (Highrise
        erlaubt nur feste Stückelungen pro tip_user-Aufruf).

        Gibt (gesendeter_betrag, anzahl_fehlgeschlagener_bars) zurück.
        """
        bars = gold_to_bars(amount)
        sent_amount = 0
        failed = 0
        for item in bars:
            denom = next(d for d, i in GOLD_BAR_MAP.items() if i == item)
            try:
                await self.highrise.tip_user(user_id, item)
                sent_amount += denom
                await asyncio.sleep(0.5)
            except Exception as e:
                failed += 1
                logger.warning(f"_send_gold: {item} an {user_id} fehlgeschlagen: {e}")
                log_payments.warning(f"_send_gold: {item} an {user_id} fehlgeschlagen: {e}")
        return sent_amount, failed

    # ── Permission Helpers ────────────────────────────────────

    def _is_vip_plus(self, uid: str) -> bool:
        """True für Owner, Co-Owner, Moderatoren, VIPs und Designer."""
        db = self.db
        return (
            uid == self.config.get("owner_id")
            or uid in db.get("owner_list", [])
            or uid in db.get("mod_list", [])
            or uid in db.get("vip_list", [])
            or uid in db.get("designer_list", [])
        )

    def _is_owner(self, uid: str) -> bool:
        """True für den Haupt-Owner und alle Co-Owner."""
        db = self.db
        return (
            uid == self.config.get("owner_id")
            or uid in db.get("owner_list", [])
        )

    def _is_mod_plus(self, uid: str) -> bool:
        """True für Owner, Co-Owner und Moderatoren."""
        db = self.db
        return (
            uid == self.config.get("owner_id")
            or uid in db.get("owner_list", [])
            or uid in db.get("mod_list", [])
        )


# ─────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    token = os.getenv("BOT_TOKEN")
    room  = os.getenv("ROOM_ID")
    if not token or not room:
        raise SystemExit("FEHLER: BOT_TOKEN und ROOM_ID in .env setzen!")

    # ── Signal-Handler: PM2-sauberes Beenden ──
    import signal, threading

    _shutdown_requested = False

    def _force_exit():
        """Fallback: Nach 5s wird der Prozess hart beendet."""
        time.sleep(5)
        log_system.warning("Graceful Shutdown Timeout – erzwinge Exit.")
        os._exit(0)

    def _signal_handler(sig, frame):
        global _shutdown_requested
        sig_name = signal.Signals(sig).name
        if _shutdown_requested:
            # Zweites Signal → sofort raus
            log_system.warning(f"Zweites {sig_name} empfangen – erzwinge Exit.")
            os._exit(0)
        _shutdown_requested = True
        log_system.info(f"{sig_name} empfangen – fahre herunter...")
        # Fallback-Timer starten (Daemon-Thread stirbt mit dem Prozess)
        t = threading.Thread(target=_force_exit, daemon=True)
        t.start()
        # KeyboardInterrupt in den Event-Loop werfen
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    async def run_forever():
        attempt = 0
        consecutive_fast_fails = 0

        while True:
            attempt += 1
            logger.info(f"Verbindungsversuch {attempt}...")
            bot = None
            connect_time = time.time()

            try:
                bot = HighriseBot()
                await main([BotDefinition(bot, room, token)])

                session_duration = time.time() - connect_time

                if not getattr(bot, "_on_start_called", False):
                    # Sofort getrennt – "already added" oder Rate-Limit
                    consecutive_fast_fails += 1
                    # Exponentieller Backoff: 60s, 90s, 120s … max 300s
                    wait = min(60 + (consecutive_fast_fails - 1) * 30, 300)
                    logger.warning(
                        f"Verbindung sofort getrennt (Schnell-Fail #{consecutive_fast_fails}). "
                        f"Warte {wait}s..."
                    )
                    await asyncio.sleep(wait)
                else:
                    consecutive_fast_fails = 0
                    if session_duration < 30:
                        logger.warning(f"Session dauerte nur {session_duration:.0f}s. Warte 30s...")
                        await asyncio.sleep(30)
                    else:
                        logger.warning(f"Bot getrennt (nach {session_duration:.0f}s). Neustart in 15s...")
                        await asyncio.sleep(15)

            except KeyboardInterrupt:
                logger.info("Bot manuell gestoppt.")
                break
            except Exception as e:
                error_msg = str(e).lower()
                if "already added" in error_msg:
                    consecutive_fast_fails += 1
                    wait = min(60 + (consecutive_fast_fails - 1) * 30, 300)
                    logger.warning(f"'Bot is already added' (#{consecutive_fast_fails}) – Warte {wait}s...")
                    await asyncio.sleep(wait)
                else:
                    consecutive_fast_fails = 0
                    logger.error(f"Unerwarteter Fehler: {e}")
                    log_errors.exception(f"run_forever: Unerwarteter Fehler: {e}")
                    await asyncio.sleep(15)

    asyncio.run(run_forever())