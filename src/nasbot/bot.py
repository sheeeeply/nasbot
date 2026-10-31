"""Discord side: slash commands and the posting loop.

The command logic lives in ``Handlers`` (plain async methods returning embeds),
so it is tested without a Discord connection. ``NASBot`` is thin glue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from datetime import UTC, datetime

import aiohttp
import discord
from discord import app_commands

from . import collectors, formatting, heartbeat
from .arr import ArrAuthError, ArrCallError, ArrUnavailable
from .config import Config
from .monitor import Monitor, Notification
from .truenas import NASCallError, NASUnavailable, TrueNAS

log = logging.getLogger(__name__)

PERMISSIONS = discord.Permissions(view_channel=True, send_messages=True, embed_links=True,
                                  read_message_history=True)


def error_embed(exc: Exception, monitor: Monitor | None = None) -> discord.Embed:
    if isinstance(exc, ArrAuthError):
        return discord.Embed(title="🔑 API key rejected", description=formatting.truncate(str(exc), 1500)
                             + "\n\nCopy the key again from the app's **Settings → General → Security** into "
                               "nasbot's `.env`, then restart the bot.", color=formatting.COLOR_WARN)
    if isinstance(exc, ArrUnavailable | ArrCallError):
        return discord.Embed(title="🔴 Can't reach Sonarr/Radarr", description=formatting.truncate(str(exc), 1500),
                             color=formatting.COLOR_CRIT)
    if isinstance(exc, NASUnavailable):
        embed = discord.Embed(title="🔴 Can't reach the NAS", description=formatting.truncate(str(exc), 1500),
                              color=formatting.COLOR_CRIT)
        if monitor and monitor.last_poll_at:
            state = "succeeded" if monitor.last_poll_ok else "failed"
            embed.set_footer(text=f"Last background poll {state}")
        return embed
    if isinstance(exc, NASCallError):
        return discord.Embed(title="⚠️ TrueNAS returned an error",
                             description=formatting.truncate(str(exc), 1500)
                             + "\n\nIf this says *not authorized*, give the bot's TrueNAS user the "
                               "**Readonly Admin** role.",
                             color=formatting.COLOR_WARN)
    return discord.Embed(title="⚠️ Something went wrong", description=formatting.truncate(repr(exc), 1500),
                         color=formatting.COLOR_WARN)


class Handlers:
    def __init__(self, api: TrueNAS, monitor: Monitor, cfg: Config):
        self.api = api
        self.monitor = monitor
        self.cfg = cfg

    @property
    def media(self):
        return self.monitor.media

    # ---- access control

    def authorized(self, user_id: int, role_ids: Iterable[int], guild_id: int | None) -> bool:
        if guild_id is None:
            return False  # no DMs: a channel is the audit trail
        if self.cfg.guild_id and guild_id != self.cfg.guild_id:
            return False
        if self.cfg.access_is_open:
            return True
        return user_id in self.cfg.allowed_user_ids or bool(self.cfg.allowed_role_ids.intersection(role_ids))

    # ---- commands

    async def status(self) -> discord.Embed:
        return formatting.status_embed(await collectors.snapshot(self.api))

    async def pools(self) -> discord.Embed:
        return formatting.pools_embed(await collectors.snapshot(self.api))

    async def disks(self) -> discord.Embed:
        info = await collectors.system_info(self.api)
        return formatting.disks_embed(await collectors.disks(self.api), info.hostname, datetime.now(UTC))

    async def alerts(self, include_dismissed: bool = False) -> discord.Embed:
        info = await collectors.system_info(self.api)
        return formatting.alerts_embed(await collectors.alerts(self.api), info.hostname,
                                       include_dismissed=include_dismissed)

    async def apps(self) -> discord.Embed:
        info = await collectors.system_info(self.api)
        return formatting.apps_embed(await collectors.apps(self.api), info.hostname)

    async def ping(self, discord_latency_s: float) -> discord.Embed:
        lines = [f"Discord gateway: **{discord_latency_s * 1000:.0f} ms**"]
        color = formatting.COLOR_OK
        try:
            rtt = await self.api.ping()
            lines.append(f"TrueNAS API: **{rtt:.0f} ms**")
        except (NASUnavailable, NASCallError) as exc:
            lines.append(f"TrueNAS API: 🔴 {formatting.truncate(str(exc), 300)}")
            color = formatting.COLOR_CRIT
        m = self.monitor
        if m.last_poll_at:
            lines.append(f"Last poll: {formatting.ts(m.last_poll_at)} — {'ok' if m.last_poll_ok else 'failed'}")
        if (until := m.silenced_until) is not None:
            lines.append(f"🔕 Muted until {formatting.ts(until, 't')} ({formatting.ts(until)})")
        return discord.Embed(title="🏓 Pong", description="\n".join(lines), color=color)

    async def media_queue(self) -> discord.Embed:
        if self.media is None:
            return _media_off_embed()
        return formatting.media_queue_embed(await self.media.overview())

    async def media_health(self) -> discord.Embed:
        if self.media is None:
            return _media_off_embed()
        return formatting.media_health_embed(await self.media.overview())

    def mute(self, minutes: int) -> tuple[discord.Embed, list[Notification]]:
        notes = self.monitor.set_silence(minutes)
        if minutes <= 0:
            return discord.Embed(title="🔔 Notifications unmuted", color=formatting.COLOR_INFO), notes
        until = self.monitor.silenced_until
        embed = discord.Embed(
            title="🔕 Notifications muted",
            description=f"Until {formatting.ts(until, 't')} ({formatting.ts(until)}). "
                        "You'll get a summary of anything held back. `/nas mute 0` to unmute early.",
            color=formatting.COLOR_INFO,
        )
        return embed, notes


def _media_off_embed() -> discord.Embed:
    return discord.Embed(title="Media watching is off",
                         description="Set `SONARR_URL` / `SONARR_API_KEY` (and/or the Radarr pair) in nasbot's "
                                     "`.env` and restart the bot.", color=formatting.COLOR_MUTED)


class GuardedGroup(app_commands.Group):
    """A slash-command group with the bot's access check and defer → follow-up handling."""

    def __init__(self, handlers: Handlers, bot: NASBot, *, name: str, description: str):
        super().__init__(name=name, description=description, guild_only=True)
        self.h = handlers
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        roles = [r.id for r in getattr(interaction.user, "roles", [])]
        if self.h.authorized(interaction.user.id, roles, interaction.guild_id):
            return True
        await interaction.response.send_message("You're not allowed to use this bot.", ephemeral=True)
        log.warning("Denied /%s for user %s (%s)", interaction.command.qualified_name if interaction.command
                    else "?", interaction.user, interaction.user.id)
        return False

    async def _run(self, interaction: discord.Interaction, coro_factory) -> None:
        ephemeral = self.h.cfg.ephemeral_replies
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        try:
            embed = await coro_factory()
        except (NASUnavailable, NASCallError, ArrUnavailable, ArrCallError) as exc:
            embed = error_embed(exc, self.h.monitor)
        except Exception as exc:  # noqa: BLE001 - never leave the user with "thinking…" forever
            log.exception("Command failed")
            embed = error_embed(exc)
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)


