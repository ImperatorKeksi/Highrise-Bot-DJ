# 🤖 Highrise Bot — Musik + Emotes

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-orange.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://python.org)
[![Highrise](https://img.shields.io/badge/Platform-Highrise-red.svg)](https://highrise.game)
[![Icecast](https://img.shields.io/badge/Streaming-Icecast-green.svg)](https://icecast.org)
[![PM2](https://img.shields.io/badge/Process-PM2-2b037a.svg)](https://pm2.keymetrics.io)

**Ein vollständiger Bot für [Highrise](https://highrise.game) mit Musik-Streaming, Emote-System, Gold-Verwaltung und Web-Dashboard**

[Installation](#-installation) • [Features](#-features) • [Bot-Befehle](#-bot-befehle) • [Web-Panel](#-web-panel) • [Architektur](#-architektur)

**Entwickelt von [ImperatorKeksi](https://github.com/ImperatorKeksi)**

</div>

---

## 📋 Überblick

Der **Highrise Bot** ist ein vollautomatischer Bot für die Social-World-Plattform [Highrise](https://highrise.game). Er bietet:

- 🎵 **Musik-Streaming** — YouTube-Musik abspielen, via Icecast streamen, Auto-Playlist
- 🎭 **Emote-System** — Spieler können Emotes per Name oder Nummer auslösen
- 💰 **Gold-System** — In-Game-Währung mit automatischer Verteilung
- 👥 **Spieler-Management** — VIPs, Moderatoren, Zeit-Tracking
- 🌐 **Web-Dashboard** — Vollständige Steuerung über Browser
- 📻 **Radio-Stream** — MP3-Stream für Discord, Websites etc.

---

## ✨ Features

### Musik & Streaming
- **YouTube-Integration** — Jeder YouTube-Link kann abgespielt werden
- **Auto-Playlist** — YouTube-Playlists automatisch einlesen und durchspielen
- **Queue-System** — User können Songs einreihen (max 3 pro User)
- **Pre-Fetch** — Nächster Song wird im Hintergrund vorgeladen (lückenlos)
- **Radio-Stream** — MP3 128kbps via Icecast2
- **Song-Info** — Aktueller Song als JSON für externe Anzeigen

### Emote-System
- **307 Standard-Emotes** — Vollständige Highrise-Emote-Liste
- **Emote-Loop** — Kontinuierliches Senden aktiver Emotes (0.5s Intervall)
- **Custom Emotes** — Auslöser-Name + Emote-ID + Nummer konfigurierbar
- **Web-Panel** — Emotes verwalten, bearbeiten, löschen
- **Keepalive** — Bot sendet automatisch Wink/Wave als Aktivitäts-Check

### Web-Panel (6 Tabs)
- **Dashboard** — Spieleranzahl, Gold, Queue, Song, Uptime, Stream-Player
- **Einstellungen** — Gold, Musik, VIP, Bot-Token, Playlist, JSON-Editor
- **Spieler** — VIPs, Mods, alle Spieler mit Gold, Zeit, Badges
- **Emotes** — Emote-Verwaltung mit Suche, Bearbeiten, Importieren
- **Logs** — System-Logs mit Filter, Export
- **Datenbank** — JSON-Editor für database.json

### Bot-Befehle

**Alle Spieler:**
| Befehl | Beschreibung |
|--------|-------------|
| `!play [Song - Artist]` | Songwunsch einreihen (10 Gold) |
| `!np` | Aktuellen Song anzeigen |
| `!queue` | Warteschlange anzeigen |
| `!balance` | Gold-Stats anzeigen |
| `!tip` | Wie bekomme ich Musik-Credits? |
| `!top` | Top 3 Spender |
| `!help` | Alle Befehle anzeigen |
| `[Emote-Name]` | Emote starten (z.B. "winken") |
| `[Nummer]` | Emote per Nummer (z.B. "71") |
| `!stop` / `stop` | Aktives Emote stoppen |
| `!emotelist` | Alle Emotes mit Name + Nummer |

**Moderatoren:**
| Befehl | Beschreibung |
|--------|-------------|
| `!skip` | Aktuellen Song überspringen |
| `!refund [Anzahl] @user` | Musik-Credits gutschreiben |
| `!checkvip` | VIP- & Mod-Liste anzeigen |

**Owner:**
| Befehl | Beschreibung |
|--------|-------------|
| `!vip @user` | VIP-Status vergeben |
| `!unvip @user` | VIP-Status entfernen |
| `!mod @user` | Moderator ernennen |
| `!unmod @user` | Moderator entfernen |
| `!setpos` | Bot-Position setzen |
| `!reload` | Bot neu starten |
| `!checkgold` | Bot-Guthaben anzeigen |
| `!gold` | Gold-Einstellungen anzeigen |
| `!gold on/off` | Gold-Verteilung ein/aus |
| `!gold now` | Gold sofort verteilen |
| `!gold [Betrag] [Interval] [Einheit]` | Gold konfigurieren |

---

## 🚀 Installation

### Voraussetzungen
- Python 3.10+
- PM2 (`npm install -g pm2`)
- yt-dlp (`pip install yt-dlp`)
- Icecast2 (für Radio-Stream)
- Bot-Token von [create.highrise.game](https://create.highrise.game)

### Schritt 1: Repository klonen
```bash
git clone https://github.com/ImperatorKeksi/Highrise-Bot.git
cd Highrise-Bot
```

### Schritt 2: Virtual Environment
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Schritt 3: Konfiguration
```bash
cp .env.example .env
nano .env  # Bot-Token, Room-ID, Owner-ID eintragen
```

### Schritt 4: Icecast einrichten (optional)
```bash
sudo apt install icecast2
# Icecast-Passwort in .env eintragen
```

### Schritt 5: Bot starten
```bash
pm2 start ecosystem.config.js
pm2 save
pm2 startup
```

### Schritt 6: Web Panel öffnen
```
http://localhost:5000
```

---

## 🏗️ Architektur

```
Highrise Bot
├── bot.py              # Hauptdatei (Bot-Logik, API, Web-Panel)
├── music.py            # Musikplayer (YouTube, Queue, Pre-Fetch)
├── stream_master.py    # Icecast Stream-Feeder (ffmpeg)
├── emotes_dict.py      # 307 Standard-Emotes
├── emotes_complete.py  # Komplette Emote-Daten
├── index.html          # Web-Panel (Dashboard, Einstellungen, etc.)
├── ecosystem.config.js # PM2-Konfiguration
├── requirements.txt    # Python-Abhängigkeiten
├── .env                # Bot-Token & Konfiguration
├── config.json         # Bot-Einstellungen
└── database.json       # Spieler, Queue, Stats
```

### Datenfluss
1. **Highrise WebSocket** → Bot empfängt Chat/Emote/Join/Leave Events
2. **bot.py** verarbeitet Events, ruft Musik/Emote-Handler auf
3. **music.py** lädt YouTube-Videos, konvertiert zu PCM, speichert in FIFO
4. **stream_master.py** liest PCM, streamt via ffmpeg zu Icecast
5. **Web Panel** zeigt Status, ermöglicht Steuerung

---

## 📜 Lizenz

MIT License — siehe [LICENSE](LICENSE) für Details.

**Nutzungsbedingungen:**
- ✅ Nutzung für private und kommerzielle Zwecke erlaubt
- ✅ Modifikation und Weiterentwicklung erlaubt
- ❌ Verkauf des Codes als eigenes Produkt ist **nicht erlaubt**
- ❌ Als eigene Originalentwicklung ausgeben ist **nicht erlaubt**
- 📝 Attribution: "Code von ImperatorKeksi"

---

## 🙏 Credits

**[ImperatorKeksi](https://github.com/ImperatorKeksi)** — Konzeption, Entwicklung, Design

---

**Disclaimer:** Dies ist ein Open-Source-Projekt. Highrise ist eine eingetragene Marke der jeweiligen Inhaber. Dieser Bot ist nicht offiziell mit Highrise verbunden.
