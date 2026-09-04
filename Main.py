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
    "71169408",   # Dasha Guskova (@dashasl)
    "119128292",  # Anton (@milchgesicht)
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
# Потолок на общее число выкачиваемых записей - предохранитель, чтобы случайно
# не начать тянуть реестр адвокатов на 157 тысяч строк.
MAX_ROWS = int(os.environ.get("MAX_ROWS", "20000"))
# ВАЖНО: API молча обрезает limit до 1000, сколько ни проси (проверено:
# limit=2000 и limit=5000 оба возвращают ровно 1000 строк при size=1257).
# Поэтому записи забираем страницами по 1000 через offset, иначе всё, что
# в реестре после тысячной строки, для бота просто не существует - а новые
# записи дописываются как раз в конец.
PAGE_SIZE = 1000
# Пауза между реестрами внутри одного цикла проверки - вежливости ради, чтобы
# не долбить сайт пачкой запросов одновременно.
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY_SECONDS", "2"))
# Если изменений разом больше этого числа - шлём сводку вместо простыни
# (например, когда реестр пополнили сотней записей за раз).
MAX_DIFF_ITEMS = int(os.environ.get("MAX_DIFF_ITEMS", "40"))
# Версия формата снимка. Если в state лежит снимок старого формата (снятый,
# когда бот видел только первую 1000 строк), diff по нему дал бы ложную пачку
# "добавлено 257 записей" - поэтому такой снимок молча переснимаем.
STATE_VERSION = 2

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
    """Забирает ВСЕ записи реестра, страницами по PAGE_SIZE.

    Один запрос отдаёт максимум 1000 строк независимо от запрошенного limit,
    поэтому идём по offset, пока не выберем всё (поле size в ответе - общее
    число записей в реестре)."""
    rows = {}
    offset = 0
    total = None

    while True:
        r = requests.post(
            f"{BASE}/rest/registry/{registry_id}/values",
            json={"limit": PAGE_SIZE, "offset": offset},
            headers={"accept": "application/json"},
            proxies=PROXIES,
            verify=VERIFY_SSL,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        values = data.get("values", [])
        if total is None:
            total = data.get("size")

        for i, row in enumerate(values):
            # у записей есть свой id; если вдруг нет - подставляем порядковый номер
            rows[str(row.get("id") or f"_idx_{offset + i}")] = row

        offset += len(values)

        if not values:
            break
        if total is not None and offset >= total:
            break
        if offset >= MAX_ROWS:
            log.warning(
                "[%s] Достигнут потолок MAX_ROWS=%s, в реестре записей: %s - "
                "остальные не проверяются",
                registry_id,
                MAX_ROWS,
                total,
            )
            break
        time.sleep(REQUEST_DELAY)

    if total is not None and len(rows) < total:
        log.warning(
            "[%s] Получено %s записей из %s заявленных",
            registry_id,
            len(rows),
            total,
        )
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
    """Отличия между двумя снимками реестра.

    Возвращает список словарей {kind, title, text}, чтобы дальше можно было
    либо расписать всё подробно, либо свернуть в сводку."""
    parts = []

    for row_id, row in new.items():
        if row_id not in old:
            parts.append({
                "kind": "added",
                "title": row_title(row, labels),
                "text": f"➕ ДОБАВЛЕНА ЗАПИСЬ\n{format_row(row, labels)}",
            })

    for row_id, row in old.items():
        if row_id not in new:
            parts.append({
                "kind": "removed",
                "title": row_title(row, labels),
                "text": f"➖ УДАЛЕНА ЗАПИСЬ\n{format_row(row, labels)}",
            })

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
            title = row_title(new_row, labels)
            parts.append({
                "kind": "changed",
                "title": title,
                "text": f"✏️ ИЗМЕНЕНА ЗАПИСЬ: {title}\n" + "\n".join(changes),
            })

    return parts


def format_diff(parts):
    """Текст изменений для сообщения. Если изменений слишком много - вместо
    полной простыни шлём сводку со списком имён/названий."""
    if len(parts) <= MAX_DIFF_ITEMS:
        return "\n\n".join(p["text"] for p in parts)

    buckets = {"added": [], "removed": [], "changed": []}
    for p in parts:
        buckets[p["kind"]].append(p["title"])

    headers = {
        "added": "➕ Добавлено",
        "removed": "➖ Удалено",
        "changed": "✏️ Изменено",
    }
    lines = [f"Изменений сразу много - {len(parts)}, шлю кратко:"]
    for kind in ("added", "removed", "changed"):
        titles = buckets[kind]
        if not titles:
            continue
        lines.append(f"\n{headers[kind]}: {len(titles)}")
        shown = titles[:MAX_DIFF_ITEMS]
        lines.extend(f"• {t}" for t in shown)
        if len(titles) > len(shown):
            lines.append(f"…и ещё {len(titles) - len(shown)}")
    return "\n".join(lines)


# Группы изменений уходят ОТДЕЛЬНЫМИ сообщениями и именно в этом порядке:
# сначала новые записи (самое интересное), потом удаления, потом правки в
# старых записях. Так новое приходит коротким отдельным уведомлением, а не
# тонет в хвосте длинной простыни с правками.
DIFF_GROUPS = (
    ("added", "➕ Новые записи в реестре"),
    ("removed", "➖ Записи удалены из реестра"),
    ("changed", "✏️ Правки в существующих записях"),
)


def send_changes(title, page_url, parts):
    """Шлёт изменения, разбив по типам: добавления - одним сообщением,
    удаления - другим, правки - третьим. Пустые группы пропускаем."""
    for kind, header in DIFF_GROUPS:
        group = [p for p in parts if p["kind"] == kind]
        if not group:
            continue
        tg_send(
            f"🔔 {header}: {len(group)}\n{title}\n{page_url}\n\n"
            f"{format_diff(group)}"
        )


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
    prev_version = registry_state.get("version")

    # Первый запуск для этого реестра (или потеря состояния) - просто
    # запоминаем текущую картину, без алерта. Снимок старого формата
    # (version < 2, снятый когда бот видел лишь первую 1000 строк) тоже
    # переснимаем молча, иначе прилетела бы пачка ложных "добавлено".
    if prev_last_modified is None or prev_rows is None or prev_version != STATE_VERSION:
        registry_state["lastModified"] = last_modified
        registry_state["rows"] = fetch_rows(registry_id)
        registry_state["version"] = STATE_VERSION
        log.info(
            "[%s] %s: %s записей, lastModified=%s",
            title,
            "Инициализация" if prev_rows is None else "Пересъёмка снимка (новый формат)",
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
    except Exception as e:  # noqa: BLE001
        log.exception("[%s] Не удалось получить содержимое реестра", title)
        new_rows = prev_rows
        parts = None
        tg_send(
            f"🔔 Реестр обновился!\n{title}\n{page_url}\n\n"
            f"(не удалось получить содержимое: {e})"
        )

    if parts:
        send_changes(title, page_url, parts)
    elif parts is not None:
        # метка времени сдвинулась, но видимые поля те же - могли
        # поменяться скрытые или служебные поля
        tg_send(
            f"🔔 Реестр обновился!\n{title}\n{page_url}\n\n"
            "Данные в видимых полях не изменились - вероятно, правка "
            "коснулась скрытых или служебных полей."
        )

    registry_state["lastModified"] = last_modified
    registry_state["rows"] = new_rows
    registry_state["version"] = STATE_VERSION


def startup_message():
    """Собирает стартовое сообщение со списком названий реестров (не ID) -
    названия берём тем же запросом /info, которым бот и так пользуется для
    проверок. Если у какого-то реестра не получилось получить название
    (например, сайт на секунду недоступен) - показываем его ID, чтобы не
    ронять всё сообщение целиком."""
    lines = ["✅ Бот обновлён и перезапущен. Следит за реестрами:"]
    for registry_id in REGISTRY_IDS:
        try:
            title = fetch_info(registry_id).get("title") or registry_id
        except Exception:  # noqa: BLE001
            log.exception("[%s] Не удалось получить название для стартового сообщения", registry_id)
            title = registry_id
        lines.append(f"• {title}")
    lines.append(f"\nПолучателей: {len(CHAT_IDS)}. Интервал проверки: {CHECK_INTERVAL} сек.")
    return "\n".join(lines)


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
        tg_send(startup_message())
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