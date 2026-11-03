"""Hunter Market Bot — bitta faylli versiya.

Ishga tushirish:  pip install aiogram asyncpg aiohttp telethon  &&  python hunter_market.py
Kerakli env: BOT_TOKEN, DATABASE_URL, ADMIN_IDS, CARD_NUMBER, PAY_SECRET, API_ID, API_HASH, TG_SESSION,
             MONITOR_CHANNEL, BOT_USERNAME, SUPPORT_USERNAME (batafsil — pastdagi sozlamalar bo'limida)
TG_SESSION olish:  python hunter_market.py session
"""
import asyncio
import hashlib
import logging
import os
import random
import re
import string
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, CopyTextButton, KeyboardButton, KeyboardButtonRequestUsers, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand, BotCommandScopeDefault,
                           BotCommandScopeChat)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession
from telethon.errors import (PasswordHashInvalidError, PhoneCodeExpiredError, PhoneCodeInvalidError,
                             SessionPasswordNeededError)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hunter")

# ================= SOZLAMALAR =================
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
_env_ids = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x]
OWNER_ID = int(os.getenv("OWNER_ID") or (_env_ids[0] if _env_ids else 0))   # bo'sh bo'lsa ADMIN_IDS dagi birinchisi
ENV_ADMINS = set(_env_ids) | ({OWNER_ID} if OWNER_ID else set())
ADMIN_IDS = set(ENV_ADMINS)    # bazadagi adminlar startda qo'shiladi

# To'lov (karta + SMS listener)
CARD_NUMBER = os.getenv("CARD_NUMBER", "0000 0000 0000 0000")
PAY_SECRET = os.getenv("PAY_SECRET", "change-me")      # SMS listener shu kalit bilan yuboradi
PAY_TIMEOUT = int(os.getenv("PAY_TIMEOUT", "300"))      # soniya (5 daqiqa)
UNIQUE_OFFSET_MAX = int(os.getenv("UNIQUE_OFFSET_MAX", "99"))  # 0 = o'chirilgan

# Userbot (Telethon)
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
TG_SESSION = os.getenv("TG_SESSION", "")

# Kanal va sozlamalar
MONITOR_CHANNEL = os.getenv("MONITOR_CHANNEL", "")      # @HunterMarketda
BOT_USERNAME = os.getenv("BOT_USERNAME", "HunterMarketBot")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "")
PORT = int(os.getenv("PORT", "8080"))

