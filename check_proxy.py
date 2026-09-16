"""Проверка купленного прокси.

Запуск (подставьте свои адрес, порт, логин и пароль):

    python check_proxy.py 193.150.70.73 8000 логин пароль

Если у прокси разные порты под HTTP и SOCKS5, можно указать второй порт:

    python check_proxy.py 193.150.70.73 8000 логин пароль --socks-port 8001

Скрипт пробует через прокси открыть реестр Минюста - тот самый, который
потом будет опрашивать бот, - и говорит, что получилось, а что нет.
"""

import sys

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Лёгкая страница реестра: отдаёт метку времени последнего изменения.
TARGET = "https://reestrs.minjust.gov.ru/rest/registry/39b95df9-9a68-6b6d-e1e3-e6388507067e/info"


def try_proxy(label, proxy_url):
    """Одна попытка. Возвращает True, если через прокси реестр открылся."""
    print(f"\n--- {label} ---")
    print(f"    {proxy_url.replace(proxy_url.split(':')[2].split('@')[0], '***') if '@' in proxy_url else proxy_url}")
    try:
        r = requests.get(
            TARGET,
            headers={"accept": "application/json"},
            proxies={"http": proxy_url, "https": proxy_url},
            verify=False,      # у сайта неполная цепочка сертификатов, это норма
            timeout=25,
        )
    except requests.exceptions.InvalidSchema:
        print("    ✗ Нет поддержки SOCKS в requests.")
        print("      Установите: pip install \"PySocks>=1.5.6\"")
        return False
    except requests.exceptions.ProxyError as e:
        text = str(e)
        if "407" in text or "authentication" in text.lower():
            print("    ✗ Прокси не принял логин и пароль (ошибка 407).")
            print("      Либо опечатка в них, либо у прокси авторизация по IP,")
            print("      а такая на Railway не заработает.")
        elif "403" in text:
            print("    ✗ Прокси ответил 403 - отказался строить туннель.")
            print("      Так себя ведёт HTTP-порт, не умеющий в https. Попробуйте SOCKS5.")
        elif "timed out" in text.lower() or "timeout" in text.lower():
            print("    ✗ Прокси не отвечает - соединение не установилось.")
            print("      Проверьте адрес и порт; если верны - прокси мёртв")
            print("      или пускает только с разрешённого IP.")
        elif "refused" in text.lower():
            print("    ✗ Прокси отказал в соединении: на этом порту его нет.")
            print("      Скорее всего, порт другой.")
        else:
            print(f"    ✗ Прокси недоступен: {e}")
        return False
    except requests.exceptions.ConnectTimeout:
        print("    ✗ Прокси не отвечает (таймаут). Проверьте адрес и порт.")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"    ✗ Не получилось: {type(e).__name__}: {e}")
        return False

    if r.status_code != 200:
        print(f"    ✗ Реестр ответил кодом {r.status_code}, ожидался 200.")
        return False

    try:
        title = r.json().get("title", "(без названия)")
    except ValueError:
        print("    ✗ Ответ пришёл, но это не данные реестра.")
        return False

    print(f"    ✓ РАБОТАЕТ. Реестр открылся: {title}")
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

    print("Проверяю прокси. Займёт до минуты.")
    print("ВАЖНО: выключите VPN перед запуском, иначе проверка соврёт -")
    print("будет непонятно, кто именно достучался до сайта, прокси или VPN.")

    socks_ok = try_proxy(
        f"SOCKS5, порт {socks_port}",
        f"socks5://{user}:{password}@{host}:{socks_port}",
    )
    http_ok = try_proxy(
        f"HTTP, порт {port}",
        f"http://{user}:{password}@{host}:{port}",
    )

    print("\n" + "=" * 55)
    if socks_ok or http_ok:
        # SOCKS5 предпочтительнее: на прошлом прокси HTTP-порт не умел
        # строить туннель для https и отвечал 403.
        if socks_ok:
            best = f"socks5://{user}:{password}@{host}:{socks_port}"
            print("ГОДИТСЯ. Вписывайте в PROXY_URL на Railway эту строку:")
        else:
            best = f"http://{user}:{password}@{host}:{port}"
            print("ГОДИТСЯ (но только по HTTP). Строка для PROXY_URL:")
        print(f"\n    {best}\n")
        print("И раз логин с паролем приняты - значит авторизация по ним,")
        print("а не по IP. Это то, что нужно для Railway.")
    else:
        print("НЕ ГОДИТСЯ: ни одним протоколом до реестра достучаться не вышло.")
        print("Смотрите текст ошибок выше - там написано, в чём дело.")
    print("=" * 55)


if __name__ == "__main__":
    main()
