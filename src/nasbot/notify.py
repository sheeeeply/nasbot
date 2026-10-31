"""The unit the monitors hand to the bot: one Discord message to post."""

from __future__ import annotations

from dataclasses import dataclass

import discord


@dataclass
class Notification:
    kind: str
    embed: discord.Embed
    summary: str
    urgent: bool = False