# ================= BAZA =================
pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id BIGINT PRIMARY KEY, username TEXT, name TEXT,
  balance BIGINT DEFAULT 0, ref_by BIGINT, banned BOOLEAN DEFAULT FALSE,
  display_name TEXT, name_changed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS products(
  id SERIAL PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, emoji TEXT DEFAULT '',
  gift_id BIGINT DEFAULT 0, amount INT DEFAULT 0, price BIGINT NOT NULL,
  active BOOLEAN DEFAULT TRUE, sort INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS orders(
  id SERIAL PRIMARY KEY, code TEXT UNIQUE, user_id BIGINT, product_id INT,
  kind TEXT, title TEXT, amount INT, price BIGINT,
  target_username TEXT, anonymous BOOLEAN DEFAULT FALSE, comment TEXT,
  pay_amount BIGINT, status TEXT DEFAULT 'new', paid_with TEXT,
  chat_id BIGINT, msg_id BIGINT, rating INT, error TEXT,
  created_at TIMESTAMPTZ DEFAULT now(), expires_at TIMESTAMPTZ,
  paid_at TIMESTAMPTZ, done_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, amount BIGINT, max_uses INT, used INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS promo_used(code TEXT, user_id BIGINT, PRIMARY KEY(code,user_id));
CREATE TABLE IF NOT EXISTS channels(chat_id TEXT PRIMARY KEY, link TEXT);
CREATE TABLE IF NOT EXISTS accounts(
  id SERIAL PRIMARY KEY, phone TEXT, label TEXT, session TEXT NOT NULL,
  active BOOLEAN DEFAULT TRUE, created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS sms_log(h TEXT PRIMARY KEY, at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS admins(id BIGINT PRIMARY KEY, added_at TIMESTAMPTZ DEFAULT now());
"""

# Boshlang'ich mahsulotlar (narxlarni /price orqali o'zgartirasiz)
SEED = [
    ("premium", "1 oy", "💎", 0, 1, 45000, 1), ("premium", "3 oy", "💎", 0, 3, 159000, 2),
    ("premium", "6 oy", "💎", 0, 6, 210000, 3), ("premium", "12 oy", "💎", 0, 12, 380000, 4),
    ("stars", "50 Stars", "⭐", 0, 50, 11000, 1), ("stars", "100 Stars", "⭐", 0, 100, 22000, 2),
    ("stars", "250 Stars", "⭐", 0, 250, 55000, 3), ("stars", "500 Stars", "⭐", 0, 500, 110000, 4),
    ("stars", "1000 Stars", "⭐", 0, 1000, 220000, 5),
    ("gift", "Ayiqcha", "🧸", 0, 0, 4000, 1), ("gift", "Yurak", "💝", 0, 0, 4000, 2),
    ("gift", "Sovg'a", "🎁", 0, 0, 6000, 3), ("gift", "Atirgul", "🌹", 0, 0, 6000, 4),
    ("gift", "Tort", "🎂", 0, 0, 9000, 5), ("gift", "Guldasta", "💐", 0, 0, 9000, 6),
    ("gift", "Raketa", "🚀", 0, 0, 9000, 7), ("gift", "Shampan", "🍾", 0, 0, 9000, 8),
    ("gift", "Kubok", "🏆", 0, 0, 20000, 9), ("gift", "Uzuk", "💍", 0, 0, 20000, 10),
    ("gift", "Olmos", "💎", 0, 0, 20000, 11),
]
DEFAULTS = {"stars_price": "220", "stars_min": "50", "ref_percent": "2", "name_change_price": "4900"}


async def db_init(url: str):
    global pool
    pool = await asyncpg.create_pool(url, min_size=1, max_size=10)
    await pool.execute(SCHEMA)
    if not await pool.fetchval("SELECT count(*) FROM products"):
        for s in SEED:
            await pool.execute(
                "INSERT INTO products(kind,title,emoji,gift_id,amount,price,sort) VALUES($1,$2,$3,$4,$5,$6,$7)", *s)
    for k, v in DEFAULTS.items():
        await pool.execute("INSERT INTO settings VALUES($1,$2) ON CONFLICT DO NOTHING", k, v)


async def get_setting(key: str) -> str:
    return await pool.fetchval("SELECT value FROM settings WHERE key=$1", key) or ""


async def set_setting(key: str, value: str):
    await pool.execute("INSERT INTO settings VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2", key, value)


async def upsert_user(uid: int, username, name, ref_by=None):
    row = await pool.fetchrow("SELECT * FROM users WHERE id=$1", uid)
    if row:
        await pool.execute("UPDATE users SET username=$2,name=$3 WHERE id=$1", uid, username, name)
        return row, False
    if ref_by == uid:
        ref_by = None
    await pool.execute("INSERT INTO users(id,username,name,ref_by) VALUES($1,$2,$3,$4)", uid, username, name, ref_by)
    return await pool.fetchrow("SELECT * FROM users WHERE id=$1", uid), True


async def add_balance(uid: int, amount: int):
    await pool.execute("UPDATE users SET balance=balance+$2 WHERE id=$1", uid, amount)


async def take_balance(uid: int, amount: int) -> bool:
    r = await pool.fetchval(
        "UPDATE users SET balance=balance-$2 WHERE id=$1 AND balance>=$2 RETURNING id", uid, amount)
    return r is not None


def utcnow():
    return datetime.now(timezone.utc)


async def assign_pay_amount(order_id: int, price: int, offset_max: int, timeout: int) -> int:
    import random
    taken = {r["pay_amount"] for r in await pool.fetch("SELECT pay_amount FROM orders WHERE status IN ('pending','review')")}
    amount = price
    if offset_max > 0:
        for _ in range(200):
            cand = price + random.randint(1, offset_max)
            if cand not in taken:
                amount = cand
                break
    await pool.execute(
        "UPDATE orders SET status='pending', pay_amount=$2, expires_at=$3 WHERE id=$1",
        order_id, amount, utcnow() + timedelta(seconds=timeout))
    return amount


async def mark_paid(order_id: int, how: str):
    """Atomik: faqat pending buyurtma paid bo'ladi."""
    return await pool.fetchrow(
        "UPDATE orders SET status='paid', paid_with=$2, paid_at=now() "
        "WHERE id=$1 AND status IN ('pending','new','review') RETURNING *", order_id, how)

# ================= YETKAZISH (userbot, ko'p akkaunt) =================
clients: dict[int, TelegramClient] = {}     # account_id -> ulangan client
pending_login: dict[int, dict] = {}         # admin_id -> kirish jarayoni


async def api_creds():
    aid = API_ID or int(await get_setting("api_id") or 0)
    ah = API_HASH or await get_setting("api_hash")
    return aid, ah


async def connect_account(acc):
    aid, ah = await api_creds()
    if not (aid and ah):
        return None
    c = TelegramClient(StringSession(acc["session"]), aid, ah)
    await c.connect()
    if not await c.is_user_authorized():
        await c.disconnect()
        return None
    clients[acc["id"]] = c
    return c


async def userbot_start():
    # env'dagi eski TG_SESSION bo'lsa, akkaunt sifatida import qilinadi
    if TG_SESSION and not await pool.fetchval("SELECT count(*) FROM accounts"):
        await pool.execute("INSERT INTO accounts(phone,label,session) VALUES('env','env-session',$1)", TG_SESSION)
    for acc in await pool.fetch("SELECT * FROM accounts WHERE active ORDER BY id"):
        try:
            if not await connect_account(acc):
                log.warning("Akkaunt #%s ulanmadi (sessiya eskirgan yoki API_ID/HASH yo'q)", acc["id"])
        except Exception:
            log.exception("Akkaunt #%s", acc["id"])
    if not clients:
        log.warning("Userbot akkaunt yo'q: buyurtmalar qo'lda bajariladi. Botda /accounts orqali qo'shing.")


def any_client():
    return next(iter(clients.values()), None)


async def stars_balance(c) -> int:
    try:
        r = await c(functions.payments.GetStarsStatusRequest(peer=types.InputPeerSelf()))
        b = r.balance
        return int(getattr(b, "amount", b))
    except Exception:
        return -1


async def check_username(username: str) -> bool:
    c = any_client()
    if not c:
        return True  # tekshira olmaymiz
    try:
        await c.get_input_entity(username)
        return True
    except Exception:
        return False


async def list_gifts() -> list[tuple[int, int, str]]:
    """(gift_id, yulduz narxi, emoji) ro'yxati."""
    c = any_client()
    if not c:
        return []
    res = await c(functions.payments.GetStarGiftsRequest(hash=0))
    out = []
    for g in getattr(res, "gifts", []):
        emoji = getattr(getattr(g, "sticker", None), "alt", "") or ""
        out.append((g.id, g.stars, emoji))
    return out


async def _pay(c, invoice):
    form = await c(functions.payments.GetPaymentFormRequest(invoice=invoice))
    await c(functions.payments.SendStarsFormRequest(form_id=form.form_id, invoice=invoice))


async def _deliver_with(c, order, product) -> str:
    kind = order["kind"]
    peer = await c.get_input_entity(order["target_username"])
    if kind == "gift":
        msg = None
        if order["comment"]:
            msg = types.TextWithEntities(text=order["comment"], entities=[])
        inv = types.InputInvoiceStarGift(
            peer=peer, gift_id=int(product["gift_id"]),
            hide_name=bool(order["anonymous"]), message=msg)
        await _pay(c, inv)
        return "done"
    if kind == "premium":
        inv = types.InputInvoicePremiumGiftStars(user_id=peer, months=int(order["amount"]), message=None)
        await _pay(c, inv)
        return "done"
    return "manual"


async def deliver(order, product) -> str:
    """Akkauntlarni Stars balansi ko'pidan boshlab sinaydi; hammasi xato bersa Exception."""
    kind = order["kind"]
    if not clients or kind == "stars":
        return "manual"   # Stars: Fragment/TON orqali admin qo'lda
    if kind == "gift" and (not product or not product["gift_id"]):
        return "manual"   # gift_id kiritilmagan: /setgift bilan sozlang
    if kind == "premium" and not hasattr(types, "InputInvoicePremiumGiftStars"):
        return "manual"
    ranked = sorted([(await stars_balance(c), aid, c) for aid, c in clients.items()], key=lambda x: -x[0])
    last = None
    for bal, aid, c in ranked:
        try:
            res = await _deliver_with(c, order, product)
            log.info("Buyurtma #%s akkaunt #%s orqali bajarildi", order["code"], aid)
            return res
        except Exception as e:
            log.warning("Akkaunt #%s xato: %s", aid, e)
            last = e
    raise last or RuntimeError("Akkaunt topilmadi")

# ================= TO'LOV (SMS webhook) =================
NUM_RE = re.compile(r"\d[\d\s.,]*\d|\d")


def extract_amounts(text: str) -> set[int]:
    out = set()
    for m in NUM_RE.findall(text):
        s = m.replace(" ", "")
        # "6,000.00" / "6000.00" / "6 000,00" ko'rinishlari
        s = re.sub(r"[.,]\d{2}$", "", s)
        s = s.replace(",", "").replace(".", "")
        if s.isdigit() and 0 < len(s) < 10:
            out.add(int(s))
    return out


async def handle_sms(request: web.Request):
    if request.headers.get("X-Secret") != PAY_SECRET:
        return web.json_response({"ok": False}, status=403)
    try:
        data = await request.json()
    except Exception:
        data = dict(await request.post())
    text = str(data.get("text", ""))
    if not text:
        return web.json_response({"ok": False, "error": "empty"}, status=400)
    # Faqat kirim xabarlarini qabul qilamiz (chiqim/yechilish emas)
    low = text.lower()
    if any(w in low for w in ("yechildi", "списание", "purchase", "chiqim", "oplata")) and not any(
            w in low for w in ("kirim", "пополнение", "popolnenie", "+", "tushdi", "поступление")):
        return web.json_response({"ok": True, "matched": False, "reason": "not income"})
    h = hashlib.sha256(text.encode()).hexdigest()
    seen = await pool.fetchval(
        "SELECT 1 FROM sms_log WHERE h=$1 AND at > now() - interval '2 minutes'", h)
    if seen:
        return web.json_response({"ok": True, "matched": False, "reason": "duplicate"})
    await pool.execute(
        "INSERT INTO sms_log(h) VALUES($1) ON CONFLICT(h) DO UPDATE SET at=now()", h)

    amounts = list(extract_amounts(text))
    if not amounts:
        return web.json_response({"ok": True, "matched": False})
    row = await pool.fetchrow(
        "SELECT id FROM orders WHERE status IN ('pending','review') AND pay_amount = ANY($1::bigint[]) "
        "AND (expires_at > now() OR status='review') ORDER BY created_at LIMIT 1", amounts)
    if not row:
        return web.json_response({"ok": True, "matched": False})
    order = await mark_paid(row["id"], "card")
    if order:
        await on_paid(order)
    return web.json_response({"ok": True, "matched": bool(order)})


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/sms", handle_sms)
    app.router.add_get("/health", lambda r: web.json_response({"ok": True}))
    return app

# ================= SESSION =================

def make_session():
    api_id = int(input("API_ID: "))
    api_hash = input("API_HASH: ").strip()
    with TelegramClient(StringSession(), api_id, api_hash) as c:
        print("\nTG_SESSION =\n" + c.session.save())

# ================= BOT =================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{4,31})$")
NAME_RE = re.compile(r"^[\w .\-]{3,16}$", re.UNICODE)
KIND_TITLE = {"premium": "⭐ Premium", "gift": "🎁 Gift", "stars": "🌟 Stars"}
B_SELF, B_PICK, B_CANCEL = "🙋‍♂️ O'zimga", "👤 Shaxsiy chatdan tanlash", "❌ Bekor qilish"


def money(n) -> str:
    return f"{int(n):,}".replace(",", " ") + " so'm"


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def is_owner(uid: int) -> bool:
    return bool(OWNER_ID) and uid == OWNER_ID


async def show(target, text: str, reply_markup=None):
    """Tugmadan kelsa xabarni tahrirlaydi, buyruqdan kelsa yangi xabar yuboradi."""
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=reply_markup)
    else:
        await target.answer(text, reply_markup=reply_markup)


class Flow(StatesGroup):
    target = State()
    comment = State()
    stars_amount = State()
    display_name = State()
    promo = State()


# ---------------------------------------------------------------- majburiy obuna
async def not_subscribed(uid: int) -> list:
    rows = await pool.fetch("SELECT * FROM channels")
    bad = []
    for r in rows:
        try:
            m = await bot.get_chat_member(r["chat_id"], uid)
            if m.status in ("left", "kicked"):
                bad.append(r)
        except Exception:
            continue
    return bad


async def sub_prompt(target, bad):
    kb = InlineKeyboardBuilder()
    for r in bad:
        kb.button(text="📢 Obuna bo'lish", url=r["link"])
    kb.button(text="✅ Tekshirish", callback_data="chk")
    kb.adjust(1)
    text = "Botdan foydalanish uchun kanallarga obuna bo'ling:"
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, reply_markup=kb.as_markup())
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb.as_markup())


class Guard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = event.from_user
        if not user or is_admin(user.id):
            return await handler(event, data)
        row = await pool.fetchrow("SELECT banned FROM users WHERE id=$1", user.id)
        if row and row["banned"]:
            return
        if isinstance(event, CallbackQuery) and event.data == "chk":
            return await handler(event, data)
        if isinstance(event, Message) and event.chat.type != "private":
            return
        bad = await not_subscribed(user.id)
        if bad:
            return await sub_prompt(event, bad)
        return await handler(event, data)


dp.message.middleware(Guard())
dp.callback_query.middleware(Guard())


# ---------------------------------------------------------------- menyular
def main_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ Premium", callback_data="cat:premium")
    kb.button(text="🎁 Gift", callback_data="cat:gift")
    kb.button(text="🌟 Stars", callback_data="cat:stars")
    kb.button(text="👤 Profil", callback_data="profile")
    kb.button(text="🤝 Referal", callback_data="ref")
    kb.button(text="🆘 Yordam", callback_data="help")
    kb.adjust(3, 2, 1)
    return kb.as_markup()


WELCOME = ("<b>🛒 HUNTER MARKET</b>\n\n⚡ Tez va qulay buyurtma berish\n✅ Ishonchli xizmat, sifatli mahsulotlar\n"
           "🚚 Tezkor yetkazib berish\n💬 24/7 mijozlarga yordam\n\nKerakli bo'limni tanlang 👇")


@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    ref = None
    parts = (m.text or "").split()
    if len(parts) > 1 and parts[1].startswith("ref_") and parts[1][4:].isdigit():
        ref = int(parts[1][4:])
    await upsert_user(m.from_user.id, m.from_user.username, m.from_user.full_name, ref)
    await m.answer("👋", reply_markup=ReplyKeyboardRemove())
    await m.answer(WELCOME, reply_markup=main_kb())


@dp.callback_query(F.data == "chk")
async def chk(c: CallbackQuery):
    if await not_subscribed(c.from_user.id):
        return await c.answer("Hali obuna bo'lmadingiz", show_alert=True)
    await c.message.delete()
    await c.message.answer(WELCOME, reply_markup=main_kb())


@dp.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text(WELCOME, reply_markup=main_kb())


@dp.message(Command("help"))
@dp.callback_query(F.data == "help")
async def help_(c):
    who = f"@{SUPPORT_USERNAME}" if SUPPORT_USERNAME else "admin"
    kb = InlineKeyboardBuilder().button(text="⬅️ Orqaga", callback_data="menu")
    await show(c, f"🆘 Savol va muammolar uchun: {who}", reply_markup=kb.as_markup())


@dp.message(Command("profile"))
@dp.callback_query(F.data == "profile")
async def profile(c):
    u = await pool.fetchrow("SELECT * FROM users WHERE id=$1", c.from_user.id)
    n = await pool.fetchval("SELECT count(*) FROM orders WHERE user_id=$1 AND status='done'", c.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Promokod", callback_data="promo")
    kb.button(text="✏️ Monitoring nomi", callback_data="name:0")
    kb.button(text="⬅️ Orqaga", callback_data="menu")
    kb.adjust(1)
    await show(
        c, f"👤 <b>Profil</b>\n\n🆔 <code>{u['id']}</code>\n💰 Balans: <b>{money(u['balance'])}</b>\n"
        f"📦 Bajarilgan buyurtmalar: <b>{n}</b>\n🕶 Monitoring nomi: <b>{u['display_name'] or '—'}</b>",
        reply_markup=kb.as_markup())


@dp.message(Command("ref"))
@dp.callback_query(F.data == "ref")
async def ref(c):
    pct = await get_setting("ref_percent")
    cnt = await pool.fetchval("SELECT count(*) FROM users WHERE ref_by=$1", c.from_user.id)
    kb = InlineKeyboardBuilder().button(text="⬅️ Orqaga", callback_data="menu")
    await show(
        c, f"🤝 <b>Referal dasturi</b>\n\nDo'stingiz buyurtma bersa, siz <b>{pct}%</b> balansga olasiz.\n\n"
        f"🔗 Havolangiz:\n<code>https://t.me/{BOT_USERNAME}?start=ref_{c.from_user.id}</code>\n\n"
        f"👥 Taklif qilinganlar: <b>{cnt}</b>", reply_markup=kb.as_markup())


@dp.callback_query(F.data == "promo")
async def promo_ask(c: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.promo)
    await c.message.answer("🎟 Promokodni yuboring:")
    await c.answer()


@dp.message(Flow.promo)
async def promo_use(m: Message, state: FSMContext):
    code = (m.text or "").strip().upper()
    await state.clear()
    p = await pool.fetchrow("SELECT * FROM promos WHERE code=$1", code)
    if not p or p["used"] >= p["max_uses"]:
        return await m.answer("❌ Promokod topilmadi yoki tugagan.")
    ok = await pool.fetchval(
        "INSERT INTO promo_used VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING 1", code, m.from_user.id)
    if not ok:
        return await m.answer("❌ Bu promokodni allaqachon ishlatgansiz.")
    await pool.execute("UPDATE promos SET used=used+1 WHERE code=$1", code)
    await add_balance(m.from_user.id, p["amount"])
    await m.answer(f"✅ Balansingizga <b>{money(p['amount'])}</b> qo'shildi.")


@dp.message(Command("myid"))
async def myid(m: Message):
    role = "👑 owner" if is_owner(m.from_user.id) else ("🛠 admin" if is_admin(m.from_user.id) else "oddiy foydalanuvchi")
    await m.answer(f"🆔 Sizning ID: <code>{m.from_user.id}</code>\nRol: {role}")


@dp.message(Command("promo"))
async def promo_cmd(m: Message, state: FSMContext):
    await state.set_state(Flow.promo)
    await m.answer("🎟 Promokodni yuboring:")


ORDER_ICON = {"pending": "⏳", "review": "🔎", "paid": "🚚", "manual": "🚚", "done": "✅",
              "failed": "⚠️", "cancelled": "❌", "expired": "❌", "rejected": "❌"}


@dp.message(Command("myorders"))
async def myorders(m: Message):
    rows = await pool.fetch(
        "SELECT * FROM orders WHERE user_id=$1 AND status<>'new' ORDER BY id DESC LIMIT 5", m.from_user.id)
    if not rows:
        return await m.answer("📦 Hali buyurtmalaringiz yo'q.")
    await m.answer("📦 <b>Oxirgi buyurtmalar</b>\n\n" + "\n".join(
        f"{ORDER_ICON.get(r['status'], '•')} <code>#{r['code']}</code> · {r['title']} · {money(r['price'])}"
        for r in rows))


# ---------------------------------------------------------------- katalog
@dp.callback_query(F.data.startswith("cat:"))
async def category(c: CallbackQuery, state: FSMContext):
    await state.clear()
    kind = c.data.split(":")[1]
    rows = await pool.fetch("SELECT * FROM products WHERE kind=$1 AND active ORDER BY sort,id", kind)
    kb = InlineKeyboardBuilder()
    for p in rows:
        label = f"{p['emoji']} {p['title']} · {money(p['price'])}"
        kb.button(text=label, callback_data=f"prod:{p['id']}")
    if kind == "stars":
        kb.button(text="✍️ Boshqa miqdor kiritish", callback_data="stars:custom")
    kb.button(text="⬅️ Orqaga", callback_data="menu")
    kb.adjust(2 if kind == "gift" else 1)
    ask = {"premium": "Qancha muddatga Premium olmoqchisiz?", "gift": "Qaysi sovg'ani yubormoqchisiz?",
           "stars": "Necha dona Stars sotib olmoqchisiz?"}[kind]
    await c.message.edit_text(f"<b>Xizmatlarimiz</b>\n\n{ask}", reply_markup=kb.as_markup())


@dp.callback_query(F.data == "stars:custom")
async def stars_custom(c: CallbackQuery, state: FSMContext):
    mn = await get_setting("stars_min")
    await state.set_state(Flow.stars_amount)
    await c.message.answer(f"✍️ Necha Stars kerak? (kamida {mn})")
    await c.answer()


@dp.message(Flow.stars_amount)
async def stars_amount(m: Message, state: FSMContext):
    mn, per = int(await get_setting("stars_min")), int(await get_setting("stars_price"))
    if not (m.text or "").strip().isdigit() or int(m.text) < mn:
        return await m.answer(f"❌ Kamida {mn} ta raqam kiriting.")
    n = int(m.text)
    await state.update_data(product={"id": 0, "kind": "stars", "title": f"{n} Stars", "emoji": "⭐",
                                     "amount": n, "price": n * per})
    await ask_target(m, state)


@dp.callback_query(F.data.startswith("prod:"))
async def product(c: CallbackQuery, state: FSMContext):
    p = await pool.fetchrow("SELECT * FROM products WHERE id=$1 AND active", int(c.data.split(":")[1]))
    if not p:
        return await c.answer("Mahsulot mavjud emas", show_alert=True)
    await state.update_data(product=dict(p))
    await c.message.delete()
    await ask_target(c.message, state, user=c.from_user)


def target_kb():
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text=B_SELF)],
        [KeyboardButton(text=B_PICK, request_users=KeyboardButtonRequestUsers(
            request_id=1, user_is_bot=False, max_quantity=1, request_username=True))],
        [KeyboardButton(text=B_CANCEL)]])


