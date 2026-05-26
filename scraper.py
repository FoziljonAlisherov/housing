import asyncio
import random
import re
from datetime import datetime

import pandas as pd
from playwright.async_api import async_playwright


# =========================================================
# CONFIG
# =========================================================

BASE_URL = "https://www.olx.uz/nedvizhimost/kvartiry/prodazha/?currency=UZS"
MAX_PAGES = 25  # Only first page

OUTPUT_CSV  = "olx_scraped_v2.csv"
OUTPUT_XLSX = "olx_scraped_v2.xlsx"


# =========================================================
# HELPERS
# =========================================================

def clean(text):
    if not text:
        return None
    return re.sub(r"\s+", " ", text).strip()


def extract_number(text):
    """Extract first integer from a string (ignores spaces between digits)."""
    if not text:
        return None
    text = text.replace("\u00a0", "").replace(" ", "")
    m = re.search(r"(\d[\d]*)", text)
    return int(m.group(1)) if m else None


def extract_float(text):
    """Extract first float/int from a string."""
    if not text:
        return None
    text = text.replace("\u00a0", "").replace(" ", "").replace(",", ".")
    m = re.search(r"(\d+\.?\d*)", text)
    return float(m.group(1)) if m else None


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def human_delay(a=2, b=5):
    await asyncio.sleep(random.uniform(a, b))


async def human_scroll(page):
    for _ in range(random.randint(3, 5)):
        await page.mouse.wheel(0, random.randint(800, 2000))
        await asyncio.sleep(random.uniform(0.8, 2.0))


# =========================================================
# GET LISTING LINKS FROM PAGE
# =========================================================

