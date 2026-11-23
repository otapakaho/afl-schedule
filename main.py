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
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

# === НАСТРОЙКИ ===
ICS_URL = "https://crew.aeroflot.ru/api/calendar/ics/97aa812f-7253-4e69-a49f-c95bb4bee8ad"

TG_TOKEN = os.getenv("TG_TOKEN") or os.getenv("BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

SNAPSHOT_DIR = Path("data/snapshots")
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

INTERVAL_SECONDS = 30 * 60  # 30 минут
TYPE_ICONS = {"flight": "✈️", "duty": "📋", "vacation": "🏝", "other": "•"}

MONTH_NAMES = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель",
    5: "май", 6: "июнь", 7: "июль", 8: "август",
    9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

bot = Bot(token=TG_TOKEN)
dp = Dispatcher()


# === ЛОГИКА ГРАФИКА ===
def classify(summary: str) -> str:
    if summary.startswith("SU") and "✈️" in summary:
        return "flight"
    if summary.startswith("📋"):
        return "duty"
    if summary.startswith("🏝"):
        return "vacation"
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
        if not uid:
            continue
        summary = str(comp.get("SUMMARY", ""))
        dtstart = comp.get("DTSTART").dt
        dtend = comp.get("DTEND").dt
        events[uid] = {
            "summary": summary,
            "start": dtstart.isoformat(),
            "end": dtend.isoformat(),
            "description": str(comp.get("DESCRIPTION", "")),
            "type": classify(summary),
        }
    return events


def load_last_snapshot() -> dict:
    files = sorted(SNAPSHOT_DIR.glob("*.json"))
    if not files:
        return {}
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_snapshot(events: dict):
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    (SNAPSHOT_DIR / f"{ts}.json").write_text(
        json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for old in sorted(SNAPSHOT_DIR.glob("*.json"))[:-50]:
        try:
            old.unlink()
        except OSError:
            pass


# === ПОДСЧЁТ ЧАСОВ ПО МЕСЯЦАМ ===
def sum_hours_by_month(events: dict) -> dict[str, float]:
    """Часы рейсов по месяцам. Событие, пересекающее границу месяца,
    дробится по локальной полуночи 00:00 (время в календаре — московское)."""
    totals: dict[str, float] = {}
    for ev in events.values():
        if ev["type"] != "flight":
            continue
        start = datetime.fromisoformat(ev["start"])
        end = datetime.fromisoformat(ev["end"])
        if end <= start:
            continue

        cursor = start
        while cursor < end:
            # Первое число следующего месяца, 00:00
            if cursor.month == 12:
                next_month = cursor.replace(
                    year=cursor.year + 1, month=1, day=1,
                    hour=0, minute=0, second=0, microsecond=0,
                )
            else:
                next_month = cursor.replace(
                    month=cursor.month + 1, day=1,
                    hour=0, minute=0, second=0, microsecond=0,
                )
            segment_end = min(end, next_month)
            hours = (segment_end - cursor).total_seconds() / 3600
            key = cursor.strftime("%Y-%m")
            totals[key] = totals.get(key, 0.0) + hours
            cursor = segment_end
    return totals


def fmt_hours(h: float) -> str:
    total_min = round(h * 60)
    return f"{total_min // 60}ч {total_min % 60:02d}м"


def fmt_month(key: str) -> str:
    year, month = key.split("-")
    return f"{MONTH_NAMES[int(month)]} {year}"


def month_hours_report(old: dict, new: dict) -> list[str]:
    """Сравнивает часы по месяцам, возвращает строки отчёта."""
    old_h = sum_hours_by_month(old)
    new_h = sum_hours_by_month(new)
    lines = []
    for key in sorted(set(old_h) | set(new_h)):
        before = old_h.get(key, 0.0)
        after = new_h.get(key, 0.0)
        if abs(before - after) < 1 / 60:  # меньше минуты — не показываем
            continue
        diff_h = after - before
        sign = "+" if diff_h > 0 else "−"
        lines.append(
            f"📊 {fmt_month(key).capitalize()}: "
            f"{fmt_hours(before)} → {fmt_hours(after)} "
            f"({sign}{fmt_hours(abs(diff_h))})"
        )
    return lines


def format_hours_report(events: dict) -> str:
    """Текст с часами по всем месяцам (из переданного набора событий)."""
    hours = sum_hours_by_month(events)
    if not hours:
        return "✈️ В графике нет рейсов — считать нечего."
    lines = [
        f"📊 {fmt_month(k).capitalize()}: {fmt_hours(v)}"
        for k, v in sorted(hours.items())
    ]
    total = sum(hours.values())
    lines.append(f"\nΣ Всего: {fmt_hours(total)}")
    return "⏱ Часы по месяцам:\n" + "\n".join(lines)


def fetch_and_format_hours() -> str:
    """Свежий запрос ICS + отчёт по часам (без записи снимка)."""
    try:
        raw = fetch_ics()
        events = parse_ics(raw)
    except Exception as e:
        log.error("Ошибка получения/парсинга (hours): %s", e)
        return "❌ Не удалось получить график."
    return format_hours_report(events)


# === ДИФФ СОБЫТИЙ ===
def diff(old: dict, new: dict) -> list[str]:
    changes = []
    old_uids, new_uids = set(old), set(new)

    def fmt(ev):
        s = datetime.fromisoformat(ev["start"])
        e = datetime.fromisoformat(ev["end"])
        icon = TYPE_ICONS.get(ev["type"], "•")
        if ev["type"] == "vacation":
            return f"{icon} {ev['summary']} ({s:%d.%m} – {e:%d.%m})"
        return f"{icon} {ev['summary']}\n   {s:%d.%m %H:%M} – {e:%d.%m %H:%M}"

    for uid in sorted(new_uids - old_uids):
        changes.append(f"➕ ДОБАВЛЕНО:\n{fmt(new[uid])}")
    for uid in sorted(old_uids - new_uids):
        changes.append(f"➖ УДАЛЕНО:\n{fmt(old[uid])}")
    for uid in sorted(old_uids & new_uids):
        o, n = old[uid], new[uid]
        if o["start"] != n["start"] or o["end"] != n["end"]:
            os_ = datetime.fromisoformat(o["start"])
            oe = datetime.fromisoformat(o["end"])
            ns = datetime.fromisoformat(n["start"])
            ne = datetime.fromisoformat(n["end"])
            changes.append(
                f"✏️ ИЗМЕНЕНО ВРЕМЯ:\n   {n['summary']}\n"
                f"   было:  {os_:%d.%m %H:%M} – {oe:%d.%m %H:%M}\n"
                f"   стало: {ns:%d.%m %H:%M} – {ne:%d.%m %H:%M}"
            )
        elif o["description"] != n["description"]:
            changes.append(
                f"✏️ ИЗМЕНЁН СОСТАВ:\n   {n['summary']}\n   {n['description'][:200]}"
            )
    return changes


def check_once(send_if_no_changes=False) -> str:
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
        hours = sum_hours_by_month(new_events)
        hours_line = ""
        if hours:
            hours_line = "\n\n" + "\n".join(
                f"📊 {fmt_month(k).capitalize()}: {fmt_hours(v)}"
                for k, v in sorted(hours.items())
            )
        return f"🔔 Мониторинг запущен. Событий в графике: {len(new_events)}{hours_line}"

    changes = diff(old_events, new_events)
    hours_changes = month_hours_report(old_events, new_events)
    save_snapshot(new_events)

    if changes or hours_changes:
        log.info("Изменений: %d, часовых: %d", len(changes), len(hours_changes))
        parts = []
        if changes:
            parts.append(
                f"🔔 Изменения ({datetime.now():%d.%m %H:%M}):\n\n"
                + "\n\n".join(changes)
            )
        if hours_changes:
            parts.append("⏱ Часы по месяцам:\n" + "\n".join(hours_changes))
        return "\n\n".join(parts)
    else:
        log.info("Изменений нет")
        return "✅ Проверка завершена. Изменений в графике нет." if send_if_no_changes else ""


async def send_telegram(text: str, chat_id=None):
    target = chat_id or TG_CHAT_ID
    if not target:
        return
    for i in range(0, len(text), 4000):
        try:
            await bot.send_message(
                chat_id=target, text=text[i:i + 4000], disable_web_page_preview=True
            )
        except Exception as e:
            log.error("Ошибка отправки в Telegram: %s", e)


# === ФОНОВАЯ ЗАДАЧА ===
async def background_checker():
    while True:
        log.info("--- Запуск плановой проверки ---")
        report = check_once(send_if_no_changes=False)
        if report:
            await send_telegram(report)
        await asyncio.sleep(INTERVAL_SECONDS)


# === ОБРАБОТЧИКИ ===
def main_menu_kb() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Проверить сейчас", callback_data="check_now")
    builder.button(text="⏱ Часы", callback_data="show_hours")
    builder.adjust(1)
    return builder.as_markup()


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я слежу за твоим графиком.\nВыбери действие:",
        reply_markup=main_menu_kb(),
    )


@dp.message(Command("hours"))
async def cmd_hours(message: types.Message):
    await message.answer("⏳ Считаю часы...")
    report = await asyncio.to_thread(fetch_and_format_hours)
    await message.answer(report, reply_markup=main_menu_kb())


@dp.callback_query(lambda c: c.data == "check_now")
async def process_check_now(callback: types.CallbackQuery):
    await callback.answer("Проверяю... ⏳")
    await callback.message.edit_text("⏳ Скачиваю и проверяю график...")

    report = await asyncio.to_thread(check_once, True)

    await callback.message.edit_text(report, reply_markup=main_menu_kb())


@dp.callback_query(lambda c: c.data == "show_hours")
async def process_show_hours(callback: types.CallbackQuery):
    await callback.answer("Считаю... ⏳")
    report = await asyncio.to_thread(fetch_and_format_hours)
    await callback.message.edit_text(report, reply_markup=main_menu_kb())


# === ЗАПУСК ===
async def main():
    log.info("Бот запущен. Интервал фоновой проверки: %d мин", INTERVAL_SECONDS // 60)
    asyncio.create_task(background_checker())
    await send_telegram(
        "🚀 Бот перезапущен. Мониторинг работает в фоне. Используй /start для меню."
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен.")
