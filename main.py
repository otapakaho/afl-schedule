import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from icalendar import Calendar

# === НАСТРОЙКИ ===
ICS_URL = "https://crew.aeroflot.ru/api/calendar/ics/97aa812f-7253-4e69-a49f-c95bb4bee8ad"

# Bothost автоматически подставляет BOT_TOKEN из панели.
# Но нам нужен именно токен бота, которого ты создал для уведомлений.
TG_TOKEN = os.getenv("TG_TOKEN") or os.getenv("BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")  # свой chat_id (число)

# Bothost даёт папку data/ для сохранений (на платном тарифе она не стирается)
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
    # Чистим, оставляем последние 50 (~25 часов при 30-мин интервале)
    for old in sorted(SNAPSHOT_DIR.glob("*.json"))[:-50]:
        try:
            old.unlink()
        except OSError:
            pass


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
            os_, oe = datetime.fromisoformat(o["start"]), datetime.fromisoformat(o["end"])
            ns, ne = datetime.fromisoformat(n["start"]), datetime.fromisoformat(n["end"])
            changes.append(
                f"✏️ ИЗМЕНЕНО ВРЕМЯ:\n   {n['summary']}\n"
                f"   было:  {os_:%d.%m %H:%M} – {oe:%d.%m %H:%M}\n"
                f"   стало: {ns:%d.%m %H:%M} – {ne:%d.%m %H:%M}"
            )
        elif o["description"] != n["description"]:
            changes.append(f"✏️ ИЗМЕНЁН СОСТАВ:\n   {n['summary']}\n   {n['description'][:200]}")
    return changes


def send_telegram(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        log.warning("TG_TOKEN или TG_CHAT_ID не заданы")
        return
    for i in range(0, len(text), 4000):
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text[i:i+4000], "disable_web_page_preview": True},
            timeout=30,
        ).raise_for_status()


def check_once():
    """Одна итерация проверки. Возвращает True, если были изменения."""
    try:
        raw = fetch_ics()
        new_events = parse_ics(raw)
    except Exception as e:
        log.error("Ошибка получения/парсинга: %s", e)
        return False

    old_events = load_last_snapshot()

    if not old_events:
        save_snapshot(new_events)
        log.info("Первый запуск, снимок сохранён: %d событий", len(new_events))
        send_telegram(f"🔔 Мониторинг запущен. Событий в графике: {len(new_events)}")
        return True

    changes = diff(old_events, new_events)
    save_snapshot(new_events)

    if changes:
        report = f"🔔 Изменения ({datetime.now():%d.%m %H:%M}):\n\n" + "\n\n".join(changes)
        log.info("Изменений: %d", len(changes))
        send_telegram(report)
        return True

    log.info("Изменений нет")
    return False


def main():
    log.info("Мониторинг Aeroflot запущен. Интервал: %d мин", INTERVAL_SECONDS // 60)
    while True:
        check_once()
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()