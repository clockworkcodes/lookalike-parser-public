# Lookalike Parser

Telegram-бот + Mini App для поиска похожих аккаунтов Instagram по графу рекомендаций (BFS).

## Запуск на своём ПК (бесплатно, рекомендуется)

Instagram видит ваш домашний IP — тот же, на котором создана сессия, поэтому сессия живёт дольше всего.
Прокси и Render не нужны. Бот работает, пока включён ПК.

1. Python 3.12 с [python.org](https://www.python.org/downloads/).
2. Двойной клик по **`start.bat`**. При первом запуске он:
   - установит зависимости;
   - откроет `.env` в Блокноте — впишите `BOT_TOKEN` и `ALLOWED_USER_ID`, сохраните, закройте;
   - запустит вход в Instagram (`generate_session.py`, на вопрос о прокси — Enter).
3. Необязательно, для Mini App: `winget install --id Cloudflare.cloudflared` и перезапустите `start.bat`
   (бесплатный туннель, адрес меняется при каждом запуске — бот подхватывает его сам).

Если раньше бот работал на Render — приостановите там сервис (Settings → Suspend), иначе два бота
с одним токеном будут конфликтовать.

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

## Прокси (очень желательно)

Render — это IP дата-центра. Instagram с подозрением относится к таким адресам и к резкой смене
страны/сети у живой сессии: сессия, созданная дома и используемая с Render, может быть отозвана
(`LoginRequired`), а аккаунт — временно ограничен (`PleaseWaitFewMinutes`).

1. Купите **резидентный или мобильный** прокси со статичным IP (sticky), желательно в вашей стране.
2. Сгенерируйте сессию **через этот прокси**: `py -3.12 generate_session.py` → введите его в поле «Прокси».
3. На Render задайте `IG_PROXY` = тот же прокси и `IG_SESSION_JSON` = новая сессия.

После `LoginRequired` бот сам блокирует новые задачи до обновления сессии, чтобы не добить аккаунт.

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
