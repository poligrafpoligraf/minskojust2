import os
import re
import json
import time
import logging
from datetime import datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

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

# Кому уходит техническая кухня: «бот перезапущен», поломки и починки,
# переключения прокси, сроки их оплаты, кто подписался и отписался.
# Остальные получатели видят только содержательное - изменения в реестрах
# и записку из note.txt.
#
# По умолчанию это тот, кто указан в TELEGRAM_CHAT_ID, то есть владелец
# бота. Если захочется подстраховаться на случай своего отпуска - можно
# задать отдельно переменной ADMIN_CHAT_ID (через запятую) и добавить
# туда кого-то ещё.
ADMIN_CHAT_IDS = [
    cid.strip()
    for cid in os.environ.get("ADMIN_CHAT_ID", os.environ["TELEGRAM_CHAT_ID"]).split(",")
    if cid.strip()
]

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "90"))  # 1,5 минуты по умолчанию
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

# ---------------------------------------------------------------------------
# Наблюдение за обычными страницами со списком ссылок (не реестры с API)
# ---------------------------------------------------------------------------
# Каждый наблюдатель - это страница, на которой нас интересует список ссылок
# внутри одного блока. Бот запоминает список и сообщает, что добавилось,
# что пропало и что переименовали.
#
#   key       - короткое имя, под ним состояние лежит в state.json
#   title     - как называть страницу в сообщениях
#   url       - что скачивать
#   type      - "links" (список ссылок) или "table_tail" (хвост таблицы
#               с постраничной навигацией)
#   selector  - для "links": CSS-селектор ссылок (как в браузере)
#   encoding  - кодировка страницы; cdep.ru отдаёт windows-1251 и не пишет
#               об этом в заголовке, поэтому задаём явно, иначе вместо
#               русского текста придёт каша
PAGE_WATCHERS = [
    {
        "key": "cdep_sudstat",
        "type": "links",
        "title": "Судебный департамент - данные судебной статистики",
        "url": "https://cdep.ru/?id=79",
        "selector": "div.contentBody.customArea a",
        "encoding": "windows-1251",
    },
    {
        # Федеральный список экстремистских материалов - таблица на ~5500
        # пунктов, разбитая на страницы по 100 штук. Новые пункты всегда
        # дописываются в конец, поэтому смотрим только последнюю страницу:
        # два запроса вместо полусотни. Оборотная сторона - исключение
        # пункта из середины списка бот не заметит (см. README).
        "key": "minjust_extremist_materials",
        "type": "table_tail",
        "title": "Федеральный список экстремистских материалов",
        "url": "https://minjust.gov.ru/ru/extremist-materials/",
        "encoding": "utf-8",
    },
    {
        # Единый федеральный список террористических организаций (ФСБ).
        # Весь список на одной странице, поэтому тут, в отличие от списка
        # материалов, честно ловятся и исключения, и правки.
        #
        # ВАЖНО про вёрстку: пункты 1-72 лежат в одной большой таблице, а
        # каждый следующий добавлен ОТДЕЛЬНОЙ таблицей на одну строку -
        # видимо, руками дописывают новый кусок вёрстки. Поэтому разбираем
        # строки ВСЕХ таблиц страницы: если брать только самую большую,
        # бот не увидит ни нынешние 73-77, ни любое будущее пополнение.
        #
        # Сайт работает только по http и на https отвечает редиректом
        # обратно на http. Браузеры из-за этого ругаются, requests - нет.
        #
        # "fast": True - проверять каждый цикл вместе с реестрами, а не по
        # расписанию 9/14/22, как остальные страницы.
        "key": "fsb_terror_orgs",
        "type": "table_all",
        "title": "Единый федеральный список террористических организаций (ФСБ)",
        "url": "http://www.fsb.ru/fsb/npd/terror.htm",
        "encoding": "utf-8",
        "fast": True,
    },
]

# Страницы проверяем не по таймеру, а в заданные часы по местному времени:
# данные там полугодовые, чаще смысла нет.
PAGE_CHECK_HOURS = [
    int(h.strip())
    for h in os.environ.get("PAGE_CHECK_HOURS", "9,14,22").split(",")
    if h.strip()
]
# Europe/Berlin - это CET зимой и CEST летом, то есть "9 утра" остаётся
# девятью утра по стенным часам круглый год.
PAGE_CHECK_TZ = os.environ.get("PAGE_CHECK_TZ", "Europe/Berlin")

# Записка от человека - единственное в сообщении о перезапуске, что
# предназначено людям, поэтому она уходит ВСЕМ получателям отдельным
# сообщением, а техническая часть - только админам.
# Лежит в обычном текстовом файле note.txt рядом со скриптом: пишете туда
# пару строк, коммитите вместе с остальными правками - и при следующем
# запуске они уедут. Строки, начинающиеся с #, считаются пояснением к
# самому файлу и не отправляются. Пустой файл (или его отсутствие) =
# подписчики при передеплое не увидят вообще ничего.
NOTE_FILE = os.environ.get("NOTE_FILE", "note.txt")
# Если лезть в git ради одной записки не хочется - то же самое можно
# положить в переменную STARTUP_NOTE на Railway: она главнее файла, а само
# её сохранение уже вызывает передеплой, то есть записка уедет сразу.
STARTUP_NOTE = os.environ.get("STARTUP_NOTE", "")
NOTE_LIMIT = 1500  # чтобы случайно вставленная простыня не разнесла сообщение

# Показывать ли поля, которые скрыты в таблице на сайте (дата рождения, ИНН,
# СНИЛС, номера счетов и т.п.). API их отдаёт, но сообщения становятся длинными.
SHOW_HIDDEN_FIELDS = os.environ.get("SHOW_HIDDEN_FIELDS", "false").strip().lower() in ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Прокси: список с переключением при отказе
# ---------------------------------------------------------------------------
# Задаются переменными окружения PROXY_URL, PROXY_URL_2, PROXY_URL_3 и так
# далее по порядку. Формат каждой:
#     http://логин:пароль@адрес:порт   или   socks5://логин:пароль@адрес:порт
# Рядом можно (необязательно) положить дату окончания оплаты - бот напомнит
# заранее:  PROXY_1_EXPIRES=2026-09-25, PROXY_2_EXPIRES=2026-12-16 и т.д.
#
# Порядок важен: первый в списке считается основным, к нему бот старается
# вернуться. Остальные - резерв на случай отказа.
#
# Прокси вообще не обязателен: если ни одной переменной нет, ходим напрямую.
PROXY_EXPIRY_WARN_DAYS = int(os.environ.get("PROXY_EXPIRY_WARN_DAYS", "5"))
# Как часто пробовать вернуться на основной прокси, если сидим на резервном.
PROXY_RETURN_INTERVAL = float(os.environ.get("PROXY_RETURN_INTERVAL_SECONDS", str(30 * 60)))