async def ask_target(m: Message, state: FSMContext, user=None):
    d = await state.get_data()
    p = d["product"]
    await state.set_state(Flow.target)
    await m.answer(
        f"{p['emoji']} <b>{p['title']}</b> · {money(p['price'])}\n\n🎯 <b>Kimga yuborilsin?</b>\n"
        "Username yuboring (masalan: <code>@hunter</code>)\n\n⚠️ Faqat username qabul qilinadi, ID ishlamaydi.\n\n"
        "👇 Yoki pastdagi tugma orqali shaxsiy kontaktlaringizdan birini tanlang.", reply_markup=target_kb())


@dp.message(Flow.target, F.text == B_CANCEL)
async def target_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("❌ Bekor qilindi.", reply_markup=ReplyKeyboardRemove())
    await m.answer(WELCOME, reply_markup=main_kb())


async def set_target(m: Message, state: FSMContext, username: str):
    if not await check_username(username):
        return await m.answer("❌ Bu username topilmadi. Qayta yuboring.")
    await state.update_data(target=username)
    p = (await state.get_data())["product"]
    await m.answer("✅", reply_markup=ReplyKeyboardRemove())
    if p["kind"] != "gift":
        return await create_and_pay(m, state, m.from_user)
    kb = InlineKeyboardBuilder()
    kb.button(text="🙈 Anonim", callback_data="anon:1")
    kb.button(text="👤 Anonim emas", callback_data="anon:0")
    await m.answer("🎁 <b>Sovg'a sozlamalari</b>\n\n🙈 Sovg'a anonim yuborilsinmi? (ismingiz oluvchiga ko'rinmaydi)",
                   reply_markup=kb.as_markup())


