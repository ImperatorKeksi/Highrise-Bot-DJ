// PM2 Ecosystem Konfiguration
// Highrise Bot – 24/7 Betrieb
module.exports = {
  apps: [
    {
      // ── Stream-Master (24/7 Icecast-Feeder) ────────────────
      name:        "stream-master",
      script:      "stream_master.py",
      interpreter: "/opt/highrise-bot/venv/bin/python3",
      cwd:         "/opt/highrise-bot",

      env: {
        PYTHONUNBUFFERED: "1",
        PYTHONIOENCODING: "utf-8",
      },

      autorestart:   true,
      watch:         false,
      max_restarts:  50,
      min_uptime:    "10s",
      restart_delay: 3000,
      kill_timeout:  10000,

      error_file:      "logs/stream-master-error.log",
      out_file:        "logs/stream-master-output.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      merge_logs:      true,
      max_size:        "50M",
    },
    {
      // ── Bot Prozess ──────────────────────────────────────────
      name:        "highrise-bot",
      script:      "bot.py",
      interpreter: "/opt/highrise-bot/venv/bin/python3",
      cwd:         "/opt/highrise-bot",

      env: {
        PYTHONUNBUFFERED: "1",
        PYTHONIOENCODING: "utf-8",
        NODE_OPTIONS: "--max-old-space-size=512",
        NODE_PATH: "/usr/local/bin",
        PATH: "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
      },

      autorestart:  true,
      watch:        false,
      max_restarts: 15,
      min_uptime:   "30s",
      restart_delay: 5000,
      kill_timeout: 30000,

      error_file:      "logs/bot-error.log",
      out_file:        "logs/bot-output.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      merge_logs:      true,
      max_size:        "50M",
    },
    // ── Cloudflare Tunnel ────────────────────────────────────
    // DEAKTIVIERT — braucht Cloudflare Auth Config
    // Wird wieder aktiviert wenn Cloudflare Tunnel eingerichtet ist
  ]
};
