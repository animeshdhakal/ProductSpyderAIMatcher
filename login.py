import asyncio
import os

import zendriver as zd

user_data_dir = os.path.join(os.path.dirname(__file__), ".temp")


async def login():
    browser = await zd.start(user_data_dir=user_data_dir)

    page = await browser.get("https://x.com/i/grok/")

    await page.sleep(1000)

    await browser.stop()


if __name__ == "__main__":
    asyncio.run(login())