class NASGroup(GuardedGroup):
    def __init__(self, handlers: Handlers, bot: NASBot):
        super().__init__(handlers, bot, name="nas", description="TrueNAS status and alerts")

    @app_commands.command(name="status", description="Overall health: CPU, memory, pools, alerts, apps")
    async def status(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, self.h.status)

    @app_commands.command(name="pools", description="Pool health, capacity and scrub status")
    async def pools(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, self.h.pools)

    @app_commands.command(name="disks", description="Disks with temperatures")
    async def disks(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, self.h.disks)

    @app_commands.command(name="alerts", description="Current TrueNAS alerts")
    @app_commands.describe(include_dismissed="Also show alerts that were dismissed in the TrueNAS UI")
    async def alerts(self, interaction: discord.Interaction, include_dismissed: bool = False) -> None:
        await self._run(interaction, lambda: self.h.alerts(include_dismissed))

    @app_commands.command(name="apps", description="Installed apps and their state")
    async def apps(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, self.h.apps)

    @app_commands.command(name="ping", description="Check the bot and its connection to the NAS")
    async def ping(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, lambda: self.h.ping(self.bot.latency))

    @app_commands.command(name="mute", description="Mute alert notifications for a while (0 = unmute)")
    @app_commands.describe(minutes="How long to mute, in minutes (0 unmutes; max one week)")
    async def mute(self, interaction: discord.Interaction, minutes: app_commands.Range[int, 0, 10080]) -> None:
        embed, notes = self.h.mute(minutes)
        await interaction.response.send_message(embed=embed, ephemeral=self.h.cfg.ephemeral_replies)
        log.info("%s set mute=%d min", interaction.user, minutes)
        for n in notes:
            await self.bot.post(n)


class MediaGroup(GuardedGroup):
    def __init__(self, handlers: Handlers, bot: NASBot):
        super().__init__(handlers, bot, name="media", description="Sonarr / Radarr downloads")

    @app_commands.command(name="queue", description="What's downloading, and anything that needs you")
    async def queue(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, self.h.media_queue)

    @app_commands.command(name="health", description="Sonarr / Radarr health checks")
    async def health(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, self.h.media_health)


