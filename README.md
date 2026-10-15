# Lookalike Parser

Telegram-бот + Mini App для поиска похожих аккаунтов Instagram по графу рекомендаций (BFS).

## Переменные окружения

| Переменная | Обязательна | Описание |
|---|---|---|
| `BOT_TOKEN` | да | Токен от @BotFather |
| `ALLOWED_USER_ID` | да | Ваш Telegram ID (узнать: @userinfobot) |
| `IG_SESSION_JSON` | да | Строка из `generate_session.py` |
| `IG_PROXY` | нет | `http://user:pass@ip:port` |
| `RENDER_EXTERNAL_URL` | авто | Render задаёт сам |

## 1. Сессия Instagram

Нужен Python 3.10+ (instagrapi 3.x не ставится на 3.8/3.9). На Windows с несколькими версиями Python:

```bash
py -3.12 -m pip install instagrapi==3.0.25 Pillow
py -3.12 generate_session.py
```

Если на Render будет `IG_PROXY`, укажите тот же прокси при генерации.
Повторный запуск переиспользует `session.json` и то же «устройство», а не логинится с нуля.

## Защита от чекпоинтов

- Перед **каждым** HTTP-запросом к Instagram — `time.sleep(random.uniform(5, 15))`
  (`Client.delay_range`, выполняется в рабочем потоке, бот не зависает).
- Бот никогда не логинится паролем: только загружает сессию. Обновлённые cookies
  сохраняются через `dump_settings` в `session.json` каждые 20 запросов, после задачи и при остановке.
- На Render Free диск сбрасывается при рестарте — тогда сессия снова берётся из `IG_SESSION_JSON`
  (это та же сессия, повторного логина нет).
- Ориентир по времени: быстрый режим ~10 сек на один граф-запрос, глубокий — ещё ~10 сек на каждый профиль.

## 2. Деплой на Render

1. Render → **New → Blueprint** → выберите этот репозиторий (`render.yaml` подхватится сам).
2. Введите `BOT_TOKEN`, `ALLOWED_USER_ID`, `IG_SESSION_JSON` (и `IG_PROXY` при необходимости) → **Apply**.
3. В cron-job.org: `GET https://<сервис>.onrender.com/health` каждые 10 минут.
4. В Telegram: `/start` → кнопка «🚀 Открыть парсер».

## Команды

- `/parse user1, user2`
- `/parse seeds=user1,user2 limit=80 kw=prod,beats deep=1 min=1000 max=50000`
- `/diag username` — какие источники похожих аккаунтов отвечают для этого профиля
- `/status`, `/stop`

## Тесты

```bash
python -m unittest discover -s tests -v
```
