import os
import json
import time
import logging

import requests

BASE = "https://reestrs.minjust.gov.ru"

# Список реестров для наблюдения - через запятую, просто ID (GUID из ссылки
# вида https://reestrs.minjust.gov.ru/#/registry/ЭТОТ-ID). По умолчанию - те
# четыре, что уже выбрали:
#   e102742c-... - Список лиц, в отношении которых применяются временные
#                   ограничительные меры
#   39b95df9-... - Реестр иностранных агентов
#   c2d1692e-... - Реестр иностранных и международных организаций,
#                   признанных нежелательными в РФ
#   59961ebb-... - Перечень организаций, признанных экстремистскими
DEFAULT_REGISTRY_IDS = (
    "e102742c-8473-3be5-2a78-5927f0fba20f,"
    "39b95df9-9a68-6b6d-e1e3-e6388507067e,"
    "c2d1692e-a9f6-5a79-13ee-5da5b42980df,"
    "59961ebb-9f0e-d885-7ebc-b90399923b03"
)
REGISTRY_IDS = [
    rid.strip()
    for rid in os.environ.get("REGISTRY_IDS", DEFAULT_REGISTRY_IDS).split(",")
    if rid.strip()
]

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
# Получатели, вшитые прямо в код - не нужно ничего прописывать в Railway
# Variables, чтобы им слать. Сейчас тут Даша (chat_id из её /start боту).
EXTRA_CHAT_IDS = [
    "71169408",  # Dasha Guskova (@dashasl)
]
# Плюс можно указать ещё получателей через запятую в переменной
# TELEGRAM_CHAT_ID (формат тот же, что у REGISTRY_IDS) - список объединяется
# с EXTRA_CHAT_IDS выше, повторы убираются.
CHAT_IDS = list(dict.fromkeys(
    EXTRA_CHAT_IDS
    + [cid.strip() for cid in os.environ["TELEGRAM_CHAT_ID"].split(",") if cid.strip()]
))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "180"))  # 3 минуты по умолчанию
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
ERROR_ALERT_THRESHOLD = int(os.environ.get("ERROR_ALERT_THRESHOLD", "3"))  # алерт в ТГ после N подряд ошибок
MAX_ROWS = int(os.environ.get("MAX_ROWS", "2000"))
# Пауза между реестрами внутри одного цикла проверки - вежливости ради, чтобы
# не долбить сайт пачкой запросов одновременно.
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY_SECONDS", "2"))

# Показывать ли поля, которые скрыты в таблице на сайте (дата рождения, ИНН,
# СНИЛС, номера счетов и т.п.). API их отдаёт, но сообщения становятся длинными.
SHOW_HIDDEN_FIELDS = os.environ.get("SHOW_HIDDEN_FIELDS", "false").strip().lower() in ("1", "true", "yes", "on")

# Прокси не обязателен. Оставьте PROXY_URL пустым, чтобы ходить напрямую.
# Формат: http://user:pass@host:port  или  socks5://user:pass@host:port
PROXY_URL = os.environ.get("PROXY_URL")
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

