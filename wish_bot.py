# ⚡ WISH — IMVU wishlist giveaway bot
# Env: DISCORD_TOKEN (required), GIVEAWAY_CHANNEL_ID (optional), TIMEZONE, DRAW_HOUR_LOCAL, WIN_COOLDOWN_DAYS
# Run: python wish_bot.py

import os, re, json, sqlite3, asyncio, random, urllib.parse, html
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List, Dict

import aiohttp
import discord
from discord import ui, app_commands
from discord.ext import commands, tasks

# =========================
# Config / ENV 
# =========================
TOKEN = os.getenv("DISCORD_TOKEN") or ""
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN")
GIVEAWAY_CHANNEL_ID = int(os.getenv("GIVEAWAY_CHANNEL_ID", "0"))
DRAW_HOUR_LOCAL = int(os.getenv("DRAW_HOUR_LOCAL", "18"))
WIN_COOLDOWN_DAYS = int(os.getenv("WIN_COOLDOWN_DAYS", "7"))
DB_PATH = os.getenv("DB_PATH", "wish.db")
PRODUCT_SAMPLE_LIMIT = int(os.getenv("PRODUCT_SAMPLE_LIMIT", "60"))
PRODUCT_CONCURRENCY = int(os.getenv("PRODUCT_CONCURRENCY", "4"))
PRODUCT_CACHE_TTL_HOURS = int(os.getenv("PRODUCT_CACHE_TTL_HOURS", "168"))
ONE_WIN_ONLY = os.getenv("ONE_WIN_ONLY", "1") == "1"  # 1 = lifetime one win; set to 0 to disable
STRICT_SHOP_MATCH = os.getenv("STRICT_SHOP_MATCH", "1") == "1"  # 1 = no fallback when shops exist

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Qatar"))
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=3))  # fallback

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree

