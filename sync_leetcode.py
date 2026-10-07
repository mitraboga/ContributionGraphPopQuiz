"""Standalone scheduled sync (also works without the Render service running)."""
import asyncio
import logging
import os
from dotenv import load_dotenv

load_dotenv(override=False)
from rewards import RewardService
from telegram import Bot

logging.getLogger("httpx").setLevel(logging.WARNING)


async def run():
    result = await asyncio.to_thread(RewardService().sync)
    print(result.message())
    if os.getenv("BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID") and (result.errors or any(n for _, n in result.completed)):
        async with Bot(os.environ["BOT_TOKEN"]) as bot:
            await bot.send_message(os.environ["TELEGRAM_CHAT_ID"], result.message())
    if result.errors:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(run())
