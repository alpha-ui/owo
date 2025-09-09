import asyncio
import sqlite3
import random
import time
import uuid
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters.callback_data import CallbackData
import os

# --- Load bot token from environment variable (Render) ---
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("Please set BOT_TOKEN environment variable")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- Database setup ---
conn = sqlite3.connect("game.db", check_same_thread=False)
cur = conn.cursor()

def db_execute(query, params=()):
    cur.execute(query, params)
    conn.commit()

def db_fetchone(query, params=()):
    cur.execute(query, params)
    return cur.fetchone()

def db_fetchall(query, params=()):
    cur.execute(query, params)
    return cur.fetchall()

# --- Tables ---
db_execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    balance INTEGER DEFAULT 0,
    last_daily INTEGER DEFAULT 0,
    last_hunt INTEGER DEFAULT 0,
    xp_boost_active INTEGER DEFAULT 0,
    double_coins_active INTEGER DEFAULT 0,
    autohunt_active INTEGER DEFAULT 0
)
""")

db_execute("""
CREATE TABLE IF NOT EXISTS zoo (
    user_id INTEGER,
    animal TEXT,
    rarity TEXT,
    count INTEGER DEFAULT 0,
    xp INTEGER DEFAULT 0,
    level INTEGER DEFAULT 1,
    evolution_stage INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, animal)
)
""")

db_execute("""
CREATE TABLE IF NOT EXISTS achievements (
    user_id INTEGER,
    achievement TEXT,
    unlocked INTEGER DEFAULT 0,
    reward_coins INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, achievement)
)
""")

db_execute("""
CREATE TABLE IF NOT EXISTS shop_items (
    item TEXT PRIMARY KEY,
    price INTEGER,
    effect TEXT
)
""")

# --- Shop items ---
shop_items = [
    ("XP Boost", 200, "xp_boost"),
    ("Double Coins", 300, "double_coins"),
    ("AutoHunt Boost", 500, "autohunt")
]
for item, price, effect in shop_items:
    db_execute("INSERT OR IGNORE INTO shop_items (item, price, effect) VALUES (?, ?, ?)", (item, price, effect))

# --- Animals and rarities ---
RARITY_POOL = {
    "Common": ["🐇 Rabbit", "🐿️ Squirrel", "🐥 Chick", "🐛 Caterpillar", "🐌 Snail"],
    "Uncommon": ["🦊 Fox", "🦌 Deer", "🦔 Hedgehog", "🦉 Owl", "🐗 Boar"],
    "Rare": ["🐻 Bear", "🦅 Eagle", "🦁 Lion", "🐆 Leopard", "🐊 Crocodile"],
    "Legendary": ["🐉 Dragon", "🦄 Unicorn", "🦎 Phoenix", "🦖 T-Rex", "🦕 Dinosaur"]
}
RARITY_WEIGHTS = {"Common": 70, "Uncommon": 20, "Rare": 8, "Legendary": 2}
RARITY_BONUS = {"Common":0, "Uncommon":10, "Rare":30, "Legendary":100}
EVOLUTION_EMOJIS = ["🐣", "🐥", "🦅", "🦖", "🌟"]
RARITY_EMOJIS = {"Common": "🟢", "Uncommon": "🔵", "Rare": "🟣", "Legendary": "🌟"}

# --- Constants ---
AUTOHUNT_COST = 20
autohunt_users = set()
trade_requests = {}

# --- Achievements ---
ACHIEVEMENTS = {
    "First Hunt": {"condition": lambda uid: sum([row[0] for row in db_fetchall("SELECT count FROM zoo WHERE user_id=?", (uid,))]) >= 1, "reward_coins": 50},
    "Collector": {"condition": lambda uid: sum([row[0] for row in db_fetchall("SELECT count FROM zoo WHERE user_id=?", (uid,))]) >= 10, "reward_coins": 200},
    "Rare Hunter": {"condition": lambda uid: db_fetchone("SELECT count(*) FROM zoo WHERE user_id=? AND rarity='Rare'", (uid,))[0] >= 3, "reward_coins": 300},
}

# --- Helper functions ---
def get_random_animal():
    rarities = list(RARITY_WEIGHTS.keys())
    weights = list(RARITY_WEIGHTS.values())
    rarity = random.choices(rarities, weights=weights, k=1)[0]
    animal = random.choice(RARITY_POOL[rarity])
    return animal, rarity

async def add_xp(uid, animal, amount=10):
    row = db_fetchone("SELECT xp, level, evolution_stage FROM zoo WHERE user_id=? AND animal=?", (uid, animal))
    if not row:
        return
    xp, level, evo = row
    xp += amount
    while xp >= level * 100:
        xp -= level * 100
        level += 1
        await bot.send_message(uid, f"✨ {animal} leveled up! Now Level {level}!")
        if level % 5 == 0:
            evo += 1
            await bot.send_message(uid, f"🌟 {animal} is evolving! Get ready...")
            for stage in range(min(evo, len(EVOLUTION_EMOJIS))):
                await bot.send_message(uid, EVOLUTION_EMOJIS[stage])
                await asyncio.sleep(0.3)
            await bot.send_message(uid, f"🎉 {animal} evolved to Stage {evo}! Amazing!")
    db_execute("UPDATE zoo SET xp=?, level=?, evolution_stage=? WHERE user_id=? AND animal=?", (xp, level, evo, uid, animal))

def ensure_achievements(uid):
    for ach_name, ach_data in ACHIEVEMENTS.items():
        db_execute("INSERT OR IGNORE INTO achievements (user_id, achievement, unlocked, reward_coins) VALUES (?, ?, 0, ?)", (uid, ach_name, ach_data["reward_coins"]))

def check_achievements(uid):
    ensure_achievements(uid)
    unlocked_messages = []
    for ach_name, ach_data in ACHIEVEMENTS.items():
        row = db_fetchone("SELECT unlocked FROM achievements WHERE user_id=? AND achievement=?", (uid, ach_name))
        if not row or row[0] == 0:
            if ach_data["condition"](uid):
                db_execute("UPDATE achievements SET unlocked=1 WHERE user_id=? AND achievement=?", (uid, ach_name))
                db_execute("UPDATE users SET balance=balance+? WHERE id=?", (ach_data["reward_coins"], uid))
                unlocked_messages.append(f"🏆 Achievement unlocked: {ach_name}! Reward: {ach_data['reward_coins']} coins.")
    return unlocked_messages

async def update_achievements_after_hunt(uid):
    for msg_text in check_achievements(uid):
        await bot.send_message(uid, msg_text)

# --- Battle system ---
class BattlePet(CallbackData, prefix="battle"):
    user_id: int
    animal: str
    battle_id: str

ongoing_battles = {}  # battle_id -> {"challenger": uid, "opponent": uid, "challenger_pet": None, "opponent_pet": None}

# --- Commands ---
@dp.message(Command("start"))
async def start(msg: types.Message):
    db_execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (msg.from_user.id,))
    ensure_achievements(msg.from_user.id)
    await msg.answer("👋 Welcome! Commands: /start, /daily, /zoo, /hunt, /battle, /autohunt, /shop, /buy <item>, /achievements, /balance")

@dp.message(Command("help"))
async def help_command(msg: types.Message):
    help_text = (
        "📝 **Bot Commands:**\n\n"
        "👋 /start – Initialize your account and see the welcome message.\n"
        "🎁 /daily – Claim your daily coins (once every 24h).\n"
        "💰 /balance – Check your current coin balance.\n"
        "🏹 /hunt – Hunt for pets and earn coins & XP.\n"
        "🤖 /autohunt – Toggle AutoHunt (automatic hunting every ~30s).\n"
        "🐾 /zoo – View your pets with levels, XP, rarity, and evolution.\n"
        "🛒 /shop – Show available shop items.\n"
        "💸 /buy <item> – Purchase an item from the shop.\n"
        "🎖️ /achievements – View your achievements and rewards.\n"
        "⚔️ /battle @username – Challenge another player to a pet battle.\n"
        "❓ /help – Show this help message.\n"
    )
    await msg.answer(help_text)


@dp.message(Command("daily"))
async def daily(msg: types.Message):
    row = db_fetchone("SELECT last_daily, balance FROM users WHERE id=?", (msg.from_user.id,))
    last_daily, balance = row or (0, 0)
    now = int(time.time())
    if now - last_daily < 86400:
        remaining = 86400 - (now - last_daily)
        await msg.answer(f"⏳ Already claimed! Try again in {remaining//3600}h {(remaining%3600)//60}m")
        return
    reward = 100
    db_execute("UPDATE users SET balance=balance+?, last_daily=? WHERE id=?", (reward, now, msg.from_user.id))
    await msg.answer(f"🎁 Daily reward: {reward} coins!")

@dp.message(Command("balance"))
async def balance(msg: types.Message):
    bal = db_fetchone("SELECT balance FROM users WHERE id=?", (msg.from_user.id,))[0]
    await msg.answer(f"💰 Your balance: {bal} coins")

@dp.message(Command("hunt"))
async def hunt(msg: types.Message):
    db_execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (msg.from_user.id,))
    now = int(time.time())
    last, xp_boost, double_coins = db_fetchone("SELECT last_hunt, xp_boost_active, double_coins_active FROM users WHERE id=?", (msg.from_user.id,))
    if now - last < 10:
        await msg.answer(f"⏳ Wait {10-(now-last)}s before hunting again.")
        return
    animal, rarity = get_random_animal()
    coins_earned = (50 + RARITY_BONUS[rarity]) * (2 if double_coins else 1)
    db_execute("UPDATE users SET balance=balance+?, last_hunt=? WHERE id=?", (coins_earned, now, msg.from_user.id))
    db_execute("INSERT OR IGNORE INTO zoo (user_id, animal, rarity) VALUES (?, ?, ?)", (msg.from_user.id, animal, rarity))
    db_execute("UPDATE zoo SET count=count+1 WHERE user_id=? AND animal=?", (msg.from_user.id, animal))
    xp_amount = 10 * (2 if xp_boost else 1)
    await add_xp(msg.from_user.id, animal, xp_amount)
    await msg.answer(f"🏹 You hunted {animal} ({rarity})! Coins: {coins_earned}, XP: {xp_amount}")
    await update_achievements_after_hunt(msg.from_user.id)

# --- Autohunt toggle command ---
@dp.message(Command("autohunt"))
async def autohunt_toggle(msg: types.Message):
    uid = msg.from_user.id
    row = db_fetchone("SELECT autohunt_active FROM users WHERE id=?", (uid,))
    active = row[0] if row else 0
    if active:
        autohunt_users.discard(uid)
        db_execute("UPDATE users SET autohunt_active=0 WHERE id=?", (uid,))
        await msg.answer("🤖 AutoHunt stopped.")
    else:
        autohunt_users.add(uid)
        db_execute("UPDATE users SET autohunt_active=1 WHERE id=?", (uid,))
        await msg.answer("🤖 AutoHunt started!")

# --- All other commands (zoo, shop, buy, achievements, battle) are the same ---
# For brevity, they are unchanged from the previous full code
# Include the previous implementations for /zoo, /shop, /buy, /achievements, /battle here

# --- Autohunt loop ---
async def autohunt_loop():
    while True:
        for uid in list(autohunt_users):
            row = db_fetchone("SELECT last_hunt, xp_boost_active, double_coins_active, balance FROM users WHERE id=?", (uid,))
            if not row: continue
            last, xp_boost, double_coins, balance = row
            now = int(time.time())
            if balance < AUTOHUNT_COST:
                autohunt_users.discard(uid)
                db_execute("UPDATE users SET autohunt_active=0 WHERE id=?", (uid,))
                await bot.send_message(uid, "❌ AutoHunt stopped (not enough coins).")
                continue
            if now - last >= 10:
                animal, rarity = get_random_animal()
                coins_earned = (50 + RARITY_BONUS[rarity]) * (2 if double_coins else 1)
                db_execute("UPDATE users SET balance=balance-?+?, last_hunt=? WHERE id=?", (AUTOHUNT_COST, coins_earned, now, uid))
                db_execute("INSERT OR IGNORE INTO zoo (user_id, animal, rarity) VALUES (?, ?, ?)", (uid, animal, rarity))
                db_execute("UPDATE zoo SET count=count+1 WHERE user_id=? AND animal=?", (uid, animal))
                xp_amount = 10 * (2 if xp_boost else 1)
                await add_xp(uid, animal, xp_amount)
                await bot.send_message(uid, f"🤖 AutoHunt: {animal} ({rarity}) found! Coins: {coins_earned}, XP: {xp_amount}")
        await asyncio.sleep(30)

# --- Main ---
async def main():
    asyncio.create_task(autohunt_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
