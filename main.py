import asyncio
import logging
import os
import random
import sqlite3
import traceback

import zendriver as zd
from curl_cffi import requests
from dotenv import load_dotenv

load_dotenv()


token = os.getenv("SYNC_TOKEN")
sync_url = f"https://app.pricespy.ai/api/aimatch/sync?token={token}"


async def random_delay(page):
    await page.sleep(random.randint(1, 5))


db = sqlite3.connect("ai_queue.db")
user_data_dir = os.path.join(os.path.dirname(__file__), ".temp")


def sync_to_local_db():
    cursor = db.cursor()

    cursor.execute(
        "CREATE TABLE IF NOT EXISTS ai_queue (id INTEGER PRIMARY KEY, url TEXT, client_url TEXT, tracking TEXT)"
    )

    response = requests.get(sync_url)

    for competitor_page in response.json():
        competitor_id = competitor_page["id"]
        url = competitor_page["url"]
        client_url = competitor_page["client_url"]

        cursor.execute("SELECT * FROM ai_queue WHERE id = ?", (competitor_id,))

        if cursor.fetchone() is None:
            logging.info("Adding %s to the queue", competitor_id)
            cursor.execute(
                "INSERT INTO ai_queue (id, url, client_url, tracking) VALUES (?, ?, ?, ?)",
                (competitor_id, url, client_url, "PROCESSING"),
            )

    db.commit()


def sync_to_server():
    cursor = db.cursor()

    cursor.execute("SELECT * FROM ai_queue WHERE tracking != 'PROCESSING'")
    rows = cursor.fetchall()

    payload = []

    for row in rows:
        competitor_id = row[0]
        tracking = row[3]

        payload.append({"id": competitor_id, "tracking": tracking})

    response = requests.post(sync_url, json=payload)

    if response.status_code == 200:
        logging.info("Synced to server successfully")
    else:
        logging.info("Failed to sync to server")


async def process_competitor(browser: zd.Browser, competitor_id, url, client_url):
    cursor = db.cursor()

    logging.info("Processing competitor_id: %s", competitor_id)

    page = await browser.get("https://x.com/i/grok/")

    textarea = await page.wait_for("textarea[placeholder='Ask anything']", timeout=20)

    await random_delay(page)

    deep_search_button = await page.find("DeepSearch", best_match=True, timeout=20)
    await deep_search_button.mouse_click()

    prompt = f"Determine if this page {url} is a Category page containing multiple products or an individual Product page. If it is a Category page and then no need to proceed further, terminate your processing and report ANSWER as NO If it is a Product page then compare with this product page {client_url} to determine if they are identical for price-matching. Use product identifiers (SKU, MPN, UPC, GTIN), photos, and descriptions for verification, noting that retailers and manufacturers may interchange these identifiers. If it is a match then report ANSWER as YES otherwise report ANSWER as NO\n"

    await random_delay(page)

    await textarea.send_keys(prompt)

    try:
        upgrade_popup = await page.find(
            "You've reached your limit of 30 Grok DeepSearch questions per 2 hours for now. Please check back later to continue.",
            best_match=True,
            timeout=3,
        )

        if upgrade_popup:
            logging.info("DeepSearch limit reached. Stopping browser.")
            await browser.stop()
            sync_to_server()
            await asyncio.sleep(2 * 60 * 60)
            browser = await zd.start(user_data_dir=user_data_dir)
            return
    except Exception:
        pass

    await page.sleep(2.1 * 60)

    await page.reload()

    await random_delay(page)

    textarea = await page.wait_for("textarea[placeholder='Ask anything']", timeout=20)
    await textarea.send_keys(
        "On the basis of above analysis, ANSWER only YES or NO. No explanation is needed. Wrap your ANSWER in curly braces.\n"
    )

    await page.sleep(5)

    tracking = "NONE"

    try:
        no_match = await page.find("{NO}", best_match=True, timeout=2)
        if no_match:
            tracking = "IGNORE"
    except Exception:
        pass

    try:
        yes_match = await page.find("{YES}", best_match=True, timeout=2)
        if yes_match:
            tracking = "VERIFIED"
    except Exception:
        pass

    if tracking == "NONE":
        logging.info("No response found for competitor_id: %s", competitor_id)
        return

    logging.info("Tracking: %s", tracking)

    cursor.execute(
        "UPDATE ai_queue SET tracking = ?  WHERE id = ?",
        (tracking, competitor_id),
    )
    db.commit()
    logging.info("Updated competitor_id: %s", competitor_id)


async def run_scraper():
    logging.info("Starting AI Queue")

    cur = db.cursor()

    cur.execute("SELECT * FROM ai_queue WHERE tracking = 'PROCESSING'")

    rows = cur.fetchall()

    browser = await zd.start(user_data_dir=user_data_dir)

    logging.info("Found %s rows in AI Queue", len(rows))

    for row in rows:
        competitor_id = row[0]
        url = row[1]
        client_url = row[2]

        try:
            await process_competitor(browser, competitor_id, url, client_url)
        except Exception:
            logging.error("Error processing competitor_id: %s", competitor_id)
            logging.error(traceback.format_exc())

    await browser.stop()


async def main():
    logging.getLogger("nodriver").setLevel(logging.WARNING)
    logging.basicConfig(level=logging.INFO, filename="ai_queue.log", filemode="a")

    while True:
        sync_to_local_db()
        await run_scraper()
        sync_to_server()
        logging.info("Sleeping for 1 hour")
        await asyncio.sleep(10 * 60)


if __name__ == "__main__":
    asyncio.run(main())
