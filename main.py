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
        logging.info(response.json())


async def process_competitor(browser: zd.Browser, competitor_id, url, client_url):
    cursor = db.cursor()

    logging.info("Processing competitor_id: %s", competitor_id)

    try:
        page = await browser.get("https://x.com/i/grok/")

        # Wait longer for the page to fully load
        await page.sleep(5)

        # Try multiple selectors for the textarea
        try:
            textarea = await page.wait_for("textarea[placeholder='Ask anything']", timeout=30)
        except Exception as e:
            logging.info("Could not find textarea with initial selector, trying alternatives")
            try:
                # Try a different selector or wait for page content to be fully loaded
                await page.wait_for("body", timeout=10)  # Wait for body to ensure page is loaded
                textarea = await page.find("textarea", best_match=True, timeout=20)
                if not textarea:
                    # Try a more generic approach
                    all_textareas = await page.find_all("textarea")
                    if all_textareas and len(all_textareas) > 0:
                        textarea = all_textareas[0]
                    else:
                        raise Exception("No textarea found on the page")
            except Exception as inner_e:
                logging.error(f"Failed to find textarea: {str(inner_e)}")
                await page.reload()
                await page.sleep(10)
                textarea = await page.wait_for("textarea[placeholder='Ask anything']", timeout=30)

        await random_delay(page)

        # Try to find the DeepSearch button with a more robust approach
        try:
            deep_search_button = await page.find("DeepSearch", best_match=True, timeout=20)
            await deep_search_button.mouse_click()
        except Exception as e:
            logging.error(f"Could not find DeepSearch button: {str(e)}")
            # Try alternative ways to find the button
            buttons = await page.find_all("button")
            for button in buttons:
                text = await button.text()
                if "search" in text.lower() or "deep" in text.lower():
                    await button.mouse_click()
                    break

        prompt = f"Determine if this page {url} is a Category page containing multiple products or an individual Product page. If it is a Category page and then no need to proceed further, terminate your processing and report ANSWER as NO If it is a Product page then compare with this product page {client_url} to determine if they are identical for price-matching. Use product identifiers (SKU, MPN, UPC, GTIN), photos, and descriptions for verification, noting that retailers and manufacturers may interchange these identifiers. If it is a match then report ANSWER as YES otherwise report ANSWER as NO\n"

        await random_delay(page)

        try:
            await textarea.send_keys(prompt)
        except Exception as e:
            logging.error(f"Failed to send keys to textarea: {str(e)}")
            # Try to refocus and retry
            await page.sleep(5)
            textarea = await page.wait_for("textarea", timeout=20)
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

        # Wait longer to ensure Grok processes the request
        await page.sleep(2.5 * 60)

        await page.reload()

        await random_delay(page)

        # Try to find textarea after reload with more robust approach
        try:
            textarea = await page.wait_for("textarea[placeholder='Ask anything']", timeout=30)
        except Exception as e:
            logging.error(f"Failed to find textarea after reload: {str(e)}")
            textareas = await page.find_all("textarea")
            if textareas and len(textareas) > 0:
                textarea = textareas[0]
            else:
                raise Exception("No textarea found after reload")

        try:
            await textarea.send_keys(
                "On the basis of above analysis, ANSWER only YES or NO. No explanation is needed. Wrap your ANSWER in curly braces.\n"
            )
        except Exception as e:
            logging.error(f"Failed to send follow-up message: {str(e)}")
            await page.reload()
            await page.sleep(10)
            textarea = await page.wait_for("textarea", timeout=20)
            await textarea.send_keys(
                "On the basis of above analysis, ANSWER only YES or NO. No explanation is needed. Wrap your ANSWER in curly braces.\n"
            )

        await page.sleep(10)

        tracking = "NONE"

        try:
            no_match = await page.find("{NO}", best_match=True, timeout=5)
            if no_match:
                tracking = "IGNORE"
        except Exception:
            pass

        try:
            yes_match = await page.find("{YES}", best_match=True, timeout=5)
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
    except Exception as e:
        logging.error(f"Error in process_competitor: {str(e)}")
        logging.error(traceback.format_exc())
        # Don't update tracking status as we couldn't process this competitor


async def restart_browser():
    """Helper function to safely restart the browser"""
    logging.info("Attempting to restart browser")
    try:
        browser = await zd.start(user_data_dir=user_data_dir)
        logging.info("Browser started successfully")
        return browser
    except Exception as e:
        logging.error(f"Failed to restart browser: {str(e)}")
        logging.error(traceback.format_exc())
        return None


async def run_scraper():
    logging.info("Starting AI Queue")

    cur = db.cursor()
    cur.execute("SELECT * FROM ai_queue WHERE tracking = 'PROCESSING'")
    rows = cur.fetchall()

    if not rows:
        logging.info("No items to process in AI Queue")
        return

    logging.info("Found %s rows in AI Queue", len(rows))

    browser = None
    try:
        browser = await zd.start(user_data_dir=user_data_dir)
    except Exception as e:
        logging.error(f"Failed to start browser: {str(e)}")
        logging.error(traceback.format_exc())
        # Try one more time after waiting
        await asyncio.sleep(30)
        browser = await restart_browser()

    if not browser:
        logging.error("Could not start browser after retries. Aborting run.")
        return

    try:
        for row in rows:
            competitor_id = row[0]
            url = row[1]
            client_url = row[2]

            try:
                await process_competitor(browser, competitor_id, url, client_url)
            except Exception as e:
                logging.error(f"Error processing competitor_id: {competitor_id}, {str(e)}")
                logging.error(traceback.format_exc())

                # Check if browser needs to be restarted
                if "connection" in str(e).lower() or "protocol" in str(e).lower():
                    try:
                        await browser.stop()
                    except:
                        pass

                    # Wait before restarting
                    await asyncio.sleep(10)
                    browser = await restart_browser()

                    if not browser:
                        logging.error("Could not restart browser. Skipping remaining competitors.")
                        break
    finally:
        # Ensure browser is properly closed even if exceptions occur
        if browser:
            try:
                await browser.stop()
                logging.info("Browser closed successfully")
            except Exception as e:
                logging.error(f"Error closing browser: {str(e)}")
                pass


async def main():
    logging.getLogger("nodriver").setLevel(logging.WARNING)
    logging.basicConfig(level=logging.INFO, filename="ai_queue.log", filemode="a")

    while True:
        try:
            sync_to_local_db()
            await run_scraper()
            sync_to_server()
        except Exception as e:
            logging.error(f"Error in main loop: {str(e)}")
            logging.error(traceback.format_exc())
            # Still try to sync to server to avoid losing progress
            try:
                sync_to_server()
            except Exception as sync_error:
                logging.error(f"Failed to sync to server after error: {str(sync_error)}")

            # Wait a bit before retrying to avoid rapid failure loops
            await asyncio.sleep(60)

            # Try to restart the browser if it's causing problems
            try:
                browser = await zd.start(user_data_dir=user_data_dir)
                await browser.stop()
                logging.info("Successfully restarted browser")
            except Exception as browser_error:
                logging.error(f"Failed to restart browser: {str(browser_error)}")

        logging.info("Sleeping for 10 minutes before next run")
        await asyncio.sleep(10 * 60)


if __name__ == "__main__":
    asyncio.run(main())