class NASBot(discord.Client):
    def __init__(self, cfg: Config, api: TrueNAS, monitor: Monitor):
        intents = discord.Intents.none()
        intents.guilds = True  # enough for slash commands and role checks; no privileged intents
        super().__init__(intents=intents)
        self.cfg = cfg
        self.api = api
        self.monitor = monitor
        self.handlers = Handlers(api, monitor, cfg)
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(NASGroup(self.handlers, self))
        if monitor.media is not None:
            self.tree.add_command(MediaGroup(self.handlers, self))
        self._monitor_task: asyncio.Task | None = None
        self._http: aiohttp.ClientSession | None = None
        self._announced = False

    async def setup_hook(self) -> None:
        if self.cfg.guild_id:
            guild = discord.Object(id=self.cfg.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d command group(s) to guild %s", len(synced), self.cfg.guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global command group(s) (can take up to an hour to appear; set "
                     "DISCORD_GUILD_ID for instant updates)", len(synced))
        self._http = aiohttp.ClientSession()
        self._monitor_task = asyncio.create_task(self._monitor_loop(), name="nas-monitor")

    async def on_ready(self) -> None:
        assert self.user is not None
        invite = discord.utils.oauth_url(self.user.id, permissions=PERMISSIONS,
                                         scopes=("bot", "applications.commands"))
        log.info("Logged in as %s (%s). Invite URL: %s", self.user, self.user.id, invite)
        if self.cfg.access_is_open:
            log.warning("No DISCORD_ALLOWED_USER_IDS / DISCORD_ALLOWED_ROLE_IDS set: anyone in the server can "
                        "use /nas commands.")

    async def close(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task
        if self._http:
            await self._http.close()
        if self.monitor.media is not None:
            await self.monitor.media.close()
        await self.api.close()
        await super().close()

    # ---- posting

    async def _alert_channel(self) -> discord.abc.Messageable:
        channel = self.get_channel(self.cfg.alert_channel_id)
        if channel is None:
            channel = await self.fetch_channel(self.cfg.alert_channel_id)
        return channel  # type: ignore[return-value]

    async def post(self, n: Notification) -> None:
        mention = self.cfg.mention if n.urgent else None
        allowed = discord.AllowedMentions.none()
        if mention:
            allowed = discord.AllowedMentions(
                roles=[discord.Object(self.cfg.mention_role_id)] if self.cfg.mention_role_id else False,
                users=[discord.Object(self.cfg.mention_user_id)] if self.cfg.mention_user_id else False,
                everyone=False,
            )
        try:
            channel = await self._alert_channel()
            await channel.send(content=mention, embed=n.embed, allowed_mentions=allowed)
        except discord.Forbidden:
            log.error("Missing permission to post in channel %s — the bot needs View Channel, Send Messages "
                      "and Embed Links there. Lost notification: %s", self.cfg.alert_channel_id, n.summary)
        except discord.NotFound:
            log.error("Alert channel %s not found — check DISCORD_ALERT_CHANNEL_ID. Lost notification: %s",
                      self.cfg.alert_channel_id, n.summary)
        except discord.HTTPException as exc:
            log.error("Failed to post notification %r: %s", n.summary, exc)

    # ---- background loop

    async def _monitor_loop(self) -> None:
        await self.wait_until_ready()
        log.info("Monitoring %s every %ss", self.cfg.truenas_url, self.cfg.poll_interval)
        while not self.is_closed():
            try:
                notes = await self.monitor.poll()
                if not self._announced:
                    self._announced = True
                    if self.cfg.announce_startup:
                        await self.post(self._startup_notice())
                for n in notes:
                    await self.post(n)
                heartbeat.touch(self.cfg.heartbeat_path)
                if self.cfg.healthcheck_url and self._http:
                    await heartbeat.ping(self._http, self.cfg.healthcheck_url, bool(self.monitor.last_poll_ok))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("Monitor iteration crashed; continuing")
            await asyncio.sleep(self.cfg.poll_interval)

    def _startup_notice(self) -> Notification:
        m = self.monitor
        if m.last_poll_ok:
            n_alerts = len(m.state.known_alerts or {})
            lvl = self.cfg.min_alert_level.lower()
            desc = (f"Watching **{m.hostname}** every {self.cfg.poll_interval}s.\n"
                    f"{n_alerts} active alert(s) at {lvl} or above — use `/nas alerts` to see them.\n"
                    "Commands: `/nas status` `/nas pools` `/nas disks` `/nas alerts` `/nas apps` `/nas ping` "
                    "`/nas mute`")
            color = formatting.COLOR_OK if n_alerts == 0 else formatting.COLOR_WARN
            if m.media is not None:
                desc += (f"\nAlso watching **{' & '.join(m.media.names)}** every "
                         f"{self.cfg.media_poll_interval // 60} min — `/media queue` `/media health`")
            if self.cfg.mention:
                desc += f"\nUrgent ({self.cfg.mention_min_level.lower()}+) notifications ping {self.cfg.mention}."
        else:
            err = formatting.truncate(self.api.last_error or "?", 800)
            desc = (f"Started, but can't reach TrueNAS yet:\n```{err}```"
                    f"I'll alert here if it stays down for {self.cfg.unreachable_after} polls.")
            color = formatting.COLOR_WARN
        return Notification("startup", discord.Embed(title="🤖 NAS bot online", description=desc, color=color),
                            "bot online")
