"""Entry point.

    nasbot run          start the bot (default)
    nasbot check        verify the TrueNAS connection and API permissions, no Discord needed
    nasbot healthcheck  exit 0 if the monitor loop is alive (used by Docker HEALTHCHECK)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from . import __version__, collectors, formatting, heartbeat
from .config import ConfigError, from_env, level_rank
from .truenas import NASAuthError, NASCallError, NASUnavailable, TrueNAS


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("discord").setLevel(max(logging.INFO, logging.getLevelName(level)))
    logging.getLogger("websocket").setLevel(logging.WARNING)
    # This bot has no voice features; don't alarm people with discord.py's voice-dependency warnings.
    logging.getLogger("discord.client").addFilter(lambda r: "voice will NOT be supported" not in r.getMessage())


def cmd_run() -> int:
    try:
        cfg = from_env()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    _setup_logging(cfg.log_level)

    from .bot import NASBot
    from .monitor import Monitor
    from .state import StateStore

    log = logging.getLogger("nasbot")
    log.info("nasbot %s starting (TrueNAS %s, auth=%s)", __version__, cfg.truenas_url, cfg.truenas_auth)
    api = TrueNAS(cfg)
    monitor = Monitor(api, cfg, StateStore(cfg.state_path))
    bot = NASBot(cfg, api, monitor)
    import discord

    try:
        bot.run(cfg.discord_token, log_handler=None)
    except discord.LoginFailure:
        log.error("Discord rejected DISCORD_TOKEN. Reset it under Developer Portal → your app → Bot → "
                  "Reset Token, and paste the new value.")
        return 2
    except discord.PrivilegedIntentsRequired:  # pragma: no cover - we request none, kept for safety
        log.error("Discord refused a privileged intent; this bot needs none — check you're using the right app.")
        return 2
    return 0


async def _check(cfg) -> int:
    api = TrueNAS(cfg, base_backoff=0, auth_backoff=0)
    ok = True

    def line(mark: str, what: str, detail: str) -> None:
        print(f"  {mark} {what:<24} {detail}")

    print(f"TrueNAS check — {cfg.truenas_url}  (user: {cfg.truenas_username}, auth: {cfg.truenas_auth}, "
          f"verify_ssl: {cfg.truenas_verify_ssl})")
    try:
        info = await collectors.system_info(api)
    except NASAuthError as exc:
        line("✘", "login", str(exc))
        print("\nThe key was rejected. Check TRUENAS_USERNAME matches the user the key belongs to, and "
              "TRUENAS_AUTH (plain for 25.x, scram for 26+).")
        return 1
    except NASUnavailable as exc:
        line("✘", "connect", str(exc))
        print("\nCould not connect. Check TRUENAS_URL is reachable from here (tailnet up?), and set "
              "TRUENAS_VERIFY_SSL=false if TrueNAS uses its default self-signed certificate.")
        return 1
    except NASCallError as exc:
        line("✘", "system.info", str(exc))
        print("\nLogged in, but not allowed to read system info: give the user the Readonly Admin role.")
        return 1
    line("✔", "connect + login", "ok")
    line("✔", "system.info", f"{info.hostname} · TrueNAS {info.version} · up "
                             f"{formatting.human_duration(info.uptime_seconds)}")

    async def section(name: str, coro, describe, *, required: bool = True):
        nonlocal ok
        try:
            result = await coro
        except (NASCallError, NASUnavailable) as exc:
            line("✘" if required else "!", name, formatting.truncate(str(exc), 200))
            ok = ok and not required
            return None
        line("✔", name, describe(result))
        return result

    await section("pool.query", collectors.pools(api),
                  lambda ps: ", ".join(f"{p.name} {p.status} "
                                       f"{(p.used_fraction or 0) * 100:.0f}%" for p in ps) or "no pools")
    await section("boot.get_state", collectors.boot_pool(api), lambda p: p.status, required=False)
    min_rank = level_rank(cfg.min_alert_level)
    await section("alert.list", collectors.alerts(api),
                  lambda al: f"{len(al)} total, "
                             f"{sum(1 for a in al if not a.dismissed and a.rank >= min_rank)} would be reported")
    await section("app.query", collectors.apps(api), lambda ap: f"{len(ap)} apps", required=False)
    await section("disks + temperatures", collectors.disks(api),
                  lambda ds: f"{len(ds)} disks"
                             + (f", max {max(t)}°C" if (t := [d.temp_c for d in ds if d.temp_c]) else ""),
                  required=False)

    rt = None
    for _ in range(10):  # realtime events arrive every ~2s
        rt = api.realtime()
        if rt:
            break
        await asyncio.sleep(0.5)
    if rt:
        cpu = f"{rt.cpu_percent:.0f}%" if rt.cpu_percent is not None else "?"
        mem = f"{(rt.mem_used_fraction or 0) * 100:.0f}%"
        line("✔", "reporting.realtime", f"CPU {cpu}, memory {mem}")
    else:
        line("!", "reporting.realtime", "no live stats received (CPU/memory will be omitted)")

    rtt = await api.ping()
    line("✔", "latency", f"{rtt:.0f} ms")
    await api.close()
    print("\nAll good." if ok else "\nSome required checks failed (see ✘ above).")
    return 0 if ok else 1


def cmd_check() -> int:
    try:
        cfg = from_env(require_discord=False)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    _setup_logging("ERROR")
    return asyncio.run(_check(cfg))


def cmd_healthcheck() -> int:
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    try:
        interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
    except ValueError:
        interval = 60
    fresh = heartbeat.is_fresh(data_dir / "heartbeat", max(3 * interval, 180))
    print("healthy" if fresh else "stale heartbeat")
    return 0 if fresh else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nasbot", description="Discord bot for TrueNAS")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "check", "healthcheck"])
    parser.add_argument("--version", action="version", version=f"nasbot {__version__}")
    args = parser.parse_args(argv)
    return {"run": cmd_run, "check": cmd_check, "healthcheck": cmd_healthcheck}[args.command]()


if __name__ == "__main__":
    sys.exit(main())