async def get_listing_links(page, url):
    print(f"\nOPEN LIST: {url}")
    await page.goto(url, wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(random.randint(3000, 6000))
    await human_scroll(page)

    hrefs = await page.locator("a").evaluate_all(
        "elements => elements.map(e => e.href)"
    )

    links = set()
    for href in hrefs:
        if href and "/d/obyavlenie/" in href:
            # strip query params for clean URLs
            links.add(href.split("?")[0])

    print(f"FOUND {len(links)} listings")
    return list(links)


# =========================================================
# HELPER: read a structured attribute list (ListContainer)
# =========================================================

async def get_list_container_attrs(page):
    """
    Reads all rows inside [data-nx-name='ListContainer'].
    Returns a dict like:
      { "Общая площадь": "105 м²", "Количество комнат": "3", ... }
    """
    attrs = {}
    try:
        container = page.locator('[data-nx-name="ListContainer"]')
        if await container.count() == 0:
            return attrs

        # Each attribute row is a <li> or a <p> — grab all text pairs
        # OLX renders rows as elements with a label span and a value span/link
        rows = container.locator("li, p")
        count = await rows.count()

        for i in range(count):
            row_text = clean(await rows.nth(i).inner_text())
            if not row_text:
                continue
            # Rows look like "Общая площадь: 105 м²"  or  "Количество комнат 3"
            if ":" in row_text:
                key, _, val = row_text.partition(":")
                attrs[clean(key)] = clean(val)
            else:
                # Try splitting on first whitespace boundary between label and value
                # Some rows: "Этаж 3 из 9"
                parts = row_text.split(None, 1)
                if len(parts) == 2:
                    attrs[clean(parts[0])] = clean(parts[1])

    except Exception as e:
        print(f"  [list_container] {e}")

    return attrs


# =========================================================
# SCRAPE SINGLE AD
# =========================================================

async def scrape_ad(page, url):
    try:
        print(f"\nOPEN: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(random.randint(4000, 8000))
        await human_scroll(page)

        page_title = await page.title()
        print(f"TITLE: {page_title}")

        # Block detection
        if any(x in page_title.lower() for x in ["403", "access denied", "captcha", "just a moment"]):
            print("BLOCKED:", url)
            return None

        # =================================================
        # 1. LISTING ID  (data-nx-name="Label2")
        # =================================================
        listing_id = None
        try:
            label2 = page.locator('[data-nx-name="Label2"]')
            if await label2.count() > 0:
                raw = clean(await label2.first.inner_text())
                # Text is like "ID: 64554517" or just "64554517"
                m = re.search(r"(\d{5,})", raw or "")
                if m:
                    listing_id = m.group(1)
        except Exception:
            pass

        # Fallback: extract from full page text
        if not listing_id:
            try:
                body = clean(await page.text_content("body"))
                m = re.search(r"\bID[:\s#]*(\d{5,})", body or "", re.I)
                if m:
                    listing_id = m.group(1)
            except Exception:
                pass

        # Last resort: alphanumeric ID from URL slug  e.g. ID4mRzT
        if not listing_id:
            m = re.search(r"-(ID[A-Za-z0-9]+)\.html", url)
            if m:
                listing_id = m.group(1)

        # =================================================
        # 2. TITLE  (h1)
        # =================================================
        title = None
        try:
            h1 = page.locator("h1")
            if await h1.count() > 0:
                title = clean(await h1.first.inner_text())
        except Exception:
            pass
        if not title:
            title = clean(page_title.split(":")[0])

        # =================================================
        # 3. PRICE & CURRENCY  (data-testid="ad-price-container")
        # =================================================
        price = None
        currency = None
        try:
            price_loc = page.locator('[data-testid="ad-price-container"]')
            if await price_loc.count() > 0:
                price_text = clean(await price_loc.first.inner_text()) or ""
            else:
                price_text = clean(await page.text_content("body")) or ""

            price = extract_number(price_text)
            low = price_text.lower()
            if "$" in price_text or "у.е" in low or "usd" in low:
                currency = "USD"
            elif "сум" in low or "uzs" in low or "sum" in low:
                currency = "UZS"
        except Exception:
            pass

        # =================================================
        # 4. STRUCTURED ATTRIBUTES via ListContainer
        #    Area, Rooms, Market Type, Stair
        # =================================================
        attrs = await get_list_container_attrs(page)

        # Area  →  "Общая площадь"
        area_raw = (
            attrs.get("Общая площадь")
            or attrs.get("Umumiy maydoni")       # Uzbek key
            or attrs.get("Умумий майдони")
        )
        # Keep clean numeric value with unit, e.g. "105 м²"
        area = clean(area_raw) if area_raw else None

        # Rooms  →  "Количество комнат"
        rooms_raw = (
            attrs.get("Количество комнат")
            or attrs.get("Xonalar soni")
            or attrs.get("Xona soni")
        )
        num_rooms = extract_number(rooms_raw) if rooms_raw else None

        # Market type  →  "Тип жилья"
        market_type_raw = (
            attrs.get("Тип жилья")
            or attrs.get("Uy-joy turi")
        )
        market_type = clean(market_type_raw) if market_type_raw else None

        # Stair / Floor  →  "Этаж"
        stair_raw = (
            attrs.get("Этаж")
            or attrs.get("Qavat")
        )
        stair = clean(stair_raw) if stair_raw else None

        # Fallback via full page text if attrs came back empty
        if not area or not num_rooms or not stair:
            try:
                body_text = clean(await page.text_content("body")) or ""

                if not area:
                    m = re.search(r"Общая площадь[:\s]*([^\n\r]+?)(?=\s*(?:Этаж|Количество|Тип|$))", body_text, re.I)
                    if m:
                        val = clean(m.group(1))
                        # Only keep if it looks like a measurement (digits + м)
                        if re.search(r"\d", val or "") and len(val) < 30:
                            area = val

                if not num_rooms:
                    m = re.search(r"Количество комнат[:\s]*(\d+)", body_text, re.I)
                    if m:
                        num_rooms = int(m.group(1))

                if not market_type:
                    m = re.search(r"Тип жилья[:\s]*([^\n\r]+?)(?=\s*(?:Этаж|Количество|Общая|$))", body_text, re.I)
                    if m:
                        val = clean(m.group(1))
                        if val and len(val) < 60:
                            market_type = val

                if not stair:
                    m = re.search(r"\bЭтаж[:\s]*([^\n\r]+?)(?=\s*(?:Количество|Общая|Тип|$))", body_text, re.I)
                    if m:
                        val = clean(m.group(1))
                        if val and len(val) < 40:
                            stair = val
            except Exception:
                pass

        # =================================================
        # 5. VIEWS  (data-testid="page-view-counter")
        # =================================================
        views = None
        try:
            views_loc = page.locator('[data-testid="page-view-counter"]')
            if await views_loc.count() > 0:
                views = extract_number(await views_loc.first.inner_text())
        except Exception:
            pass

        # =================================================
        # 6. POSTED DATE  (Дата публикации or "Опубликовано")
        # =================================================
        posted_date = None
        try:
            # Try dedicated element first
            date_loc = page.locator('[data-cy="ad-posted-at"], [data-testid="ad-posted-at"]')
            if await date_loc.count() > 0:
                posted_date = clean(await date_loc.first.inner_text())
            else:
                body_text = clean(await page.text_content("body")) or ""
                m = re.search(r"Опубликовано[:\s]*([^\n\r]{3,40})", body_text, re.I)
                if m:
                    posted_date = clean(m.group(1))
                if not posted_date:
                    m = re.search(r"Дата публикации[:\s]*([^\n\r]{3,40})", body_text, re.I)
                    if m:
                        posted_date = clean(m.group(1))
        except Exception:
            pass

        # =================================================
        # 7. NEGOTIATION  (data-nx-name="P4")
        # =================================================
        negotiation = False
        try:
            p4 = page.locator('[data-nx-name="P4"]')
            if await p4.count() > 0:
                p4_text = (clean(await p4.first.inner_text()) or "").lower()
                if "договорная" in p4_text or "negotiable" in p4_text or "kelishiladi" in p4_text:
                    negotiation = True
            # Also check full page as fallback
            if not negotiation:
                body_text = clean(await page.text_content("body")) or ""
                if "договорная" in body_text.lower() or "negotiable" in body_text.lower():
                    negotiation = True
        except Exception:
            pass

        # =================================================
        # 8. SELLER NAME  (data-testid="user-profile-user-name")
        # =================================================
        seller = None
        try:
            seller_loc = page.locator('[data-testid="user-profile-user-name"]')
            if await seller_loc.count() > 0:
                seller = clean(await seller_loc.first.inner_text())
        except Exception:
            pass

        # =================================================
        # 9. LOCATION  Old code
        # =================================================
        location = None

        try:

            texts = await page.locator(
                "p, span"
            ).all_inner_texts()

            for text in texts:

                text = clean(text)

                if not text:
                    continue

                low = text.lower()

                if any(
                    x in low
                    for x in [
                        "район",
                        "ташкент",
                        "toshkent",
                        "область"
                    ]
                ):

                    if len(text) < 120:

                        location = text

                        break

        except:
            pass

        # Fallback to old approach if still empty
        if not location:
            try:
                texts = await page.locator("p, span").all_inner_texts()
                for text in texts:
                    text = clean(text)
                    if not text:
                        continue
                    low = text.lower()
                    if any(x in low for x in ["район", "ташкент", "toshkent", "область"]):
                        if 5 < len(text) < 100:
                            location = text
                            break
            except Exception:
                pass

        # =================================================
        # 10. SELLER JOINED  (data-testid="member-since")
        # =================================================
        seller_joined = None
        try:
            ms_loc = page.locator('[data-testid="member-since"]')
            if await ms_loc.count() > 0:
                seller_joined = clean(await ms_loc.first.inner_text())
        except Exception:
            pass

        # Fallback: look for "на OLX с"
        if not seller_joined:
            try:
                texts = await page.locator("span, p").all_inner_texts()
                for text in texts:
                    text = clean(text)
                    if text and "на olx с" in text.lower():
                        seller_joined = text
                        break
            except Exception:
                pass

        # =================================================
        # 11. DESCRIPTION  (data-testid="ad_description")
        # =================================================
        description = None
        try:
            desc_loc = page.locator('[data-testid="ad_description"]')
            if await desc_loc.count() > 0:
                description = clean(await desc_loc.first.inner_text())
        except Exception:
            pass

        # =================================================
        # 12. DESCRIPTION-BASED FALLBACK EXTRACTION
        #     If structured fields still missing, mine description
        # =================================================
        if description:
            desc_low = description.lower()

            if not area:
                m = re.search(r"(?:общая площадь|площадь)[:\s]*(\d+[\.,]?\d*\s*м²?)", description, re.I)
                if not m:
                    m = re.search(r"(\d+[\.,]?\d*)\s*м²", description, re.I)
                if m:
                    area = clean(m.group(1)) + " м²" if "м²" not in m.group(1) else clean(m.group(1))

            if not num_rooms:
                m = re.search(r"комнат[:\s]*(\d+)", description, re.I)
                if not m:
                    m = re.search(r"(\d+)[\s-]*комнат", description, re.I)
                if m:
                    num_rooms = int(m.group(1))

            if not stair:
                m = re.search(r"этаж[:\s]*([\d\s/\-]+(?:из\s*\d+)?)", description, re.I)
                if m:
                    stair = clean(m.group(1))

            if not market_type:
                if any(x in desc_low for x in ["вторичный", "вторичка", "secondary"]):
                    market_type = "Вторичный рынок"
                elif any(x in desc_low for x in ["новостройка", "первичный", "new build", "yangi"]):
                    market_type = "Новостройка"

        # =================================================
        # RESULT
        # =================================================
        data = {
            "Listing ID":    listing_id,
            "Title":         title,
            "Price":         price,
            "Currency":      currency,
            "Area":          area,
            "Num Rooms":     num_rooms,
            "Market Type":   market_type,
            "Views":         views,
            "Stair":         stair,
            "Posted Date":   posted_date,
            "Scraped Date":  now(),
            "Negotiation":   negotiation,
            "Seller":        seller,
            "Location":      location,
            "Seller Joined": seller_joined,
            "Description":   description,
            "URL":           url,
        }

        print(f"OK: {listing_id} | {title[:60] if title else '?'}")
        return data

    except Exception as e:
        print(f"FAILED: {url}\n  {e}")
        return None


# =========================================================
# MAIN
# =========================================================

async def main():
    all_data = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            slow_mo=100,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            locale="ru-RU",
            timezone_id="Asia/Tashkent",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()

        # Stealth patches
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU','ru','en-US'] });
        """)

        # Warm-up
        print("WARMING SESSION...")
        await page.goto("https://www.olx.uz/", wait_until="domcontentloaded")
        await human_delay(2, 5)
        await human_scroll(page)

        # ── Collect links (page 1 only) ──────────────────
        all_links = await get_listing_links(page, BASE_URL)
        all_links = list(set(all_links))
        print(f"\nTOTAL UNIQUE LINKS: {len(all_links)}")

        # ── Scrape each ad ────────────────────────────────
        for idx, link in enumerate(all_links, start=1):
            print(f"\n[{idx}/{len(all_links)}]")
            data = await scrape_ad(page, link)
            if data:
                all_data.append(data)
            await human_delay(2, 5)

            if idx % 10 == 0:
                print("\nLONG BREAK...\n")
                await human_delay(7, 12)

        await browser.close()

    # ── Save ─────────────────────────────────────────────
    df = pd.DataFrame(all_data)

    # Enforce column order
    col_order = [
        "Listing ID", "Title", "Price", "Currency",
        "Area", "Num Rooms", "Market Type", "Views",
        "Stair", "Posted Date", "Scraped Date", "Negotiation",
        "Seller", "Location", "Seller Joined",
        "Description", "URL",
    ]
    df = df[[c for c in col_order if c in df.columns]]

    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    df.to_excel(OUTPUT_XLSX, index=False)

    print(f"\nDONE — SCRAPED: {len(df)} listings")
    print(f"FILES: {OUTPUT_CSV}  /  {OUTPUT_XLSX}")


if __name__ == "__main__":
    asyncio.run(main())
