# nasbot — TrueNAS ⇄ Discord

A self-hosted Discord bot for a TrueNAS server. Ask it how the NAS is doing with slash commands, and it
pushes alerts to a channel on its own — including the things TrueNAS can't tell you itself, like the NAS
dropping off the network or an app crashing. Optionally it watches Sonarr and Radarr too, and tells you when a
download needs a human.

- **Outbound-only.** The bot dials out to Discord and to your NAS. No ports to open, nothing exposed — it
  works fine over Tailscale.
- **Read-only.** It uses a dedicated TrueNAS user with the *Readonly Admin* role. It can't change anything.
- **Future-proof API.** Built on TrueNAS's JSON-RPC websocket API via iXsystems' official client — not the
  REST API, which was deprecated in 25.04 and is removed in 26/27.

## What you get

| Command | Shows |
|---|---|
| `/nas status` | Health roll-up: version, uptime, live CPU % / temp, memory & ARC, every pool, alert summary, apps |
| `/nas pools` | Per pool: state, capacity bar, used/free, fragmentation, last scrub (or live scrub/resilver progress) |
| `/nas disks` | Every disk with pool, temperature, size and model |
| `/nas alerts` | Current TrueNAS alerts, worst first (`include_dismissed:True` to see dismissed ones) |
| `/nas apps` | Installed apps, their state, and available updates |
| `/nas ping` | Bot ↔ Discord and bot ↔ TrueNAS latency, last poll result, mute status |
| `/nas mute minutes:N` | Hold notifications during maintenance (`0` unmutes). You get a summary of what was held back |
| `/media queue` | *(with Sonarr/Radarr configured)* What's downloading with progress & ETA, and anything that needs you |
| `/media health` | *(with Sonarr/Radarr configured)* Sonarr/Radarr health checks |

And it posts, unprompted, to your alert channel:

| Event | Notes |
|---|---|
| **New TrueNAS alert** | Anything at `ALERT_MIN_LEVEL` or above (default WARNING): degraded pools, SMART errors, failed scrubs, capacity, failed replication/cloud sync, updates… |
| **Alert resolved / dismissed** | Distinguishes "TrueNAS cleared it" from "someone dismissed it in the UI" |
| **NAS unreachable** | After `UNREACHABLE_AFTER_FAILURES` failed polls (default 3 min). One message, not one per minute |
| **NAS back** | With how long it was gone, and whether it rebooted meanwhile |
| **Rebooted** | Detected from boot time, even if it was quick enough to never look "down" |
| **App crashed / stopped / recovered** | TrueNAS itself raises no alert for these |
| **Daily digest** (optional) | `/nas status` at a time you pick, in your time zone |
| **Download needs you** (Sonarr/Radarr) | Finished but *Unable to Import Automatically*, or failed — once per download, after a grace period, with what Sonarr said and what to do. A season pack is one message, not one per episode |
| **Download sorted** | When a download you were told about leaves that state |
| **Sonarr/Radarr health** | Indexers failing, download client unreachable, disk space… appearing and clearing |
| **Sonarr/Radarr unreachable / back** | Not while the whole NAS is down — that alert already covers it |
| **Ready to watch** (optional) | One message per download when something is imported |

Urgent notifications (default: CRITICAL and above, NAS unreachable, app crashes when the threshold allows)
can `@mention` a role or a user. Timestamps use Discord's native format, so everyone sees them in their own
time zone. Bursts are bundled (a scrub finding 40 errors is one message, not 40). State is persisted, so
restarting the bot neither re-announces open alerts nor misses ones that cleared while it was down.

Requires **TrueNAS 25.04 or newer** (SCALE / Community Edition).

---

## Setup (about 10 minutes)

### 1. TrueNAS: a read-only user and API key

1. **Credentials → Users → Add**
   - Username: `nasbot` (any name works; it goes in `TRUENAS_USERNAME`)
   - **Disable Password** (it only ever logs in with the key)
   - Under **Allow Access** pick **TrueNAS Access → Readonly Admin**. On versions without that selector,
     add the auxiliary group `truenas_readonly_administrators` instead.
   - No shell, SSH or SMB access needed.
2. Select the new user → **Access** card → **Add API Key** (or top-right user menu → *My API Keys*).
   Name it `discord-bot`, pick the `nasbot` user, save, and **copy the key now** — it's shown only once.
   It looks like `5-a8Kf…` (an id, a dash, then 64 characters).

> ⚠️ **Never point the bot at `http://` / `ws://` with a plain API key.** TrueNAS automatically revokes a key
> it receives over an unencrypted connection. A bare hostname defaults to `wss://`, and the bot refuses an
> insecure URL unless you override it, so you'd have to work hard to get this wrong.