@dp.message(Flow.target, F.users_shared)
async def got_shared(m: Message, state: FSMContext):
    u = m.users_shared.users[0]
    if not u.username:
        return await m.answer("❌ Bu foydalanuvchida username yo'q. Username'ni qo'lda yuboring.")
    await set_target(m, state, u.username)


@dp.message(Flow.target, F.text)
async def got_target(m: Message, state: FSMContext):
    if m.text == B_SELF:
        if not m.from_user.username:
            return await m.answer("❌ Sizda username yo'q. Sozlamalardan username o'rnating yoki boshqa username yuboring.")
        return await set_target(m, state, m.from_user.username)
    g = USERNAME_RE.match(m.text.strip())
    if not g:
        return await m.answer("❌ Username noto'g'ri. Masalan: <code>@hunter</code>")
    await set_target(m, state, g.group(1))


@dp.callback_query(F.data.startswith("anon:"))
async def got_anon(c: CallbackQuery, state: FSMContext):
    await state.update_data(anon=c.data.endswith("1"))
    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Izoh yozaman", callback_data="cmt:1")
    kb.button(text="🚫 Izohsiz", callback_data="cmt:0")
    await c.message.edit_text("💬 Sovg'aga izoh (xabar) qo'shasizmi?", reply_markup=kb.as_markup())


@dp.callback_query(F.data == "cmt:1")
async def cmt_yes(c: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.comment)
    await c.message.edit_text("✍️ Izohni yozing (150 belgigacha):")


@dp.message(Flow.comment, F.text)
async def cmt_text(m: Message, state: FSMContext):
    if len(m.text) > 150:
        return await m.answer("❌ 150 belgidan oshmasin.")
    await state.update_data(comment=m.text)
    await create_and_pay(m, state, m.from_user)


@dp.callback_query(F.data == "cmt:0")
async def cmt_no(c: CallbackQuery, state: FSMContext):
    await c.message.delete()
    await create_and_pay(c.message, state, c.from_user)


# ---------------------------------------------------------------- buyurtma va to'lov
async def create_and_pay(m: Message, state: FSMContext, user):
    d = await state.get_data()
    p = d["product"]
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    oid = await pool.fetchval(
        "INSERT INTO orders(code,user_id,product_id,kind,title,amount,price,target_username,anonymous,comment) "
        "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id",
        code, user.id, p["id"] or None, p["kind"], f"{p['emoji']} {p['title']}", p["amount"], p["price"],
        d["target"], d.get("anon", False), d.get("comment"))
    await state.clear()
    await show_payment(m.chat.id, oid)