def load_proxies():
    proxies = []
    for i in range(1, 10):
        name = "PROXY_URL" if i == 1 else f"PROXY_URL_{i}"
        url = (os.environ.get(name) or "").strip()
        if not url:
            continue
        proxies.append({
            "n": i,
            "url": url,
            "expires": (os.environ.get(f"PROXY_{i}_EXPIRES") or "").strip() or None,
        })
    return proxies


PROXY_LIST = load_proxies()


def proxy_label(proxy):
    """Как называть прокси в сообщениях - без логина и пароля."""
    host = proxy["url"].split("@")[-1]
    return f"прокси №{proxy['n']} ({host})"

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


# Подписавшиеся по паролю. Живут в файле состояния и добавляются к постоянным
# получателям из CHAT_IDS. Заполняется при загрузке состояния.
DYNAMIC_CHAT_IDS = {}


def all_recipients():
    """Постоянные получатели плюс подписавшиеся по паролю, без повторов."""
    return list(dict.fromkeys(CHAT_IDS + sorted(DYNAMIC_CHAT_IDS)))


def tg_send_to(chat_id, text):
    """Одному адресату. Длинный текст режется на части."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for chunk in split_message(text):
        r = requests.post(
            url,
            json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
            timeout=15,
        )
        r.raise_for_status()


def _broadcast(chat_ids, text, what):
    """Рассылка списку адресатов. Если для одного отправка не удалась
    (например, он заблокировал бота), это не мешает отправить остальным -
    ошибка просто попадёт в лог."""
    for chat_id in chat_ids:
        try:
            tg_send_to(chat_id, text)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отправить %s получателю %s", what, chat_id)


def tg_send(text):
    """Содержательное сообщение - всем получателям."""
    _broadcast(all_recipients(), text, "сообщение")


def tg_admin(text):
    """Техническая кухня - только админам.

    Перезапуски, поломки, переключения прокси, сроки оплаты, кто подписался.
    Обычным подписчикам это не нужно: они подписались на изменения в
    реестрах, а не на внутреннюю жизнь бота."""
    _broadcast(ADMIN_CHAT_IDS, text, "техническое сообщение")


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


# ---------------------------------------------------------------------------
# Подписка по паролю
# ---------------------------------------------------------------------------
# Человек пишет боту /start, бот просит пароль, человек его вводит - и
# начинает получать те же уведомления. Не нужно добывать chat_id и вписывать
# его руками.
#
# Пароль задаётся переменной ACCESS_PASSWORD на Railway. Если её нет,
# подписка выключена совсем и бот ни на что не отвечает - как раньше.
ACCESS_PASSWORD = (os.environ.get("ACCESS_PASSWORD") or "").strip()
# Сколько неверных попыток подряд прощаем, прежде чем перестать отвечать.
PASSWORD_ATTEMPTS = int(os.environ.get("PASSWORD_ATTEMPTS", "5"))
# И насколько замолкаем для того, кто их исчерпал.
PASSWORD_LOCKOUT = float(os.environ.get("PASSWORD_LOCKOUT_SECONDS", str(3600)))

HELP_TEXT = (
    "Это бот-наблюдатель за реестрами Минюста, списком экстремистских "
    "материалов и списком террористических организаций ФСБ.\n\n"
    "Чтобы получать уведомления, пришлите пароль одним сообщением.\n"
    "Отписаться потом - командой /stop."
)


def is_permanent(chat_id):
    """Получатель, вписанный в настройки бота (код или TELEGRAM_CHAT_ID).
    Такого нельзя отписать командой /stop - только правкой настроек."""
    return str(chat_id) in CHAT_IDS


def is_admin(chat_id):
    """Админ - тот, кому идёт техническая кухня и доступна команда /who."""
    return str(chat_id) in ADMIN_CHAT_IDS


def describe_user(user):
    """Имя человека для сообщений: «Имя Фамилия (@username)»."""
    parts = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])).strip()
    username = user.get("username")
    if parts and username:
        return f"{parts} (@{username})"
    return parts or (f"@{username}" if username else "без имени")


def handle_message(message, state):
    """Обрабатывает одно входящее сообщение. Возвращает True, если состояние
    изменилось и его надо сохранить."""
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return False  # в группах ничего не слушаем

    chat_id = str(chat.get("id"))
    text = (message.get("text") or "").strip()
    user = message.get("from") or {}
    who = describe_user(user)

    subscribers = state.setdefault("subscribers", {})
    attempts = state.setdefault("password_attempts", {})

    if text == "/stop":
        if chat_id in subscribers:
            subscribers.pop(chat_id)
            DYNAMIC_CHAT_IDS.pop(chat_id, None)
            tg_send_to(chat_id, "Отписал. Чтобы вернуться, пришлите пароль ещё раз.")
            tg_admin(f"➖ Отписался: {who}")
            log.info("Отписался %s (%s)", who, chat_id)
            return True
        if is_permanent(chat_id):
            tg_send_to(chat_id, "Вы вписаны в настройках бота, отписать себя командой нельзя.")
        else:
            tg_send_to(chat_id, "Вы и так не подписаны.")
        return False

    if text == "/who" and is_admin(chat_id):
        lines = [
            f"Постоянных получателей: {len(CHAT_IDS)}",
            f"Админов (получают техническое): {len(ADMIN_CHAT_IDS)}",
        ]
        if subscribers:
            lines.append(f"\nПодписались по паролю ({len(subscribers)}):")
            lines += [f"• {info.get('name', '?')} — с {info.get('since', '?')}"
                      for info in subscribers.values()]
        else:
            lines.append("\nПо паролю пока никто не подписывался.")
        tg_send_to(chat_id, "\n".join(lines))
        return False

    if chat_id in subscribers or is_permanent(chat_id):
        if text in ("/start", "/help"):
            tg_send_to(chat_id, "Вы уже получаете уведомления. Отписаться - /stop.")
        return False

    # Дальше - незнакомец. Считаем неудачные попытки, чтобы пароль нельзя
    # было подобрать перебором. Блокировка не вечная: через PASSWORD_LOCKOUT
    # счётчик обнуляется, иначе человек, пять раз опечатавшийся, не смог бы
    # подписаться уже никогда.
    record = attempts.get(chat_id) or {}
    locked_until = record.get("until", 0)
    if locked_until > time.time():
        log.warning("Игнорирую %s: попытки пароля исчерпаны", chat_id)
        return False
    if locked_until:
        # Блокировка была и уже истекла - прощаем и начинаем счёт заново.
        # Важно сбрасывать именно здесь, а не при любой записи: иначе
        # счётчик обнулялся бы на каждой попытке и перебор не ограничивался.
        attempts.pop(chat_id, None)
        record = {}

    if text in ("/start", "/help", ""):
        tg_send_to(chat_id, HELP_TEXT)
        return False

    if text == ACCESS_PASSWORD:
        subscribers[chat_id] = {
            "name": who,
            "since": datetime.now(ZoneInfo(PAGE_CHECK_TZ)).strftime("%d.%m.%Y"),
        }
        DYNAMIC_CHAT_IDS[chat_id] = True
        attempts.pop(chat_id, None)
        tg_send_to(
            chat_id,
            "Готово, пароль принят. Теперь вы получаете уведомления об "
            "изменениях в реестрах.\n\nОтписаться - /stop.",
        )
        tg_admin(f"➕ Новый подписчик: {who}")
        log.info("Подписался %s (%s)", who, chat_id)
        return True

    count = record.get("count", 0) + 1
    left = PASSWORD_ATTEMPTS - count
    if left > 0:
        attempts[chat_id] = {"count": count}
        tg_send_to(chat_id, f"Пароль неверный. Осталось попыток: {left}.")
    else:
        attempts[chat_id] = {"count": count, "until": time.time() + PASSWORD_LOCKOUT}
        minutes = int(PASSWORD_LOCKOUT // 60)
        tg_send_to(chat_id, f"Пароль неверный, попытки исчерпаны. Попробуйте через {minutes} мин.")
        tg_admin(f"⚠️ {who} исчерпал попытки ввода пароля.")
    log.warning("Неверный пароль от %s (%s), попытка %s", who, chat_id, count)
    return True


def poll_updates(state, seconds):
    """Слушает входящие сообщения примерно `seconds` секунд.

    Вместо того чтобы просто спать между проверками реестров, бот проводит
    это время на длинном опросе Telegram. Так ответ на /start приходит за
    секунду, а не через полтора цикла, и при этом не нужен отдельный поток
    со своими сложностями."""
    if not ACCESS_PASSWORD:
        time.sleep(seconds)
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    deadline = time.time() + seconds
    changed = False

    while True:
        left = deadline - time.time()
        if left <= 0:
            break
        try:
            r = requests.post(
                url,
                json={
                    "offset": state.get("updates_offset", 0),
                    "timeout": int(min(left, 25)),
                    "allowed_updates": ["message"],
                },
                timeout=min(left, 25) + 10,
            )
            r.raise_for_status()
            updates = r.json().get("result", [])
        except Exception as e:  # noqa: BLE001
            # 409 бывает, если тот же бот где-то запущен вторым экземпляром:
            # Telegram отдаёт входящие только одному слушателю.
            log.warning("Не удалось получить входящие: %s", e)
            time.sleep(min(left, 10))
            continue

        for update in updates:
            state["updates_offset"] = update["update_id"] + 1
            changed = True
            message = update.get("message")
            if not message:
                continue
            try:
                if handle_message(message, state):
                    changed = True
            except Exception:  # noqa: BLE001
                log.exception("Ошибка обработки входящего сообщения")

    return changed


# ---------------------------------------------------------------------------
# Переключение прокси при отказе
# ---------------------------------------------------------------------------

class ProxyPool:
    """Держит список прокси и помнит, через какой сейчас ходим.

    Если запрос не прошёл по сетевой причине - пробуем следующий. Про каждое
    переключение сообщаем админам: иначе прокси будут тихо умирать один за
    другим, бот продолжит бодро работать, и в тот день, когда кончится
    последний, всё встанет разом и без предупреждения."""

    def __init__(self, proxies):
        self.proxies = proxies
        self.index = 0
        self.returned_at = time.time()
        # О каком прокси уже сообщили, что он лёг. Нужно, чтобы не слать
        # одно и то же сообщение каждые полчаса, пока он не починится.
        self.reported_dead = set()

    def enabled(self):
        return bool(self.proxies)

    def current(self):
        return self.proxies[self.index] if self.proxies else None

    def as_requests_dict(self):
        proxy = self.current()
        if not proxy:
            return None
        return {"http": proxy["url"], "https": proxy["url"]}

    def maybe_return_to_main(self):
        """Периодически возвращаемся на основной прокси: вдруг ожил."""
        if self.index == 0 or not self.proxies:
            return
        if time.time() - self.returned_at < PROXY_RETURN_INTERVAL:
            return
        log.info("Пробуем вернуться на основной прокси")
        self.index = 0
        self.returned_at = time.time()

    def mark_success(self):
        """Запрос через текущий прокси прошёл. Если мы про него писали, что
        он лёг, - сообщаем, что ожил, и снимаем пометку."""
        proxy = self.current()
        if proxy and proxy["n"] in self.reported_dead:
            self.reported_dead.discard(proxy["n"])
            tg_admin(f"✅ {proxy_label(proxy)} снова работает.")

    def switch_after_failure(self, error):
        """Переходит на следующий прокси в списке."""
        if len(self.proxies) < 2:
            return
        failed = self.current()
        self.index = (self.index + 1) % len(self.proxies)
        nxt = self.current()
        log.warning("%s не отвечает (%s), перехожу на %s", proxy_label(failed), error, proxy_label(nxt))

        # Сообщаем только про первый отказ этого прокси. Иначе, пока он лежит,
        # бот будет слать одно и то же каждые полчаса - при каждой попытке
        # вернуться на основной.
        if failed["n"] in self.reported_dead:
            return
        self.reported_dead.add(failed["n"])
        alive = len(self.proxies) - len(self.reported_dead)
        tg_admin(
            f"🔁 {proxy_label(failed)} не отвечает, перешёл на {proxy_label(nxt)}.\n"
            f"Рабочих прокси осталось: {alive} из {len(self.proxies)}.\n\n{error}"
        )


POOL = ProxyPool(PROXY_LIST)

# Ошибки, при которых виноват скорее прокси, чем сайт: до сайта вообще не
# доехали. HTTP-коды и разбор страницы сюда не относятся - при них менять
# прокси бессмысленно.
NETWORK_ERRORS = (
    requests.exceptions.ProxyError,
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ReadTimeout,
)


def request_via_proxy(method, url, **kwargs):
    """requests.request, но с перебором прокси при сетевых отказах."""
    if not POOL.enabled():
        return requests.request(method, url, proxies=None, **kwargs)

    attempts = len(POOL.proxies)
    last_error = None
    for attempt in range(attempts):
        try:
            response = requests.request(method, url, proxies=POOL.as_requests_dict(), **kwargs)
        except NETWORK_ERRORS as e:
            last_error = e
            # Перебираем ровно по разу каждый прокси из списка: переключаемся
            # после каждой неудачи, кроме последней попытки.
            if attempt < attempts - 1:
                POOL.switch_after_failure(e)
            continue
        POOL.mark_success()
        return response
    raise last_error


# ---------------------------------------------------------------------------
# Кто сейчас сломан и о чём мы уже сообщали
# ---------------------------------------------------------------------------
# Как часто напоминать о поломке, которую до сих пор не починили.
REMINDER_INTERVAL = float(os.environ.get("PROBLEM_REMINDER_SECONDS", str(6 * 3600)))


def human_duration(seconds):
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        return f"{seconds // 3600} ч {(seconds % 3600) // 60} мин"
    return f"{seconds // 86400} дн {(seconds % 86400) // 3600} ч"


class Problems:
    """Решает, о чём писать админам, а о чём промолчать.

    Всё это - техническая кухня, обычные подписчики её не видят.

    Правила, чтобы не спамить:

    1. Единичный сбой - молчим, только в лог. Сеть иногда моргает, и писать
       об этом людям незачем.
    2. Источник не отвечает N раз подряд (ERROR_ALERT_THRESHOLD) - одно
       сообщение про этот источник.
    3. Если в одном цикле легли ВСЕ проверяемые источники - это не про них,
       а про связь. Тогда уходит ОДНО общее сообщение вместо семи отдельных,
       а частные жалобы гасим.
    4. Пока не починилось - напоминание раз в REMINDER_INTERVAL, с указанием,
       сколько это уже длится. Молчать нельзя: тишина неотличима от порядка.
    5. Починилось - одно сообщение, тоже с длительностью простоя.
    """

    def __init__(self):
        self.streak = {}   # источник -> сколько неудач подряд
        self.active = {}   # проблема -> {since, last_alert, label}
        self.cycle = []    # что проверяли в текущем цикле

    def ok(self, key, title):
        self.cycle.append((key, title, None))

    def failed(self, key, title, error):
        self.cycle.append((key, title, error))

    # -- внутреннее --------------------------------------------------------

    def notify(self, text):
        """Техническое сообщение админам, не роняя бота, если Telegram
        не ответил."""
        try:
            tg_admin(text)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отправить сообщение о поломке")

    _say = notify  # прежнее имя, используется внутри класса

    def _raise(self, pid, label, error, fixed_label=None):
        now = time.time()
        problem = self.active.get(pid)
        if problem is None:
            self.active[pid] = {
                "since": now,
                "last_alert": now,
                "label": label,
                "fixed": fixed_label or f"снова отвечает: {label}",
            }
            self._say(f"⚠️ {label}\n\n{error}")
            return
        if now - problem["last_alert"] >= REMINDER_INTERVAL:
            problem["last_alert"] = now
            self._say(
                f"⚠️ Всё ещё не работает: {label}\n"
                f"Длится уже {human_duration(now - problem['since'])}.\n\n{error}"
            )

    def _resolve(self, pid):
        problem = self.active.pop(pid, None)
        if problem:
            self._say(
                f"✅ Починилось — {problem['fixed']}.\n"
                f"Не работало {human_duration(time.time() - problem['since'])}."
            )

    def _forget(self, pid):
        self.active.pop(pid, None)

    # -- итог цикла --------------------------------------------------------

    def end_cycle(self):
        checked, self.cycle = self.cycle, []
        if not checked:
            return

        for key, _title, error in checked:
            self.streak[key] = 0 if error is None else self.streak.get(key, 0) + 1

        failed = [(k, t, e) for k, t, e in checked if e is not None]
        everything_down = (
            len(failed) == len(checked)
            and len(checked) >= 2
            and all(self.streak[k] >= ERROR_ALERT_THRESHOLD for k, _, _ in failed)
        )

        if everything_down:
            # Не отвечает вообще ничего - значит дело не в сайтах, а в связи
            # или прокси. Одно сообщение вместо пачки одинаковых.
            where = f" (сейчас через {proxy_label(POOL.current())})" if POOL.enabled() else ""
            self._raise(
                "infra",
                f"Ни один источник не отвечает{where}",
                failed[0][2],
                fixed_label="связь восстановлена, источники снова отвечают",
            )
            for key, _title, _error in failed:
                self._forget(f"src:{key}")
            return

        self._resolve("infra")
        for key, title, error in checked:
            pid = f"src:{key}"
            if error is None:
                self._resolve(pid)
            elif self.streak[key] >= ERROR_ALERT_THRESHOLD:
                self._raise(
                    pid,
                    f"Не отвечает: {title}",
                    error,
                    fixed_label=f"снова отвечает {title}",
                )


PROBLEMS = Problems()


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
    r = request_via_proxy(
        "GET",
        f"{BASE}/rest/registry/{registry_id}/info",
        headers={"accept": "application/json"},
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
        r = request_via_proxy(
            "POST",
            f"{BASE}/rest/registry/{registry_id}/values",
            json={"limit": PAGE_SIZE, "offset": offset},
            headers={"accept": "application/json"},
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
        return True

    if last_modified == prev_last_modified:
        log.info("[%s] Без изменений (lastModified=%s)", title, last_modified)
        return False

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
    return True


# ---------------------------------------------------------------------------
# Проверка страниц со списком ссылок
# ---------------------------------------------------------------------------

def page_item_key(href, text):
    """Устойчивый ключ ссылки. На cdep.ru у каждого набора свой номер в адресе
    (?id=79&item=9291) - по нему видно, что набор тот же самый, даже если его
    переименовали. Если номера нет, ключом будет сам адрес, а в крайнем
    случае - текст ссылки."""
    match = re.search(r"item=(\d+)", href or "")
    if match:
        return f"item{match.group(1)}"
    return (href or "").strip() or (text or "").strip()


def fetch_soup(url, encoding=None, validators=None):
    """Скачивает страницу и отдаёт (разобранный HTML, метки версии).

    Если передать `validators` - метки, полученные при прошлом скачивании, -
    запрос уходит с вопросом «отдай, только если изменилось». Когда страница
    не менялась, сервер отвечает пустым 304 вместо всего документа, и мы
    возвращаем (None, те же метки). Для страницы ФСБ это 70 килобайт против
    пары сотен байт на каждой проверке.

    Сервер может этого не уметь - тогда он просто продолжит отдавать всё
    целиком, и ничего не сломается."""
    headers = {
        "accept": "text/html,application/xhtml+xml",
        # без внятного User-Agent некоторые сайты отдают заглушку
        "user-agent": "Mozilla/5.0 (compatible; reestr-watch-bot/1.0)",
    }
    if validators:
        if validators.get("last_modified"):
            headers["if-modified-since"] = validators["last_modified"]
        if validators.get("etag"):
            headers["if-none-match"] = validators["etag"]

    r = request_via_proxy(
        "GET",
        url,
        headers=headers,
        verify=VERIFY_SSL,
        timeout=30,
    )

    # 304 = "с прошлого раза не менялось". Тела в ответе нет и разбирать
    # нечего - отдаём прежние метки, чтобы и дальше ими спрашивать.
    if r.status_code == 304:
        return None, validators

    r.raise_for_status()

    # Некоторые сайты не сообщают кодировку в заголовке, а requests в таком
    # случае угадывает latin-1 и превращает русский текст в мусор.
    if encoding:
        r.encoding = encoding

    fresh = {
        "last_modified": r.headers.get("Last-Modified"),
        "etag": r.headers.get("ETag"),
    }
    supported = bool(fresh["last_modified"] or fresh["etag"])
    return BeautifulSoup(r.text, "html.parser"), (fresh if supported else None)


def parse_page_links(watcher, soup):
    """{ключ: {title, href}} по ссылкам страницы."""
    items = {}
    for a in soup.select(watcher["selector"]):
        text = " ".join(a.get_text().split())
        href = (a.get("href") or "").strip()
        if not text:
            continue
        items[page_item_key(href, text)] = {
            "title": text,
            "href": urljoin(watcher["url"], href),
        }
    return items


def last_page_number(soup):
    """Сколько всего страниц в постраничной навигации."""
    numbers = []
    for a in soup.find_all("a"):
        text = a.get_text().strip()
        if text.isdigit():
            numbers.append(int(text))
        match = re.search(r"[?&]page=(\d+)", a.get("href") or "")
        if match:
            numbers.append(int(match.group(1)))
    return max(numbers) if numbers else 1


def parse_table_rows(soup):
    """Строки таблицы вида «№ | текст | дата» в {ключ: {num, text, date}}."""
    items = {}
    for tr in soup.select("table tr"):
        cells = [" ".join(td.get_text().split()) for td in tr.find_all("td")]
        if len(cells) < 3 or not cells[0].isdigit():
            continue
        num = int(cells[0])
        items[f"m{num}"] = {"num": num, "text": cells[1], "date": cells[2]}
    return items


def parse_all_tables(soup):
    """Строки ВСЕХ таблиц страницы, у которых в первой ячейке номер.

    Именно всех, а не самой большой: на странице ФСБ свежие пункты добавлены
    отдельными таблицами на одну строку."""
    items = {}
    for tr in soup.select("table tr"):
        cells = [" ".join(td.get_text().split()) for td in tr.find_all("td")]
        if len(cells) < 3 or not re.fullmatch(r"\d{1,4}", cells[0]):
            continue
        num = int(cells[0])
        items[f"n{num}"] = {"num": num, "name": cells[1], "court": cells[2]}
    return items


# Как часто скачивать страницу целиком, даже если сервер уверяет, что она не
# менялась. Подстраховка: если его метки версии врут или залипли, мы всё
# равно свежую картину увидим - не позже чем через этот срок.
FULL_FETCH_INTERVAL = float(os.environ.get("FULL_FETCH_INTERVAL_SECONDS", str(6 * 3600)))


def fetch_single_page(watcher, watcher_state):
    """Скачивает одностраничного наблюдателя, по возможности условным
    запросом. Возвращает soup либо None, если страница не менялась."""
    validators = watcher_state.get("http")
    last_full = watcher_state.get("full_at", 0)
    if time.time() - last_full > FULL_FETCH_INTERVAL:
        validators = None  # время для честной полной перекачки

    soup, fresh = fetch_soup(watcher["url"], watcher.get("encoding"), validators)

    if soup is None:
        return None

    watcher_state["http"] = fresh
    watcher_state["full_at"] = time.time() if validators is None else last_full
    return soup


def format_org(item, marker):
    """Сообщение про одну организацию: номер, наименование целиком, решение."""
    lines = [f"{marker} №{item['num']}", item.get("name") or "(без наименования)"]
    if item.get("court"):
        lines.append("")
        lines.append(f"Решение: {item['court']}")
    return "\n".join(lines)


def diff_org_items(old, new):
    """Что изменилось в списке организаций. Тут страница одна и видна
    целиком, поэтому исключения и правки ловятся честно."""
    parts = []

    for key, item in sorted(new.items(), key=lambda kv: kv[1]["num"]):
        if key not in old:
            parts.append({
                "kind": "added",
                "title": f"№{item['num']}",
                "text": format_org(item, "➕ НОВАЯ ОРГАНИЗАЦИЯ"),
            })

    for key, item in sorted(old.items(), key=lambda kv: kv[1]["num"]):
        if key not in new:
            parts.append({
                "kind": "removed",
                "title": f"№{item['num']}",
                "text": format_org(item, "➖ ОРГАНИЗАЦИЯ ИСКЛЮЧЕНА"),
            })

    for key, item in sorted(new.items(), key=lambda kv: kv[1]["num"]):
        was = old.get(key)
        if not was:
            continue
        changes = []
        for field, label in (("name", "наименование"), ("court", "решение")):
            if was.get(field) != item.get(field):
                changes.append(f"  {label}:\n    было: {was.get(field)}\n    стало: {item.get(field)}")
        if changes:
            parts.append({
                "kind": "changed",
                "title": f"№{item['num']}",
                "text": f"✏️ ЗАПИСЬ ИЗМЕНЕНА №{item['num']}\n" + "\n".join(changes),
            })

    return parts


def fetch_table_tail(watcher):
    """Забирает последнюю страницу постраничной таблицы.

    Пункты в таких списках дописываются в конец, поэтому хватает двух
    запросов: первая страница - узнать, сколько их всего, и последняя -
    за самими записями."""
    encoding = watcher.get("encoding")
    # Тут условные запросы не используем: страниц две, ходим трижды в сутки,
    # экономить нечего, а лишняя логика - лишний способ ошибиться.
    first, _ = fetch_soup(watcher["url"], encoding)
    pages = last_page_number(first)

    if pages <= 1:
        return parse_table_rows(first), pages

    # пауза между запросами: сайт не любит, когда страницы дёргают подряд
    time.sleep(REQUEST_DELAY)
    separator = "&" if "?" in watcher["url"] else "?"
    last, _ = fetch_soup(f"{watcher['url']}{separator}page={pages}", encoding)
    return parse_table_rows(last), pages


def court_decision(text):
    """Вытаскивает первое решение суда из описания: «... (решение
    Пензенского областного суда от 15.07.2026, апелляционное ...)» ->
    «Пензенский областной суд от 15.07.2026». Если формат другой - None,
    и строку про суд просто не показываем, чтобы ничего не выдумывать."""
    match = re.search(r"решени[ея]\s+(.+?)\s+от\s+(\d{1,2}\.\d{1,2}\.\d{4})", text or "")
    if not match:
        return None
    court = match.group(1).strip()
    # в старых записях встречается «решение вынесено ... судом ... от ...»
    court = re.sub(r"^вынесено\s+", "", court)
    return f"{court} от {match.group(2)}"


def format_material(item, marker):
    """Сообщение про один пункт списка. Описание идёт целиком, как на сайте,
    без сокращений - оно редко длиннее 700 символов."""
    lines = [f"{marker} №{item['num']}"]
    if item.get("date"):
        lines.append(f"Внесён в список: {item['date']}")
    decision = court_decision(item.get("text"))
    if decision:
        lines.append(f"Решение: {decision}")
    lines.append("")
    lines.append(item.get("text") or "(без описания)")
    return "\n".join(lines)


def diff_table_items(old, new):
    """Что изменилось в хвосте таблицы: добавили, убрали, поправили текст."""
    parts = []

    for key, item in sorted(new.items(), key=lambda kv: kv[1]["num"]):
        if key not in old:
            parts.append({
                "kind": "added",
                "title": f"№{item['num']}",
                "text": format_material(item, "➕ НОВЫЙ МАТЕРИАЛ"),
            })

    # Исключения из списка здесь НЕ ловим, и это осознанно. Мы видим только
    # последнюю страницу, поэтому "пункт пропал из нашего окна" и "пункт
    # исключили" неотличимы. Хуже того: когда последняя страница заполнится
    # и начнётся следующая, из окна разом выпадут все 100 пунктов прежней -
    # и бот отрапортовал бы "исключено 100 материалов", хотя не изменилось
    # ничего. Для настоящей ловли исключений нужен полный обход всех страниц.

    for key, item in sorted(new.items(), key=lambda kv: kv[1]["num"]):
        was = old.get(key)
        if not was:
            continue
        changes = []
        if was.get("text") != item.get("text"):
            changes.append(f"  было: {was.get('text')}\n  стало: {item.get('text')}")
        if was.get("date") != item.get("date"):
            changes.append(f"  дата внесения: {was.get('date')} -> {item.get('date')}")
        if changes:
            parts.append({
                "kind": "changed",
                "title": f"№{item['num']}",
                "text": f"✏️ МАТЕРИАЛ ИЗМЕНЁН №{item['num']}\n" + "\n".join(changes),
            })

    return parts


def diff_page_items(old, new):
    """Что изменилось в списке ссылок: добавили, убрали, переименовали."""
    parts = []

    for key, item in new.items():
        if key not in old:
            parts.append({
                "kind": "added",
                "title": item["title"],
                "text": f"➕ НОВЫЙ НАБОР\n{item['title']}\n{item['href']}",
            })

    for key, item in old.items():
        if key not in new:
            parts.append({
                "kind": "removed",
                "title": item["title"],
                "text": f"➖ НАБОР ПРОПАЛ СО СТРАНИЦЫ\n{item['title']}\n{item['href']}",
            })

    for key, item in new.items():
        was = old.get(key)
        if was and was.get("title") != item["title"]:
            parts.append({
                "kind": "changed",
                "title": item["title"],
                "text": (
                    f"✏️ НАБОР ПЕРЕИМЕНОВАН\n"
                    f"  было: {was.get('title')}\n"
                    f"  стало: {item['title']}\n{item['href']}"
                ),
            })

    return parts


PAGE_DIFF_GROUPS = (
    ("added", "➕ Новые наборы данных"),
    ("removed", "➖ Наборы пропали со страницы"),
    ("changed", "✏️ Наборы переименованы"),
)

TABLE_DIFF_GROUPS = (
    ("added", "➕ Новые материалы в списке"),
    ("changed", "✏️ Материалы изменены"),
)

ORG_DIFF_GROUPS = (
    ("added", "➕ Новые организации в списке"),
    ("removed", "➖ Организации исключены из списка"),
    ("changed", "✏️ Записи изменены"),
)


def check_page_watcher(watcher, watcher_state):
    """Проверяет одну страницу. Первый раз - молча запоминает содержимое."""
    kind = watcher.get("type", "links")

    if kind == "table_all":
        soup = fetch_single_page(watcher, watcher_state)
        if soup is None:
            # сервер ответил "не менялось" - разбирать нечего
            log.info("[%s] Без изменений (страница не менялась, 304)", watcher["title"])
            return False
        items = parse_all_tables(soup)
        page_url = watcher["url"]
        diff = diff_org_items
        groups = ORG_DIFF_GROUPS
        full_text = True
        empty_error = "в таблицах не найдено ни одной пронумерованной строки"
        unit = "организаций"
    elif kind == "table_tail":
        items, pages = fetch_table_tail(watcher)
        page_url = f"{watcher['url']}{'&' if '?' in watcher['url'] else '?'}page={pages}"
        diff = diff_table_items
        groups = TABLE_DIFF_GROUPS
        # описания тут длинные и осмысленные, сокращать их до списка номеров
        # бессмысленно - шлём всегда целиком, Telegram сам разобьёт на части
        full_text = True
        empty_error = "в таблице не найдено ни одной строки"
        unit = "строк"
    else:
        soup = fetch_single_page(watcher, watcher_state)
        if soup is None:
            log.info("[%s] Без изменений (страница не менялась, 304)", watcher["title"])
            return False
        items = parse_page_links(watcher, soup)
        page_url = watcher["url"]
        diff = diff_page_items
        groups = PAGE_DIFF_GROUPS
        full_text = False
        empty_error = f"по селектору '{watcher.get('selector')}' не найдено ни одной ссылки"
        unit = "ссылок"

    if not items:
        # пусто почти наверняка значит, что вёрстка поменялась или вместо
        # страницы приехала заглушка - лучше упасть, чем решить, что
        # "всё удалили", и разослать панику
        raise RuntimeError(f"{empty_error} - возможно, изменилась вёрстка страницы")

    prev_items = watcher_state.get("items")
    if prev_items is None:
        watcher_state["items"] = items
        log.info("[%s] Инициализация: %s %s", watcher["title"], len(items), unit)
        return True

    parts = diff(prev_items, items)
    if not parts:
        log.info("[%s] Без изменений (%s %s)", watcher["title"], len(items), unit)
        return False

    log.info("[%s] Изменений: %s", watcher["title"], len(parts))
    for group_kind, header in groups:
        group = [p for p in parts if p["kind"] == group_kind]
        if not group:
            continue
        body = "\n\n".join(p["text"] for p in group) if full_text else format_diff(group)
        tg_send(f"🔔 {header}: {len(group)}\n{watcher['title']}\n{page_url}\n\n{body}")

    watcher_state["items"] = items
    return True


def current_page_slot(now=None):
    """Последний наступивший момент проверки страниц - строкой вида
    '2026-09-07T09'. Пока эта строка не сменилась, повторно не проверяем,
    так что перезапуск бота не приводит к лишним заходам на сайт."""
    if not PAGE_CHECK_HOURS:
        return None
    tz = ZoneInfo(PAGE_CHECK_TZ)
    now = now or datetime.now(tz)
    today = [now.replace(hour=h, minute=0, second=0, microsecond=0) for h in sorted(PAGE_CHECK_HOURS)]
    past = [slot for slot in today if slot <= now]
    slot = past[-1] if past else today[-1] - timedelta(days=1)
    return slot.strftime("%Y-%m-%dT%H")


def read_note():
    """Записка от человека - уходит всем получателям при перезапуске.

    Сначала смотрим переменную STARTUP_NOTE, потом файл note.txt. Строки,
    начинающиеся с #, выкидываем - в них в файле лежит инструкция, как им
    пользоваться. Если записки нет, возвращаем None, и подписчики при
    перезапуске не увидят вообще ничего."""
    raw = STARTUP_NOTE
    if not raw.strip():
        try:
            with open(NOTE_FILE, encoding="utf-8") as f:
                raw = f.read()
        except FileNotFoundError:
            return None
        except OSError:
            log.exception("Не удалось прочитать файл записки %s", NOTE_FILE)
            return None

    lines = [line for line in raw.splitlines() if not line.lstrip().startswith("#")]
    note = "\n".join(lines).strip()
    if not note:
        return None
    if len(note) > NOTE_LIMIT:
        note = note[:NOTE_LIMIT].rstrip() + "…"
    return note


def startup_message():
    """Техническая сводка при запуске - ТОЛЬКО админам.

    Названия реестров берём тем же запросом /info, которым бот и так
    пользуется для проверок. Если у какого-то не получилось получить
    название (например, сайт на секунду недоступен) - показываем его ID,
    чтобы не ронять всё сообщение целиком."""
    lines = ["✅ Бот обновлён и перезапущен. Следит за реестрами:"]
    for registry_id in REGISTRY_IDS:
        try:
            title = fetch_info(registry_id).get("title") or registry_id
        except Exception:  # noqa: BLE001
            log.exception("[%s] Не удалось получить название для стартового сообщения", registry_id)
            title = registry_id
        lines.append(f"• {title}")

    fast_pages = [w for w in PAGE_WATCHERS if w.get("fast")]
    scheduled_pages = [w for w in PAGE_WATCHERS if not w.get("fast")]

    if fast_pages:
        lines.append("\nИ за страницами - так же часто, как за реестрами:")
        for watcher in fast_pages:
            lines.append(f"• {watcher['title']}")

    if scheduled_pages:
        hours = ", ".join(f"{h}:00" for h in sorted(PAGE_CHECK_HOURS))
        lines.append(f"\nА за этими - в {hours} по {PAGE_CHECK_TZ}:")
        for watcher in scheduled_pages:
            lines.append(f"• {watcher['title']}")

    people = len(all_recipients())
    tail = (
        f"\nПолучателей: {people} (из них админов: {len(ADMIN_CHAT_IDS)}). "
        f"Интервал проверки реестров: {CHECK_INTERVAL} сек."
    )
    if ACCESS_PASSWORD:
        tail += "\nНовые подписчики: /start боту и пароль."
    lines.append(tail)
    return "\n".join(lines)


def announce_startup():
    """При запуске уходит два разных сообщения.

    Записка - всем: это единственное, что адресовано людям. Техническая
    сводка - только админам: подписчикам незачем знать, что владелец бота
    выкатил очередную правку."""
    note = read_note()
    if note:
        try:
            tg_send(f"📝 {note}")
        except Exception:  # noqa: BLE001
            log.exception("Не удалось разослать записку")

    try:
        tg_admin(startup_message())
    except Exception:  # noqa: BLE001
        log.exception("Не удалось отправить стартовое сообщение админам")


def check_proxy_expiry(state):
    """Раз в сутки смотрим на даты окончания прокси и предупреждаем заранее."""
    today = datetime.now(ZoneInfo(PAGE_CHECK_TZ)).date()
    if state.get("expiry_checked") == today.isoformat():
        return
    state["expiry_checked"] = today.isoformat()

    for proxy in POOL.proxies:
        if not proxy.get("expires"):
            continue
        try:
            left = (datetime.strptime(proxy["expires"], "%Y-%m-%d").date() - today).days
        except ValueError:
            log.warning("Не понимаю дату окончания у прокси №%s: %s", proxy["n"], proxy["expires"])
            continue
        if left < 0:
            PROBLEMS.notify(f"⌛️ {proxy_label(proxy)} просрочен ({proxy['expires']}).")
        elif left <= PROXY_EXPIRY_WARN_DAYS:
            PROBLEMS.notify(
                f"⌛️ {proxy_label(proxy)} заканчивается через {left} дн. "
                f"({proxy['expires']}). Пора продлевать."
            )


def main():
    log.info(
        "Старт. Реестров: %s. Получателей: %s (админов: %s). Интервал: %s сек. "
        "Прокси в списке: %s. Скрытые поля: %s",
        len(REGISTRY_IDS),
        len(CHAT_IDS),
        len(ADMIN_CHAT_IDS),
        CHECK_INTERVAL,
        len(POOL.proxies) or "нет (прямое подключение)",
        "показываются" if SHOW_HIDDEN_FIELDS else "не показываются",
    )
    state = load_state()

    # Поднимаем подписавшихся по паролю из файла состояния.
    DYNAMIC_CHAT_IDS.update({cid: True for cid in state.get("subscribers", {})})
    if ACCESS_PASSWORD:
        log.info(
            "Подписка по паролю включена. Постоянных получателей: %s, подписавшихся: %s",
            len(CHAT_IDS),
            len(DYNAMIC_CHAT_IDS),
        )
    else:
        log.info("Подписка по паролю выключена (нет ACCESS_PASSWORD)")

    announce_startup()

    while True:
        # Если сидим на резервном прокси - периодически пробуем вернуться
        # на основной: вдруг он уже ожил.
        POOL.maybe_return_to_main()
        check_proxy_expiry(state)

        registries_state = state.setdefault("registries", {})
        for i, registry_id in enumerate(REGISTRY_IDS):
            registry_state = registries_state.setdefault(registry_id, {})
            changed = False
            try:
                changed = check_registry(registry_id, registry_state)
                PROBLEMS.ok(f"reg:{registry_id}", registry_id)
            except Exception as e:  # noqa: BLE001
                log.exception("[%s] Ошибка проверки: %s", registry_id, e)
                PROBLEMS.failed(
                    f"reg:{registry_id}",
                    f"реестр {BASE}/#/registry/{registry_id}",
                    e,
                )
            finally:
                # Пишем файл состояния только когда там правда что-то
                # поменялось. В покое реестры не меняются неделями, а файл
                # весит мегабайты - переписывать его вхолостую каждые
                # полторы минуты незачем.
                if changed:
                    save_state(state)
            if i < len(REGISTRY_IDS) - 1:
                time.sleep(REQUEST_DELAY)

        # Страницы: помеченные "fast" проверяем каждый цикл, вместе с
        # реестрами, остальные - только когда наступил очередной назначенный
        # час из PAGE_CHECK_HOURS.
        slot = current_page_slot()
        pages_state = state.setdefault("pages", {})
        for watcher in PAGE_WATCHERS:
            watcher_state = pages_state.setdefault(watcher["key"], {})
            fast = bool(watcher.get("fast"))
            if not fast and (slot is None or watcher_state.get("slot") == slot):
                continue
            page_changed = False
            try:
                page_changed = check_page_watcher(watcher, watcher_state)
                if not fast:
                    watcher_state["slot"] = slot
                    page_changed = True  # отметку о проверке надо сохранить
                PROBLEMS.ok(f"page:{watcher['key']}", watcher["title"])
            except Exception as e:  # noqa: BLE001
                log.exception("[%s] Ошибка проверки страницы: %s", watcher["title"], e)
                PROBLEMS.failed(f"page:{watcher['key']}", watcher["title"], e)
                if not fast and PROBLEMS.streak.get(f"page:{watcher['key']}", 0) >= ERROR_ALERT_THRESHOLD:
                    # Про поломку уже сообщили - перестаём долбить сайт
                    # каждый цикл и ждём следующего назначенного часа.
                    watcher_state["slot"] = slot
                    page_changed = True
                    log.warning("[%s] Пропускаем до следующей проверки по расписанию", watcher["title"])
            finally:
                # Как и с реестрами: пишем файл, только если есть что писать.
                # Иначе быстрый наблюдатель за ФСБ заставлял бы переписывать
                # многомегабайтный state.json каждые полторы минуты.
                if page_changed:
                    save_state(state)
            time.sleep(REQUEST_DELAY)

        # Разбор итогов цикла: решаем, о чём писать людям, а о чём смолчать.
        PROBLEMS.end_cycle()

        # Паузу между проверками проводим не во сне, а слушая входящие:
        # так подписка по паролю отвечает сразу, а не через полтора цикла.
        if poll_updates(state, CHECK_INTERVAL):
            save_state(state)


if __name__ == "__main__":
    main()