### 2. Discord: create the bot

1. <https://discord.com/developers/applications> → **New Application** → name it (e.g. *NAS*).
2. **Bot** tab → **Reset Token** → copy it (`DISCORD_TOKEN`). Turn **Public Bot** off.
   No privileged intents are needed — leave all three off.
3. **OAuth2 → URL Generator** → scopes **`bot`** and **`applications.commands`**; bot permissions
   **View Channels, Send Messages, Embed Links, Read Message History**. Open the generated URL and add it
   to your server. (The bot also logs a ready-made invite URL on startup.)
4. In Discord: **User Settings → Advanced → Developer Mode** on. Then right-click to **Copy ID** for:
   your alert channel (`DISCORD_ALERT_CHANNEL_ID`), your server (`DISCORD_GUILD_ID`), and yourself
   (`DISCORD_ALLOWED_USER_IDS` / `DISCORD_MENTION_USER_ID`).

### 3. Configure

```bash
git clone https://github.com/sheeeeply/nasbot.git && cd nasbot
cp .env.example .env
nano .env        # fill in the values from steps 1–2
```

Minimum: `DISCORD_TOKEN`, `DISCORD_ALERT_CHANNEL_ID`, `TRUENAS_URL`, `TRUENAS_USERNAME`, `TRUENAS_API_KEY`.
Strongly recommended: `DISCORD_GUILD_ID` (commands appear instantly), `DISCORD_ALLOWED_USER_IDS`, and
`TRUENAS_VERIFY_SSL=false` unless you've installed a trusted certificate on TrueNAS.

### 4. Verify the TrueNAS side before going live

```bash
docker compose run --rm nasbot check
```

```
TrueNAS check — wss://truenas.tail1234.ts.net/api/current  (user: nasbot, auth: plain, verify_ssl: False)
  ✔ connect + login          ok
  ✔ system.info              truenas · TrueNAS 25.10.2 · up 12d 4h
  ✔ pool.query               tank ONLINE 31%
  ✔ boot.get_state           ONLINE
  ✔ alert.list               3 total, 1 would be reported
  ✔ app.query                6 apps
  ✔ disks + temperatures     4 disks, max 41°C
  ✔ reporting.realtime       CPU 4%, memory 38%
  ✔ latency                  92 ms

All good.
```

Every failure prints what to fix (wrong URL, TLS, rejected key, missing role).

### 5. Run it

```bash
docker compose up -d
docker compose logs -f
```

Within a minute the bot posts **🤖 NAS bot online** in your alert channel. Try `/nas status`.

---

## Where to run it

Anywhere with outbound internet that can reach the NAS's web UI port.

- **A Docker host next to the NAS (recommended).** E.g. an Ubuntu VM on the same Proxmox box or LAN. Point
  `TRUENAS_URL` at the NAS's LAN IP.
- **Any machine on your tailnet.** Use the NAS's MagicDNS name or `100.x.y.z` address. If the name doesn't
  resolve inside the container, use the IP or set `network_mode: host` in `docker-compose.yml`.
- **On TrueNAS itself as a Custom App.** Works, using the image CI publishes to GHCR
  (`ghcr.io/<you>/<repo>:latest`) with the same environment variables and a dataset mounted at `/data`.
  Caveat: a bot running *on* the NAS can't tell you the NAS is down.