def pay_kb(oid: int, show_balance: bool):
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Kartani nusxalash", copy_text=CopyTextButton(text=CARD_NUMBER.replace(" ", "")))
    kb.button(text="✅ To'ladim", callback_data=f"paid:{oid}")
    if show_balance:
        kb.button(text="💰 Balansdan to'lash", callback_data=f"bal:{oid}")
    kb.button(text="❌ Bekor qilish", callback_data=f"cancel:{oid}")
    kb.button(text="⬅️ Orqaga", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


async def show_payment(chat_id: int, oid: int):
    o = await pool.fetchrow("SELECT * FROM orders WHERE id=$1", oid)
    amount = await assign_pay_amount(oid, o["price"], UNIQUE_OFFSET_MAX, PAY_TIMEOUT)
    bal = await pool.fetchval("SELECT balance FROM users WHERE id=$1", o["user_id"])
    text = (f"💳 <b>To'lovni amalga oshiring</b>\n\n🏦 Karta: <code>{CARD_NUMBER}</code>\n"
            f"💵 To'lov summasi: <b>{money(amount)}</b>\nID: <code>DEP-{o['code']}</code>\n\n"
            f"⚠️ <b>Muhim:</b> aynan shu summani o'tkazing, so'ng pastdagi <b>«✅ To'ladim»</b> tugmasini bosing. Admin tekshirib tasdiqlaydi.\n\n"
            f"⏱ To'lov {PAY_TIMEOUT // 60} daqiqa amal qiladi.")
    msg = await bot.send_message(chat_id, text, reply_markup=pay_kb(oid, bal >= o["price"]))
    await pool.execute("UPDATE orders SET chat_id=$2, msg_id=$3 WHERE id=$1", oid, chat_id, msg.message_id)


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel(c: CallbackQuery):
    oid = int(c.data.split(":")[1])
    r = await pool.fetchval(
        "UPDATE orders SET status='cancelled' WHERE id=$1 AND user_id=$2 AND status IN ('pending','new') RETURNING id",
        oid, c.from_user.id)
    await c.message.edit_text("❌ Buyurtma bekor qilindi." if r else "Buyurtmani bekor qilib bo'lmaydi.",
                              reply_markup=InlineKeyboardBuilder().button(text="🏠 Bosh menyu", callback_data="menu").as_markup())


@dp.callback_query(F.data.startswith("bal:"))
async def pay_balance(c: CallbackQuery):
    oid = int(c.data.split(":")[1])
    o = await pool.fetchrow("SELECT * FROM orders WHERE id=$1 AND user_id=$2 AND status='pending'", oid, c.from_user.id)
    if not o:
        return await c.answer("Buyurtma faol emas", show_alert=True)
    if not await take_balance(c.from_user.id, o["price"]):
        return await c.answer("Balans yetarli emas", show_alert=True)
    paid = await mark_paid(oid, "balance")
    if not paid:
        await add_balance(c.from_user.id, o["price"])
        return await c.answer("Buyurtma faol emas", show_alert=True)
    await on_paid(paid)


async def on_paid(o):
    """To'lov tasdiqlandi -> yetkazish."""
    uid = o["user_id"]
    price = o["price"]
    try:
        await bot.edit_message_text(
            f"🧾 <b>To'lov qabul qilindi</b>\n\n💰 {money(price)}\n📦 {o['title']}\n\n⏳ Natija alohida xabarda keladi.",
            chat_id=o["chat_id"], message_id=o["msg_id"])
    except Exception:
        await bot.send_message(uid, "🧾 <b>To'lov qabul qilindi.</b> ⏳ Natija alohida xabarda keladi.")
    product = await pool.fetchrow("SELECT * FROM products WHERE id=$1", o["product_id"]) if o["product_id"] else None
    try:
        result = await deliver(o, product)
    except Exception as e:
        log.exception("delivery failed")
        await pool.execute("UPDATE orders SET status='failed', error=$2 WHERE id=$1", o["id"], str(e)[:300])
        await add_balance(uid, price)
        await bot.send_message(uid, f"⚠️ Buyurtmani bajarib bo'lmadi. <b>{money(price)}</b> balansingizga qaytarildi.")
        await notify_admins(f"❌ Buyurtma #{o['code']} xato: <code>{str(e)[:200]}</code>\nPul foydalanuvchi balansiga qaytarildi.")
        return
    if result == "manual":
        await pool.execute("UPDATE orders SET status='manual' WHERE id=$1", o["id"])
        kb = InlineKeyboardBuilder().button(text="✅ Bajarildi", callback_data=f"adone:{o['id']}")
        await notify_admins(
            f"🛠 <b>Qo'lda bajarish kerak</b>\n#{o['code']} · {o['title']}\n👤 @{o['target_username']}\n"
            f"🙈 Anonim: {'ha' if o['anonymous'] else 'yo`q'}\n💬 {o['comment'] or '—'}", kb.as_markup())
        return
    await finish_order(o["id"])


async def finish_order(oid: int):
    o = await pool.fetchrow(
        "UPDATE orders SET status='done', done_at=now() WHERE id=$1 AND status IN ('paid','manual') RETURNING *", oid)
    if not o:
        return
    u = await pool.fetchrow("SELECT * FROM users WHERE id=$1", o["user_id"])
    kb = InlineKeyboardBuilder()
    if MONITOR_CHANNEL:
        kb.button(text="📡 Monitoring", url=f"https://t.me/{MONITOR_CHANNEL.lstrip('@')}")
    for i in range(1, 6):
        kb.button(text=f"{i}⭐", callback_data=f"rate:{o['id']}:{i}")
    kb.adjust(1, 5)
    await bot.send_message(
        o["user_id"], f"✅ <b>To'lov muvaffaqiyatli qabul qilindi!</b>\n\n📦 Buyurtma: {o['title']}\n"
        f"🆔 Buyurtma ID: <code>#{o['code']}</code>\n\n🚀 <b>Buyurtmangiz muvaffaqiyatli bajarildi.</b>", reply_markup=kb.as_markup())
    # anonim sovg'a: oluvchiga xabar (agar bot bilan ishlagan bo'lsa)
    if o["kind"] == "gift" and o["anonymous"]:
        r = await pool.fetchrow("SELECT id FROM users WHERE lower(username)=lower($1)", o["target_username"])
        if r:
            try:
                await bot.send_message(r["id"], f"🎁 Bu sovg'ani kimdir sizga @{BOT_USERNAME} orqali anonim tarzda buyurtma qildi")
            except Exception:
                pass
    if MONITOR_CHANNEL:
        try:
            await bot.send_message(MONITOR_CHANNEL, f"✅ <b>{u['display_name'] or 'Mijoz'}</b> — {o['title']} buyurtmasi bajarildi.")
        except Exception:
            log.warning("monitoring kanaliga yozib bo'lmadi")
    pct = int(await get_setting("ref_percent"))
    if u["ref_by"] and pct:
        bonus = o["price"] * pct // 100
        await add_balance(u["ref_by"], bonus)
        try:
            await bot.send_message(u["ref_by"], f"🤝 Referal bonusi: <b>+{money(bonus)}</b>")
        except Exception:
            pass


@dp.callback_query(F.data.startswith("rate:"))
async def rate(c: CallbackQuery, state: FSMContext):
    _, oid, n = c.data.split(":")
    await pool.execute("UPDATE orders SET rating=$2 WHERE id=$1 AND user_id=$3", int(oid), int(n), c.from_user.id)
    await c.answer("Rahmat! ⭐")
    u = await pool.fetchrow("SELECT display_name FROM users WHERE id=$1", c.from_user.id)
    await c.message.edit_reply_markup(reply_markup=None)
    if not u["display_name"]:
        await ask_name(c.message, state, c.from_user.id)


async def ask_name(m: Message, state: FSMContext, uid: int):
    price = await get_setting("name_change_price")
    await state.set_state(Flow.display_name)
    await m.answer(
        "🕶 <b>Maxfiylik uchun o'zingizga nom qo'ying</b>\n\n📡 Monitoring kanalimiz ommaviy — u yerda haqiqiy ismingiz "
        "yoki username'ingiz KO'RINMAYDI, faqat shu nom ko'rinadi.\n\n✍️ 3–16 belgi: harf, raqam, probel, _ . -\n"
        f"🔁 Keyin o'zgartirish: 7 kunda 1 marta bepul (oldinroq — {money(price)}).\n\nNomingizni yozib yuboring 👇")


@dp.callback_query(F.data == "name:0")
async def name_btn(c: CallbackQuery, state: FSMContext):
    await ask_name(c.message, state, c.from_user.id)
    await c.answer()


@dp.message(Flow.display_name, F.text)
async def set_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if not NAME_RE.match(name):
        return await m.answer("❌ 3–16 belgi: harf, raqam, probel, _ . -")
    u = await pool.fetchrow("SELECT * FROM users WHERE id=$1", m.from_user.id)
    if u["display_name"] and u["name_changed_at"] and utcnow() - u["name_changed_at"] < timedelta(days=7):
        price = int(await get_setting("name_change_price"))
        if not await take_balance(m.from_user.id, price):
            await state.clear()
            return await m.answer(f"❌ 7 kundan oldin o'zgartirish pullik: {money(price)}. Balans yetarli emas.")
    await pool.execute("UPDATE users SET display_name=$2, name_changed_at=now() WHERE id=$1", m.from_user.id, name)
    await state.clear()
    await m.answer(f"✅ Monitoring nomingiz: <b>{name}</b>")


# ---------------------------------------------------------------- admin
async def notify_admins(text: str, markup=None):
    for a in ADMIN_IDS:
        try:
            await bot.send_message(a, text, reply_markup=markup)
        except Exception:
            pass


OWNER_ONLY = {"addadmin", "deladmin", "admins", "accounts", "card", "addbal", "newpromo",
              "starsprice", "refpct", "addch", "delch", "broadcast"}


def admin_only(fn):
    async def w(m: Message, *a, **k):
        if not is_admin(m.from_user.id):
            return
        cmd = (m.text or "").split()[0].lstrip("/").split("@")[0].lower() if m.text else ""
        if cmd in OWNER_ONLY and not is_owner(m.from_user.id):
            return await m.answer("🔒 Bu buyruq faqat owner uchun.")
        return await fn(m, *a, **k)
    return w


@dp.message(Command("admin"))
@admin_only
async def admin(m: Message):
    t = ("<b>🛠 Admin panel</b>\n\n/stats — statistika\n/orders — oxirgi buyurtmalar\n/pending — tasdiqlash kutayotganlar\n"
         "/products — mahsulotlar\n/price &lt;id&gt; &lt;narx&gt; — narx o'zgartirish\n/toggle &lt;id&gt; — yoqish/o'chirish\n"
         "/addprod kind|nom|emoji|gift_id|miqdor|narx\n/setgift &lt;id&gt; &lt;gift_id&gt; — gift ID bog'lash\n"
         "/gifts — userbot orqali mavjud giftlar\n/ban &lt;id&gt; /unban &lt;id&gt;")
    if is_owner(m.from_user.id):
        t += ("\n\n<b>👑 Owner buyruqlari</b>\n/addadmin &lt;id&gt; — admin qo'shish (yoki xabarga reply)\n"
              "/deladmin &lt;id&gt; — adminni olib tashlash\n/admins — adminlar ro'yxati\n"
              "/accounts — userbot akkauntlar\n/card &lt;raqam&gt; — karta almashtirish\n/addbal &lt;user_id&gt; &lt;summa&gt;\n"
              "/newpromo KOD SUMMA LIMIT — promokod yaratish\n/starsprice &lt;so'm&gt; — 1 Stars narxi\n/refpct &lt;foiz&gt;\n"
              "/addch &lt;@kanal yoki -100id&gt; &lt;havola&gt; — majburiy obuna\n/delch &lt;chat_id&gt;\n"
              "/broadcast — xabarga reply qiling")
    await m.answer(t)


@dp.message(Command("stats"))
@admin_only
async def stats(m: Message):
    q = pool.fetchval
    users = await q("SELECT count(*) FROM users")
    done = await q("SELECT count(*) FROM orders WHERE status='done'")
    rev = await q("SELECT coalesce(sum(price),0) FROM orders WHERE status='done'")
    today = await q("SELECT coalesce(sum(price),0) FROM orders WHERE status='done' AND done_at::date=now()::date")
    pend = await q("SELECT count(*) FROM orders WHERE status IN ('pending','manual')")
    bal = await q("SELECT coalesce(sum(balance),0) FROM users")
    await m.answer(f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: {users}\n✅ Bajarilgan: {done}\n💰 Jami tushum: {money(rev)}\n"
                   f"📅 Bugun: {money(today)}\n⏳ Kutilayotgan/qo'lda: {pend}\n🏦 Mijozlar balansi: {money(bal)}")


@dp.message(Command("orders"))
@admin_only
async def orders(m: Message):
    rows = await pool.fetch("SELECT * FROM orders ORDER BY id DESC LIMIT 15")
    await m.answer("\n".join(f"#{r['code']} · {r['title']} · {money(r['price'])} · @{r['target_username']} · <b>{r['status']}</b>"
                             for r in rows) or "Buyurtma yo'q")


@dp.message(Command("products"))
@admin_only
async def products(m: Message):
    rows = await pool.fetch("SELECT * FROM products ORDER BY kind,sort,id")
    await m.answer("\n".join(f"{'✅' if r['active'] else '❌'} <b>{r['id']}</b> {r['kind']} {r['emoji']} {r['title']} · "
                             f"{money(r['price'])} · gift_id={r['gift_id']}" for r in rows))


async def _arg_int(m: Message, n: int):
    p = (m.text or "").split()[1:]
    if len(p) < n or not all(x.lstrip("-").isdigit() for x in p[:n]):
        await m.answer("❌ Format noto'g'ri. /admin ga qarang.")
        return None
    return [int(x) for x in p[:n]]


@dp.message(Command("price"))
@admin_only
async def price(m: Message):
    a = await _arg_int(m, 2)
    if a:
        await pool.execute("UPDATE products SET price=$2 WHERE id=$1", *a)
        await m.answer("✅ Narx yangilandi")


@dp.message(Command("toggle"))
@admin_only
async def toggle(m: Message):
    a = await _arg_int(m, 1)
    if a:
        await pool.execute("UPDATE products SET active=NOT active WHERE id=$1", a[0])
        await m.answer("✅ Holat o'zgardi")


@dp.message(Command("setgift"))
@admin_only
async def setgift(m: Message):
    a = await _arg_int(m, 2)
    if a:
        await pool.execute("UPDATE products SET gift_id=$2 WHERE id=$1", *a)
        await m.answer("✅ Gift ID bog'landi")


@dp.message(Command("addprod"))
@admin_only
async def addprod(m: Message):
    try:
        kind, title, emoji, gid, amt, pr = [x.strip() for x in m.text.split(None, 1)[1].split("|")]
        await pool.execute(
            "INSERT INTO products(kind,title,emoji,gift_id,amount,price,sort) VALUES($1,$2,$3,$4,$5,$6,99)",
            kind, title, emoji, int(gid), int(amt), int(pr))
        await m.answer("✅ Qo'shildi")
    except Exception:
        await m.answer("Format: /addprod gift|Nom|🎁|gift_id|0|6000")


@dp.message(Command("gifts"))
@admin_only
async def gifts(m: Message):
    try:
        g = await list_gifts()
    except Exception as e:
        return await m.answer(f"❌ {e}")
    await m.answer("\n".join(f"{e} id=<code>{i}</code> · {s}⭐" for i, s, e in g[:60]) or "Userbot ulanmagan")


@dp.message(Command("starsprice"))
@admin_only
async def starsprice(m: Message):
    a = await _arg_int(m, 1)
    if a:
        await set_setting("stars_price", str(a[0]))
        await m.answer("✅")


@dp.message(Command("refpct"))
@admin_only
async def refpct(m: Message):
    a = await _arg_int(m, 1)
    if a:
        await set_setting("ref_percent", str(a[0]))
        await m.answer("✅")


@dp.message(Command("card"))
@admin_only
async def card(m: Message):
    parts = m.text.split(None, 1)
    if len(parts) == 2:
        global CARD_NUMBER
        CARD_NUMBER = parts[1].strip()
        await m.answer(f"✅ Karta: {CARD_NUMBER}\n(Doimiy bo'lishi uchun CARD_NUMBER env'ni ham o'zgartiring)")


@dp.message(Command("addbal"))
@admin_only
async def addbal(m: Message):
    a = await _arg_int(m, 2)
    if a:
        await add_balance(a[0], a[1])
        await m.answer("✅")


@dp.message(Command("newpromo"))
@admin_only
async def newpromo(m: Message):
    p = m.text.split()
    if len(p) == 4 and p[2].isdigit() and p[3].isdigit():
        await pool.execute("INSERT INTO promos(code,amount,max_uses) VALUES($1,$2,$3) "
                              "ON CONFLICT(code) DO UPDATE SET amount=$2,max_uses=$3", p[1].upper(), int(p[2]), int(p[3]))
        await m.answer("✅ Promokod yaratildi")


@dp.message(Command("addch"))
@admin_only
async def addch(m: Message):
    p = m.text.split()
    if len(p) == 3:
        await pool.execute("INSERT INTO channels VALUES($1,$2) ON CONFLICT DO NOTHING", p[1], p[2])
        await m.answer("✅ Qo'shildi (bot kanalda admin bo'lishi shart)")


@dp.message(Command("delch"))
@admin_only
async def delch(m: Message):
    p = m.text.split()
    if len(p) == 2:
        await pool.execute("DELETE FROM channels WHERE chat_id=$1", p[1])
        await m.answer("✅")


@dp.message(Command("ban", "unban"))
@admin_only
async def ban(m: Message):
    a = await _arg_int(m, 1)
    if a:
        await pool.execute("UPDATE users SET banned=$2 WHERE id=$1", a[0], m.text.startswith("/ban"))
        await m.answer("✅")


@dp.message(Command("broadcast"))
@admin_only
async def broadcast(m: Message):
    if not m.reply_to_message:
        return await m.answer("Yubormoqchi bo'lgan xabarga reply qilib /broadcast yozing.")
    ids = [r["id"] for r in await pool.fetch("SELECT id FROM users WHERE NOT banned")]
    ok = 0
    for uid in ids:
        try:
            await m.reply_to_message.copy_to(uid)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"📨 Yuborildi: {ok}/{len(ids)}")


