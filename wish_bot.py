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

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Qatar"))
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=3))  # fallback

INTENTS = discord.Intents.default()
INTENTS.message_content = True   # allow reading normal messages (and silences the warning)
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

# PATCH: atomically claim a giveaway so only one loop processes it
def giveaway_claim(gid: int) -> bool:
    with db() as conn:
        cur = conn.execute(
            "UPDATE giveaways SET status='DRAWING' WHERE id=? AND status='OPEN'",
            (gid,)
        )
        return cur.rowcount > 0

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

# PATCH: helper to get unique entrant user IDs for a giveaway
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

# =========================
# Scraping & eligibility (left intact; not used for entry anymore)
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

async def wishlist_url_and_products(username: str, sample_limit: int = PRODUCT_SAMPLE_LIMIT) -> Tuple[Optional[str], List[str]]:
    uname = username.strip()
    if not uname:
        return (None, [])
    timeout = aiohttp.ClientTimeout(total=12, connect=10)
    async with aiohttp.ClientSession(timeout=timeout, headers=DEFAULT_HEADERS) as s:
        # via profile
        for tmpl in PROFILE_CANDIDATES:
            purl = tmpl.format(username=uname)
            html = await _fetch_html(purl, s)
            if not html: continue
            for wl in _extract_wishlist_links_from_profile(html):
                whtml = await _fetch_html(wl, s)
                if not whtml: continue
                pids = _product_ids_from_html(whtml)
                if pids:
                    return (wl, pids[:sample_limit])
        # direct guesses
        for tmpl in WISHLIST_CANDIDATES:
            wurl = tmpl.format(username=uname)
            whtml = await _fetch_html(wurl, s)
            if not whtml: continue
            pids = _product_ids_from_html(whtml)
            if pids:
                return (wurl, pids[:sample_limit])
    return (None, [])