# reestrs.minjust.gov.ru отдаёт неполную цепочку сертификатов: присылает свой
# сертификат, но не промежуточный. Браузеры это молча чинят (докачивают
# недостающее звено по ссылке из сертификата), а Python/OpenSSL - нет, и падает
# с "unable to get local issuer certificate". Для чтения публичных данных
# проверку сертификата отключаем. Вернуть строгую проверку: VERIFY_SSL=true.
VERIFY_SSL = os.environ.get("VERIFY_SSL", "false").strip().lower() in ("1", "true", "yes", "on")
if not VERIFY_SSL:
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Технические поля, которые в сообщения не попадают никогда
TECH_FIELDS = {"grid_s", "_version_", "id", "lastModified_l"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reestr-watch")

TG_LIMIT = 3900  # запас к телеграмному лимиту в 4096 символов


def tg_send(text):
    """Шлёт сообщение всем получателям из CHAT_IDS, при необходимости разбивая
    длинный текст на части. Если для одного получателя отправка не удалась
    (например, он ещё не написал боту /start), это не мешает отправить
    остальным - ошибка просто попадёт в лог."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    chunks = split_message(text)
    for chat_id in CHAT_IDS:
        try:
            for chunk in chunks:
                r = requests.post(
                    url,
                    json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
                    timeout=15,
                )
                r.raise_for_status()
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отправить сообщение получателю %s", chat_id)


def split_message(text):
    """Режет длинный текст по границам строк, чтобы влезал в лимит Telegram."""
    if len(text) <= TG_LIMIT:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > TG_LIMIT:
            if current:
                chunks.append(current)
            # одна строка длиннее лимита - режем жёстко
            while len(line) > TG_LIMIT:
                chunks.append(line[:TG_LIMIT])
                line = line[TG_LIMIT:]
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    directory = os.path.dirname(STATE_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def fetch_info(registry_id):
    r = requests.get(
        f"{BASE}/rest/registry/{registry_id}/info",
        headers={"accept": "application/json"},
        proxies=PROXIES,
        verify=VERIFY_SSL,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def fetch_rows(registry_id):
    r = requests.post(
        f"{BASE}/rest/registry/{registry_id}/values",
        json={"limit": MAX_ROWS},
        headers={"accept": "application/json"},
        proxies=PROXIES,
        verify=VERIFY_SSL,
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    rows = {}
    for i, row in enumerate(data.get("values", [])):
        # у записей есть свой id; если вдруг нет - подставляем порядковый номер
        rows[str(row.get("id") or f"_idx_{i}")] = row
    return rows


def build_labels(info):
    """{имя_поля: (человеческое название, скрыто ли на сайте)} из описания реестра."""
    labels = {}
    for col in info.get("columns", []):
        name = col.get("name")
        if name:
            labels[name] = (col.get("title") or name, bool(col.get("hidden")))
    return labels


def visible_fields(labels):
    return [
        name
        for name, (_, hidden) in labels.items()
        if name not in TECH_FIELDS and (SHOW_HIDDEN_FIELDS or not hidden)
    ]


def row_title(row, labels):
    """Короткая подпись записи для заголовка - ФИО или название организации."""
    for name, (title, _) in labels.items():
        if title.strip().lower() in ("фио", "наименование", "полное наименование"):
            value = row.get(name)
            if value:
                return str(value)
    return row.get("field_2_s") or f"запись {row.get('field_1_i', '?')}"


def format_row(row, labels):
    lines = []
    for name in visible_fields(labels):
        value = row.get(name)
        if value not in (None, ""):
            human, _ = labels[name]
            lines.append(f"{human}: {value}")
    return "\n".join(lines) if lines else "(пустая запись)"


def diff_rows(old, new, labels):
    """Человекочитаемое описание отличий между двумя снимками реестра."""
    parts = []

    for row_id, row in new.items():
        if row_id not in old:
            parts.append(f"➕ ДОБАВЛЕНА ЗАПИСЬ\n{format_row(row, labels)}")

    for row_id, row in old.items():
        if row_id not in new:
            parts.append(f"➖ УДАЛЕНА ЗАПИСЬ\n{format_row(row, labels)}")

    for row_id, new_row in new.items():
        old_row = old.get(row_id)
        if old_row is None:
            continue
        changes = []
        for name in visible_fields(labels):
            before, after = old_row.get(name), new_row.get(name)
            if before != after:
                human, _ = labels[name]
                changes.append(f"  {human}:\n    было: {before or '(пусто)'}\n    стало: {after or '(пусто)'}")
        if changes:
            parts.append(f"✏️ ИЗМЕНЕНА ЗАПИСЬ: {row_title(new_row, labels)}\n" + "\n".join(changes))

    return parts


def check_registry(registry_id, registry_state):
    """Проверяет один реестр. registry_state - словарь для этого конкретного
    реестра внутри общего state (может быть пустым при первом запуске)."""
    page_url = f"{BASE}/#/registry/{registry_id}"

    info = fetch_info(registry_id)
    labels = build_labels(info)
    title = info.get("title", registry_id)
    last_modified = info.get("lastModified")
    prev_last_modified = registry_state.get("lastModified")
    prev_rows = registry_state.get("rows")

    # первый запуск для этого реестра (или потеря состояния) - просто
    # запоминаем текущую картину, без алерта
    if prev_last_modified is None or prev_rows is None:
        registry_state["lastModified"] = last_modified
        registry_state["rows"] = fetch_rows(registry_id)
        log.info(
            "[%s] Инициализация: %s записей, lastModified=%s",
            title,
            len(registry_state["rows"]),
            last_modified,
        )
        return

    if last_modified == prev_last_modified:
        log.info("[%s] Без изменений (lastModified=%s)", title, last_modified)
        return

    log.info("[%s] Обнаружено изменение: %s -> %s", title, prev_last_modified, last_modified)

    try:
        new_rows = fetch_rows(registry_id)
        parts = diff_rows(prev_rows, new_rows, labels)
        if parts:
            body = "\n\n".join(parts)
        else:
            # метка времени сдвинулась, но видимые поля те же - могли
            # поменяться скрытые или служебные поля
            body = (
                "Данные в видимых полях не изменились - вероятно, правка "
                "коснулась скрытых или служебных полей."
            )
    except Exception as e:  # noqa: BLE001
        log.exception("[%s] Не удалось получить содержимое реестра", title)
        new_rows = prev_rows
        body = f"(не удалось получить содержимое: {e})"

    tg_send(f"🔔 Реестр обновился!\n{title}\n{page_url}\n\n{body}")

    registry_state["lastModified"] = last_modified
    registry_state["rows"] = new_rows


def main():
    log.info(
        "Старт. Реестров: %s. Получателей в Telegram: %s. Интервал проверки: %s сек. Прокси: %s. Скрытые поля: %s",
        len(REGISTRY_IDS),
        len(CHAT_IDS),
        CHECK_INTERVAL,
        "включен" if PROXY_URL else "выключен (прямое подключение)",
        "показываются" if SHOW_HIDDEN_FIELDS else "не показываются",
    )
    state = load_state()
    # счётчик подряд идущих ошибок отдельно на каждый реестр, чтобы один
    # упавший реестр не топил алертами остальные и не мешал их проверке
    consecutive_errors = {rid: 0 for rid in REGISTRY_IDS}

    try:
        tg_send(f"✅ Бот-наблюдатель запущен. Реестров под наблюдением: {len(REGISTRY_IDS)}.")
    except Exception:  # noqa: BLE001
        log.exception("Не удалось отправить стартовое сообщение в Telegram")

    while True:
        registries_state = state.setdefault("registries", {})
        for i, registry_id in enumerate(REGISTRY_IDS):
            registry_state = registries_state.setdefault(registry_id, {})
            try:
                check_registry(registry_id, registry_state)
                if consecutive_errors[registry_id] >= ERROR_ALERT_THRESHOLD:
                    tg_send(f"✅ Реестр снова доступен, проверки восстановлены:\n{registry_id}")
                consecutive_errors[registry_id] = 0
            except Exception as e:  # noqa: BLE001
                consecutive_errors[registry_id] += 1
                log.exception(
                    "[%s] Ошибка проверки (%s подряд): %s", registry_id, consecutive_errors[registry_id], e
                )
                if consecutive_errors[registry_id] == ERROR_ALERT_THRESHOLD:
                    try:
                        tg_send(
                            f"⚠️ Не получается достучаться до реестра уже "
                            f"{consecutive_errors[registry_id]} проверки подряд:\n"
                            f"{BASE}/#/registry/{registry_id}\n{e}"
                        )
                    except Exception:  # noqa: BLE001
                        log.exception("И в Telegram написать тоже не вышло")
            finally:
                save_state(state)
            if i < len(REGISTRY_IDS) - 1:
                time.sleep(REQUEST_DELAY)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()