@dp.callback_query(F.data.startswith("adone:"))
async def adone(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return
    await finish_order(int(c.data.split(":")[1]))
    await c.message.edit_text(c.message.html_text + "\n\n✅ Bajarildi")


# ---------------------------------------------------------------- qo'lda to'lov tasdiqlash
@dp.callback_query(F.data.startswith("paid:"))
async def user_paid(c: CallbackQuery):
    oid = int(c.data.split(":")[1])
    o = await pool.fetchrow(
        "UPDATE orders SET status='review' WHERE id=$1 AND user_id=$2 AND status='pending' AND expires_at > now() "
        "RETURNING *", oid, c.from_user.id)
    if not o:
        cur = await pool.fetchval("SELECT status FROM orders WHERE id=$1 AND user_id=$2", oid, c.from_user.id)
        if cur == "review":
            return await c.answer("⏳ To'lovingiz allaqachon tekshirilmoqda", show_alert=True)
        return await c.answer("Buyurtma faol emas yoki vaqti tugagan", show_alert=True)
    back = InlineKeyboardBuilder().button(text="🏠 Bosh menyu", callback_data="menu")
    await c.message.edit_text(
        f"⏳ <b>To'lovingiz tekshirilmoqda</b>\n\n📦 {o['title']}\n💵 {money(o['pay_amount'])}\n"
        f"🆔 <code>#{o['code']}</code>\n\nAdmin tasdiqlashi bilan natija shu yerga keladi.", reply_markup=back.as_markup())
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Tasdiqlash", callback_data=f"aok:{oid}")
    kb.button(text="❌ Rad etish", callback_data=f"ano:{oid}")
    kb.adjust(2)
    uname = f"@{c.from_user.username}" if c.from_user.username else "username yo'q"
    await notify_admins(
        f"💳 <b>To'lovni tasdiqlang</b>\n\n#{o['code']} · {o['title']}\n💵 Kutilgan summa: <b>{money(o['pay_amount'])}</b>\n"
        f"👤 {uname} (<code>{c.from_user.id}</code>)\n🎯 Oluvchi: @{o['target_username']}\n\n"
        "Uzum Bank ilovasida shu summa tushganini tekshiring.", kb.as_markup())
    await c.answer("✅ Adminga yuborildi")


@dp.callback_query(F.data.startswith("aok:"))
async def admin_ok(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer()
    o = await mark_paid(int(c.data.split(":")[1]), f"admin:{c.from_user.id}")
    if not o:
        return await c.answer("Allaqachon ishlangan yoki bekor qilingan", show_alert=True)
    await c.message.edit_text(c.message.html_text + f"\n\n✅ Tasdiqlandi (<code>{c.from_user.id}</code>)")
    await c.answer("✅")
    await on_paid(o)


@dp.callback_query(F.data.startswith("ano:"))
async def admin_no(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer()
    o = await pool.fetchrow("UPDATE orders SET status='rejected' WHERE id=$1 AND status='review' RETURNING *",
                            int(c.data.split(":")[1]))
    if not o:
        return await c.answer("Allaqachon ishlangan", show_alert=True)
    await c.message.edit_text(c.message.html_text + f"\n\n❌ Rad etildi (<code>{c.from_user.id}</code>)")
    await c.answer("❌")
    who = f"@{SUPPORT_USERNAME}" if SUPPORT_USERNAME else "admin"
    try:
        await bot.send_message(
            o["user_id"], f"❌ <b>To'lovingiz topilmadi</b> (#{o['code']}).\n"
                          f"Agar pul o'tkazgan bo'lsangiz, chek bilan {who} ga yozing.")
    except Exception:
        pass


@dp.message(Command("pending"))
@admin_only
async def pending(m: Message):
    rows = await pool.fetch("SELECT * FROM orders WHERE status IN ('review','manual') ORDER BY id LIMIT 10")
    if not rows:
        return await m.answer("✅ Kutayotgan buyurtma yo'q.")
    for o in rows:
        kb = InlineKeyboardBuilder()
        if o["status"] == "review":
            kb.button(text="✅ Tasdiqlash", callback_data=f"aok:{o['id']}")
            kb.button(text="❌ Rad etish", callback_data=f"ano:{o['id']}")
            kb.adjust(2)
        else:
            kb.button(text="✅ Bajarildi", callback_data=f"adone:{o['id']}")
        await m.answer(f"#{o['code']} · {o['title']} · <b>{money(o['pay_amount'] or o['price'])}</b>\n"
                       f"🎯 @{o['target_username']} · {o['status']}", reply_markup=kb.as_markup())


# ---------------------------------------------------------------- adminlarni boshqarish (owner)
USER_CMDS = [
    BotCommand(command="start", description="🏠 Bosh menyu"),
    BotCommand(command="profile", description="👤 Profilim va balans"),
    BotCommand(command="myorders", description="📦 Buyurtmalarim"),
    BotCommand(command="ref", description="🤝 Referal havolam"),
    BotCommand(command="promo", description="🎟 Promokod kiritish"),
    BotCommand(command="help", description="🆘 Yordam"),
]
ADMIN_CMDS = [
    BotCommand(command="admin", description="🛠 Admin panel"),
    BotCommand(command="pending", description="⏳ Tasdiqlash kutayotganlar"),
    BotCommand(command="stats", description="📊 Statistika"),
    BotCommand(command="orders", description="🧾 Oxirgi buyurtmalar"),
    BotCommand(command="products", description="🛍 Mahsulotlar"),
    BotCommand(command="price", description="💲 Narx o'zgartirish"),
    BotCommand(command="toggle", description="🔀 Mahsulotni yoqish/o'chirish"),
    BotCommand(command="ban", description="🚫 Foydalanuvchini bloklash"),
    BotCommand(command="unban", description="♻️ Blokdan chiqarish"),
]
OWNER_CMDS = [
    BotCommand(command="addadmin", description="👑 Admin qo'shish"),
    BotCommand(command="deladmin", description="👑 Adminni olib tashlash"),
    BotCommand(command="admins", description="👑 Adminlar ro'yxati"),
    BotCommand(command="accounts", description="👤 Userbot akkauntlar"),
    BotCommand(command="card", description="💳 Karta almashtirish"),
    BotCommand(command="addbal", description="💰 Balans qo'shish"),
    BotCommand(command="newpromo", description="🎟 Promokod yaratish"),
    BotCommand(command="broadcast", description="📨 Xabar tarqatish"),
    BotCommand(command="starsprice", description="⭐ 1 Stars narxi"),
    BotCommand(command="refpct", description="🤝 Referal foizi"),
]


async def load_admins():
    for r in await pool.fetch("SELECT id FROM admins"):
        ADMIN_IDS.add(r["id"])
    log.info("OWNER_ID=%s ADMIN_IDS=%s", OWNER_ID, sorted(ADMIN_IDS))
    if not OWNER_ID:
        log.warning("OWNER_ID yo'q: ADMIN_IDS yoki OWNER_ID env'ni to'ldiring")


async def setup_commands(only=None):
    """'/' menyusi: hamma uchun oddiy, adminlar va owner uchun kengaytirilgan."""
    if only is None:
        try:
            await bot.set_my_commands(USER_CMDS, scope=BotCommandScopeDefault())
        except Exception:
            log.exception("default buyruqlar")
    for uid in (only if only is not None else list(ADMIN_IDS)):
        cmds = USER_CMDS + ADMIN_CMDS + (OWNER_CMDS if is_owner(uid) else [])
        try:
            await bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id=uid))
        except Exception as e:
            log.warning("Admin %s uchun menyu o'rnatilmadi (botga /start bosmagan bo'lishi mumkin): %s", uid, e)


@dp.message(Command("addadmin"))
@admin_only
async def addadmin(m: Message):
    uid = None
    if m.reply_to_message and m.reply_to_message.from_user:
        uid = m.reply_to_message.from_user.id
    else:
        p = (m.text or "").split()
        if len(p) > 1 and p[1].isdigit():
            uid = int(p[1])
    if not uid:
        return await m.answer("Format: <code>/addadmin 123456789</code> yoki foydalanuvchi xabariga reply qiling.\n"
                              "ID ni foydalanuvchi /profile da ko'radi.")
    if uid in ADMIN_IDS:
        return await m.answer("Bu foydalanuvchi allaqachon admin.")
    await pool.execute("INSERT INTO admins(id) VALUES($1) ON CONFLICT DO NOTHING", uid)
    ADMIN_IDS.add(uid)
    await setup_commands([uid])
    try:
        await bot.send_message(uid, "🛠 Sizga admin huquqi berildi. /admin")
    except Exception:
        await m.answer("⚠️ Unga xabar yuborib bo'lmadi: u botga /start bosishi kerak.")
    await m.answer(f"✅ Admin qo'shildi: <code>{uid}</code>")


@dp.message(Command("deladmin"))
@admin_only
async def deladmin(m: Message):
    a = await _arg_int(m, 1)
    if not a:
        return
    uid = a[0]
    if uid == OWNER_ID:
        return await m.answer("❌ Ownerni olib bo'lmaydi.")
    if uid in ENV_ADMINS:
        return await m.answer("❌ Bu admin ADMIN_IDS env'da turibdi. Uni o'sha yerdan olib tashlang.")
    await pool.execute("DELETE FROM admins WHERE id=$1", uid)
    ADMIN_IDS.discard(uid)
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=uid))
    except Exception:
        pass
    await m.answer(f"✅ Admin olib tashlandi: <code>{uid}</code>")


