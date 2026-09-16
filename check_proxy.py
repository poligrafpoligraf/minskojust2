"""Проверка купленного прокси.

Запуск (подставьте свои адрес, порт, логин и пароль):

    python check_proxy.py 193.150.70.73 8000 логин пароль

Если у прокси разные порты под HTTP и SOCKS5, укажите второй порт:

    python check_proxy.py 193.150.70.73 8000 логин пароль --socks-port 8001

Проверка идёт в два шага, чтобы отличать разные поломки:

  шаг 1 - открыть через прокси нейтральный российский сайт (ya.ru).
          Если не вышло - виноват сам прокси.
  шаг 2 - открыть реестр Минюста, тот самый, который опрашивает бот.
          Если шаг 1 прошёл, а шаг 2 нет - прокси живой, но именно до
          Минюста не доходит. Например, его адрес там забанили.
"""

import sys
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Нейтральная проверка: жив ли прокси вообще и ходит ли по https.
NEUTRAL = "https://ya.ru"
# Настоящая цель: лёгкая страница реестра с меткой последнего изменения.
TARGET = "https://reestrs.minjust.gov.ru/rest/registry/39b95df9-9a68-6b6d-e1e3-e6388507067e/info"

TIMEOUT = 40


def mask(proxy_url):
    """Прячет пароль, чтобы вывод можно было слать скриншотом."""
    if "@" not in proxy_url:
        return proxy_url
    head, tail = proxy_url.split("@", 1)
    scheme, creds = head.split("://", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{tail}"


def explain(error, stage=1):
    """Человеческое объяснение вместо трейсбека.

    stage=1 - проверяем сам прокси, stage=2 - уже целевой сайт. Разница
    важна: один и тот же таймаут на первом шаге значит «прокси мёртв», а на
    втором - «прокси жив, но до сайта не дозвонился»."""
    text = str(error)
    name = type(error).__name__

    if isinstance(error, requests.exceptions.ConnectionError) and "BadStatusLine" in text:
        return ("на этом порту не HTTP, а SOCKS.\n"
                "      Он ответил своим двоичным протоколом. Пробуйте socks5.")

    if isinstance(error, requests.exceptions.InvalidSchema):
        return ("нет поддержки SOCKS в requests.\n"
                '      Установите: pip install "PySocks>=1.5.6"')
    if isinstance(error, requests.exceptions.ProxyError):
        if "407" in text or "authentication" in text.lower():
            return ("прокси не принял логин и пароль (407).\n"
                    "      Либо опечатка, либо авторизация по IP - такая на Railway не заработает.")
        if "403" in text:
            return ("прокси ответил 403 - отказался строить туннель.\n"
                    "      Так себя ведёт HTTP-порт, не умеющий в https. Попробуйте SOCKS-порт.")
        if "timed out" in text.lower() or "timeout" in text.lower():
            if stage == 2:
                return ("прокси не смог соединиться с сайтом.\n"
                        "      Сам он жив (шаг 1 прошёл), а до этого сайта дороги нет.")
            return ("не отвечает сам прокси.\n"
                    "      На этом порту его нет или он не того протокола.")
        if "refused" in text.lower():
            return "прокси отказал в соединении: на этом порту его нет."
        return f"прокси недоступен ({name})."
    if isinstance(error, requests.exceptions.ReadTimeout):
        return ("туннель прокси принял, но ответа от сайта нет.\n"
                "      Значит прокси живой, а вот до этого сайта у него дороги нет.")
    if isinstance(error, requests.exceptions.ConnectTimeout):
        if stage == 2:
            return ("прокси не смог соединиться с сайтом.\n"
                    "      Сам он жив (шаг 1 прошёл) - похоже, его адрес на сайте заблокирован.")
        return "не отвечает сам прокси (таймаут подключения)."
    return f"{name}: {text[:160]}"


def fetch(proxy_url, url, stage=1):
    """Возвращает (успех, сообщение, секунды)."""
    started = time.time()
    try:
        r = requests.get(
            url,
            headers={"accept": "*/*", "user-agent": "Mozilla/5.0 (compatible; reestr-watch-bot/1.0)"},
            proxies={"http": proxy_url, "https": proxy_url},
            verify=False,      # у сайта Минюста неполная цепочка сертификатов, это норма
            timeout=TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return False, explain(e, stage), time.time() - started

    took = time.time() - started
    if r.status_code != 200:
        return False, f"ответ с кодом {r.status_code}, ожидался 200.", took
    return True, f"{len(r.content)} байт", took


def check(label, proxy_url):
    """Два шага для одного протокола. Возвращает True, если реестр открылся."""
    print(f"\n--- {label} ---")
    print(f"    {mask(proxy_url)}")

    ok, msg, took = fetch(proxy_url, NEUTRAL)
    print(f"    шаг 1, нейтральный сайт (ya.ru): {'✓' if ok else '✗'} {msg}  [{took:.0f} c]")
    if not ok:
        print("    ИТОГ: прокси не работает.")
        return False

    ok, msg, took = fetch(proxy_url, TARGET, stage=2)
    print(f"    шаг 2, реестр Минюста:           {'✓' if ok else '✗'} {msg}  [{took:.0f} c]")
    if not ok:
        print("    ИТОГ: прокси живой, но до реестра не доходит.")
        print("          Возможно, его адрес там заблокирован. Продавцу стоит пожаловаться:")
        print("          обычно такие меняют бесплатно.")
        return False

    print("    ИТОГ: ✓ РАБОТАЕТ полностью.")
    return True


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 4:
        print(__doc__)
        sys.exit(1)

    host, port, user, password = args[0], args[1], args[2], args[3]
    socks_port = port
    if "--socks-port" in sys.argv:
        socks_port = sys.argv[sys.argv.index("--socks-port") + 1]

    print("Проверяю прокси. Займёт пару минут.")
    print("ВАЖНО: выключите VPN перед запуском, иначе проверка соврёт.")

    socks_url = f"socks5://{user}:{password}@{host}:{socks_port}"
    http_url = f"http://{user}:{password}@{host}:{port}"

    socks_ok = check(f"SOCKS5, порт {socks_port}", socks_url)
    http_ok = check(f"HTTP, порт {port}", http_url)

    print("\n" + "=" * 58)
    if socks_ok or http_ok:
        # SOCKS5 предпочтительнее: HTTP-порт у части провайдеров не умеет
        # строить туннель для https.
        best = socks_url if socks_ok else http_url
        print("ГОДИТСЯ. Строка для Railway:\n")
        print(f"    {best}\n")
        print("Логин с паролем приняты - значит авторизация по ним, а не по IP.")
        print("Это то, что нужно для Railway.")
    else:
        print("НЕ ГОДИТСЯ. Смотрите строки «ИТОГ» выше - там написано, в чём дело.")
    print("=" * 58)


if __name__ == "__main__":
    main()