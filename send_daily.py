"""Standalone Telegram reminder for GitHub Actions; PTB v21 calls are awaited."""
import asyncio
import logging
import os
from dotenv import load_dotenv
load_dotenv(override=False)
from telegram import Bot
from leetcode_client import LeetCodeClient, LeetCodeError

logging.getLogger("httpx").setLevel(logging.WARNING)


async def run():
    try:
        question = await asyncio.to_thread(LeetCodeClient().daily_problem)
        text = (f"🧠 Daily LeetCode reminder: {question['title']} ({question['difficulty']})\n"
                f"https://leetcode.com/problems/{question['titleSlug']}/\n\n"
                "Any Accepted problem qualifies: Easy 10 / Medium 20 / Hard 50 commits.\nUse /check after solving, or wait for the scheduled sync.")
    except LeetCodeError:
        text = "🧠 Solve a LeetCode problem today. Easy → 10 commits, Medium → 20, Hard → 50. Use /check after an Accepted submission."
    async with Bot(os.environ["BOT_TOKEN"]) as bot:
        await bot.send_message(os.environ["TELEGRAM_CHAT_ID"], text)


if __name__ == "__main__":
    asyncio.run(run())
