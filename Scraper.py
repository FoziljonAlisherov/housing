# [26.05.2026 9:41] Foziljon: `
# """
# OLX Uzbekistan Housing Sales Ads Scraper
# Scrapes all pages of housing sale listings and saves to CSV.
# Usage: python scraper.py
# """

import requests
from bs4 import BeautifulSoup
import pandas as pd
import json
import time
import random
import os
import re
from datetime import date

BASE_URL = "https://www.olx.uz/nedvizhimost/kvartiry/prodazha-kvartir/tashkent/"
DATA_DIR = "data"
CSV_FILE = os.path.join(DATA_DIR, "housing_ads.csv")
MAX_PAGES = 50

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ── helpers ──────────────────────────────────────────────────────────────────

def load_seen_ids() -> set:
    """Return set of ad IDs already in the CSV (avoids duplicates)."""
    if not os.path.exists(CSV_FILE):
        return set()
    try:
        df = pd.read_csv(CSV_FILE, usecols=["id"])
        return set(df["id"].astype(str))
    except Exception:
        return set()


def save_ads(ads: list[dict]) -> None:
    """Append new ads to the CSV, creating it if necessary."""
    if not ads:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    df = pd.DataFrame(ads)
    write_header = not os.path.exists(CSV_FILE)
    df.to_csv(CSV_FILE, mode="a", index=False, header=write_header, encoding="utf-8-sig")
    print(f"  ✓ Saved {len(ads)} new ads → {CSV_FILE}")


# ── parsers ──────────────────────────────────────────────────────────────────

def extract_from_json(soup: BeautifulSoup) -> list[dict]:
    """
    OLX embeds a NEXT_DATA JSON blob in the page — much more reliable
    than scraping raw HTML.
    """
    script = soup.find("script", id="NEXT_DATA")
    if not script:
        return []

    try:
        data = json.loads(script.string)
        ads_raw = (
            data["props"]["pageProps"]["ads"]
            if "ads" in data["props"]["pageProps"]
            else data["props"]["pageProps"].get("listing", {}).get("ads", [])
        )
    except (KeyError, json.JSONDecodeError):
        return []

    results = []
    for ad in ads_raw:
        params = {p["key"]: p.get("value", {}).get("label", "") for p in ad.get("params", [])}
        results.append({
            "id": str(ad.get("id", "")),
            "title": ad.get("title", "").strip(),
            "price": ad.get("price", {}).get("regularPrice", {}).get("value", ""),
            "currency": ad.get("price", {}).get("regularPrice", {}).get("currency", "UZS"),
            "rooms": params.get("rooms", ""),
            "floor": params.get("floor_select", ""),
            "area_m2": params.get("total_area", ""),
            "district": ad.get("location", {}).get("district", {}).get("name", ""),
            "city": ad.get("location", {}).get("city", {}).get("name", "Tashkent"),
            "url": "https://www.olx.uz" + ad.get("url", ""),
            "posted_at": ad.get("createdTime", ""),
            "scraped_date": str(date.today()),
        })
    return results


def extract_from_html(soup: BeautifulSoup) -> list[dict]:
    """
    Fallback HTML parser. Uses CSS selectors that were valid as of 2025-05.
    Update selectors here if OLX changes their markup.
    """
    results = []
    cards = soup.select("div[data-cy='l-card']")

    for card in cards:
        try:
            ad_id = card.get("id", "").replace("ad-", "")
            title_el = card.select_one("h6, h4, [data-testid='ad-title']")
            price_el = card.select_one("[data-testid='ad-price']")
            location_el = card.select_one("[data-testid='location-date']")
            link_el = card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else ""
            price_raw = price_el.get_text(strip=True) if price_el else ""
            price_digits = re.sub(r"[^\d]", "", price_raw)
            location_raw = location_el.get_text(strip=True) if location_el else ""
            location_parts = [p.strip() for p in location_raw.split(",")]

            results.append({
                "id": ad_id,
                "title": title,
                "price": price_digits,
                "currency": "UZS" if "сум" in price_raw.lower() or "sum" in price_raw.lower() else "USD",
                "rooms": "",
                "floor": "",
                "area_m2": "",
                "district": location_parts[0] if location_parts else "",
                "city": "Tashkent",
                "url": "https://www.olx.uz" + link_el["href"] if link_el else "",
                "posted_at": "",
                "scraped_date": str(date.today()),
            })
        except Exception:
            continue

    return results


def scrape_page(page: int, session: requests.Session) -> tuple[list[dict], bool]:
    """
    Fetch one page and return (ads, has_next_page).
    """
    url = BASE_URL if page == 1 else f"{BASE_URL}?page={page}"
    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  ✗ Page {page} request failed: {e}")
        return [], False

    soup = BeautifulSoup(resp.text, "html.parser")

    # Try JSON first, fall back to HTML
    ads = extract_from_json(soup)
    if not ads:
        print(f"  ⚠ JSON parse failed on page {page}, trying HTML…")
        ads = extract_from_html(soup)

    # Detect if there's a next page
    next_btn = soup.select_one("a[data-testid='pagination-forward']")
    has_next = next_btn is not None

    return ads, has_next


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    seen_ids = load_seen_ids()
    print(f"Loaded {len(seen_ids)} previously seen ad IDs.")

    session = requests.Session()
    all_new_ads: list[dict] = []
    early_stop = False

    for page in range(1, MAX_PAGES + 1):
        print(f"Scraping page {page}…")
        ads, has_next = scrape_page(page, session)

        if not ads:
            print("  No ads found — stopping.")
            break

        new_ads = []
        for ad in ads:
            if ad["id"] in seen_ids:
                early_stop = True
            else:
                seen_ids.add(ad["id"])
                new_ads.append(ad)

        all_new_ads.extend(new_ads)
        print(f"  {len(new_ads)} new / {len(ads) - len(new_ads)} already seen")

        if early_stop:
            print("  ↳ Hit previously seen ads — stopping early.")
            break

        if not has_next:
            print("  ↳ No next page — done.")
            break

        # Polite delay
        delay = random.uniform(1.5, 3.0)
        print(f"  Waiting {delay:.1f}s…")
        time.sleep(delay)

    save_ads(all_new_ads)
    print(f"\nDone. Total new ads collected: {len(all_new_ads)}")


if name == "main":
    main()