# =========================
# Database
# =========================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS Participants(
          discord_id TEXT PRIMARY KEY,
          username   TEXT NOT NULL,
          created_at TEXT NOT NULL,
          last_checked_at TEXT,
          total_items INTEGER DEFAULT 0,
          eligible   INTEGER DEFAULT 0,
          last_win_at TEXT
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS creators(
          creator_id TEXT PRIMARY KEY,
          label      TEXT
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS rules(
          key TEXT PRIMARY KEY,
          value TEXT
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS cache_products(
          product_id TEXT PRIMARY KEY,
          creator_id TEXT,
          fetched_at TEXT
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS giveaways(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          channel_id TEXT NOT NULL,
          message_id TEXT,
          prize TEXT NOT NULL,
          description TEXT,
          winners INTEGER NOT NULL,
          end_at TEXT NOT NULL,
          created_by TEXT NOT NULL,
          status TEXT DEFAULT 'OPEN'
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS giveaway_winners(
          giveaway_id INTEGER NOT NULL,
          discord_id  TEXT NOT NULL,
          created_at  TEXT NOT NULL,
          PRIMARY KEY (giveaway_id, discord_id)
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS giveaway_entries(
          giveaway_id INTEGER NOT NULL,
          discord_id  TEXT NOT NULL,
          imvu_username TEXT NOT NULL,
          wishlist_product_id TEXT,
          created_at TEXT NOT NULL,
          PRIMARY KEY (giveaway_id, discord_id)
        );""")
        for k, v in [("mode","NONE"), ("threshold","10"), ("min_total","10"), ("map_json","{}")]:
            conn.execute("INSERT OR IGNORE INTO rules(key,value) VALUES(?,?)", (k, v))

DB_INITIALISED = False

def ensure_db():
    """Idempotent DB bootstrap so interactions never hit 'no such table'."""
    global DB_INITIALISED
    if DB_INITIALISED:
        return
    init_db()
    purge_bad_cache_rows()
    DB_INITIALISED = True


def set_rule(key: str, value: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO rules(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value;", (key, value)
        )

def get_rules() -> Dict[str,str]:
    with db() as conn:
        cur = conn.execute("SELECT key,value FROM rules;")
        return {k:v for k,v in cur.fetchall()}

def add_creator(creator_id: str, label: Optional[str] = None):
    with db() as conn:
        conn.execute(
            "INSERT INTO creators(creator_id,label) VALUES(?,?) "
            "ON CONFLICT(creator_id) DO UPDATE SET label=excluded.label;",
            (creator_id, label)
        )

def list_creators() -> List[tuple]:
    with db() as conn:
        cur = conn.execute("SELECT creator_id,label FROM creators ORDER BY creator_id;")
        return cur.fetchall()

def upsert_entrant(discord_id: int, username: str, total_items: int, eligible: int):
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute("""
        INSERT INTO Participants(discord_id, username, created_at, last_checked_at, total_items, eligible)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(discord_id) DO UPDATE SET
          username=excluded.username, last_checked_at=excluded.last_checked_at,
          total_items=excluded.total_items, eligible=excluded.eligible
        """, (str(discord_id), username, now, now, total_items, eligible))

def all_Participants():
    with db() as conn:
        cur = conn.execute("SELECT discord_id, username, total_items, eligible, last_win_at FROM Participants;")
        return cur.fetchall()

def set_winner(discord_id: int):
    with db() as conn:
        conn.execute("UPDATE Participants SET last_win_at=? WHERE discord_id=?",
                     (datetime.now(timezone.utc).isoformat(), str(discord_id)))

def giveaway_insert(channel_id: int, prize: str, desc: str, winners: int, end_at_iso: str, created_by: int) -> int:
    with db() as conn:
        cur = conn.execute("""
          INSERT INTO giveaways(channel_id, message_id, prize, description, winners, end_at, created_by, status)
          VALUES(?,?,?,?,?,?,?, 'OPEN');
        """, (str(channel_id), "", prize, desc, winners, end_at_iso, str(created_by)))
        return cur.lastrowid

def giveaway_set_message(gid: int, message_id: int):
    with db() as conn:
        conn.execute("UPDATE giveaways SET message_id=? WHERE id=?", (str(message_id), gid))

def giveaway_mark_done(gid: int):
    with db() as conn:
        conn.execute("UPDATE giveaways SET status='DONE' WHERE id=?", (gid,))

# ---- Per-giveaway shops (stored in rules table as `shops:<gid>`), no schema change ----
def set_giveaway_shops(gid: int, cids: List[str]):
    set_rule(f"shops:{gid}", ",".join([str(x) for x in cids]))

def get_giveaway_shops(gid: int) -> List[str]:
    r = get_rules().get(f"shops:{gid}", "")
    return [x for x in r.split(",") if x]

# ---- New: explicit helpers to avoid name collision & prefer rules ----
def get_giveaway_shops_from_rules(gid: int) -> List[str]:
    r = get_rules().get(f"shops:{gid}", "")
    return [x for x in r.split(",") if x]

SHOP_LINK_RX = re.compile(r'manufacturers_id=(\d+)')

async def get_giveaway_shops_from_embed(gid: int) -> List[str]:
    """Read the giveaway message embed and extract manufacturers_id values."""
    with db() as conn:
        row = conn.execute("SELECT channel_id, message_id FROM giveaways WHERE id=?", (gid,)).fetchone()
    if not row or not row[0] or not row[1]:
        return []
    ch_id, msg_id = int(row[0]), int(row[1])

    channel = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
    try:
        msg = await channel.fetch_message(msg_id)
    except Exception:
        return []

    if not msg.embeds:
        return []

    desc = (msg.embeds[0].description or "")
    ids = SHOP_LINK_RX.findall(desc)
    # fallback: if someone pasted bare CIDs in text
    if not ids:
        ids = re.findall(r'\b(\d{5,})\b', desc)
    # uniq, keep order
    return list(dict.fromkeys(ids))

async def resolve_giveaway_shops(gid: int) -> List[str]:
    """Prefer shops from rules (what admin set); fallback to scraping the embed."""
    ids = get_giveaway_shops_from_rules(gid)
    if ids:
        return ids
    return await get_giveaway_shops_from_embed(gid)

def giveaway_add_entry(gid: int, discord_id: int, uname: str, pid: str):
    with db() as conn:
        conn.execute("""
          INSERT INTO giveaway_entries(giveaway_id, discord_id, imvu_username, wishlist_product_id, created_at)
          VALUES(?,?,?,?, datetime('now'))
        """, (gid, str(discord_id), uname, pid))

def giveaway_count_entries(gid: int) -> int:
    with db() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id=?", (gid,))
        return int(cur.fetchone()[0])

ADOPT_TITLE = "⚡ WISH — Giveaway"
ENDS_RX = re.compile(r"<t:(\d+):[Rr]>")
WINNERS_LINE_RX = re.compile(r"\*\*Winners:\*\*\s*(\d+)")
SHOPS_IN_DESC_RX = re.compile(r'manufacturers_id=(\d+)')

async def auto_adopt_open_posts():
    """Recreate a missing DB row by scanning the channel for our existing giveaway post."""
    if not GIVEAWAY_CHANNEL_ID:
        return  # we need a channel to look in

    ch = bot.get_channel(GIVEAWAY_CHANNEL_ID) or await bot.fetch_channel(GIVEAWAY_CHANNEL_ID)
    if not ch:
        return

    async for msg in ch.history(limit=50, oldest_first=False):
        # Only messages from this bot with our embed title
        if msg.author.id != bot.user.id or not msg.embeds:
            continue
        e = msg.embeds[0]
        if (e.title or "").strip() != ADOPT_TITLE:
            continue

        # If DB already knows this message, skip
        with db() as conn:
            row = conn.execute("SELECT id FROM giveaways WHERE message_id=?", (str(msg.id),)).fetchone()
        if row:
            continue

        # Parse bits from the embed description
        desc = e.description or ""
        # 1) end time from <t:epoch:R>
        m_end = ENDS_RX.search(desc)
        if not m_end:
            continue
        end_epoch = int(m_end.group(1))
        end_at_utc = datetime.fromtimestamp(end_epoch, tz=timezone.utc)

        # 2) winners
        m_win = WINNERS_LINE_RX.search(desc)
        winners_n = int(m_win.group(1)) if m_win else 1
        winners_n = max(1, winners_n)

        # 3) prize (store raw line, formatting doesn’t matter for DB)
        prize = "—"
        for line in desc.splitlines():
            if line.strip().startswith("**Prize:**"):
                prize = line.split("**Prize:**", 1)[-1].strip()
                break

        # 4) shops (IDs from desc)
        shop_ids = SHOPS_IN_DESC_RX.findall(desc) or re.findall(r"\b(\d{5,})\b", desc)
        shop_ids = list(dict.fromkeys(shop_ids))

        # Insert row + link to message
        gid = giveaway_insert(ch.id, prize, json.dumps({"shops": shop_ids}), winners_n, end_at_utc.isoformat(), bot.user.id)
        giveaway_set_message(gid, msg.id)
        set_giveaway_shops(gid, shop_ids)

        # Reattach the button
        view = EnterButton(gid, disabled=False, timeout=None)
        try:
            await msg.edit(view=view)
        except Exception:
            pass
        bot.add_view(view)

        print(f"[wish] auto-adopted message {msg.id} as giveaway #{gid}")
        break  # adopt the first match only


# helper to get unique entrant user IDs for a giveaway
def giveaway_entry_user_ids(gid: int) -> List[int]:
    with db() as conn:
        cur = conn.execute("SELECT DISTINCT discord_id FROM giveaway_entries WHERE giveaway_id=?", (gid,))
        return [int(r[0]) for r in cur.fetchall()]
        
def add_giveaway_winner(gid: int, discord_id: int):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO giveaway_winners(giveaway_id, discord_id, created_at) VALUES(?,?,datetime('now'))",
            (gid, str(discord_id))
        )

def list_giveaway_winners(gid: int) -> List[int]:
    with db() as conn:
        cur = conn.execute("SELECT discord_id FROM giveaway_winners WHERE giveaway_id=?", (gid,))
        return [int(r[0]) for r in cur.fetchall()]

def imvu_profile_link(username: str) -> str:
    u = (username or "").strip()
    u_safe = re.sub(r"[^A-Za-z0-9_.-]", "", u)
    return f"https://www.imvu.com/next/av/{u_safe}/"
    
def purge_bad_cache_rows():
    with db() as conn:
        conn.execute("DELETE FROM cache_products WHERE creator_id='' OR creator_id IS NULL;")

# =========================
# Scraping & eligibility (kept; not used for entry check)
# =========================
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
PROFILE_CANDIDATES = [
    "https://www.imvu.com/catalog/web_profile.php?user={username}",
    "https://www.imvu.com/people/{username}/",
    "https://www.imvu.com/catalog/web_profile.php?display_name={username}",
]
WISHLIST_CANDIDATES = [
    "https://www.imvu.com/catalog/web_wishlist.php?user={username}",
    "https://www.imvu.com/people/{username}/wishlist/",
]
PRODUCT_LINK_RX = re.compile(r'/shop/product(?:\.php\?products_id=|/)(\d+)', re.I)
MANUFACTURER_RX = re.compile(r'manufacturers?_id(?:=|["\': ]*)(\d+)', re.I)

def _product_ids_from_html(html: str) -> List[str]:
    ids = [m.group(1) for m in PRODUCT_LINK_RX.finditer(html)]
    seen, out = set(), []
    for pid in ids:
        if pid not in seen:
            seen.add(pid); out.append(pid)
    return out

async def _fetch_html(url: str, session: aiohttp.ClientSession, min_len=3000) -> Optional[str]:
    try:
        async with session.get(url, allow_redirects=True) as r:
            if r.status != 200:
                return None
            t = await r.text(errors="ignore")
            if len(t) < min_len:
                return None
            return t
    except Exception:
        return None

def _extract_wishlist_links_from_profile(html: str) -> List[str]:
    out = []
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I):
        href = m.group(1)
        if "wish" not in href.lower():
            continue
        if href.startswith("//"): href = "https:" + href
        if href.startswith("/"):  href = "https://www.imvu.com" + href
        if "imvu.com" in href:
            out.append(href)
    seen, res = set(), []
    for u in out:
        if u not in seen: seen.add(u); res.append(u)
    return res

async def product_creator_id(session: aiohttp.ClientSession, product_id: str, sem: asyncio.Semaphore) -> Optional[str]:
    cached = cache_get(product_id)
    # If cache has a real creator_id, use it. If it's "", treat as a miss and retry.
    if cached is not None and cached != "":
        return cached

    urls = [
        f"https://www.imvu.com/shop/product/{product_id}",
        f"https://www.imvu.com/shop/product.php?products_id={product_id}",
    ]
    async with sem:
        for url in urls:
            htmlp = await _fetch_html(url, session)
            if not htmlp:
                continue
            m = MANUFACTURER_RX.search(htmlp)
            if m:
                cid = m.group(1)
                cache_put(product_id, cid)   # cache only on success
                return cid
    # do NOT cache failures
    return None


def cache_get(product_id: str) -> Optional[str]:
    with db() as conn:
        cur = conn.execute("SELECT creator_id, fetched_at FROM cache_products WHERE product_id=?", (product_id,))
        row = cur.fetchone()
    if not row: return None
    creator_id, fetched_at = row
    try:
        ts = datetime.fromisoformat(fetched_at.replace("Z","")).replace(tzinfo=timezone.utc)
    except Exception:
        ts = datetime.now(timezone.utc) - timedelta(days=9999)
    if datetime.now(timezone.utc) - ts > timedelta(hours=PRODUCT_CACHE_TTL_HOURS):
        return None
    return creator_id

def cache_put(product_id: str, creator_id: Optional[str]):
    # Skip caching failures/empties
    if not creator_id:
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO cache_products(product_id,creator_id,fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(product_id) DO UPDATE SET creator_id=excluded.creator_id, fetched_at=excluded.fetched_at;",
            (product_id, creator_id, datetime.now(timezone.utc).isoformat())
        )

async def evaluate_user(username: str):
    wl_url, product_ids = await wishlist_url_and_products(username)
    if not wl_url or not product_ids:
        return (0, {})
    timeout = aiohttp.ClientTimeout(total=25, connect=10)
    async with aiohttp.ClientSession(timeout=timeout, headers=DEFAULT_HEADERS) as s:
        sem = asyncio.Semaphore(PRODUCT_CONCURRENCY)
        creators = await asyncio.gather(*[product_creator_id(s, pid, sem) for pid in product_ids])
    per: Dict[str,int] = {}
    for cid in creators:
        if not cid: continue
        per[cid] = per.get(cid,0) + 1
    return (len(product_ids), per)

def _eligible_by_creator_rule(per_creator: Dict[str,int], rules: Dict[str,str], allowed_creators: List[str]) -> bool:
    mode = rules.get("mode", "NONE").upper()
    if mode == "NONE" or not allowed_creators:
        return True
    allowed = set(allowed_creators)
    if mode in ("ANY", "EACH"):
        thr = max(1, int(rules.get("threshold", "1")))
        if mode == "ANY":
            return any(per_creator.get(cid, 0) >= thr for cid in allowed)
        return all(per_creator.get(cid, 0) >= thr for cid in allowed)
    if mode == "MAP":
        try:
            req = json.loads(rules.get("map_json", "{}"))
        except Exception:
            req = {}
        if not req:
            return True
        for cid, need in req.items():
            if per_creator.get(str(cid), 0) < int(need):
                return False
        return True
    return True

# Return (imvu_username, first_product_id) a user submitted in this giveaway
def giveaway_entry_username_and_pid(gid: int, discord_id: int) -> Tuple[Optional[str], Optional[str]]:
    with db() as conn:
        row = conn.execute(
            "SELECT imvu_username, wishlist_product_id FROM giveaway_entries "
            "WHERE giveaway_id=? AND discord_id=? LIMIT 1",
            (gid, str(discord_id))
        ).fetchone()
    if not row:
        return (None, None)
    uname, raw_pid = row
    pid = None
    if raw_pid:
        ids = parse_product_ids(str(raw_pid), limit=1)
        pid = ids[0] if ids else None
    return (uname, pid)

# =========================
# Input sanitizers
# =========================
USERNAME_FROM_LINK = re.compile(r"/people/([^/]+)/|[?&]user=([^&/#]+)", re.I)
def normalize_username(raw: str) -> str:
    s = raw.strip()
    if s.startswith("http"):
        m = USERNAME_FROM_LINK.search(s)
        if m:
            u = m.group(1) or m.group(2)
            return urllib.parse.unquote(u)
        return ""
    return s

PROD_ID_RX = re.compile(r'(\d{5,})')
def parse_product_ids(raw: str, limit: int = 10) -> List[str]:
    ids = PROD_ID_RX.findall(raw or "")
    seen, out = set(), []
    for pid in ids:
        if pid not in seen:
            seen.add(pid); out.append(pid)
        if len(out) >= limit: break
    return out

# Turn product IDs/URLs in the prize string into clickable links
URL_RX = re.compile(r'(https?://\S+)', re.I)

def imvu_product_link(pid: str) -> str:
    pid = re.sub(r"\D", "", str(pid))
    return f"https://www.imvu.com/shop/product.php?products_id={pid}"

def format_prize_text(prize: str) -> str:
    prize = str(prize or "").strip()
    if not prize:
        return prize
    pids = parse_product_ids(prize, limit=5)
    links: List[str] = []
    for pid in pids:
        links.append(f"<{imvu_product_link(pid)}>")
    for m in URL_RX.findall(prize):
        if m not in links:
            links.append(m if m.startswith("<") else f"<{m}>")
    return ", ".join(links) if links else prize

# =========================
# Creator helper (robust name resolver)
# =========================
CREATOR_NAME_RX_1 = re.compile(r'by\s*<a[^>]*>\s*([A-Za-z0-9_.\- ]{2,40})\s*</a>', re.I)
CREATOR_NAME_RX_2 = re.compile(r'(?:manufacturer|manufacturers?_name)\s*[:=]\s*["\']([^"\']{2,40})["\']', re.I)
CREATOR_NAME_RX_3 = re.compile(r'<title>[^<]*\bby\s+([A-Za-z0-9_.\- ]{2,40})\b', re.I)

async def fetch_creator_name(mid: str) -> Optional[str]:
    search_url = f"https://www.imvu.com/shop/web_search.php?manufacturers_id={mid}"
    timeout = aiohttp.ClientTimeout(total=12, connect=8)
    async with aiohttp.ClientSession(timeout=timeout, headers=DEFAULT_HEADERS) as s:
        html_text = await _fetch_html(search_url, s, min_len=400)
        if html_text:
            m = CREATOR_NAME_RX_1.search(html_text) or CREATOR_NAME_RX_2.search(html_text) or CREATOR_NAME_RX_3.search(html_text)
            if m:
                return html.unescape(m.group(1)).strip()
            pids = _product_ids_from_html(html_text)
            for pid in pids[:3]:
                for purl in (
                    f"https://www.imvu.com/shop/product/{pid}",
                    f"https://www.imvu.com/shop/product.php?products_id={pid}",
                ):
                    phtml = await _fetch_html(purl, s, min_len=400)
                    if not phtml:
                        continue
                    m2 = CREATOR_NAME_RX_1.search(phtml) or CREATOR_NAME_RX_2.search(phtml) or CREATOR_NAME_RX_3.search(phtml)
                    if m2:
                        return html.unescape(m2.group(1)).strip()
    return None

def shop_masked_link(cid: str, label: Optional[str]) -> str:
    url = f"https://www.imvu.com/shop/web_search.php?manufacturers_id={cid}"
    return f"[{label or cid}]({url})"
    
# --- helpers used by per-shop draw ---
SHOP_LINK_RX = re.compile(r'manufacturers_id=(\d+)')

async def get_giveaway_shops_from_embed(gid: int) -> List[str]:
    """Read the giveaway message embed and extract manufacturers_id values."""
    with db() as conn:
        row = conn.execute("SELECT channel_id, message_id FROM giveaways WHERE id=?", (gid,)).fetchone()
    if not row or not row[0] or not row[1]:
        return []
    ch_id, msg_id = int(row[0]), int(row[1])

    channel = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
    try:
        msg = await channel.fetch_message(msg_id)
    except Exception:
        return []

    if not msg.embeds:
        return []

    desc = (msg.embeds[0].description or "")
    ids = SHOP_LINK_RX.findall(desc)
    # fallback: if someone pasted bare CIDs in text
    if not ids:
        ids = re.findall(r'\b(\d{5,})\b', desc)
    # uniq, keep order
    return list(dict.fromkeys(ids))

def giveaway_entry_raw_products(gid: int, uid: int) -> List[str]:
    """Return all product IDs the user submitted for this giveaway (parsed from the stored text)."""
    with db() as conn:
        row = conn.execute(
            "SELECT wishlist_product_id FROM giveaway_entries "
            "WHERE giveaway_id=? AND discord_id=? LIMIT 1",
            (gid, str(uid))
        ).fetchone()
    if not row or not row[0]:
        return []
    return parse_product_ids(str(row[0]), limit=10)

async def find_pid_for_shop(session: aiohttp.ClientSession, sem: asyncio.Semaphore,
                            pid_list: List[str], shop_cid: str) -> Optional[str]:
    """Pick the first PID from pid_list that belongs to the given manufacturer (shop_cid)."""
    for pid in pid_list:
        cid = await product_creator_id(session, pid, sem)
        if cid == str(shop_cid):
            return pid
    return None

# =========================
# Entrant UI — button + modal
# =========================
class EnterModal(ui.Modal, title="⚡ WISH — Enter Giveaway"):
    def __init__(self, giveaway_id: int):
        super().__init__()
        self.gid = giveaway_id

    imvu_username = ui.TextInput(label="IMVU username",
                                 placeholder="e.g., YaEli (not a link)",
                                 required=True, max_length=40)
    product_ids   = ui.TextInput(label="Product IDs/Links (max 10)",
                                 style=discord.TextStyle.paragraph,
                                 placeholder="12345678, https://www.imvu.com/shop/product/87654321\n(one per line or comma-separated)",
                                 required=True, max_length=1000)

    async def on_submit(self, interaction: discord.Interaction):
        gid = self.gid
        uname = normalize_username(str(self.imvu_username))
        ids   = parse_product_ids(str(self.product_ids), limit=10)

        if not uname or not ids:
            return await interaction.response.send_message(
                "Enter a valid **IMVU username** and at least **one** product ID/link.", ephemeral=True
            )
        # one entry per user per giveaway — allow edits instead of blocking
        with db() as conn:
            cur = conn.execute(
                "SELECT 1 FROM giveaway_entries WHERE giveaway_id=? AND discord_id=?",
                (gid, str(interaction.user.id))
            )
            exists = cur.fetchone() is not None

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Trust entrant input; store ALL submitted IDs (comma-joined)
        all_ids_csv = ",".join(ids)

        if exists:
            # UPDATE existing record (edit entry)
            with db() as conn:
                conn.execute(
                    "UPDATE giveaway_entries "
                    "SET imvu_username=?, wishlist_product_id=?, created_at=datetime('now') "
                    "WHERE giveaway_id=? AND discord_id=?",
                    (uname, all_ids_csv, gid, str(interaction.user.id))
                )
            # ensure participant row exists/refresh
            upsert_entrant(interaction.user.id, uname, total_items=0, eligible=1)

            await update_giveaway_counter_embed(gid)
            return await interaction.followup.send(
                f"✏️ Updated your entry as **{uname}** (saved **{len(ids)}** product ID(s)).",
                ephemeral=True
            )
        else:
            # NEW entry
            try:
                giveaway_add_entry(gid, interaction.user.id, uname, all_ids_csv)
            except sqlite3.IntegrityError:
                # rare race: fallback to update
                with db() as conn:
                    conn.execute(
                        "UPDATE giveaway_entries "
                        "SET imvu_username=?, wishlist_product_id=?, created_at=datetime('now') "
                        "WHERE giveaway_id=? AND discord_id=?",
                        (uname, all_ids_csv, gid, str(interaction.user.id))
                    )

            upsert_entrant(interaction.user.id, uname, total_items=0, eligible=1)

            await update_giveaway_counter_embed(gid)
            return await interaction.followup.send(
                f"✅ Entered as **{uname}** (saved **{len(ids)}** product ID(s)).",
                ephemeral=True
            )

 
class EnterButton(ui.View):
    def __init__(self, giveaway_id: int, disabled: bool = False, timeout=None):
        super().__init__(timeout=timeout)
        self.gid = giveaway_id
        # make the button route uniquely even after restarts
        self.enter_btn.custom_id = f"wish:enter:{giveaway_id}"
        self.enter_btn.disabled = disabled

    @ui.button(label="Enter Giveaway", style=discord.ButtonStyle.primary, custom_id="wish:enter_btn")
    async def enter_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(EnterModal(self.gid))


async def update_giveaway_counter_embed(giveaway_id: int):
    with db() as conn:
        cur = conn.execute("SELECT channel_id, message_id FROM giveaways WHERE id=?", (giveaway_id,))
        row = cur.fetchone()
    if not row: return
    ch_id, msg_id = map(int, row)
    channel = bot.get_channel(ch_id)
    if not channel: return
    try:
        msg = await channel.fetch_message(msg_id)
    except Exception:
        return
    count = giveaway_count_entries(giveaway_id)
    if not msg.embeds: return
    e = msg.embeds[0]
    new = discord.Embed(title=e.title, description=e.description, color=e.color)
    if e.footer and e.footer.text:
        new.set_footer(text=e.footer.text)
    has_field = False
    for f in e.fields:
        if f.name == "Participants":
            new.add_field(name="Participants", value=str(count), inline=True); has_field = True
        else:
            new.add_field(name=f.name, value=f.value, inline=f.inline)
    if not has_field:
        new.add_field(name="Participants", value=str(count), inline=True)
    if e.thumbnail and e.thumbnail.url:
        new.set_thumbnail(url=e.thumbnail.url)
    await msg.edit(embed=new, view=EnterButton(giveaway_id))

# =========================
# /wish — ONE admin modal
# =========================
DUR_RX = re.compile(r'^\s*(\d+)\s*([smhdw])\s*$', re.I)
def parse_duration_to_seconds(s: str) -> int:
    m = DUR_RX.match(s or "")
    if not m: raise ValueError("Use formats like 30m, 2h, 1d, 1w")
    n, unit = int(m.group(1)), m.group(2).lower()
    mult = dict(s=1, m=60, h=3600, d=86400, w=604800)[unit]
    return n * mult

class WishSingle(ui.Modal, title="Create WISH Giveaway"):
    duration = ui.TextInput(label="Duration", placeholder="30m, 24h, 3d, 1w", required=True)
    winners  = ui.TextInput(label="Number of Winners", placeholder="1", required=True, max_length=4)
    prize    = ui.TextInput(label="Prize", placeholder="Text or URL", required=True)
    shops    = ui.TextInput(label="Shops (IDs or shop URLs, comma/lines)",
                            style=discord.TextStyle.paragraph, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            secs = parse_duration_to_seconds(str(self.duration))
            winners_typed = max(1, int(str(self.winners)))
        except Exception as e:
            await interaction.response.send_message(f"Invalid: {e}", ephemeral=True)
            return

        prize = str(self.prize).strip()

        await interaction.response.defer()

        # resolve shops (IDs -> display name), also collect clickable links
        ids = re.findall(r'(\d{5,})', str(self.shops or ""))
        unique_ids = list(dict.fromkeys(ids))
        creator_names: List[str] = []
        creator_clicks: List[str] = []
        for cid in unique_ids:
            add_creator(cid, None)
            name = None
            try:
                name = await asyncio.wait_for(fetch_creator_name(cid), timeout=4.0)
            except Exception:
                name = None
            if name:
                add_creator(cid, name)
            creator_names.append(name or cid)
            creator_clicks.append(shop_masked_link(cid, name or cid))

        # winners = number of unique shops; else what was typed
        winners_n = len(unique_ids) if unique_ids else winners_typed
        
        desc_meta = json.dumps({"shops": unique_ids})

        end_at_utc = datetime.now(timezone.utc) + timedelta(seconds=secs)
        # no announcement text anymore -> pass "" to description
        gid = giveaway_insert(
            interaction.channel.id, prize, desc_meta, winners_n, end_at_utc.isoformat(), interaction.user.id
        )

        # save shops for this giveaway (used by per-shop draw)
        set_giveaway_shops(gid, unique_ids)

        end_rel = discord.utils.format_dt(end_at_utc, style='R')
        creators_txt = ", ".join(creator_clicks) if creator_clicks else "—"
        host_mention = interaction.user.mention

        desc = (
            f"**Host:** {host_mention}\n"
            f"**Prize:** {format_prize_text(prize)}\n"
            f"**Winners:** {winners_n}\n"
            f"**Ends:** {end_rel}\n\n"
            f"**This round we support Shops:** {creators_txt}\n\n"
            f"Hit **Enter Giveaway** button, drop your **IMVU username**, and follow steps\n"
            f"or you're just window shopping."
        )

        embed = discord.Embed(title="⚡ WISH — Giveaway", description=desc, color=discord.Color.gold())
        embed.set_footer(text="👉 WL missing? You’re done. One per shop, non-negotiable.😎")
        embed.add_field(name="Participants", value="0", inline=True)

        try:
            msg = await interaction.channel.send(embed=embed, view=EnterButton(gid))
            giveaway_set_message(gid, msg.id)
        except Exception as e:
            await interaction.followup.send(f"Couldn’t post the giveaway: {e}", ephemeral=True)

@tree.command(name="wish", description="Create a WISH giveaway (admin only).")
async def wish_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    await interaction.response.send_modal(WishSingle())

# =========================
# Reroll
# =========================
@tree.command(name="rebind_link", description="Admin: rebind or adopt a giveaway by message link (hard refresh).")
@app_commands.describe(
    message_link="Right-click the stale post → Copy Message Link",
    duration="If it’s expired, extend from now (e.g., 2d, 24h, 45m). Default 2d."
)
async def rebind_link_cmd(
    interaction: discord.Interaction,
    message_link: str,
    duration: str = "2d",
):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admins only.", ephemeral=True)

    # Parse the channel & message IDs from the link
    m = re.search(r"/channels/\d+/(\d+)/(\d+)$", message_link.strip())
    if not m:
        return await interaction.response.send_message("I couldn’t parse that message link.", ephemeral=True)
    ch_id, msg_id = int(m.group(1)), int(m.group(2))

    # If we already know this message, use its row; otherwise adopt it
    with db() as conn:
        row = conn.execute("SELECT id, status FROM giveaways WHERE message_id=?", (str(msg_id),)).fetchone()

    if row:
        gid, status = int(row[0]), row[1]
    else:
        # Adopt minimal row with a future end time
        try:
            secs = parse_duration_to_seconds(duration)
        except Exception as e:
            return await interaction.response.send_message(f"Invalid duration: {e}", ephemeral=True)
        end_at_utc = datetime.now(timezone.utc) + timedelta(seconds=secs)
        gid = giveaway_insert(ch_id, "—", json.dumps({"shops": []}), 1, end_at_utc.isoformat(), interaction.user.id)
        giveaway_set_message(gid, msg_id)

    # Ensure it is OPEN and has a *future* end time so watcher won’t instantly close it
    try:
        secs = parse_duration_to_seconds(duration)
    except Exception:
        secs = 48 * 3600  # fallback 2 days
    new_end = (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat()

    with db() as conn:
        conn.execute("UPDATE giveaways SET status='OPEN', end_at=? WHERE id=?", (new_end, gid))

    # Hard-refresh the actual message you linked
    ch  = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
    msg = await ch.fetch_message(msg_id)
    view = EnterButton(gid, disabled=False, timeout=None)
    try:
        await msg.edit(view=None)   # clear legacy components
    except Exception:
        pass
    await msg.edit(view=view)       # attach our button
    bot.add_view(view)              # register handler

    return await interaction.response.send_message(
        f"✅ Rebound **#{gid}** in <#{ch_id}> (ends {discord.utils.format_dt(datetime.fromisoformat(new_end), style='R')}).",
        ephemeral=True
    )
@tree.command(name="rebind_here", description="Admin: rebind/adopt the latest WISH giveaway in this channel.")
@app_commands.describe(duration="Keep it open from now (e.g., 19h, 1d, 45m). Default 2d.")
async def rebind_here_cmd(interaction: discord.Interaction, duration: str = "2d"):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    try:
        secs = parse_duration_to_seconds(duration)
    except Exception:
        secs = 48 * 3600
    new_end_dt = datetime.now(timezone.utc) + timedelta(seconds=secs)

    # find the latest WISH giveaway embed in this channel
    ch = interaction.channel
    target = None
    async for m in ch.history(limit=50, oldest_first=False):
        if m.author.id == bot.user.id and m.embeds and (m.embeds[0].title or "").strip() == "⚡ WISH — Giveaway":
            target = m
            break
    if not target:
        return await interaction.response.send_message("No WISH giveaway message found in this channel.", ephemeral=True)

    with db() as conn:
        row = conn.execute("SELECT id FROM giveaways WHERE message_id=?", (str(target.id),)).fetchone()
    if row:
        gid = int(row[0])
    else:
        gid = giveaway_insert(ch.id, "—", json.dumps({"shops": []}), 1, new_end_dt.isoformat(), interaction.user.id)
        giveaway_set_message(gid, target.id)

    with db() as conn:
        conn.execute("UPDATE giveaways SET status='OPEN', end_at=? WHERE id=?", (new_end_dt.isoformat(), gid))

    view = EnterButton(gid, disabled=False, timeout=None)
    await target.edit(view=None)
    await target.edit(view=view)
    bot.add_view(view)

    return await interaction.response.send_message(
        f"✅ Rebound **#{gid}** here. Ends {discord.utils.format_dt(new_end_dt, style='R')}.",
        ephemeral=True
    )


@tree.command(name="reroll", description="Admin: reroll winner(s) for a past giveaway.")
@app_commands.describe(giveaway_id="Giveaway ID (see bot message or DB)", count="How many new winners (default 1)")
async def reroll_cmd(interaction: discord.Interaction, giveaway_id: int, count: int = 1):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admins only.", ephemeral=True)

    with db() as conn:
        row = conn.execute("SELECT channel_id, prize FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
    if not row:
        return await interaction.response.send_message("Unknown giveaway ID.", ephemeral=True)
    ch_id, prize = int(row[0]), row[1]
    channel = bot.get_channel(ch_id)
    if not channel:
        return await interaction.response.send_message("I can't see that channel anymore.", ephemeral=True)

    entries = giveaway_entry_user_ids(giveaway_id)
    already = set(list_giveaway_winners(giveaway_id))
    pool = [u for u in entries if u not in already]
    if not pool:
        return await interaction.response.send_message("No remaining entrants to reroll from.", ephemeral=True)

    if ONE_WIN_ONLY:
        with db() as conn:
            pool = [
                u for u in pool
                if not (conn.execute(
                    "SELECT last_win_at FROM Participants WHERE discord_id=?",
                    (str(u),)
                ).fetchone() or [None])[0]
            ]
        if not pool:
            return await interaction.response.send_message(
                "No eligible entrants left to reroll (lifetime one-win is enabled).",
                ephemeral=True
            )

    random.shuffle(pool)
    picks = pool[:max(1, int(count))]

    # announce & buttons
    rows = []
    for u in picks:
        pid = giveaway_entry_product_id(giveaway_id, u)
        if pid:
            url = imvu_product_link(pid)
            rows.append(f"• <@{u}> — <{url}>")
        else:
            rows.append(f"• <@{u}>")
    lines = "\n".join(rows)

    text = (
        f"🔁 **REROLL** for Giveaway #{giveaway_id}\n"
        f"**Prize:** {format_prize_text(prize)}\n"
        f"**New winner{'s' if len(picks)!=1 else ''}:**\n{lines}"
    )
    view = ui.View(timeout=None)
    with db() as conn:
        for uid in picks:
            rowu = conn.execute(
                "SELECT imvu_username FROM giveaway_entries "
                "WHERE giveaway_id=? AND discord_id=? LIMIT 1",
                (giveaway_id, str(uid))
            ).fetchone()
            if not rowu or not rowu[0]:
                continue
            uname = (rowu[0] or "").strip()
            url = imvu_profile_link(uname)
            label = f"Gift {uname}"[:80]
            view.add_item(ui.Button(style=discord.ButtonStyle.link, label=label, url=url))
    view_to_send = view if len(view.children) > 0 else None
    try:
        await channel.send(text, view=view_to_send)
    except Exception:
        pass

    for uid in picks:
        set_winner(uid)
        add_giveaway_winner(giveaway_id, uid)

    await interaction.response.send_message(f"Rerolled ✅ Picked {len(picks)} new winner(s).", ephemeral=True)

# Return the first product ID a user submitted for this giveaway (if any)
def giveaway_entry_product_id(gid: int, discord_id: int) -> Optional[str]:
    with db() as conn:
        cur = conn.execute(
            "SELECT wishlist_product_id FROM giveaway_entries "
            "WHERE giveaway_id=? AND discord_id=? LIMIT 1",
            (gid, str(discord_id))
        )
        row = cur.fetchone()
    if not row or not row[0]:
        return None
    ids = parse_product_ids(str(row[0]), limit=1)
    return ids[0] if ids else None

# ======== Per-shop picking helpers ========
def giveaway_entry_raw_products(gid: int, discord_id: int) -> List[str]:
    with db() as conn:
        row = conn.execute(
            "SELECT wishlist_product_id FROM giveaway_entries WHERE giveaway_id=? AND discord_id=? LIMIT 1",
            (gid, str(discord_id))
        ).fetchone()
    if not row or not row[0]:
        return []
    return parse_product_ids(str(row[0]), limit=10)

async def find_pid_for_shop(session: aiohttp.ClientSession, sem: asyncio.Semaphore,
                            pid_list: List[str], shop_cid: str) -> Optional[str]:
    for pid in pid_list:
        cid = await product_creator_id(session, pid, sem)
        if cid and str(cid) == str(shop_cid):
            return pid
    return None
                                
def giveaway_entries_with_pid(gid: int) -> List[Tuple[int, Optional[str]]]:
    with db() as conn:
        cur = conn.execute(
            "SELECT discord_id, wishlist_product_id FROM giveaway_entries WHERE giveaway_id=?",
            (gid,)
        )
        rows = []
        for uid, raw in cur.fetchall():
            pid = parse_product_ids(str(raw or ""), limit=1)
            rows.append((int(uid), pid[0] if pid else None))
    return rows

# =========================
# Draw/close watcher (one winner per shop if shops were supplied)
# =========================
def giveaway_claim(gid: int) -> bool:
    with db() as conn:
        cur = conn.execute(
            "UPDATE giveaways SET status='DRAWING' WHERE id=? AND status='OPEN'",
            (gid,)
        )
        return cur.rowcount > 0

@tasks.loop(seconds=30)
async def giveaway_watcher():
    now = datetime.now(timezone.utc)
    with db() as conn:
        cur = conn.execute(
            # FIX: match unpack count (5)
            "SELECT id, channel_id, message_id, winners, prize "
            "FROM giveaways WHERE status='OPEN' AND end_at <= ?",
            (now.isoformat(),)
        )
        due = cur.fetchall()
    if not due:
        return

    for gid, ch_id, msg_id, winners, prize in due:
        try:
            # claim so only one worker handles this giveaway
            if not giveaway_claim(gid):
                continue

            # get channel (cache first, then API)
            channel = bot.get_channel(int(ch_id))
            if channel is None:
                channel = await bot.fetch_channel(int(ch_id))

            # disable the Enter button on the original message (best effort)
            try:
                if int(msg_id):
                    msg = await channel.fetch_message(int(msg_id))
                    await msg.edit(view=EnterButton(gid, disabled=True))
            except Exception:
                pass

            # -------- pick winners (one per shop if shops were supplied) --------
            entries = giveaway_entry_user_ids(gid)
            winners_n = max(1, int(winners))

            picks: List[Tuple[int, Optional[str]]] = []   # (uid, matched_pid)
            picked_users: set[int] = set()
            pool = list(entries)
            random.shuffle(pool)

            # CHANGED: resolve shops preferring rules, fallback to embed
            shops = await resolve_giveaway_shops(gid)
            sem = asyncio.Semaphore(PRODUCT_CONCURRENCY)

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=25, connect=10),
                headers=DEFAULT_HEADERS
            ) as session:
                if shops:
                    # try to award one unique user per shop
                    for shop_cid in shops:
                        # build eligible candidates (respect ONE_WIN_ONLY / cooldown)
                        candidates: List[int] = []
                        with db() as conn:
                            for uid in pool:
                                if uid in picked_users:
                                    continue
                                row = conn.execute(
                                    "SELECT last_win_at FROM Participants WHERE discord_id=?",
                                    (str(uid),)
                                ).fetchone()

                                if ONE_WIN_ONLY:
                                    if row and row[0]:
                                        continue
                                elif WIN_COOLDOWN_DAYS > 0 and row and row[0]:
                                    try:
                                        lw = datetime.fromisoformat(row[0].replace("Z","")).replace(tzinfo=timezone.utc)
                                    except Exception:
                                        lw = datetime.now(timezone.utc) - timedelta(days=9999)
                                    if lw > datetime.now(timezone.utc) - timedelta(days=WIN_COOLDOWN_DAYS):
                                        continue

                                candidates.append(uid)

                        random.shuffle(candidates)

                        chosen: Optional[Tuple[int, Optional[str]]] = None
                        for uid in candidates:
                            pid_list = giveaway_entry_raw_products(gid, uid)
                            if not pid_list:
                                continue
                            match_pid = await find_pid_for_shop(session, sem, pid_list, shop_cid)
                            if match_pid:
                                chosen = (uid, match_pid)
                                break

                        if chosen:
                            picks.append(chosen)
                            picked_users.add(chosen[0])
                            if len(picks) >= winners_n:
                                break

                # Fallback fill if we didn’t reach winners_n
                if len(picks) < winners_n and pool:
                    # If shops exist AND strict mode is on, do NOT fallback — keep only shop-matched winners
                    if shops and STRICT_SHOP_MATCH:
                        pass  # no fallback; winners remain as matched (possibly zero)
                    else:
                        with db() as conn:
                            remaining = [u for u in pool if u not in picked_users]
                            random.shuffle(remaining)
                            for uid in remaining:
                                row = conn.execute(
                                    "SELECT last_win_at FROM Participants WHERE discord_id=?",
                                    (str(uid),)
                                ).fetchone()
                
                                if ONE_WIN_ONLY:
                                    if row and row[0]:
                                        continue
                                elif WIN_COOLDOWN_DAYS > 0 and row and row[0]:
                                    try:
                                        lw = datetime.fromisoformat(row[0].replace("Z","")).replace(tzinfo=timezone.utc)
                                    except Exception:
                                        lw = datetime.now(timezone.utc) - timedelta(days=9999)
                                    if lw > datetime.now(timezone.utc) - timedelta(days=WIN_COOLDOWN_DAYS):
                                        continue
                
                                pid_list = giveaway_entry_raw_products(gid, uid)
                                picks.append((uid, pid_list[0] if pid_list else None))
                                if len(picks) >= winners_n:
                                    break

            # -------- build announcement text (FIX: define mention_line) --------
            if not pool:
                mention_line = "No entries 😔"
            elif not picks:
                mention_line = "No eligible Participants 😔"
            else:
                lines = []
                for uid, pid in picks:
                    if pid:
                        url = imvu_product_link(pid)
                        lines.append(f"• <@{uid}> — <{url}>")
                    else:
                        lines.append(f"• <@{uid}>")
                mention_line = "\n".join(lines)

            text = (
                " **Giveaway Ended**\n\n"
                f"**Prize:** {format_prize_text(prize)}\n\n"
                f"**Winner{'s' if winners_n != 1 else ''}:**\n{mention_line}"
            )

            # profile buttons (one per winner, opens IMVU profile)
            view = ui.View(timeout=None)
            for uid, _pid in picks:
                with db() as conn:
                    rowu = conn.execute(
                        "SELECT imvu_username FROM giveaway_entries "
                        "WHERE giveaway_id=? AND discord_id=? LIMIT 1",
                        (gid, str(uid))
                    ).fetchone()
                if not rowu or not rowu[0]:
                    continue
                uname = (rowu[0] or "").strip()
                url = imvu_profile_link(uname)
                label = f"Gift {uname}"[:80]
                view.add_item(ui.Button(style=discord.ButtonStyle.link, label=label, url=url))
            view_to_send = view if len(view.children) > 0 else None

            posted = False
            try:
                await channel.send(text, view=view_to_send)
                posted = True
            except Exception as e:
                print(f"[wish] send failed for gid {gid} in ch {ch_id}: {e}")

            if posted:
                for uid, _pid in picks:
                    set_winner(uid)
                    add_giveaway_winner(gid, uid)
                giveaway_mark_done(gid)
            else:
                with db() as conn:
                    conn.execute("UPDATE giveaways SET status='OPEN' WHERE id=? AND status='DRAWING'", (gid,))

        except Exception as e:
            # any unexpected error: log and unlock so the watcher can retry next tick
            print(f"[wish] fatal draw error gid {gid}: {e}")
            with db() as conn:
                conn.execute("UPDATE giveaways SET status='OPEN' WHERE id=? AND status='DRAWING'", (gid,))


# =========================
# Optional: Product image helper (unused by default)
# =========================
PRODUCT_OG_IMAGE_RX = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
async def product_image_url_by_pid(pid: str) -> Optional[str]:
    timeout = aiohttp.ClientTimeout(total=12, connect=10)
    async with aiohttp.ClientSession(timeout=timeout, headers=DEFAULT_HEADERS) as s:
        for url in (
            f"https://www.imvu.com/shop/product/{pid}",
            f"https://www.imvu.com/shop/product.php?products_id={pid}",
        ):
            html_page = await _fetch_html(url, s, min_len=500)
            if not html_page:
                continue
            m = PRODUCT_OG_IMAGE_RX.search(html_page)
            if m:
                return m.group(1)
    return None

# =========================
# Utilities
# =========================
@tree.command(name="settings", description="Show current settings.")
async def settings_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    r = get_rules()
    creators_txt = ", ".join([f"{cid}" + (f" ({lbl})" if lbl else "") for cid,lbl in list_creators()]) or "—"
    await interaction.response.send_message(
        f"Mode: **{r.get('mode')}** | Threshold: **{r.get('threshold')}**\n"
        f"Min total items: **{r.get('min_total')}**\n"
        f"MAP: `{r.get('map_json')}`\n"
        f"Creators: {creators_txt}",
        ephemeral=True
    )

@tree.command(name="sync", description="Admin: re-register slash commands here.")
async def sync_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    await tree.sync(guild=interaction.guild)
    await interaction.response.send_message("✅ Synced for this server.", ephemeral=True)
    
@tree.command(name="rebind", description="Admin: reattach the Enter button to a giveaway message.")
@app_commands.describe(giveaway_id="The numeric giveaway ID")
async def rebind_cmd(interaction: discord.Interaction, giveaway_id: int):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admins only.", ephemeral=True)

    with db() as conn:
        row = conn.execute(
            "SELECT channel_id, message_id, status FROM giveaways WHERE id=?", (giveaway_id,)
        ).fetchone()
    if not row:
        return await interaction.response.send_message("Unknown giveaway ID.", ephemeral=True)
    ch_id, msg_id, status = int(row[0]), int(row[1] or 0), row[2]
    # Allow reopening so we can rebind
    if status != "OPEN":
        with db() as conn:
            conn.execute("UPDATE giveaways SET status='OPEN' WHERE id=?", (giveaway_id,))
        status = "OPEN"


    ch  = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
    msg = await ch.fetch_message(msg_id)

    # HARD refresh: remove any old components, then attach ONE persistent view
    view = EnterButton(giveaway_id, disabled=False, timeout=None)
    try:
        await msg.edit(view=None)      # <- clears legacy rows/buttons
    except Exception:
        pass
    await msg.edit(view=view)          # <- attaches our new button
    bot.add_view(view)                 # <- registers same instance for persistence

    await interaction.response.send_message("✅ Button reattached (hard refresh).", ephemeral=True)


# =========================
# Startup
# =========================
@bot.event
async def on_interaction(interaction: discord.Interaction):
    ensure_db()  # <<< make sure tables exist before any DB query

    if interaction.type != discord.InteractionType.component:
        return

    cid = (interaction.data or {}).get("custom_id", "")
    if not cid:
        return

    try:
        if cid.startswith("wish:enter:"):
            gid = int(cid.split(":")[-1])
            return await interaction.response.send_modal(EnterModal(gid))

        if cid == "wish:enter_btn":
            with db() as conn:
                row = conn.execute(
                    "SELECT id FROM giveaways WHERE message_id=? LIMIT 1",
                    (str(interaction.message.id),)
                ).fetchone()
            if row:
                return await interaction.response.send_modal(EnterModal(int(row[0])))
            else:
                return await interaction.response.send_message(
                    "This giveaway button is stale. Ask an admin to rebind.", ephemeral=True
                )
    except Exception as e:
        print(f"[wish] on_interaction fallback error: {e}")
        try:
            await interaction.response.send_message("Something went wrong. Try again.", ephemeral=True)
        except Exception:
            pass


@bot.event
async def on_ready():
    ensure_db()
    print(f"[wish] DB_PATH={DB_PATH}")
    # try auto-adopt
    try:
        await auto_adopt_open_posts()
    except Exception as e:
        print("[wish] auto-adopt failed:", e)
  
    # --- Rebind views to existing OPEN giveaways ---
    with db() as conn:
        rows = conn.execute(
            "SELECT id, channel_id, message_id FROM giveaways "
            "WHERE status='OPEN' AND message_id IS NOT NULL AND message_id <> ''"
        ).fetchall()

    print(f"[wish] rebinding views for {len(rows)} giveaway(s)")

    for gid, ch_id, msg_id in rows:
        try:
            ch  = bot.get_channel(int(ch_id)) or await bot.fetch_channel(int(ch_id))
            msg = await ch.fetch_message(int(msg_id))

            view = EnterButton(gid, disabled=False, timeout=None)  # one instance
            await msg.edit(view=view)                              # attach to message
            bot.add_view(view)                                     # register persistent handler

            print(f"[wish] rebound view for giveaway #{gid} (msg {msg_id})")
        except Exception as e:
            print(f"[wish] rebind failed for gid {gid}: {e}")

    # --- Unlock any stuck draws and start watcher ---
    with db() as conn:
        conn.execute("UPDATE giveaways SET status='OPEN' WHERE status='DRAWING'")

    try:
        await tree.sync(guild=None)
        for g in bot.guilds:
            await tree.sync(guild=g)
        print("Slash commands synced.")
    except Exception as e:
        print("Slash sync failed:", e)

    if not giveaway_watcher.is_running():
        giveaway_watcher.start()

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")



bot.run(TOKEN)
