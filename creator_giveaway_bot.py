# creator_giveaway_bot.py — Daily IMVU Giveaway (username only, creator-aware)
# pip install -U discord.py aiohttp

import os
import re
import json
import random
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List, Dict

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
import urllib.parse, re

USERNAME_FROM_LINK = re.compile(
    r"/people/([^/]+)/|[?&]user=([^&/#]+)", re.I
)

def normalize_username(raw: str) -> str:
    s = raw.strip()
    if s.startswith("http"):
        m = USERNAME_FROM_LINK.search(s)
        if m:
            u = m.group(1) or m.group(2)
            return urllib.parse.unquote(u)
        # Links like /next/av/<id> are avatar IDs, not usernames → skip
        return ""
    return s

# ==============================
# Env / Defaults
# ==============================
TOKEN = os.getenv("DISCORD_TOKEN") or ""
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN")

DB_PATH = os.getenv("DB_PATH", "imvu_creator_giveaway.db")
ANNOUNCE_CHANNEL_ID = int(os.getenv("GIVEAWAY_CHANNEL_ID", "0"))

# Eligibility rules
MIN_TOTAL_WISHLIST_ITEMS = int(os.getenv("MIN_TOTAL_WISHLIST_ITEMS", "10"))  # total items on wishlist
RULE_MODE = os.getenv("RULE_MODE", "NONE").upper()  # NONE | ANY | EACH | MAP
RULE_THRESHOLD = int(os.getenv("RULE_THRESHOLD", "10"))  # for ANY/EACH
RULE_MAP_JSON = os.getenv("RULE_MAP_JSON", "{}")  # for MAP, e.g. {"360644281":5,"123456":3}

# Draw schedule
DRAW_HOUR_LOCAL = int(os.getenv("DRAW_HOUR_LOCAL", "18"))  # 0-23 (Asia/Qatar)
WIN_COOLDOWN_DAYS = int(os.getenv("WIN_COOLDOWN_DAYS", "7"))

# Scraping / performance
PRODUCT_SAMPLE_LIMIT = int(os.getenv("PRODUCT_SAMPLE_LIMIT", "60"))   # check up to N wishlist items
PRODUCT_CONCURRENCY = int(os.getenv("PRODUCT_CONCURRENCY", "4"))     # concurrent product fetches
PRODUCT_CACHE_TTL_HOURS = int(os.getenv("PRODUCT_CACHE_TTL_HOURS", "168"))  # 7 days

# Timezone
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Qatar"))
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=3))  # Fallback UTC+3

INTENTS = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree

# ==============================
# DB
# ==============================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS entrants(
            discord_id TEXT PRIMARY KEY,
            username   TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_checked_at TEXT,
            total_items INTEGER DEFAULT 0,
            eligible   INTEGER DEFAULT 0,
            last_win_at TEXT
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS creators(
            creator_id TEXT PRIMARY KEY,     -- IMVU manufacturer ID (string)
            label      TEXT                  -- optional friendly name
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS rules(
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS cache_products(
            product_id TEXT PRIMARY KEY,
            creator_id TEXT,
            fetched_at TEXT
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS meta(
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('last_draw_date','');")

        # seed defaults if absent
        for k, v in [
            ("mode", RULE_MODE),
            ("threshold", str(RULE_THRESHOLD)),
            ("min_total", str(MIN_TOTAL_WISHLIST_ITEMS)),
            ("map_json", RULE_MAP_JSON),
        ]:
            conn.execute("INSERT OR IGNORE INTO rules(key,value) VALUES(?,?)", (k, v))

def set_rule(key: str, value: str):
    with db() as conn:
        conn.execute("INSERT INTO rules(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value;", (key, value))

def get_rules() -> Dict[str, str]:
    with db() as conn:
        cur = conn.execute("SELECT key,value FROM rules;")
        return {k: v for (k, v) in cur.fetchall()}

def add_creator(creator_id: str, label: Optional[str]):
    with db() as conn:
        conn.execute("INSERT INTO creators(creator_id,label) VALUES(?,?) ON CONFLICT(creator_id) DO UPDATE SET label=excluded.label;", (creator_id, label))

def remove_creator(creator_id: str):
    with db() as conn:
        conn.execute("DELETE FROM creators WHERE creator_id=?", (creator_id,))

def list_creators() -> List[tuple]:
    with db() as conn:
        cur = conn.execute("SELECT creator_id,label FROM creators ORDER BY creator_id;")
        return cur.fetchall()

def set_meta(key: str, value: str):
    with db() as conn:
        conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value;", (key, value))

def get_meta(key: str, default: str = "") -> str:
    with db() as conn:
        cur = conn.execute("SELECT value FROM meta WHERE key=?;", (key,))
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else default

def upsert_entrant(discord_id: int, username: str, total_items: int = 0, eligible: int = 0):
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute("""
        INSERT INTO entrants(discord_id, username, created_at, last_checked_at, total_items, eligible)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(discord_id) DO UPDATE SET
            username=excluded.username,
            last_checked_at=excluded.last_checked_at,
            total_items=excluded.total_items,
            eligible=excluded.eligible
        """, (str(discord_id), username, now, now, total_items, eligible))

def set_winner(discord_id: int):
    with db() as conn:
        conn.execute("UPDATE entrants SET last_win_at=? WHERE discord_id=?;", (datetime.now(timezone.utc).isoformat(), str(discord_id)))

def all_entrants() -> List[tuple]:
    with db() as conn:
        cur = conn.execute("SELECT discord_id, username, total_items, eligible, last_win_at FROM entrants;")
        return cur.fetchall()

def cache_get(product_id: str) -> Optional[str]:
    with db() as conn:
        cur = conn.execute("SELECT creator_id, fetched_at FROM cache_products WHERE product_id=?", (product_id,))
        row = cur.fetchone()
        if not row:
            return None
        creator_id, fetched_at = row
        try:
            ts = datetime.fromisoformat(fetched_at.replace("Z","")).replace(tzinfo=timezone.utc)
        except Exception:
            ts = datetime.now(timezone.utc) - timedelta(days=3650)
        if datetime.now(timezone.utc) - ts > timedelta(hours=PRODUCT_CACHE_TTL_HOURS):
            return None
        return creator_id

def cache_put(product_id: str, creator_id: Optional[str]):
    with db() as conn:
        conn.execute("INSERT INTO cache_products(product_id,creator_id,fetched_at) VALUES(?,?,?) ON CONFLICT(product_id) DO UPDATE SET creator_id=excluded.creator_id, fetched_at=excluded.fetched_at;", (product_id, creator_id or "", datetime.now(timezone.utc).isoformat()))

# ==============================
# HTTP / Scraping (best-effort)
# ==============================
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
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

# before
# PRODUCT_LINK_RX = re.compile(r'/shop/product(?:\.php\?products_id=|/)(\d+)', re.I)

# after (handles both forms and some variants)
PRODUCT_LINK_RX = re.compile(
    r'/shop/product(?:\.php\?products_id=|/)(\d+)|data-product-id=["\'](\d+)["\']',
    re.I
)


async def _fetch_html(url: str, session: aiohttp.ClientSession, min_len=3500) -> Optional[str]:
    try:
        async with session.get(url, allow_redirects=True) as r:
            if r.status != 200:
                return None
            text = await r.text(errors="ignore")
            if len(text) < min_len:
                return None
            return text
    except Exception:
        return None

def _extract_wishlist_links_from_profile(html: str) -> List[str]:
    out = []
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I):
        href = m.group(1)
        if "wish" not in href.lower():
            continue
        if href.startswith("//"):
            href = "https:" + href
        if href.startswith("/"):
            href = "https://www.imvu.com" + href
        if "imvu.com" in href:
            out.append(href)
    # dedupe keep order
    seen = set()
    res = []
    for u in out:
        if u not in seen:
            seen.add(u)
            res.append(u)
    return res
    # after: return (wl, pids[:sample_limit])
async def wishlist_url_and_products(username: str, sample_limit: int = PRODUCT_SAMPLE_LIMIT) -> Tuple[Optional[str], List[str]]:
    uname = username.strip()
    if not uname:
        return (None, [])
    timeout = aiohttp.ClientTimeout(total=12, connect=10)
    async with aiohttp.ClientSession(timeout=timeout, headers=DEFAULT_HEADERS) as s:
        # 1) PROFILE → discover wishlist links
        for tmpl in PROFILE_CANDIDATES:
            purl = tmpl.format(username=uname)
            html = await _fetch_html(purl, s)
            if not html:
                continue
            for wl in _extract_wishlist_links_from_profile(html):
                whtml = await _fetch_html(wl, s)
                if not whtml:
                    continue
                pids = _product_ids_from_html(whtml)
                # ✅ wl exists here
                print(f"[WISH] profile candidate OK: {wl} | items={len(pids)} | user={uname}")
                if pids:
                    return (wl, pids[:sample_limit])

        # 2) FALLBACK → guessed wishlist URLs
        for tmpl in WISHLIST_CANDIDATES:
            wurl = tmpl.format(username=uname)
            whtml = await _fetch_html(wurl, s)
            if not whtml:
                continue
            pids = _product_ids_from_html(whtml)
            # ✅ wurl exists here
            print(f"[WISH] fallback tried: {wurl} | items={len(pids)} | user={uname}")
            if pids:
                return (wurl, pids[:sample_limit])

    print(f"[WISH] no wishlist found | user={uname}")
    return (None, [])



def _product_ids_from_html(html: str) -> List[str]:
    ids = []
    for m in PRODUCT_LINK_RX.finditer(html):
        pid = m.group(1) or m.group(2)
        if pid:
            ids.append(pid)
    # de-dupe preserving order
    seen, res = set(), []
    for pid in ids:
        if pid not in seen:
            seen.add(pid)
            res.append(pid)
    return res


async def wishlist_url_and_products(username: str, sample_limit: int = PRODUCT_SAMPLE_LIMIT) -> Tuple[Optional[str], List[str]]:
    uname = username.strip()
    if not uname:
        return (None, [])
    timeout = aiohttp.ClientTimeout(total=12, connect=10)
    async with aiohttp.ClientSession(timeout=timeout, headers=DEFAULT_HEADERS) as s:
        # 1) Profile → find wishlist link(s)
        for tmpl in PROFILE_CANDIDATES:
            purl = tmpl.format(username=uname)
            html = await _fetch_html(purl, s)
            if not html:
                continue
            for wl in _extract_wishlist_links_from_profile(html):
                whtml = await _fetch_html(wl, s)
                if not whtml:
                    continue
                pids = _product_ids_from_html(whtml)
                if pids:
                    return (wl, pids[:sample_limit])
        # 2) Fallback: guess wishlist URLs
        for tmpl in WISHLIST_CANDIDATES:
            wl = tmpl.format(username=uname)
            whtml = await _fetch_html(wl, s)
            if not whtml:
                continue
            pids = _product_ids_from_html(whtml)
            if pids:
                return (wl, pids[:sample_limit])
    return (None, [])

async def product_creator_id(session: aiohttp.ClientSession, product_id: str, sem: asyncio.Semaphore) -> Optional[str]:
    # cache first
    cached = cache_get(product_id)
    if cached is not None and cached != "":
        return cached if cached else None

    urls = [
        f"https://www.imvu.com/shop/product/{product_id}",
        f"https://www.imvu.com/shop/product.php?products_id={product_id}",
    ]
    async with sem:
        for url in urls:
            html = await _fetch_html(url, session)
            if not html:
                continue
            m = MANUFACTURER_RX.search(html)
            if m:
                cid = m.group(1)
                cache_put(product_id, cid)
                return cid
    cache_put(product_id, None)
    return None

# ==============================
# Eligibility Evaluation
# ==============================
def _recent_win_cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=WIN_COOLDOWN_DAYS)

async def evaluate_user(username: str) -> Tuple[int, Dict[str,int]]:
    """Return (total_items_found, counts_per_creator) using sampled wishlist products."""
    wl_url, product_ids = await wishlist_url_and_products(username)
    if not wl_url:
        return (0, {})
    if not product_ids:
        return (0, {})

    timeout = aiohttp.ClientTimeout(total=25, connect=10)
    async with aiohttp.ClientSession(timeout=timeout, headers=DEFAULT_HEADERS) as s:
        sem = asyncio.Semaphore(PRODUCT_CONCURRENCY)
        tasks = [product_creator_id(s, pid, sem) for pid in product_ids]
        creators = await asyncio.gather(*tasks)

    per_creator: Dict[str, int] = {}
    for cid in creators:
        if not cid:
            continue
        per_creator[cid] = per_creator.get(cid, 0) + 1

    total = len(product_ids)  # sampled total; fine for threshold checks
    return (total, per_creator)

def _eligible_by_creator_rule(per_creator: Dict[str,int], rules: Dict[str,str], allowed_creators: List[str]) -> bool:
    mode = rules.get("mode", "NONE").upper()
    if mode == "NONE" or not allowed_creators:
        return True

    allowed_set = set(allowed_creators)
    if mode in ("ANY", "EACH"):
        thr = max(1, int(rules.get("threshold", "1")))
        if mode == "ANY":
            return any(per_creator.get(cid, 0) >= thr for cid in allowed_set)
        else:  # EACH
            return all(per_creator.get(cid, 0) >= thr for cid in allowed_set)

    if mode == "MAP":
        try:
            req = json.loads(rules.get("map_json", "{}"))
        except Exception:
            req = {}
        if not req:
            return True
        # only consider creators that are in the map (acts as allowlist)
        for cid, need in req.items():
            if per_creator.get(str(cid), 0) < int(need):
                return False
        return True

    return True

async def refresh_and_collect_eligibles() -> List[int]:
    rows = all_entrants()
    rules = get_rules()
    allowed = [cid for (cid, _) in list_creators()]

    elig_ids: List[int] = []
    for discord_id, username, _, _, last_win_at in rows:
        total, per_creator = await evaluate_user(username)

        eligible_total = total >= int(rules.get("min_total", str(MIN_TOTAL_WISHLIST_ITEMS)))
        eligible_creator = _eligible_by_creator_rule(per_creator, rules, allowed)
        eligible = int(eligible_total and eligible_creator)

        upsert_entrant(int(discord_id), username, total, eligible)

        if eligible:
            # apply recent win cooldown
            if last_win_at:
                try:
                    lw = datetime.fromisoformat(last_win_at.replace("Z","")).replace(tzinfo=timezone.utc)
                except Exception:
                    lw = datetime.now(timezone.utc) - timedelta(days=9999)
                if lw > _recent_win_cutoff():
                    await asyncio.sleep(0.15)
                    continue
            elig_ids.append(int(discord_id))

        await asyncio.sleep(0.15)  # be polite
    return elig_ids

async def run_daily_draw() -> Optional[str]:
    elig_ids = await refresh_and_collect_eligibles()
    if not elig_ids:
        return "No eligible entrants today (wishlist/private/threshold not met)."

    winner_id = random.choice(elig_ids)
    set_winner(winner_id)

    rules = get_rules()
    creators_txt = ", ".join([f"{cid} ({lbl})" if lbl else cid for cid, lbl in list_creators()]) or "—"
    text = (
        f"🎉 **Daily IMVU Giveaway Winner**\n"
        f"Congrats <@{winner_id}>!\n\n"
        f"• Min total items: **{rules.get('min_total','')}**\n"
        f"• Creator rule: **{rules.get('mode','NONE').upper()}** "
        f"{'(threshold '+rules.get('threshold','')+')' if rules.get('mode','NONE').upper() in ('ANY','EACH') else ''}\n"
        f"• Allowed creators: {creators_txt}"
    )

    channel = bot.get_channel(ANNOUNCE_CHANNEL_ID) if ANNOUNCE_CHANNEL_ID else None
    if channel:
        try:
            await channel.send(text)
            return text
        except Exception:
            pass

    # Fallback: send to first text channel we can post in
    for g in bot.guilds:
        for ch in g.text_channels:
            try:
                await ch.send(text)
                return text
            except Exception:
                continue
    return text

# ==============================
# Permissions helper
# ==============================
def is_admin(inter: discord.Interaction) -> bool:
    return bool(inter.user.guild_permissions.administrator)

# ==============================
# Slash Commands
# ==============================
@tree.command(name="enter", description="Enter the giveaway with your IMVU username")
async def enter(interaction: discord.Interaction, username: str):
    uname = normalize_username(username)
    if not uname:
        return await interaction.response.send_message(
            "⚠️ Please enter your **IMVU username** (not a link). Example: `/enter mikeymoon`",
            ephemeral=True
        )
    await interaction.response.defer(ephemeral=True, thinking=True)
    total, _ = await evaluate_user(uname)
    eligible = int(total >= int(get_rules().get("min_total", str(MIN_TOTAL_WISHLIST_ITEMS))))
    upsert_entrant(interaction.user.id, uname, total, eligible)
    
    if total == 0:
        await interaction.followup.send(
            f"Saved **{username}**. I couldn't find a public wishlist or items yet — "
            f"you can still stay entered; I’ll re-check before each draw."
        )
    else:
        await interaction.followup.send(
            f"Registered **{username}** — detected **{total}** wishlist items (sampled). "
            f"I’ll check creator rules & thresholds at draw time."
        )

@tree.command(description="Leave the giveaway.")
async def leave(interaction: discord.Interaction):
    with db() as conn:
        conn.execute("DELETE FROM entrants WHERE discord_id=?", (str(interaction.user.id),))
    await interaction.response.send_message("You’ve been removed from the daily giveaway.", ephemeral=True)

@tree.command(description="Show entrant stats.")
async def entrants(interaction: discord.Interaction):
    rows = all_entrants()
    total = len(rows)
    eligible = sum(1 for r in rows if r[3] == 1)
    await interaction.response.send_message(
        f"Entrants: **{total}** | Eligible now: **{eligible}** | Min total: **{get_rules().get('min_total')}**",
        ephemeral=True
    )

# ----- Admin: creators -----
@tree.command(description="(Admin) Add an allowed creator by ID or shop URL.")
@app_commands.describe(id_or_url="Manufacturers ID (digits) OR a shop URL containing manufacturers_id=...")
@app_commands.describe(label="Optional friendly label")
async def creator_add(interaction: discord.Interaction, id_or_url: str, label: Optional[str] = None):
    if not is_admin(interaction):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    m = re.search(r'(\d+)', id_or_url)
    if not m:
        return await interaction.response.send_message("Couldn’t find a numeric manufacturer ID.", ephemeral=True)
    cid = m.group(1)
    add_creator(cid, label)
    await interaction.response.send_message(f"Added creator **{cid}**{(' ('+label+')') if label else ''}.", ephemeral=True)

@tree.command(description="(Admin) Remove an allowed creator.")
async def creator_remove(interaction: discord.Interaction, creator_id: str):
    if not is_admin(interaction):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    remove_creator(creator_id)
    await interaction.response.send_message(f"Removed creator **{creator_id}**.", ephemeral=True)

@tree.command(description="List allowed creators.")
async def creator_list(interaction: discord.Interaction):
    items = list_creators()
    if not items:
        return await interaction.response.send_message("No creators configured. Creator rule will be skipped.", ephemeral=True)
    lines = [f"• {cid}" + (f" — {lbl}" if lbl else "") for cid, lbl in items]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

# ----- Admin: rules -----
@tree.command(description="(Admin) Set creator rule mode: NONE, ANY, EACH, MAP.")
async def rule_mode(interaction: discord.Interaction, mode: str):
    if not is_admin(interaction):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    mode = mode.upper()
    if mode not in ("NONE", "ANY", "EACH", "MAP"):
        return await interaction.response.send_message("Mode must be NONE, ANY, EACH, or MAP.", ephemeral=True)
    set_rule("mode", mode)
    await interaction.response.send_message(f"Rule mode set to **{mode}**.", ephemeral=True)

@tree.command(description="(Admin) Set threshold for ANY/EACH.")
async def rule_set_threshold(interaction: discord.Interaction, threshold: int):
    if not is_admin(interaction):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    set_rule("threshold", str(max(1, threshold)))
    await interaction.response.send_message(f"Threshold set to **{threshold}**.", ephemeral=True)

@tree.command(description="(Admin) Set MAP rule as JSON: {\"creatorId\": minCount, ...}")
@app_commands.describe(map_json='Example: {"360644281":5,"123456":2}')
async def rule_set_map(interaction: discord.Interaction, map_json: str):
    if not is_admin(interaction):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    try:
        json.loads(map_json)
    except Exception as e:
        return await interaction.response.send_message(f"Invalid JSON: {e}", ephemeral=True)
    set_rule("map_json", map_json)
    await interaction.response.send_message("MAP rule updated.", ephemeral=True)

@tree.command(description="(Admin) Set minimum total wishlist items.")
async def min_total(interaction: discord.Interaction, n: int):
    if not is_admin(interaction):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    set_rule("min_total", str(max(1, n)))
    await interaction.response.send_message(f"Minimum total wishlist items set to **{n}**.", ephemeral=True)

@tree.command(description="Show current settings.")
async def settings(interaction: discord.Interaction):
    r = get_rules()
    creators_txt = ", ".join([f"{cid} ({lbl})" if lbl else cid for cid, lbl in list_creators()]) or "—"
    msg = (
        f"Mode: **{r.get('mode')}** | Threshold: **{r.get('threshold')}**\n"
        f"Min total: **{r.get('min_total')}**\n"
        f"MAP: `{r.get('map_json')}`\n"
        f"Creators: {creators_txt}\n"
        f"Draw hour (local): **{DRAW_HOUR_LOCAL}:00** | Cooldown: **{WIN_COOLDOWN_DAYS}d**"
    )
    await interaction.response.send_message(msg, ephemeral=True)

# ----- Admin: manual draw / verify -----
@tree.command(description="(Admin) Draw a winner now.")
async def draw_now(interaction: discord.Interaction):
    if not is_admin(interaction):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    msg = await run_daily_draw()
    await interaction.followup.send(msg or "Done.", ephemeral=True)

@tree.command(description="(Admin) Re-check a user's eligibility by username.")
async def verify_username(interaction: discord.Interaction, username: str):
    if not is_admin(interaction):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    total, per_creator = await evaluate_user(username)
    upsert_entrant(interaction.user.id, username, total, 0)
    await interaction.followup.send(f"Checked **{username}** → total items (sampled): **{total}**, per-creator: `{per_creator}`", ephemeral=True)

# ==============================
# Scheduler
# ==============================
@tasks.loop(minutes=1)
async def daily_scheduler():
    now_local = datetime.now(LOCAL_TZ)
    today = now_local.date().isoformat()
    last = get_meta("last_draw_date", "")
    if last == today:
        return
    if now_local.hour == DRAW_HOUR_LOCAL and now_local.minute == 0:
        msg = await run_daily_draw()
        set_meta("last_draw_date", today)
        print("[Daily Draw]", msg)

@daily_scheduler.before_loop
async def before_loop():
    await bot.wait_until_ready()
    await asyncio.sleep(2)

# ==============================
# Lifecycle
# ==============================
@bot.event
async def on_ready():
    init_db()
    try:
        await tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print("Slash sync failed:", e)
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if not daily_scheduler.is_running():
        daily_scheduler.start()

bot.run(TOKEN)