@dp.message(Command("admins"))
@admin_only
async def admins_list(m: Message):
    lines = []
    for uid in sorted(ADMIN_IDS, key=lambda x: (x != OWNER_ID, x)):
        tag = "👑 owner" if uid == OWNER_ID else ("env" if uid in ENV_ADMINS else "admin")
        lines.append(f"<code>{uid}</code> · {tag}")
    await m.answer("👥 <b>Adminlar</b>\n\n" + "\n".join(lines))


# ---------------------------------------------------------------- userbot akkauntlar (admin)
class Acc(StatesGroup):
    creds = State()
    phone = State()
    code = State()
    password = State()


async def _drop_login(uid: int):
    st = pending_login.pop(uid, None)
    if st:
        try:
            await st["client"].disconnect()
        except Exception:
            pass


async def accounts_view():
    rows = await pool.fetch("SELECT * FROM accounts ORDER BY id")
    kb = InlineKeyboardBuilder()
    lines = []
    for r in rows:
        c = clients.get(r["id"])
        bal = await stars_balance(c) if c else None
        state = "🟢 ulangan" if c else ("⏸ o'chirilgan" if not r["active"] else "🔴 ulanmagan")
        lines.append(f"<b>#{r['id']}</b> {r['label'] or r['phone']} · {state}" + (f" · {bal}⭐" if bal is not None and bal >= 0 else ""))
        kb.button(text=f"{'⏸' if r['active'] else '▶️'} #{r['id']}", callback_data=f"acc:t:{r['id']}")
        kb.button(text=f"🗑 #{r['id']}", callback_data=f"acc:d:{r['id']}")
    kb.button(text="➕ Akkaunt qo'shish", callback_data="acc:add")
    kb.adjust(2)
    text = "👤 <b>Userbot akkauntlar</b>\n\n" + ("\n".join(lines) or "Hozircha akkaunt yo'q.") + \
           "\n\nBuyurtma Stars balansi eng ko'p akkaunt orqali yuboriladi; xato bo'lsa keyingisiga o'tadi."
    return text, kb.as_markup()


