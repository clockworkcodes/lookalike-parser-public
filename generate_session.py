"""
Локальный генератор сессии instagrapi.

Запуск на домашнем ПК (нужен Python 3.10+):
    py -3.12 -m pip install instagrapi==3.0.25 Pillow
    py -3.12 generate_session.py

Скрипт логинится в Instagram (с поддержкой 2FA и challenge-кодов),
сохраняет session.json рядом с собой через dump_settings и печатает
JSON-сессию одной строкой для переменной окружения IG_SESSION_JSON на Render.

Повторный запуск не создаёт «новое устройство»: если session.json уже есть,
сессия переиспользуется, а при её истечении instagrapi логинится заново
с теми же uuids/device — так Instagram видит тот же телефон.

Совет: если на Render используется IG_PROXY, укажите тот же прокси и здесь —
так Instagram увидит один и тот же IP при логине и при работе бота,
что заметно снижает риск challenge / LoginRequired.
"""

import getpass
import json
import sys
from pathlib import Path

from instagrapi import Client
from instagrapi.exceptions import BadPassword, ChallengeRequired, TwoFactorRequired

SESSION_PATH = Path(__file__).resolve().parent / "session.json"
REQUEST_DELAY_RANGE = [5, 15]


def challenge_code_handler(username: str, choice) -> str:
    return input(
        f"Instagram отправил код подтверждения для @{username} (способ: {choice}). Введите код: "
    ).strip()


def login_with_2fa(cl: Client, username: str, password: str) -> None:
    try:
        cl.login(username, password)
    except TwoFactorRequired:
        code = input("Включена двухфакторная аутентификация. Код 2FA: ").strip().replace(" ", "")
        cl.login(username, password, verification_code=code)


def login(cl: Client, username: str, password: str) -> None:
    """
    Логин с переиспользованием сохранённой сессии.

    После load_settings instagrapi в login() сам проверяет сессию через account_info()
    и логинится заново только если она истекла — с теми же uuids/device.
    """
    if SESSION_PATH.exists():
        print(f"Найден {SESSION_PATH.name} — переиспользую сессию и устройство…")
        cl.load_settings(SESSION_PATH)
    login_with_2fa(cl, username, password)


def main() -> int:
    print("=" * 60)
    print(" Генератор сессии instagrapi для Lookalike Parser")
    print("=" * 60)

    username = input("Логин Instagram: ").strip().lstrip("@")
    if not username:
        print("Логин не может быть пустым.")
        return 1

    password = getpass.getpass("Пароль (ввод скрыт): ")
    if not password:
        print("Пароль не может быть пустым.")
        return 1

    proxy = input(
        "Прокси http://user:pass@ip:port (Enter — без прокси, лучше тот же, что IG_PROXY): "
    ).strip()

    cl = Client()
    cl.delay_range = REQUEST_DELAY_RANGE
    cl.challenge_code_handler = challenge_code_handler
    if proxy:
        cl.set_proxy(proxy)

    print(f"\nМежду запросами к Instagram пауза {REQUEST_DELAY_RANGE[0]}–{REQUEST_DELAY_RANGE[1]} сек, это нормально.")
    try:
        login(cl, username, password)
    except BadPassword:
        print("\nНеверный пароль (или Instagram временно блокирует вход с этого IP).")
        return 1
    except ChallengeRequired as exc:
        print(
            "\nInstagram запросил проверку (challenge), которую не удалось пройти автоматически.\n"
            "Откройте приложение Instagram, подтвердите вход «Это я» и запустите скрипт снова.\n"
            f"Детали: {exc}"
        )
        return 1
    except Exception as exc:
        print(f"\nОшибка входа: {type(exc).__name__}: {exc}")
        return 1

    try:
        me = cl.account_info()
        print(f"\nВход выполнен: @{me.username} (pk={me.pk})")
    except Exception as exc:
        print(f"\nВход выполнен, но проверка аккаунта не удалась: {exc}")

    cl.dump_settings(SESSION_PATH)
    session_str = json.dumps(cl.get_settings(), ensure_ascii=False, separators=(",", ":"))

    print(f"\nСессия сохранена в: {SESSION_PATH}")
    print("\nСкопируйте строку ниже целиком в переменную окружения IG_SESSION_JSON на Render:")
    print("-" * 60)
    print(session_str)
    print("-" * 60)
    print("\nНикому не передавайте эту строку — она даёт полный доступ к аккаунту.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
