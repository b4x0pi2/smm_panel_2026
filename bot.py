import asyncio
import html
import io
import json
import logging
import os
import sqlite3
import time

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =============================== CONFIG ======================================
BOT_TOKEN = os.environ.get("9292451037:AAHdJkxyPW_3ubRLBiMPAgKwRLp3EoXvUeg", "").strip()
if ":" not in BOT_TOKEN:
    raise SystemExit("BOT_TOKEN variable missing hai. Railway > Variables mein BOT_TOKEN add karein.")
ADMIN_IDS = {int(x) for x in os.getenv("8556289078", "").replace(" ", "").split(",") if x.isdigit()}
CUR = os.getenv("CURRENCY", "Rs")
SUPPORT = os.getenv("SUPPORT_USERNAME", "@the_babarrrr")
DB_PATH = os.getenv("DB_PATH", "bot.db")
MIN_DEP = float(os.getenv("MIN_DEPOSIT", "1"))
MAX_DEP = float(os.getenv("MAX_DEPOSIT", "100000"))
PER_PAGE = 10
HTML = ParseMode.HTML

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("smmbot")
if not ADMIN_IDS:
    log.warning("ADMIN_IDS khali hai! Admin features kaam nahi karenge.")

EMOJI = {
    "instagram": "📸", "telegram": "✈️", "youtube": "▶️", "snapchat": "👻",
    "whatsapp": "💬", "tiktok": "🎵", "facebook": "📘", "twitter": "🐦",
    "x": "🐦", "spotify": "🎧", "linkedin": "💼", "pinterest": "📌",
}