@dp.message(Command("accounts"))
@admin_only
async def accounts_cmd(m: Message):
    text, kb = await accounts_view()
    await m.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("acc:"))
async def acc_cb(c: CallbackQuery, state: FSMContext):
    if not is_owner(c.from_user.id):
        return await c.answer()
    parts = c.data.split(":")
    if parts[1] == "add":
        aid, ah = await api_creds()
        if not (aid and ah):
            await state.set_state(Acc.creds)
            await c.message.answer("🔑 my.telegram.org → API development tools dan olingan <b>API_ID</b> va <b>API_HASH</b> ni "
                                   "bitta xabarda yuboring:\n<code>12345678 0123456789abcdef0123456789abcdef</code>\n\n/cancel — bekor qilish")
        else:
            await state.set_state(Acc.phone)
            await c.message.answer("📱 Akkaunt telefon raqamini yuboring (masalan <code>+998901234567</code>).\n\n/cancel — bekor qilish")
        return await c.answer()
    acc_id = int(parts[2])
    if parts[1] == "t":
        r = await pool.fetchrow("UPDATE accounts SET active=NOT active WHERE id=$1 RETURNING *", acc_id)
        if r and r["active"]:
            try:
                await connect_account(r)
            except Exception:
                log.exception("ulanish")
        elif r:
            old = clients.pop(acc_id, None)
            if old:
                await old.disconnect()
    elif parts[1] == "d":
        await pool.execute("DELETE FROM accounts WHERE id=$1", acc_id)
        old = clients.pop(acc_id, None)
        if old:
            await old.disconnect()
    text, kb = await accounts_view()
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer("✅")


@dp.message(Command("cancel"))
async def cancel_any(m: Message, state: FSMContext):
    if await state.get_state() in (Acc.creds.state, Acc.phone.state, Acc.code.state, Acc.password.state):
        await _drop_login(m.from_user.id)
        await state.clear()
        await m.answer("❌ Bekor qilindi.")


@dp.message(Acc.creds, F.text)
async def acc_creds(m: Message, state: FSMContext):
    parts = m.text.split()
    if len(parts) != 2 or not parts[0].isdigit() or len(parts[1]) < 20:
        return await m.answer("❌ Format: <code>API_ID API_HASH</code>")
    await set_setting("api_id", parts[0])
    await set_setting("api_hash", parts[1])
    try:
        await m.delete()
    except Exception:
        pass
    await state.set_state(Acc.phone)
    await m.answer("✅ Saqlandi.\n📱 Endi akkaunt telefon raqamini yuboring (<code>+998901234567</code>).")


@dp.message(Acc.phone, F.text)
async def acc_phone(m: Message, state: FSMContext):
    phone = re.sub(r"[^\d+]", "", m.text)
    aid, ah = await api_creds()
    cl = TelegramClient(StringSession(), aid, ah)
    try:
        await cl.connect()
        sent = await cl.send_code_request(phone)
    except Exception as e:
        await cl.disconnect()
        return await m.answer(f"❌ Kod yuborib bo'lmadi: <code>{str(e)[:150]}</code>")
    await _drop_login(m.from_user.id)
    pending_login[m.from_user.id] = {"client": cl, "phone": phone, "hash": sent.phone_code_hash}
    await state.set_state(Acc.code)
    await m.answer("📨 Telegram akkauntga kod yubordi.\n\n⚠️ Kodni <b>raqamlar orasiga tire qo'yib</b> yuboring: "
                   "<code>1-2-3-4-5</code> — aks holda Telegram kodni bekor qiladi.")


@dp.message(Acc.code, F.text)
async def acc_code(m: Message, state: FSMContext):
    st = pending_login.get(m.from_user.id)
    if not st:
        await state.clear()
        return await m.answer("Sessiya tugagan. /accounts dan qayta boshlang.")
    code = re.sub(r"\D", "", m.text)
    try:
        await m.delete()
    except Exception:
        pass
    try:
        await st["client"].sign_in(st["phone"], code, phone_code_hash=st["hash"])
    except SessionPasswordNeededError:
        await state.set_state(Acc.password)
        return await m.answer("🔐 Akkauntda 2 bosqichli parol bor. Parolni yuboring (xabar darhol o'chiriladi).")
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        return await m.answer("❌ Kod noto'g'ri yoki eskirgan. Qayta yuboring yoki /cancel.")
    except Exception as e:
        return await m.answer(f"❌ Xato: <code>{str(e)[:150]}</code>")
    await _finish_login(m, state)


@dp.message(Acc.password, F.text)
async def acc_password(m: Message, state: FSMContext):
    st = pending_login.get(m.from_user.id)
    if not st:
        await state.clear()
        return await m.answer("Sessiya tugagan. /accounts dan qayta boshlang.")
    pw = m.text
    try:
        await m.delete()
    except Exception:
        pass
    try:
        await st["client"].sign_in(password=pw)
    except PasswordHashInvalidError:
        return await m.answer("❌ Parol noto'g'ri. Qayta yuboring yoki /cancel.")
    except Exception as e:
        return await m.answer(f"❌ Xato: <code>{str(e)[:150]}</code>")
    await _finish_login(m, state)


async def _finish_login(m: Message, state: FSMContext):
    st = pending_login.pop(m.from_user.id)
    cl = st["client"]
    me = await cl.get_me()
    label = f"@{me.username}" if me.username else (me.first_name or st["phone"])
    acc_id = await pool.fetchval("INSERT INTO accounts(phone,label,session) VALUES($1,$2,$3) RETURNING id",
                                 st["phone"], label, cl.session.save())
    clients[acc_id] = cl
    await state.clear()
    text, kb = await accounts_view()
    await m.answer(f"✅ Akkaunt ulandi: <b>{label}</b> (#{acc_id})\n\n" + text, reply_markup=kb)


# ---------------------------------------------------------------- fon vazifalar
async def expire_loop():
    while True:
        try:
            rows = await pool.fetch(
                "UPDATE orders SET status='expired' WHERE status='pending' AND expires_at < now() "
                "RETURNING id, chat_id, msg_id")
            for r in rows:
                try:
                    await bot.edit_message_text("⌛ To'lov vaqti tugadi. Buyurtmani qaytadan boshlang: /start",
                                                chat_id=r["chat_id"], message_id=r["msg_id"])
                except Exception:
                    pass
        except Exception:
            log.exception("expire loop")
        await asyncio.sleep(20)


async def main():
    await db_init(DATABASE_URL)
    await load_admins()
    await userbot_start()
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    asyncio.create_task(expire_loop())
    await setup_commands()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "session":
        make_session()
    else:
        asyncio.run(main())