**Catching "the whole thing is off".** If the box running the bot loses power or internet, the bot can't
report it. Close that gap with a free dead-man's switch: create a check on
[healthchecks.io](https://healthchecks.io) (period = your poll interval, grace ≈ 5 min), add its Discord
integration, and put the ping URL in `HEALTHCHECK_PING_URL`. The bot pings on every successful poll and
pings `/fail` when the NAS is unreachable; if the pings stop entirely, healthchecks.io tells you on Discord.
Uptime Kuma push URLs work too.

---

## Sonarr & Radarr (optional)

If you run an *arr* media stack, nasbot can tell you when it needs a human — the downloads that otherwise sit
in **Activity → Queue** with an orange icon until you happen to look. It's read-only, like the TrueNAS side.

1. Copy each app's API key: **Settings → General → Security → API Key**.
2. Add to `.env` (only the apps you use):

   ```bash
   SONARR_URL=http://192.168.1.158:8989
   SONARR_API_KEY=...
   RADARR_URL=http://192.168.1.158:7878
   RADARR_API_KEY=...
   # optional: the address you open the apps at, for links in Discord
   SONARR_LINK_URL=http://nas.your-tailnet.ts.net:8989
   ```

   Use the NAS's LAN IP and the app's published port — that's reachable from the bot's container. Plain
   `http` is fine on your own network (unlike TrueNAS, the *arr apps don't revoke keys).
3. `docker compose run --rm nasbot check` now also checks each app:

   ```
   Sonarr check — http://192.168.1.158:8989
     ✔ connect + API key        Sonarr 4.0.15.2941
     ✔ queue                    3 download(s), 1 need attention now
     ✔ health                   0 issue(s)
   ```
4. `docker compose up -d --build`. The startup message lists the apps it's watching.

**What counts as "needs you":** a finished download Sonarr/Radarr couldn't import on its own (wrong episode
numbering, a sample, an unexpected file layout…) and failed downloads. Stalled torrents are left alone by
default because tools like [Cleanuparr](https://github.com/Cleanuparr/Cleanuparr) replace them automatically —
set `MEDIA_WATCH_STALLED=true` if you don't run one. Nothing is reported until it has been stuck for
`MEDIA_GRACE_MINUTES` (default 10), so downloads that sort themselves out never make noise. *Waiting to
Import* is treated as the temporary hold it usually is — e.g. Sonarr waits up to 48 hours to import a new
episode whose title is still TBA — and is only reported if it lasts more than 49 hours.

Mute (`/nas mute`) and state persistence cover media notifications too.

---

## Configuration reference

All settings are environment variables (see `.env.example`). Secrets also accept a `_FILE` variant —
`DISCORD_TOKEN_FILE`, `TRUENAS_API_KEY_FILE` — for Docker secrets.

| Variable | Default | |
|---|---|---|
| `DISCORD_TOKEN` | — | Bot token **(required)** |
| `DISCORD_ALERT_CHANNEL_ID` | — | Where notifications go **(required)** |
| `DISCORD_GUILD_ID` | — | Register commands only in this server (instant; otherwise global sync can take up to an hour) |
| `DISCORD_ALLOWED_USER_IDS` | — | Comma-separated user IDs allowed to run commands |
| `DISCORD_ALLOWED_ROLE_IDS` | — | …or role IDs. If both are empty, anyone in the server can |
| `DISCORD_MENTION_ROLE_ID` / `DISCORD_MENTION_USER_ID` | — | Who gets pinged on urgent notifications |
| `MENTION_MIN_LEVEL` | `CRITICAL` | Alert level that triggers the mention |
| `DISCORD_EPHEMERAL_REPLIES` | `false` | Command replies visible only to the caller |
| `ANNOUNCE_STARTUP` | `true` | Post "bot online" on start |
| `TRUENAS_URL` | — | Host, IP or URL. `nas` → `wss://nas/api/current` **(required)** |
| `TRUENAS_USERNAME` | — | Owner of the API key **(required)** |
| `TRUENAS_API_KEY` | — | **(required)** |
| `TRUENAS_AUTH` | `plain` | `plain` for 25.04/25.10. `scram` for 26-beta/27+: proves the key without ever sending it |
| `TRUENAS_VERIFY_SSL` | `true` | `false` for TrueNAS's default self-signed certificate |
| `TRUENAS_CHANNEL_BINDING` | `true` | SCRAM channel binding (scram only) |
| `POLL_INTERVAL_SECONDS` | `60` | Minimum 15 |
| `UNREACHABLE_AFTER_FAILURES` | `3` | Failed polls before "unreachable" |
| `ALERT_MIN_LEVEL` | `WARNING` | `INFO` `NOTICE` `WARNING` `ERROR` `CRITICAL` `ALERT` `EMERGENCY` |
| `ALERT_IGNORE_CLASSES` | — | Alert classes to drop, e.g. `HasUpdate`. The class is shown in each alert's footer |
| `WATCH_APPS` | `true` | App crash/stop/recover notifications |
| `DIGEST_TIME` / `DIGEST_TZ` | — / `UTC` | Daily digest, e.g. `08:30` + `Asia/Seoul` |
| `HEALTHCHECK_PING_URL` | — | healthchecks.io / Uptime Kuma push URL |
| `LOG_LEVEL` | `INFO` | |
| `DATA_DIR` | `/data` | State + heartbeat (advanced) |
| `TRUENAS_CALL_TIMEOUT` | `30` | Seconds per API call (advanced) |
| `TRUENAS_ALLOW_INSECURE` | `false` | Permit `plain` auth over `ws://`. Only for test rigs — a real NAS will revoke the key |
| `SONARR_URL` / `SONARR_API_KEY` | — | Watch Sonarr (both needed). `_FILE` variant for the key |
| `RADARR_URL` / `RADARR_API_KEY` | — | Watch Radarr (both needed). `_FILE` variant for the key |
| `SONARR_LINK_URL` / `RADARR_LINK_URL` | the URL | Address used for links in Discord, if you open the apps differently |
| `MEDIA_POLL_INTERVAL_SECONDS` | `300` | How often to check Sonarr/Radarr. Minimum 60 |
| `MEDIA_GRACE_MINUTES` | `10` | How long a download must be stuck before it's reported |
| `MEDIA_WATCH_STALLED` | `false` | Also report stalled (no seeders) downloads |
| `MEDIA_NOTIFY_IMPORTS` | `false` | "Ready to watch" messages |
| `MEDIA_MENTION` | `false` | @mention on downloads that need you |
| `MEDIA_HEALTH_IGNORE` | — | Health check names to drop, e.g. `IndexerLongTermStatusCheck`. Shown in each message's footer |
| `MEDIA_VERIFY_SSL` | `true` | `false` if the apps sit behind a self-signed `https` proxy (advanced) |
| `MEDIA_TIMEOUT` | `20` | Seconds per Sonarr/Radarr request (advanced) |

**Upgrading TrueNAS to 26/27:** set `TRUENAS_AUTH=scram`. Nothing else changes.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `check`: **✘ connect … CERTIFICATE_VERIFY_FAILED** | `TRUENAS_VERIFY_SSL=false` (self-signed cert), or install a trusted cert on TrueNAS |
| `check`: **✘ connect … refused / timed out** | Wrong host/port, NAS off, or the machine isn't on the tailnet. `curl -k https://<host>` from the same machine |
| `check`: **✘ login** | Key typo, `TRUENAS_USERNAME` isn't the key's owner, or wrong `TRUENAS_AUTH` for your version. Was the key ever sent over `http://`? Then TrueNAS revoked it — reset it |
| `check`: **✘ pool.query … Not authorized** | The user lacks the Readonly Admin role |
| `! app.query` | Apps aren't configured on this NAS (no pool chosen for apps). Harmless |
| Slash commands don't appear | Set `DISCORD_GUILD_ID`, and make sure the invite included `applications.commands`. Restart the bot |
| "Missing permission to post" in logs | Give the bot View Channel + Send Messages + Embed Links in the alert channel |
| `Discord rejected DISCORD_TOKEN` | Reset the token in the Developer Portal and update `.env` |
| Bot locked out for 10 minutes after many restarts | TrueNAS rate-limits failed logins (20/min). The bot backs off 10 min after a rejected key so it never trips this itself |
| Container shows `unhealthy` | The monitor loop stopped cycling; usually Discord login. `docker compose logs` |
| `check`: **✘ connect** under Sonarr/Radarr | Use the NAS's LAN IP + published port (`http://192.168.1.158:8989`), not a container name — the bot isn't on the *arr stack's Docker network |
| `check`: **✘ request … didn't return JSON** | The URL points at something else (a login page, the wrong port), or the app uses a URL base: add it, e.g. `http://host:8989/sonarr` |
| `/media` commands don't appear | Only registered when a Sonarr/Radarr URL + key are set. Restart after editing `.env` |

**Zero-code alternative / belt-and-braces.** TrueNAS can also push alerts to Discord directly: **System →
Alert Settings → Add → Slack**, with your channel's webhook URL plus `/slack` on the end (Discord accepts
Slack-format webhooks). That covers alerts only — no commands, no "unreachable", no app watch — but it
doesn't depend on this bot.

---

## Development

```bash
pip install -e ".[dev]"
ruff check src tests
pytest -q            # 237 tests, ~10s
```

The suite includes end-to-end tests that run iXsystems' real `truenas_api_client` against a
protocol-faithful mock of middlewared (`tests/mock_truenas.py`): handshake, API-key login, realtime event
streaming, permission errors, dropped connections and reconnects, plus the `check` CLI as a subprocess.
The media watcher is tested the same way against a Sonarr/Radarr API stand-in (`tests/mock_arr.py`).

```
src/nasbot/
  config.py      env parsing & validation (reports every problem at once)
  truenas.py     persistent, auto-reconnecting async wrapper over the official sync client
  collectors.py  API calls → models; partial results when a section fails
  models.py      defensive parsing across TrueNAS versions
  monitor.py     the alert engine: diffing, outages, reboots, apps, mute, digest (no Discord I/O)
  state.py       atomic JSON persistence
  formatting.py  embeds, within Discord's size limits
  bot.py         slash commands + posting loop
  heartbeat.py   Docker healthcheck + dead-man's-switch pings
  arr.py         Sonarr/Radarr v3 API client + models (queue, health, history)
  media.py       the media watcher: needs-attention, health, reachability, imports (no Discord I/O)
  notify.py      the Notification both watchers hand to the bot
```

MIT licensed.