# =============================== DATABASE ====================================
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, name TEXT,
  balance REAL DEFAULT 0, banned INTEGER DEFAULT 0, joined INTEGER);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, category TEXT,
  service TEXT, server TEXT, price REAL, link TEXT, qty INTEGER,
  charge REAL, status TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS deposits(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
  file_id TEXT, kind TEXT, status TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
"""
)
db.commit()


def one(sql, args=()):
    return db.execute(sql, args).fetchone()


def get_setting(k):
    r = one("SELECT v FROM settings WHERE k=?", (k,))
    return r["v"] if r else None


def set_setting(k, v):
    db.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    db.commit()


def ensure_user(u):
    row = one("SELECT * FROM users WHERE id=?", (u.id,))
    if not row:
        db.execute(
            "INSERT INTO users(id,username,name,joined) VALUES(?,?,?,?)",
            (u.id, u.username, u.full_name, int(time.time())),
        )
        db.commit()
        row = one("SELECT * FROM users WHERE id=?", (u.id,))
    elif row["username"] != u.username or row["name"] != u.full_name:
        db.execute("UPDATE users SET username=?, name=? WHERE id=?", (u.username, u.full_name, u.id))
        db.commit()
        row = one("SELECT * FROM users WHERE id=?", (u.id,))
    return row


def add_balance(uid, amount):
    db.execute("UPDATE users SET balance=MAX(balance+?,0) WHERE id=?", (amount, uid))
    db.commit()


def load_services():
    raw = get_setting("services")
    return json.loads(raw) if raw else {}


# ================================ HELPERS ====================================
def esc(s):
    return html.escape(str(s))


def money(x):
    s = f"{float(x):,.2f}"
    return s[:-3] if s.endswith(".00") else s


def fm(x):
    return f"{CUR} {money(x)}"


def emoji(cat):
    return EMOJI.get(cat.strip().lower(), "🔹")


def is_admin(uid):
    return uid in ADMIN_IDS


def reset(ctx):
    ctx.user_data.pop("flow", None)
    ctx.user_data.pop("await", None)


def main_menu(uid):
    rows = [
        [Btn("🛒 Services & Order", callback_data="shop")],
        [Btn("💰 Add Funds", callback_data="deposit"), Btn("👛 Balance", callback_data="balance")],
        [Btn("📦 My Orders", callback_data="orders"), Btn("🆘 Support", callback_data="support")],
    ]
    if is_admin(uid):
        rows.append([Btn("👑 Admin Panel", callback_data="ad:home")])
    return Markup(rows)


def menu_only():
    return Markup([[Btn("⬅️ Main Menu", callback_data="menu")]])


def cancel_only():
    return Markup([[Btn("❌ Cancel", callback_data="menu")]])


async def show(q, text, markup=None):
    """Edit message; agar edit na ho sake (photo msg etc.) to naya message bhejo."""
    try:
        await q.edit_message_text(text, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        await q.message.reply_text(text, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True)


async def show_caption(q, text):
    try:
        await q.edit_message_caption(caption=text, parse_mode=HTML, reply_markup=None)
    except BadRequest:
        pass


async def tell(bot, uid, text, markup=None):
    try:
        await bot.send_message(uid, text, parse_mode=HTML, reply_markup=markup)
    except TelegramError as e:
        log.warning("user %s ko msg nahi gaya: %s", uid, e)


async def notify_admins(bot, text, markup=None, file_id=None, kind="photo"):
    for a in ADMIN_IDS:
        try:
            if file_id and kind == "document":
                await bot.send_document(a, file_id, caption=text, parse_mode=HTML, reply_markup=markup)
            elif file_id:
                await bot.send_photo(a, file_id, caption=text, parse_mode=HTML, reply_markup=markup)
            else:
                await bot.send_message(a, text, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True)
        except TelegramError as e:
            log.warning("admin %s ko msg nahi gaya: %s", a, e)


# ========================= SERVICES JSON VALIDATION ==========================
SAMPLE_JSON = {
    "Instagram": [
        {
            "name": "Followers",
            "desc": "High quality followers",
            "min": 100,
            "max": 10000,
            "servers": [
                {"name": "Server 1", "price": 120, "note": "Fast"},
                {"name": "Server 2", "price": 90, "note": "Slow but cheap"},
            ],
        },
        {"name": "Likes", "min": 50, "max": 5000, "servers": [{"name": "Server 1", "price": 40}]},
    ],
    "Telegram": [
        {"name": "Channel Members", "min": 100, "max": 20000, "servers": [{"name": "Server 1", "price": 150}]}
    ],
    "YouTube": [
        {"name": "Views", "min": 500, "max": 100000, "servers": [{"name": "Server 1", "price": 200}]},
        {"name": "Subscribers", "min": 50, "max": 5000, "servers": [{"name": "Server 1", "price": 900}]},
    ],
    "TikTok": [
        {"name": "Followers", "min": 100, "max": 10000, "servers": [{"name": "Server 1", "price": 180}]}
    ],
    "Snapchat": [
        {"name": "Followers", "min": 100, "max": 5000, "servers": [{"name": "Server 1", "price": 250}]}
    ],
    "WhatsApp": [
        {"name": "Channel Followers", "min": 100, "max": 5000, "servers": [{"name": "Server 1", "price": 300}]}
    ],
}


def validate_services(data):
    if not isinstance(data, dict) or not data:
        raise ValueError('JSON ka top level object hona chahiye, jaise {"Instagram": [ ... ]}')
    if len(data) > 60:
        raise ValueError("Maximum 60 categories allowed hain.")
    clean = {}
    for cat, items in data.items():
        cat_name = str(cat).strip()
        if not cat_name:
            raise ValueError("Category ka naam khali nahi ho sakta.")
        if not isinstance(items, list) or not items:
            raise ValueError(f"'{cat_name}' mein services ki list [ ... ] honi chahiye.")
        out = []
        for i, s in enumerate(items, 1):
            where = f"{cat_name} → service #{i}"
            if not isinstance(s, dict):
                raise ValueError(f"{where}: {{ ... }} object hona chahiye.")
            name = str(s.get("name", "")).strip()
            if not name:
                raise ValueError(f"{where}: 'name' missing hai.")
            try:
                mn, mx = int(s.get("min", 1)), int(s.get("max", 100000))
            except (TypeError, ValueError):
                raise ValueError(f"{where} ({name}): 'min' aur 'max' number hone chahiye.")
            if mn < 1 or mx < mn:
                raise ValueError(f"{where} ({name}): min 1 ya zyada aur max >= min hona chahiye.")
            servers = s.get("servers")
            if servers is None and "price" in s:
                servers = [{"name": "Server 1", "price": s["price"]}]
            if not isinstance(servers, list) or not servers:
                raise ValueError(f"{where} ({name}): 'servers' list missing hai.")
            sv = []
            for j, sr in enumerate(servers, 1):
                if not isinstance(sr, dict):
                    raise ValueError(f"{where} ({name}) server #{j}: {{ ... }} object hona chahiye.")
                try:
                    price = float(sr.get("price"))
                except (TypeError, ValueError):
                    raise ValueError(f"{where} ({name}) server #{j}: 'price' number hona chahiye.")
                if price <= 0:
                    raise ValueError(f"{where} ({name}) server #{j}: price 0 se zyada honi chahiye.")
                sv.append(
                    {
                        "name": str(sr.get("name") or f"Server {j}").strip()[:40],
                        "price": price,
                        "note": str(sr.get("note") or "").strip()[:80],
                    }
                )
            out.append(
                {"name": name[:60], "desc": str(s.get("desc") or "").strip()[:300], "min": mn, "max": mx, "servers": sv}
            )
        clean[cat_name[:40]] = out
    return clean


def services_summary(svs):
    lines = []
    for cat, items in svs.items():
        lines.append(f"{emoji(cat)} <b>{esc(cat)}</b> ({len(items)})")
        for s in items:
            sv = ", ".join(f"{esc(v['name'])}: {money(v['price'])}" for v in s["servers"])
            lines.append(f"  • {esc(s['name'])} [{s['min']}-{s['max']}] — {sv}")
    text = "\n".join(lines)
    return text if len(text) < 3800 else text[:3800] + "\n…"


# ============================= TEXT BUILDERS =================================
def user_line(uid):
    u = one("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        return f"<code>{uid}</code>"
    uname = f"@{u['username']}" if u["username"] else "no username"
    return f"{esc(u['name'] or '')} ({esc(uname)})\n🆔 <code>{uid}</code>"


ORDER_ICON = {"Pending": "🕐", "Processing": "⚙️", "Completed": "✅", "Rejected": "❌"}


def order_text(o):
    return (
        f"{ORDER_ICON.get(o['status'], '')} <b>Order #{o['id']}</b> — <b>{o['status']}</b>\n\n"
        f"👤 {user_line(o['user_id'])}\n"
        f"📂 {esc(o['category'])} → {esc(o['service'])}\n"
        f"🖥 {esc(o['server'])} • {fm(o['price'])}/1000\n"
        f"🔢 Qty: {o['qty']}\n"
        f"💵 Charge: {fm(o['charge'])}\n"
        f"🔗 {esc(o['link'])}"
    )


def order_kb(o):
    oid = o["id"]
    if o["status"] == "Pending":
        return Markup(
            [
                [Btn("⚙️ Accept", callback_data=f"ord:ok:{oid}"), Btn("✅ Complete", callback_data=f"ord:done:{oid}")],
                [Btn("❌ Reject (Refund)", callback_data=f"ord:no:{oid}")],
            ]
        )
    if o["status"] == "Processing":
        return Markup(
            [
                [Btn("✅ Complete", callback_data=f"ord:done:{oid}")],
                [Btn("❌ Reject (Refund)", callback_data=f"ord:no:{oid}")],
            ]
        )
    return None


def dep_text(d):
    icon = {"Pending": "🕐", "Approved": "✅", "Rejected": "❌"}.get(d["status"], "")
    return (
        f"{icon} <b>Deposit #{d['id']}</b> — <b>{d['status']}</b>\n\n"
        f"👤 {user_line(d['user_id'])}\n💵 Amount: <b>{fm(d['amount'])}</b>"
    )


def dep_kb(d):
    if d["status"] != "Pending":
        return None
    return Markup(
        [[Btn("✅ Accept", callback_data=f"dep:ok:{d['id']}"), Btn("❌ Reject", callback_data=f"dep:no:{d['id']}")]]
    )


def stats_text():
    users = one("SELECT COUNT(*) c FROM users")["c"]
    banned = one("SELECT COUNT(*) c FROM users WHERE banned=1")["c"]
    bal = one("SELECT COALESCE(SUM(balance),0) s FROM users")["s"]
    o = one(
        "SELECT COUNT(*) total, COALESCE(SUM(status='Pending'),0) pend, COALESCE(SUM(status='Processing'),0) proc,"
        " COALESCE(SUM(status='Completed'),0) done, COALESCE(SUM(status='Rejected'),0) rej,"
        " COALESCE(SUM(CASE WHEN status!='Rejected' THEN charge END),0) sales FROM orders"
    )
    d = one(
        "SELECT COALESCE(SUM(status='Pending'),0) pend,"
        " COALESCE(SUM(CASE WHEN status='Approved' THEN amount END),0) approved FROM deposits"
    )
    return (
        "📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total users: <b>{users}</b> (banned: {banned})\n"
        f"👛 Users ka total balance: <b>{fm(bal)}</b>\n\n"
        f"📦 Total orders: <b>{o['total']}</b>\n"
        f"   🕐 Pending: {int(o['pend'])}\n   ⚙️ Processing: {int(o['proc'])}\n"
        f"   ✅ Completed: {int(o['done'])}\n   ❌ Rejected: {int(o['rej'])}\n"
        f"💵 Total sales: <b>{fm(o['sales'])}</b>\n\n"
        f"💳 Pending deposits: <b>{int(d['pend'])}</b>\n"
        f"💰 Total approved deposits: <b>{fm(d['approved'])}</b>"
    )


def admin_kb():
    return Markup(
        [
            [Btn("📊 Stats", callback_data="ad:stats"), Btn("👥 Users", callback_data="ad:users")],
            [Btn("🕐 Pending Orders", callback_data="ad:orders"), Btn("💳 Pending Deposits", callback_data="ad:deps")],
            [Btn("📤 Upload Services", callback_data="ad:upload"), Btn("📋 View Services", callback_data="ad:view")],
            [Btn("🧾 Sample JSON", callback_data="ad:sample"), Btn("🖼 Set QR", callback_data="ad:qr")],
            [Btn("✍️ Payment Text", callback_data="ad:paytext"), Btn("📢 Broadcast", callback_data="ad:bc")],
            [Btn("⬅️ Main Menu", callback_data="menu")],
        ]
    )


ADMIN_HOME = "👑 <b>Admin Panel</b>\n\nNeeche se option chunein 👇"


# ============================== USER COMMANDS ================================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reset(ctx)
    u = update.effective_user
    row = ensure_user(u)
    if row["banned"] and not is_admin(u.id):
        await update.message.reply_text("🚫 Aap banned hain.")
        return
    await update.message.reply_text(
        f"👋 Welcome <b>{esc(u.first_name)}</b>!\n\n"
        "Yahan se aap Instagram, Telegram, YouTube, TikTok, Snapchat, WhatsApp waghera ki services order kar sakte hain.\n\n"
        "Neeche menu se option chunein 👇",
        parse_mode=HTML,
        reply_markup=main_menu(u.id),
    )


# ============================ USER: BROWSE / ORDER ===========================
async def do_menu(q, ctx, p):
    reset(ctx)
    await q.answer()
    await show(q, "🏠 <b>Main Menu</b>", main_menu(q.from_user.id))


async def do_shop(q, ctx, p):
    reset(ctx)
    await q.answer()
    cats = list(load_services())
    if not cats:
        await show(q, "😕 Abhi koi service available nahi hai.\nThori der baad try karein.", menu_only())
        return
    rows, row = [], []
    for i, c in enumerate(cats):
        row.append(Btn(f"{emoji(c)} {c}", callback_data=f"cat:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Btn("⬅️ Main Menu", callback_data="menu")])
    await show(q, "📂 <b>Category chunein:</b>", Markup(rows))


async def do_cat(q, ctx, p):
    reset(ctx)
    await q.answer()
    services = load_services()
    cats = list(services)
    ci = int(p[1])
    page = int(p[2]) if len(p) > 2 else 0
    if ci >= len(cats):
        await show(q, "⚠️ Services update ho gayi hain, dobara try karein.", Markup([[Btn("🛒 Services", callback_data="shop")]]))
        return
    cat = cats[ci]
    items = services[cat]
    start_i = page * PER_PAGE
    rows = []
    for i, s in enumerate(items[start_i : start_i + PER_PAGE], start=start_i):
        low = min(v["price"] for v in s["servers"])
        rows.append([Btn(f"{s['name'][:28]} • from {money(low)}/1k", callback_data=f"svc:{ci}:{i}")])
    nav = []
    if page > 0:
        nav.append(Btn("⬅️ Prev", callback_data=f"cat:{ci}:{page-1}"))
    if start_i + PER_PAGE < len(items):
        nav.append(Btn("Next ➡️", callback_data=f"cat:{ci}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([Btn("⬅️ Categories", callback_data="shop")])
    await show(q, f"{emoji(cat)} <b>{esc(cat)}</b>\nService chunein 👇 (price per 1000)", Markup(rows))


async def do_svc(q, ctx, p):
    reset(ctx)
    await q.answer()
    services = load_services()
    cats = list(services)
    ci, si = int(p[1]), int(p[2])
    if ci >= len(cats) or si >= len(services[cats[ci]]):
        await show(q, "⚠️ Services update ho gayi hain, dobara try karein.", Markup([[Btn("🛒 Services", callback_data="shop")]]))
        return
    cat = cats[ci]
    s = services[cat][si]
    lines = [f"{emoji(cat)} <b>{esc(cat)} → {esc(s['name'])}</b>"]
    if s["desc"]:
        lines.append(esc(s["desc"]))
    lines.append(f"\n📉 Min: <b>{s['min']}</b>   📈 Max: <b>{s['max']}</b>")
    lines.append("\n💰 <b>Servers & Price (per 1000):</b>")
    for v in s["servers"]:
        note = f" — {esc(v['note'])}" if v["note"] else ""
        lines.append(f"• {esc(v['name'])}: <b>{fm(v['price'])}</b>{note}")
    lines.append("\nOrder ke liye server chunein 👇")
    rows = [
        [Btn(f"🛒 {v['name'][:20]} • {fm(v['price'])}/1k", callback_data=f"srv:{ci}:{si}:{vi}")]
        for vi, v in enumerate(s["servers"])
    ]
    rows.append([Btn("⬅️ Back", callback_data=f"cat:{ci}:{si // PER_PAGE}")])
    await show(q, "\n".join(lines), Markup(rows))


async def do_srv(q, ctx, p):
    reset(ctx)
    await q.answer()
    services = load_services()
    cats = list(services)
    ci, si, vi = int(p[1]), int(p[2]), int(p[3])
    try:
        cat = cats[ci]
        s = services[cat][si]
        v = s["servers"][vi]
    except (IndexError, KeyError):
        await show(q, "⚠️ Services update ho gayi hain, dobara try karein.", Markup([[Btn("🛒 Services", callback_data="shop")]]))
        return
    o = {
        "category": cat, "service": s["name"], "server": v["name"],
        "price": v["price"], "min": s["min"], "max": s["max"],
    }
    ctx.user_data["flow"] = {"type": "qty", "o": o}
    await show(
        q,
        f"✅ <b>{esc(cat)} → {esc(s['name'])}</b>\n🖥 {esc(v['name'])} • <b>{fm(v['price'])}</b> per 1000\n\n"
        f"🔢 Quantity likhein (<b>{s['min']}</b> se <b>{s['max']}</b> tak):",
        cancel_only(),
    )


async def qty_step(update, ctx, flow):
    msg, o = update.message, flow["o"]
    t = (msg.text or "").strip().replace(",", "")
    if not t.isdigit():
        await msg.reply_text("❌ Sirf number likhein, jaise 1000", reply_markup=cancel_only())
        return
    qty = int(t)
    if qty < o["min"] or qty > o["max"]:
        await msg.reply_text(f"❌ Quantity {o['min']} se {o['max']} ke darmiyan honi chahiye.", reply_markup=cancel_only())
        return
    o["qty"] = qty
    o["charge"] = max(round(o["price"] * qty / 1000, 2), 0.01)
    flow["type"] = "link"
    await msg.reply_text(
        f"💵 Charge: <b>{fm(o['charge'])}</b>\n\n🔗 Ab apna <b>link</b> (ya @username) bhejein:",
        parse_mode=HTML,
        reply_markup=cancel_only(),
    )


async def link_step(update, ctx, flow):
    msg, o = update.message, flow["o"]
    link = (msg.text or "").strip()
    if not (link.startswith(("http://", "https://", "@")) and 3 <= len(link) <= 500 and " " not in link):
        await msg.reply_text("❌ Sahi link bhejein (https://... ya @username).", reply_markup=cancel_only())
        return
    o["link"] = link
    flow["type"] = "confirm"
    bal = one("SELECT balance FROM users WHERE id=?", (update.effective_user.id,))["balance"]
    kb = Markup(
        [[Btn("✅ Confirm Order", callback_data="confirm")], [Btn("❌ Cancel", callback_data="menu")]]
    )
    await msg.reply_text(
        "🧾 <b>Order Summary</b>\n\n"
        f"📂 {esc(o['category'])} → {esc(o['service'])}\n🖥 {esc(o['server'])}\n"
        f"🔢 Qty: {o['qty']}\n🔗 {esc(link)}\n\n"
        f"💵 Charge: <b>{fm(o['charge'])}</b>\n👛 Aapka balance: {fm(bal)}",
        parse_mode=HTML,
        reply_markup=kb,
        disable_web_page_preview=True,
    )


async def do_confirm(q, ctx, p):
    flow = ctx.user_data.get("flow")
    if not flow or flow.get("type") != "confirm":
        await q.answer("⏳ Session expire ho gaya. Dobara order karein.", show_alert=True)
        return
    ctx.user_data.pop("flow", None)
    await q.answer()
    o, uid = flow["o"], q.from_user.id
    cur = db.execute(
        "UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (o["charge"], uid, o["charge"])
    )
    if cur.rowcount == 0:
        db.rollback()
        bal = one("SELECT balance FROM users WHERE id=?", (uid,))["balance"]
        await show(
            q,
            f"❌ <b>Balance kam hai.</b>\n\nZaroorat: {fm(o['charge'])}\nAapka balance: {fm(bal)}\n\n"
            "Pehle funds add karein 👇",
            Markup([[Btn("💰 Add Funds", callback_data="deposit")], [Btn("⬅️ Main Menu", callback_data="menu")]]),
        )
        return
    cur = db.execute(
        "INSERT INTO orders(user_id,category,service,server,price,link,qty,charge,status,created)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (uid, o["category"], o["service"], o["server"], o["price"], o["link"], o["qty"], o["charge"], "Pending", int(time.time())),
    )
    db.commit()
    order = one("SELECT * FROM orders WHERE id=?", (cur.lastrowid,))
    bal = one("SELECT balance FROM users WHERE id=?", (uid,))["balance"]
    await show(
        q,
        f"🎉 <b>Order place ho gaya!</b>\n\nOrder ID: <code>#{order['id']}</code>\n"
        f"💵 Charge: {fm(order['charge'])}\n👛 Baqi balance: {fm(bal)}\n\n"
        "Admin jaldi process karega. Status 'My Orders' mein dekhein.",
        menu_only(),
    )
    await notify_admins(q.get_bot(), order_text(order), order_kb(order))


async def do_orders(q, ctx, p):
    reset(ctx)
    await q.answer()
    rows = db.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (q.from_user.id,)).fetchall()
    if not rows:
        await show(q, "📦 Abhi tak koi order nahi.", menu_only())
        return
    kb = [
        [Btn(f"{ORDER_ICON.get(r['status'], '')} #{r['id']} • {r['service'][:18]} • {r['qty']}", callback_data=f"myo:{r['id']}")]
        for r in rows
    ]
    kb.append([Btn("⬅️ Main Menu", callback_data="menu")])
    await show(q, "📦 <b>Aapke aakhri orders</b>", Markup(kb))


async def do_myorder(q, ctx, p):
    await q.answer()
    o = one("SELECT * FROM orders WHERE id=? AND user_id=?", (int(p[1]), q.from_user.id))
    if not o:
        return
    text = (
        f"{ORDER_ICON.get(o['status'], '')} <b>Order #{o['id']}</b>\n\n"
        f"📂 {esc(o['category'])} → {esc(o['service'])}\n🖥 {esc(o['server'])}\n"
        f"🔢 Qty: {o['qty']}\n💵 Charge: {fm(o['charge'])}\n🔗 {esc(o['link'])}\n\n"
        f"Status: <b>{o['status']}</b>"
    )
    await show(q, text, Markup([[Btn("🔄 Refresh", callback_data=f"myo:{o['id']}")], [Btn("⬅️ Orders", callback_data="orders")]]))


async def do_balance(q, ctx, p):
    reset(ctx)
    await q.answer()
    u = one("SELECT * FROM users WHERE id=?", (q.from_user.id,))
    n = one("SELECT COUNT(*) c FROM orders WHERE user_id=?", (u["id"],))["c"]
    await show(
        q,
        f"👛 <b>Aapka Account</b>\n\n🆔 ID: <code>{u['id']}</code>\n💰 Balance: <b>{fm(u['balance'])}</b>\n📦 Total orders: {n}",
        Markup([[Btn("💰 Add Funds", callback_data="deposit")], [Btn("⬅️ Main Menu", callback_data="menu")]]),
    )


async def do_support(q, ctx, p):
    reset(ctx)
    await q.answer()
    await show(q, f"🆘 <b>Support</b>\n\nKisi bhi masle ke liye rabta karein: {esc(SUPPORT)}", menu_only())


# ================================ DEPOSITS ===================================
async def do_deposit(q, ctx, p):
    reset(ctx)
    await q.answer()
    if not get_setting("qr") and not get_setting("paytext"):
        await show(q, f"⚠️ Admin ne abhi payment method set nahi kiya.\n{esc(SUPPORT)} se rabta karein.", menu_only())
        return
    ctx.user_data["flow"] = {"type": "dep_amt"}
    await show(
        q,
        "💰 <b>Add Funds</b>\n\nKitne paise add karne hain? Amount likhein.\n"
        f"Example: <code>100</code>, <code>500</code>, <code>1000</code>\n"
        f"(Min {fm(MIN_DEP)} • Max {fm(MAX_DEP)})",
        cancel_only(),
    )


async def dep_amt_step(update, ctx, flow):
    msg = update.message
    try:
        amt = round(float((msg.text or "").strip().replace(",", "")), 2)
    except ValueError:
        await msg.reply_text("❌ Sirf amount likhein, jaise 500", reply_markup=cancel_only())
        return
    if amt < MIN_DEP or amt > MAX_DEP:
        await msg.reply_text(f"❌ Amount {fm(MIN_DEP)} se {fm(MAX_DEP)} ke darmiyan hona chahiye.", reply_markup=cancel_only())
        return
    flow["type"], flow["amount"] = "dep_shot", amt
    pay = (get_setting("paytext") or "")[:600]
    caption = (
        f"💳 <b>Payment Details</b>\n\nAmount: <b>{fm(amt)}</b>\n\n{esc(pay)}\n\n"
        "📸 Payment karne ke baad <b>screenshot</b> yahan bhejein."
    )
    qr = get_setting("qr")
    if qr:
        try:
            await msg.reply_photo(qr, caption=caption, parse_mode=HTML, reply_markup=cancel_only())
            return
        except TelegramError as e:
            log.warning("QR send fail: %s", e)
    await msg.reply_text(caption, parse_mode=HTML, reply_markup=cancel_only())


async def dep_shot_step(update, ctx, flow):
    msg = update.message
    if msg.photo:
        fid, kind = msg.photo[-1].file_id, "photo"
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        fid, kind = msg.document.file_id, "document"
    else:
        await msg.reply_text("❌ Payment ka <b>screenshot (photo)</b> bhejein.", parse_mode=HTML, reply_markup=cancel_only())
        return
    uid = update.effective_user.id
    cur = db.execute(
        "INSERT INTO deposits(user_id,amount,file_id,kind,status,created) VALUES(?,?,?,?,?,?)",
        (uid, flow["amount"], fid, kind, "Pending", int(time.time())),
    )
    db.commit()
    ctx.user_data.pop("flow", None)
    d = one("SELECT * FROM deposits WHERE id=?", (cur.lastrowid,))
    await msg.reply_text(
        f"✅ <b>Request admin ko bhej di gayi hai.</b>\n\nDeposit ID: <code>#{d['id']}</code>\n"
        f"Amount: {fm(d['amount'])}\n\nVerify hote hi balance add ho jayega.",
        parse_mode=HTML,
        reply_markup=menu_only(),
    )
    await notify_admins(ctx.bot, dep_text(d), dep_kb(d), file_id=fid, kind=kind)


# ============================ ADMIN: ACTION BUTTONS ==========================
async def order_action(q, ctx, p):
    if not is_admin(q.from_user.id):
        await q.answer("Not allowed", show_alert=True)
        return
    action, oid = p[1], int(p[2])
    o = one("SELECT * FROM orders WHERE id=?", (oid,))
    if not o:
        await q.answer("Order nahi mila", show_alert=True)
        return
    st = o["status"]
    if action == "ok" and st == "Pending":
        new = "Processing"
    elif action == "done" and st in ("Pending", "Processing"):
        new = "Completed"
    elif action == "no" and st in ("Pending", "Processing"):
        new = "Rejected"
    else:
        await q.answer(f"Order pehle se {st} hai.", show_alert=True)
        return
    db.execute("UPDATE orders SET status=? WHERE id=?", (new, oid))
    db.commit()
    if new == "Rejected":
        add_balance(o["user_id"], o["charge"])
    o = one("SELECT * FROM orders WHERE id=?", (oid,))
    await q.answer("Done ✅")
    await show(q, order_text(o), order_kb(o))
    bot = q.get_bot()
    if new == "Processing":
        await tell(bot, o["user_id"], f"⚙️ Aapka order <b>#{oid}</b> process ho raha hai.")
    elif new == "Completed":
        await tell(bot, o["user_id"], f"✅ Aapka order <b>#{oid}</b> complete ho gaya! Shukriya 🙏")
    else:
        await tell(
            bot,
            o["user_id"],
            f"❌ Aapka order <b>#{oid}</b> reject ho gaya.\n{fm(o['charge'])} aapke balance mein wapas add kar diye gaye.",
        )


async def dep_action(q, ctx, p):
    if not is_admin(q.from_user.id):
        await q.answer("Not allowed", show_alert=True)
        return
    action, did = p[1], int(p[2])
    d = one("SELECT * FROM deposits WHERE id=?", (did,))
    if not d:
        await q.answer("Deposit nahi mila", show_alert=True)
        return
    if d["status"] != "Pending":
        await q.answer(f"Pehle se {d['status']} hai.", show_alert=True)
        return
    new = "Approved" if action == "ok" else "Rejected"
    db.execute("UPDATE deposits SET status=? WHERE id=?", (new, did))
    db.commit()
    if new == "Approved":
        add_balance(d["user_id"], d["amount"])
    d = one("SELECT * FROM deposits WHERE id=?", (did,))
    await q.answer("Done ✅")
    await show_caption(q, dep_text(d))
    bot = q.get_bot()
    if new == "Approved":
        bal = one("SELECT balance FROM users WHERE id=?", (d["user_id"],))["balance"]
        await tell(bot, d["user_id"], f"✅ Deposit <b>#{did}</b> approve ho gaya!\n💰 {fm(d['amount'])} add ho gaye.\n👛 Naya balance: <b>{fm(bal)}</b>")
    else:
        await tell(bot, d["user_id"], f"❌ Deposit <b>#{did}</b> reject ho gaya.\nAgar payment ki hai to {esc(SUPPORT)} se rabta karein.")


# ============================== ADMIN: PANEL =================================
async def do_admin(q, ctx, p):
    if not is_admin(q.from_user.id):
        await q.answer("Not allowed", show_alert=True)
        return
    reset(ctx)
    await q.answer()
    sub = p[1] if len(p) > 1 else "home"
    back = Markup([[Btn("⬅️ Admin Panel", callback_data="ad:home")]])
    bot, aid = q.get_bot(), q.from_user.id

    if sub == "home":
        await show(q, ADMIN_HOME, admin_kb())
    elif sub == "stats":
        await show(q, stats_text(), back)
    elif sub == "users":
        rows = db.execute(
            "SELECT u.*, (SELECT COUNT(*) FROM orders o WHERE o.user_id=u.id) n FROM users u ORDER BY joined DESC LIMIT 25"
        ).fetchall()
        total = one("SELECT COUNT(*) c FROM users")["c"]
        lines = [f"👥 <b>Users</b> (total {total}, latest 25)\n"]
        for r in rows:
            uname = f"@{r['username']}" if r["username"] else "-"
            ban = " 🚫" if r["banned"] else ""
            lines.append(f"• <code>{r['id']}</code> {esc(uname)} • {fm(r['balance'])} • {r['n']} orders{ban}")
        await show(q, "\n".join(lines), back)
    elif sub == "orders":
        rows = db.execute("SELECT * FROM orders WHERE status IN ('Pending','Processing') ORDER BY id LIMIT 10").fetchall()
        if not rows:
            await show(q, "✅ Koi pending order nahi.", back)
            return
        await show(q, f"🕐 <b>Pending/Processing orders</b> ({len(rows)} dikhaye ja rahe hain)", back)
        for o in rows:
            await bot.send_message(aid, order_text(o), parse_mode=HTML, reply_markup=order_kb(o), disable_web_page_preview=True)
    elif sub == "deps":
        rows = db.execute("SELECT * FROM deposits WHERE status='Pending' ORDER BY id LIMIT 10").fetchall()
        if not rows:
            await show(q, "✅ Koi pending deposit nahi.", back)
            return
        await show(q, f"💳 <b>Pending deposits</b> ({len(rows)} dikhaye ja rahe hain)", back)
        for d in rows:
            if d["kind"] == "document":
                await bot.send_document(aid, d["file_id"], caption=dep_text(d), parse_mode=HTML, reply_markup=dep_kb(d))
            else:
                await bot.send_photo(aid, d["file_id"], caption=dep_text(d), parse_mode=HTML, reply_markup=dep_kb(d))
    elif sub == "upload":
        ctx.user_data["await"] = "services"
        await show(
            q,
            "📤 <b>Services Upload</b>\n\nApni <b>.json file</b> bhejein (ya JSON text paste karein).\n"
            "Format ke liye 🧾 Sample JSON dabayein.\n\n"
            "⚠️ Nayi upload purani services ko replace kar degi.",
            Markup([[Btn("🧾 Sample JSON", callback_data="ad:sample")], [Btn("❌ Cancel", callback_data="ad:home")]]),
        )
    elif sub == "sample":
        data = json.dumps(SAMPLE_JSON, indent=2, ensure_ascii=False).encode()
        await bot.send_document(
            aid, io.BytesIO(data), filename="services_sample.json",
            caption="🧾 Isko edit karke wapis bhej dein (📤 Upload Services).\n"
                    "Har service ke 'servers' mein server ka naam aur price per 1000 likhein.",
        )
    elif sub == "view":
        svs = load_services()
        text = services_summary(svs) if svs else "Abhi koi service upload nahi hui."
        await show(q, "📋 <b>Current Services</b>\n\n" + text, back)
    elif sub == "qr":
        ctx.user_data["await"] = "qr"
        await show(q, "🖼 Apna payment <b>QR code</b> ki photo bhejein.", Markup([[Btn("❌ Cancel", callback_data="ad:home")]]))
    elif sub == "paytext":
        ctx.user_data["await"] = "paytext"
        await show(
            q,
            "✍️ Payment details likhein (account name, number, bank/JazzCash/EasyPaisa waghera).\nYe user ko QR ke saath dikhegi.",
            Markup([[Btn("❌ Cancel", callback_data="ad:home")]]),
        )
    elif sub == "bc":
        ctx.user_data["await"] = "broadcast"
        await show(q, "📢 Jo message sab users ko bhejna hai wo likhein.", Markup([[Btn("❌ Cancel", callback_data="ad:home")]]))


async def admin_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, mode = update.message, ctx.user_data.get("await")
    back = Markup([[Btn("⬅️ Admin Panel", callback_data="ad:home")]])

    if mode == "services":
        text = None
        if msg.document:
            if msg.document.file_size and msg.document.file_size > 2_000_000:
                await msg.reply_text("❌ File bohat bari hai (max 2MB).")
                return
            f = await msg.document.get_file()
            raw = bytes(await f.download_as_bytearray())
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                await msg.reply_text("❌ File UTF-8 text honi chahiye.")
                return
        elif msg.text:
            text = msg.text
        else:
            await msg.reply_text("❌ .json file ya JSON text bhejein.")
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            await msg.reply_text(f"❌ JSON mein ghalti: line {e.lineno}, column {e.colno} — {e.msg}\nTheek karke dobara bhejein.")
            return
        try:
            clean = validate_services(data)
        except ValueError as e:
            await msg.reply_text(f"❌ {e}\nTheek karke dobara bhejein.")
            return
        set_setting("services", json.dumps(clean, ensure_ascii=False))
        ctx.user_data.pop("await", None)
        n_srv = sum(len(s["servers"]) for items in clean.values() for s in items)
        n_svc = sum(len(items) for items in clean.values())
        await msg.reply_text(
            f"✅ <b>Services upload ho gayin!</b>\n\n📂 Categories: {len(clean)}\n🛍 Services: {n_svc}\n🖥 Servers: {n_srv}",
            parse_mode=HTML,
            reply_markup=back,
        )
    elif mode == "qr":
        if not msg.photo:
            await msg.reply_text("❌ QR ko <b>photo</b> ki tarah bhejein (file nahi).", parse_mode=HTML)
            return
        set_setting("qr", msg.photo[-1].file_id)
        ctx.user_data.pop("await", None)
        await msg.reply_text("✅ QR save ho gaya.", reply_markup=back)
    elif mode == "paytext":
        if not msg.text:
            await msg.reply_text("❌ Text likhein.")
            return
        set_setting("paytext", msg.text.strip()[:700])
        ctx.user_data.pop("await", None)
        await msg.reply_text("✅ Payment details save ho gayin.", reply_markup=back)
    elif mode == "broadcast":
        if not msg.text:
            await msg.reply_text("❌ Text likhein.")
            return
        ctx.user_data.pop("await", None)
        ok, total = await broadcast_to_all(ctx.bot, msg.text)
        await msg.reply_text(f"📢 {ok}/{total} users ko bhej diya.", reply_markup=back)


async def broadcast_to_all(bot, text):
    ids = [r["id"] for r in db.execute("SELECT id FROM users WHERE banned=0").fetchall()]
    ok = 0
    for i in ids:
        try:
            await bot.send_message(i, f"📢 {text}")
            ok += 1
        except TelegramError:
            pass
        await asyncio.sleep(0.05)
    return ok, len(ids)


# ============================ ADMIN: COMMANDS ================================
def admin_cmd(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            return
        return await fn(update, ctx)

    return wrapper


@admin_cmd
async def cmd_admin(update, ctx):
    reset(ctx)
    await update.message.reply_text(ADMIN_HOME, parse_mode=HTML, reply_markup=admin_kb())


@admin_cmd
async def cmd_cancel(update, ctx):
    reset(ctx)
    await update.message.reply_text("Cancel ho gaya.", reply_markup=admin_kb())


@admin_cmd
async def cmd_addbal(update, ctx):
    try:
        uid, amt = int(ctx.args[0]), float(ctx.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /addbalance <user_id> <amount>")
        return
    if not one("SELECT 1 FROM users WHERE id=?", (uid,)):
        await update.message.reply_text("❌ User nahi mila (usne bot start nahi kiya).")
        return
    add_balance(uid, amt)
    bal = one("SELECT balance FROM users WHERE id=?", (uid,))["balance"]
    await update.message.reply_text(f"✅ {fm(amt)} add ho gaye. Naya balance: {fm(bal)}")
    await tell(ctx.bot, uid, f"💰 Aapke account mein <b>{fm(amt)}</b> add ho gaye.\n👛 Balance: <b>{fm(bal)}</b>")


@admin_cmd
async def cmd_rembal(update, ctx):
    try:
        uid, amt = int(ctx.args[0]), float(ctx.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /removebalance <user_id> <amount>")
        return
    add_balance(uid, -amt)
    await update.message.reply_text(f"✅ {fm(amt)} kam kar diye.")


async def _set_ban(update, ctx, value):
    try:
        uid = int(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /ban <user_id>  ya  /unban <user_id>")
        return
    db.execute("UPDATE users SET banned=? WHERE id=?", (value, uid))
    db.commit()
    await update.message.reply_text(("🚫 Banned: " if value else "✅ Unbanned: ") + str(uid))


@admin_cmd
async def cmd_ban(update, ctx):
    await _set_ban(update, ctx, 1)


@admin_cmd
async def cmd_unban(update, ctx):
    await _set_ban(update, ctx, 0)


@admin_cmd
async def cmd_broadcast(update, ctx):
    text = " ".join(ctx.args)
    if not text:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    ok, total = await broadcast_to_all(ctx.bot, text)
    await update.message.reply_text(f"📢 {ok}/{total} users ko bhej diya.")


# ============================ ROUTERS / MAIN =================================
ROUTES = {
    "menu": do_menu, "shop": do_shop, "cat": do_cat, "svc": do_svc, "srv": do_srv,
    "confirm": do_confirm, "orders": do_orders, "myo": do_myorder, "balance": do_balance,
    "support": do_support, "deposit": do_deposit, "ord": order_action, "dep": dep_action,
    "ad": do_admin,
}


async def router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    row = ensure_user(q.from_user)
    if row["banned"] and not is_admin(q.from_user.id):
        await q.answer("🚫 Aap banned hain.", show_alert=True)
        return
    p = (q.data or "").split(":")
    handler = ROUTES.get(p[0])
    if not handler:
        await q.answer()
        return
    try:
        await handler(q, ctx, p)
    except Exception:
        log.exception("callback error: %s", q.data)
        try:
            await q.answer("⚠️ Error aaya, dobara try karein.", show_alert=True)
        except TelegramError:
            pass


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, user = update.message, update.effective_user
    if not msg or not user:
        return
    row = ensure_user(user)
    if row["banned"] and not is_admin(user.id):
        await msg.reply_text("🚫 Aap banned hain.")
        return
    try:
        if is_admin(user.id) and ctx.user_data.get("await"):
            await admin_input(update, ctx)
            return
        flow = ctx.user_data.get("flow")
        if not flow:
            if msg.text:
                await msg.reply_text("👇 Menu se option chunein:", reply_markup=main_menu(user.id))
            return
        step = flow["type"]
        if step == "qty":
            await qty_step(update, ctx, flow)
        elif step == "link":
            await link_step(update, ctx, flow)
        elif step == "dep_amt":
            await dep_amt_step(update, ctx, flow)
        elif step == "dep_shot":
            await dep_shot_step(update, ctx, flow)
        else:
            await msg.reply_text("Upar diye gaye button se Confirm ya Cancel karein.")
    except Exception:
        log.exception("message error")
        await msg.reply_text("⚠️ Error aaya, /start dabakar dobara try karein.")


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error: %s", ctx.error)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start, filters=private))
    app.add_handler(CommandHandler("admin", cmd_admin, filters=private))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=private))
    app.add_handler(CommandHandler("addbalance", cmd_addbal, filters=private))
    app.add_handler(CommandHandler("removebalance", cmd_rembal, filters=private))
    app.add_handler(CommandHandler("ban", cmd_ban, filters=private))
    app.add_handler(CommandHandler("unban", cmd_unban, filters=private))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast, filters=private))
    app.add_handler(CallbackQueryHandler(router))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