async def product_creator_id(session: aiohttp.ClientSession, product_id: str, sem: asyncio.Semaphore) -> Optional[str]:
    cached = cache_get(product_id)
    if cached is not None:
        return cached or None
    urls = [
        f"https://www.imvu.com/shop/product/{product_id}",
        f"https://www.imvu.com/shop/product.php?products_id={product_id}",
    ]
    async with sem:
        for url in urls:
            html = await _fetch_html(url, session)
            if not html: continue
            m = MANUFACTURER_RX.search(html)
            if m:
                cid = m.group(1)
                cache_put(product_id, cid)
                return cid
    cache_put(product_id, None)
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
    with db() as conn:
        conn.execute(
            "INSERT INTO cache_products(product_id,creator_id,fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(product_id) DO UPDATE SET creator_id=excluded.creator_id, fetched_at=excluded.fetched_at;",
            (product_id, creator_id or "", datetime.now(timezone.utc).isoformat())
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
        # EACH
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
        return ""  # /next/av/... not supported
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
            # fallback: open first product from that manufacturer
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

        # one entry per user per giveaway
        with db() as conn:
            cur = conn.execute("SELECT 1 FROM giveaway_entries WHERE giveaway_id=? AND discord_id=?",
                               (gid, str(interaction.user.id)))
            if cur.fetchone():
                return await interaction.response.send_message("You’ve already entered this giveaway ✅", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Trust entrant input; no wishlist scraping
        first_ok = ids[0]
        try:
            giveaway_add_entry(gid, interaction.user.id, uname, first_ok)
        except sqlite3.IntegrityError:
            return await interaction.followup.send("You’re already entered ✅", ephemeral=True)

        # ensure participant row exists so cooldown works
        upsert_entrant(interaction.user.id, uname, total_items=0, eligible=1)

        await update_giveaway_counter_embed(gid)
        await interaction.followup.send(f"✅ Entered as **{uname}** (saved product **{first_ok}**).", ephemeral=True)

class EnterButton(ui.View):
    def __init__(self, giveaway_id: int, disabled: bool = False, timeout=None):
        super().__init__(timeout=timeout)
        self.gid = giveaway_id
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
        new.add_field(name="Participants", value=str(count), inline=True)  # PATCH: show real count on first update
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
    duration = ui.TextInput(label="Duration", placeholder="24h, 3d, 45m, 1w", required=True)
    winners  = ui.TextInput(label="Number of Winners", placeholder="1", required=True, max_length=4)
    prize    = ui.TextInput(label="Prize", placeholder="Text or URL", required=True)
    announce = ui.TextInput(label="Announcement text (you can @Role here)",
                            style=discord.TextStyle.paragraph, required=False)
    shops    = ui.TextInput(label="Shops (IDs or shop URLs, comma/lines)",
                            style=discord.TextStyle.paragraph, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            secs = parse_duration_to_seconds(str(self.duration))
            winners = max(1, int(str(self.winners)))
        except Exception as e:
            return await interaction.response.send_message(f"Invalid: {e}", ephemeral=True)

        prize = str(self.prize).strip()
        announce = str(self.announce or "").strip()

        # PATCH: resolve manufacturer IDs in "shops" to human-readable names
        ids = re.findall(r'(\d{5,})', str(self.shops or ""))
        unique_ids = list(dict.fromkeys(ids))
        creator_names: List[str] = []
        for cid in unique_ids:
            add_creator(cid, None)  # ensure row exists
            try:
                name = await fetch_creator_name(cid)
            except Exception:
                name = None
            if name:
                add_creator(cid, name)           # store label for future
                creator_names.append(name)
            else:
                creator_names.append(cid)        # graceful fallback

        end_at_utc = datetime.now(timezone.utc) + timedelta(seconds=secs)
        gid = giveaway_insert(interaction.channel.id, prize, announce, winners, end_at_utc.isoformat(), interaction.user.id)

        end_rel = discord.utils.format_dt(end_at_utc, style='R')
        creators_txt = ", ".join(creator_names) if creator_names else "—"

        desc = ""
        if announce:
            desc += announce + "\n\n"
        desc += (f"**Prize:** {prize}\n"
                 f"**Winners:** {winners}\n"
                 f"**Ends:** {end_rel}\n\n"
                 f"**Today we support:** {creators_txt}\n\n"
                 f"**How to join**\n"
                 f" Hit that **Enter Giveaway** button, drop your **IMVU username**, and follow steps **\n"
                 f" or you're just window shopping. ")

        embed = discord.Embed(title="⚡ WISH — Giveaway", description=desc, color=discord.Color.gold())
        embed.set_footer(text="Your name’s in, but without a WL it’s out. No WL, no win.")
        embed.add_field(name="Participants", value="0", inline=True)

        await interaction.response.defer(thinking=True)
        msg = await interaction.channel.send(embed=embed, view=EnterButton(gid))
        giveaway_set_message(gid, msg.id)
        await interaction.followup.send(f"Giveaway posted ✅ (ID {gid})", ephemeral=True)

@tree.command(name="wish", description="Create a WISH giveaway (admin only).")
async def wish_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    await interaction.response.send_modal(WishSingle())

# =========================
# Reroll
# =========================
@tree.command(name="reroll", description="Admin: reroll winner(s) for a past giveaway.")
@app_commands.describe(giveaway_id="Giveaway ID (see bot message or DB)", count="How many new winners (default 1)")
async def reroll_cmd(interaction: discord.Interaction, giveaway_id: int, count: int = 1):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admins only.", ephemeral=True)

    # Look up giveaway channel to announce reroll there
    with db() as conn:
        row = conn.execute("SELECT channel_id, prize FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
    if not row:
        return await interaction.response.send_message("Unknown giveaway ID.", ephemeral=True)
    ch_id, prize = int(row[0]), row[1]
    channel = bot.get_channel(ch_id)
    if not channel:
        return await interaction.response.send_message("I can't see that channel anymore.", ephemeral=True)

    # Build a pool: users who actually entered that giveaway, excluding previous winners of the SAME giveaway
    entries = giveaway_entry_user_ids(giveaway_id)
    already = set(list_giveaway_winners(giveaway_id))
    pool = [u for u in entries if u not in already]
    if not pool:
        return await interaction.response.send_message("No remaining entrants to reroll from.", ephemeral=True)

    random.shuffle(pool)
    picks = pool[:max(1, int(count))]

    # Announce & store
    lines = "\n".join(f"• <@{u}>" for u in picks)
    text = f"🔁 **REROLL** for Giveaway #{giveaway_id}\n**Prize:** {prize}\n**New winner{'s' if len(picks)!=1 else ''}:**\n{lines}"
    try:
        await channel.send(text)
    except Exception:
        pass

    for uid in picks:
        set_winner(uid)
        add_giveaway_winner(giveaway_id, uid)

    await interaction.response.send_message(f"Rerolled ✅ Picked {len(picks)} new winner(s).", ephemeral=True)

# =========================
# Draw/close watcher
# =========================
@tasks.loop(seconds=30)
async def giveaway_watcher():
    now = datetime.now(timezone.utc)
    with db() as conn:
        cur = conn.execute(
            "SELECT id, channel_id, message_id, winners, prize FROM giveaways "
            "WHERE status='OPEN' AND end_at <= ?", (now.isoformat(),))
        due = cur.fetchall()
    if not due:
        return

    for gid, ch_id, msg_id, winners, prize in due:
        # PATCH: claim so only one loop/worker processes it
        if not giveaway_claim(gid):
            continue

        channel = bot.get_channel(int(ch_id))

        # disable button
        try:
            msg = await channel.fetch_message(int(msg_id))
            await msg.edit(view=EnterButton(gid, disabled=True))
        except Exception:
            pass

        # Pull entries for THIS giveaway
        entries = giveaway_entry_user_ids(gid)
        winners_n = max(1, int(winners))

        picks: List[int] = []
        pool = list(entries)
        random.shuffle(pool)

        # Try to respect cooldown
        if pool and WIN_COOLDOWN_DAYS > 0:
            with db() as conn:
                for uid in pool:
                    row = conn.execute("SELECT last_win_at FROM Participants WHERE discord_id=?", (str(uid),)).fetchone()
                    if not row or not row[0]:
                        picks.append(uid)
                    else:
                        try:
                            lw = datetime.fromisoformat(row[0].replace("Z","")).replace(tzinfo=timezone.utc)
                        except:
                            lw = datetime.now(timezone.utc) - timedelta(days=9999)
                        if lw <= datetime.now(timezone.utc) - timedelta(days=WIN_COOLDOWN_DAYS):
                            picks.append(uid)
                    if len(picks) >= winners_n:
                        break

        # Fallback — if cooldown excluded everyone, still pick from entries
        if len(picks) < winners_n and pool:
            remaining = [u for u in pool if u not in picks]
            while remaining and len(picks) < winners_n:
                picks.append(remaining.pop())

        mention_line = "No entries 😔" if not pool else (
            "We checked twice… still nada. 😔" if not picks else "\n".join(f"• <@{uid}>" for uid in picks)
        )
        text = f" **WISH Giveaway Ended**\n**Prize:** {prize}\n**Winner{'s' if winners_n!=1 else ''}:**\n{mention_line}"

        try:
            await channel.send(text)
        except Exception:
            pass

        for uid in picks:
            set_winner(uid)
            add_giveaway_winner(gid, uid)  # log winner for rerolls
        
        giveaway_mark_done(gid)

# =========================
# Optional: Product image helper (not wired to embed by default)
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

# =========================
# Startup
# =========================
@bot.event
async def on_ready():
    init_db()
    try:
        # Register commands globally…
        await tree.sync(guild=None)
        # …and per guild for instant visibility
        for g in bot.guilds:
            await tree.sync(guild=g)
        print("Slash commands synced.")
    except Exception as e:
        print("Slash sync failed:", e)

    if not giveaway_watcher.is_running():
        giveaway_watcher.start()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

bot.run(TOKEN)
