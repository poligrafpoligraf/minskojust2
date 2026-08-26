import os
import json
import time
import logging

import requests

REGISTRY_ID = os.environ.get("REGISTRY_ID", "e102742c-8473-3be5-2a78-5927f0fba20f")
INFO_URL = f"https://reestrs.minjust.gov.ru/rest/registry/{REGISTRY_ID}/info"
VALUES_URL = f"https://reestrs.minjust.gov.ru/rest/registry/{REGISTRY_ID}/values"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "900"))  # 15 минут по умолчанию
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
ERROR_ALERT_THRESHOLD = int(os.environ.get("ERROR_ALERT_THRESHOLD", "3"))  # алерт в ТГ после N подряд ошибок

# Прокси не обязателен. Оставьте PROXY_URL пустым, чтобы сходить напрямую (первый тест) -
# если реестр реально блокирует не-российские IP, запросы ниже начнут падать по таймауту,
# и тогда можно будет добавить прокси, просто прописав переменную окружения, без правки кода.
# Формат: http://user:pass@host:port  или  socks5://user:pass@host:port
PROXY_URL = os.environ.get("PROXY_URL")
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

# reestrs.minjust.gov.ru у многих не проходит проверку SSL-сертификата
# средствами Python (сайт не отдаёт полную цепочку / использует не тот
# корневой сертификат) - хотя в браузере открывается нормально. Для чтения
# публичных данных реестра это не критично, поэтому по умолчанию проверку
# сертификата для ЭТОГО сайта отключаем. Если хотите включить строгую
# проверку обратно (например, после того как разобрались с сертификатами
# в системе) - поставьте переменную окружения VERIFY_SSL=true.
VERIFY_SSL = os.environ.get("VERIFY_SSL", "false").strip().lower() in ("1", "true", "yes", "on")
if not VERIFY_SSL:
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reestr-watch")


def tg_send(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=15,
    )
    r.raise_for_status()


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def fetch_info():
    r = requests.get(
        INFO_URL,
        headers={"accept": "application/json"},
        proxies=PROXIES,
        verify=VERIFY_SSL,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def fetch_values():
    r = requests.post(
        VALUES_URL,
        json={},
        headers={"accept": "application/json"},
        proxies=PROXIES,
        verify=VERIFY_SSL,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def format_row(row):
    skip = {"grid_s", "_version_"}
    parts = [f"{k}: {v}" for k, v in row.items() if k not in skip and v not in (None, "")]
    return "\n".join(parts)


def check_once(state):
    info = fetch_info()
    last_modified = info.get("lastModified")
    prev_last_modified = state.get("lastModified")

    if prev_last_modified is None:
        # первый запуск (или потеря состояния после рестарта) - просто фиксируем
        # точку отсчёта, без алерта, чтобы не присылать "ложное" первое уведомление
        state["lastModified"] = last_modified
        save_state(state)
        log.info("Инициализация, базовый lastModified=%s", last_modified)
        return

    if last_modified != prev_last_modified:
        log.info("Обнаружено изменение: %s -> %s", prev_last_modified, last_modified)
        try:
            values = fetch_values()
            rows = values.get("values", [])
            body = "\n\n".join(format_row(r) for r in rows) if rows else "(записей нет)"
        except Exception as e:  # noqa: BLE001
            body = f"(не удалось получить содержимое: {e})"

        tg_send(
            "🔔 Реестр обновился!\n"
            f"{info.get('title', REGISTRY_ID)}\n"
            f"https://reestrs.minjust.gov.ru/#/registry/{REGISTRY_ID}\n\n"
            f"{body}"
        )
        state["lastModified"] = last_modified
        save_state(state)
    else:
        log.info("Без изменений (lastModified=%s)", last_modified)


def main():
    log.info(
        "Старт. Интервал проверки: %s сек. Прокси: %s",
        CHECK_INTERVAL,
        "включен" if PROXY_URL else "выключен (прямое подключение)",
    )
    state = load_state()
    consecutive_errors = 0

    try:
        tg_send("✅ Бот-наблюдатель за реестром запущен.")
    except Exception:  # noqa: BLE001
        log.exception("Не удалось отправить стартовое сообщение в Telegram")

    while True:
        try:
            check_once(state)
            if consecutive_errors >= ERROR_ALERT_THRESHOLD:
                tg_send("✅ Реестр снова доступен, проверки восстановлены.")
            consecutive_errors = 0
        except Exception as e:  # noqa: BLE001
            consecutive_errors += 1
            log.exception("Ошибка проверки (%s подряд): %s", consecutive_errors, e)
            if consecutive_errors == ERROR_ALERT_THRESHOLD:
                try:
                    tg_send(
                        f"⚠️ Не получается достучаться до реестра уже {consecutive_errors} "
                        f"проверки подряд: {e}\n"
                        "Если реестр реально блокирует не-российские IP - "
                        "это тот самый случай, нужен прокси (переменная PROXY_URL)."
                    )
                except Exception:  # noqa: BLE001
                    pass
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()