import asyncio
import os

from core.bot_instance import bot
from web.server import start_webhook_server

# Import modules to register commands, events, and views on the bot
import commands.tournament  # noqa: F401
import commands.match       # noqa: F401
import commands.testing     # noqa: F401
import events.lifecycle     # noqa: F401
import events.messages      # noqa: F401
import events.reactions     # noqa: F401


async def main():
    TOKEN = os.environ.get("DISCORD_TOKEN")
    async with bot:
        await start_webhook_server()
        await bot.start(TOKEN)


asyncio.run(main())
