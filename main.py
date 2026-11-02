import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from icalendar import Calendar
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder

# === НАСТРОЙКИ ===
ICS_URL = "https://crew.aeroflot.ru/api/calendar/ics/97aa812f-7253-4e69-a49f-c95bb4bee8ad"

# Берем токен из Bothost (он подставляет BOT_TOKEN автоматически)
TG_TOKEN = os.getenv("TG_TOKEN") or os.getenv("BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")  # Твой ID

SNAPSHOT_DIR = Path("data/snapshots")
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

INTERVAL_SECONDS = 30 * 60  # 30 минут
TYPE_ICONS = {"flight": "✈️", "duty": "📋", "vacation": "🏝", "other": "•"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(token=TG_TOKEN)
dp = Dispatcher()

# === ЛОГИКА ГРАФИКА (без изменений) ===
def classify(summary: str) -> str:
    if summary.startswith("SU") and "✈️" in summary: return "flight"
    if summary.startswith("📋"): return "duty"
    if summary.startswith("🏝"): return "vacation"
    return "other"

def fetch_ics() -> bytes:
    r = requests.get(ICS_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    return r.content

def parse_ics(raw: bytes) -> dict:
    cal = Calendar.from_ical(raw)
    events = {}
    for comp in cal.walk("VEVENT"):
        uid = str(comp.get("UID", "")).strip()
        if not uid: continue
        summary = str(comp.get("SUMMARY", ""))
        events[uid] = {
            "summary": summary,
            "start": comp.get("DTSTART").dt.isoformat(),
            "end": comp.get("DTEND").dt.isoformat(),
            "description": str(comp.get("DESCRIPTION", "")),
            "type": classify(summary),
        }
    return events

def load_last_snapshot() -> dict:
    files = sorted(SNAPSHOT_DIR.glob("*.json"))
    if not files: return {}
    try: return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception: return {}

def save_snapshot(events: dict):
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    (SNAPSHOT_DIR / f"{ts}.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    for old in sorted(SNAPSHOT_DIR.glob("*.json"))[:-50]:
        try: old.unlink()
        except OSError: pass

def diff(old: dict, new: dict) -> list[str]:
    changes = []
    old_uids, new_uids = set(old), set(new)

    def fmt(ev):
        s = datetime.fromisoformat(ev["start"])
        e = datetime.fromisoformat(ev["end"])
        icon = TYPE_ICONS.get(ev["type"], "•")
        if ev["type"] == "vacation": return f"{icon} {ev['summary']} ({s:%d.%m} – {e:%d.%m})"
        return f"{icon} {ev['summary']}\n   {s:%d.%m %H:%M} – {e:%d.%m %H:%M}"

    for uid in sorted(new_uids - old_uids): changes.append(f"➕ ДОБАВЛЕНО:\n{fmt(new[uid])}")
    for uid in sorted(old_uids - new_uids): changes.append(f"➖ УДАЛЕНО:\n{fmt(old[uid])}")
    for uid in sorted(old_uids & new_uids):
        o, n = old[uid], new[uid]
        if o["start"] != n["start"] or o["end"] != n["end"]:
            os_, oe = datetime.fromisoformat(o["start"]), datetime.fromisoformat(o["end"])
            ns, ne = datetime.fromisoformat(n["start"]), datetime.fromisoformat(n["end"])
            changes.append(f"✏️ ИЗМЕНЕНО ВРЕМЯ:\n   {n['summary']}\n   было:  {os_:%d.%m %H:%M} – {oe:%d.%m %H:%M}\n   стало: {ns:%d.%m %H:%M} – {ne:%d.%m %H:%M}")
        elif o["description"] != n["description"]:
            changes.append(f"✏️ ИЗМЕНЁН СОСТАВ:\n   {n['summary']}\n   {n['description'][:200]}")
    return changes

def check_once(send_if_no_changes=False) -> str:
    """Возвращает текст отчета. Если изменений нет и send_if_no_changes=False, возвращает пустую строку."""
    try:
        raw = fetch_ics()
        new_events = parse_ics(raw)
    except Exception as e:
        log.error("Ошибка получения/парсинга: %s", e)
        return "❌ Ошибка при получении графика."

    old_events = load_last_snapshot()
    if not old_events:
        save_snapshot(new_events)
        log.info("Первый запуск, снимок сохранён: %d событий", len(new_events))
        return f"🔔 Мониторинг запущен. Событий в графике: {len(new_events)}"

    changes = diff(old_events, new_events)
    save_snapshot(new_events)

    if changes:
        log.info("Изменений: %d", len(changes))
        return f"🔔 Изменения ({datetime.now():%d.%m %H:%M}):\n\n" + "\n\n".join(changes)
    else:
        log.info("Изменений нет")
        return "✅ Проверка завершена. Изменений в графике нет." if send_if_no_changes else ""

async def send_telegram(text: str, chat_id=None):
    target = chat_id or TG_CHAT_ID
    if not target: return
    for i in range(0, len(text), 4000):
        try:
            await bot.send_message(chat_id=target, text=text[i:i+4000], disable_web_page_preview=True)
        except Exception as e:
            log.error("Ошибка отправки в Telegram: %s", e)

# === ФОНОВАЯ ЗАДАЧА (АВТОПРОВЕРКА) ===
async def background_checker():
    while True:
        log.info("--- Запуск плановой проверки ---")
        report = check_once(send_if_no_changes=False)
        if report:
            await send_telegram(report)
        await asyncio.sleep(INTERVAL_SECONDS)

# === ОБРАБОТЧИКИ КНОПОК ===
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Проверить сейчас", callback_data="check_now")
    await message.answer("Привет! Я слежу за твоим графиком.\nНажми кнопку ниже, чтобы проверить изменения прямо сейчас.", reply_markup=builder.as_markup())

@dp.callback_query(lambda c: c.data == "check_now")
async def process_check_now(callback: types.CallbackQuery):
    await callback.answer("Проверяю... ⏳")
    await callback.message.edit_text("⏳ Скачиваю и проверяю график...")
    
    # Запускаем проверку в отдельном потоке, чтобы не блокировать бота
    report = await asyncio.to_thread(check_once, True)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Проверить сейчас", callback_data="check_now")
    await callback.message.edit_text(report, reply_markup=builder.as_markup())

# === ЗАПУСК ===
async def main():
    log.info("Бот запущен. Интервал фоновой проверки: %d мин", INTERVAL_SECONDS // 60)
    
    # Запускаем фоновую задачу
    asyncio.create_task(background_checker())
    
    # Отправляем приветствие при старте
    await send_telegram("🚀 Бот перезапущен. Мониторинг работает в фоне. Используй /start для меню.")
    
    # Запускаем слушателя кнопок
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен.")
