import json
import sqlite3
from contextlib import closing
from typing import List

from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright

DB_PATH = "arbitrage.db"

BWIN_MATCH_LIST_URL = "https://www.bwin.com/de-at/sports/football/matches"
BWIN_EVENT_URL = "https://www.bwin.com/de-at/sports/event/{fixture_id}"

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

app = FastAPI(title="Arbitrage Server")


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            fixture_id TEXT,
            home TEXT,
            away TEXT,
            competition TEXT,
            start_time TEXT
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            fixture_id TEXT,
            market TEXT,
            selection TEXT,
            price REAL
        );
        """)
        conn.commit()


def db_query(query, params=(), one=False):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows


def db_exec(query, params=()):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        conn.commit()


async def bwin_get_fixture_ids(page):
    await page.goto(BWIN_MATCH_LIST_URL, timeout=60000)
    await page.wait_for_timeout(4000)
    links = await page.query_selector_all("a[href*='/sports/event/']")
    fixture_ids = set()
    for link in links:
        href = await link.get_attribute("href")
        if href and "/sports/event/" in href:
            fixture_ids.add(href.split("/sports/event/")[1])
    return list(fixture_ids)


async def bwin_scrape_event(page, fixture_id: str):
    url = BWIN_EVENT_URL.format(fixture_id=fixture_id)
    await page.goto(url, timeout=60000)
    await page.wait_for_timeout(3000)
    scripts = await page.query_selector_all("script[type='application/ld+json']")
    header = {}
    for s in scripts:
        try:
            txt = await s.inner_text()
            data = json.loads(txt)
            if isinstance(data, dict) and data.get("@type") == "SportsEvent":
                header = data
                break
        except:
            continue
    home = header.get("homeTeam", {}).get("name", "")
    away = header.get("awayTeam", {}).get("name", "")
    competition = header.get("name", "")
    start_time = header.get("startDate", "")
    odds = []
    buttons = await page.query_selector_all("button[data-testid*='selection']")
    for b in buttons:
        try:
            label_el = await b.query_selector("span")
            label = (await label_el.inner_text()).strip() if label_el else ""
            price_el = await b.query_selector("span[data-testid*='price']")
            price_txt = (await price_el.inner_text()).strip() if price_el else ""
            price_txt = price_txt.replace(",", ".")
            price = float(price_txt)
            odds.append({
                "market": "main",
                "selection": label,
                "price": price
            })
        except:
            continue
    return {
        "fixture_id": fixture_id,
        "home": home,
        "away": away,
        "competition": competition,
        "start_time": start_time,
        "odds": odds
    }


async def bwin_scrape_all():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(user_agent=HEADERS["User-Agent"])
        fixture_ids = await bwin_get_fixture_ids(page)
        db_exec("DELETE FROM events WHERE source = 'bwin'")
        db_exec("DELETE FROM odds WHERE source = 'bwin'")
        for fid in fixture_ids:
            try:
                data = await bwin_scrape_event(page, fid)
            except Exception as e:
                print("Fehler bei Bwin Fixture", fid, ":", e)
                continue
            db_exec("""
                INSERT INTO events (source, fixture_id, home, away, competition, start_time)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                "bwin",
                data["fixture_id"],
                data["home"],
                data["away"],
                data["competition"],
                data["start_time"]
            ))
            for o in data["odds"]:
                db_exec("""
                    INSERT INTO odds (source, fixture_id, market, selection, price)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    "bwin",
                    data["fixture_id"],
                    o["market"],
                    o["selection"],
                    o["price"]
                ))
        await browser.close()


class AdmiralOdd(BaseModel):
    fixture_id: str
    market: str
    selection: str
    price: float


class AdmiralImport(BaseModel):
    events: List[str]
    odds: List[AdmiralOdd]


def import_admiral(data: AdmiralImport):
    db_exec("DELETE FROM events WHERE source = 'admiral'")
    db_exec("DELETE FROM odds WHERE source = 'admiral'")
    for fid in data.events:
        db_exec("""
            INSERT INTO events (source, fixture_id, home, away, competition, start_time)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("admiral", fid, "", "", "", ""))
    for o in data.odds:
        db_exec("""
            INSERT INTO odds (source, fixture_id, market, selection, price)
            VALUES (?, ?, ?, ?, ?)
        """, (
            "admiral",
            o.fixture_id,
            o.market,
            o.selection,
            o.price
        ))


class ArbitrageItem(BaseModel):
    fixture_id: str
    home: str
    away: str
    selection: str
    price_bwin: float
    price_admiral: float
    inv_sum: float
    profit_percent: float


@app.get("/arbitrage", response_model=List[ArbitrageItem])
def api_get_arbitrage():
    q = """
    SELECT
        e.fixture_id,
        e.home,
        e.away,
        b.selection,
        b.price AS price_bwin,
        a.price AS price_admiral,
        (1.0/b.price + 1.0/a.price) AS inv_sum,
        (1.0 - (1.0/b.price + 1.0/a.price)) * 100.0 AS profit_percent
    FROM odds b
    JOIN odds a
      ON a.fixture_id = b.fixture_id
     AND a.selection = b.selection
    LEFT JOIN events e
      ON e.fixture_id = b.fixture_id
     AND e.source = 'bwin'
    WHERE b.source = 'bwin'
      AND a.source = 'admiral'
      AND (1.0/b.price + 1.0/a.price) < 1.0
    ORDER BY profit_percent DESC
    """
    rows = db_query(q)
    return [
        ArbitrageItem(
            fixture_id=r["fixture_id"],
            home=r["home"] or "",
            away=r["away"] or "",
            selection=r["selection"],
            price_bwin=r["price_bwin"],
            price_admiral=r["price_admiral"],
            inv_sum=r["inv_sum"],
            profit_percent=r["profit_percent"],
        )
        for r in rows
    ]


@app.on_event("startup")
def _startup():
    init_db()


@app.post("/scrape/bwin")
async def api_scrape_bwin():
    await bwin_scrape_all()
    return {"status": "ok", "message": "Bwin scraped"}


@app.post("/import/admiral")
def api_import_admiral(payload: AdmiralImport = Body(...)):
    import_admiral(payload)
    return {"status": "ok", "message": "Admiral imported"}


@app.get("/events")
def api_get_events():
    rows = db_query("SELECT * FROM events ORDER BY start_time")
    return JSONResponse([dict(r) for r in rows])


@app.get("/odds")
def api_get_odds(source: str = "bwin"):
    rows = db_query("SELECT * FROM odds WHERE source = ? ORDER BY fixture_id", (source,))
    return JSONResponse([dict(r) for r in rows])
