# Этап 2. Архитектура

Один процесс: FastAPI (REST + веб-интерфейс) и фоновый планировщик в том же
asyncio-цикле, SQLite в одном файле. Никаких Redis/Celery/очередей.

```
                ┌──────────── sniflot (один процесс) ───────────────┐
 браузер ──────►│ web (Jinja2)   api (/api/v1)   mcp (stdio, опц.)  │
 bookmarklet    │        │             │              │             │
                │        └──────► services ◄──────────┘             │
                │                 │   │   │                         │
                │   scheduler ────┘   │   └──► notify (Telegram, …) │
                │                     ▼                             │
                │      sources (адаптеры) ── http (лимиты, ретраи,  │
                │                     │         кэш, паузы)         │
                │                     ▼                             │
                │    normalize / scoring        repo ──► SQLite     │
                └───────────────────────────────────────────────────┘
```

## Слои

| Модуль | Ответственность | Зависит от |
|---|---|---|
| `sources/base.py` | интерфейс адаптера, параметры поиска, `Offer`, ошибки | — |
| `sources/http.py` | `httpx` + ограничение частоты на источник, ретраи с backoff, TTL-кэш, пауза источника после блокировки | `httpx` |
| `sources/browser.py` | общий настоящий Chromium (Playwright, постоянный профиль, одна страница за раз, паузы по источнику, без картинок) | `playwright` (опционально) |
| `sources/avito.py`, `sources/ozon.py`, `sources/wildberries.py` | получение и разбор ответа площадки в `Offer`; Avito — HTTP или браузер, Ozon — только браузер | base, http, browser |
| `normalize.py` | исключающие слова, фильтр цены, ключ для дедупликации/сопоставления | — |
| `scoring.py` | выгодность относительно истории и соседей, «подозрительно дёшево» | — |
| `db.py`, `repo.py` | схема, миграции через `PRAGMA user_version`, запросы | `sqlite3` |
| `services.py` | сценарии: поиск, запуск сохранённого поиска, проверка карточки, события | всё выше |
| `scheduler.py` | раз в N секунд берёт «просроченные» поиски/карточки из БД и запускает | services |
| `notify/` | канал уведомлений (`Notifier`), форматирование сообщений | `httpx` |
| `api.py` | REST | services, repo |
| `web.py` + `templates/` | HTML-интерфейс без сборки фронтенда, графики — серверный SVG | services, repo |
| `mcp_server.py` | MCP-инструменты поверх services (опциональная зависимость `mcp`) | services |

Адаптеры ничего не знают о БД, уведомлениях и расписании; `services` ничего не
знает о конкретных площадках. Новая площадка = новый файл в `sources/` +
регистрация в `sources/registry.py`.

## Интерфейс адаптера

```python
class Source(Protocol):
    info: SourceInfo                         # id, название, тип (shop|classifieds), ограничения
    async def search(self, params: SearchParams) -> SearchResult: ...
    async def fetch(self, ref: str) -> Offer: ...   # ref = URL или внешний id
    def parse_ref(self, url: str) -> str | None: ...  # «этот URL мой?» → внешний id

@dataclass
class SearchParams:
    query: str                 # текст или URL поиска площадки
    min_price: int | None      # в рублях
    max_price: int | None
    region: str | None         # формат источника: dest у WB, slug у Avito
    condition: Condition       # any | new | used
    sort: Sort                 # relevance | price_asc | price_desc | newest
    pages: int                 # сколько страниц выдачи брать

@dataclass
class Offer:
    source: str; external_id: str; url: str; title: str
    price: int | None          # копейки
    old_price: int | None
    currency: str              # "RUB"
    available: bool | None
    condition: Condition
    seller, location, image_url, brand: str | None
    rating: float | None; reviews: int | None
    published_at: datetime | None
    raw: dict                  # компактные исходные поля для отладки
```

Ошибки адаптера — типизированные: `BlockedError` (403/429/капча/фаервол —
источник уходит на паузу, ретраев нет), `NotFoundError` (карточка удалена),
`SourceError` (сеть, 5xx, неизвестный формат — ретраи с backoff, затем ошибка).
Пустой результат и блокировка — разные исходы: блокировка никогда не
превращается в «объявление пропало».

## Модель данных (SQLite)

```
listings        id, source, external_id, url, title, norm_title,
                price, old_price, currency, available, condition,
                seller, location, image_url, brand, rating, reviews,
                published_at, status(active|gone|unknown),
                favorite, first_seen_at, last_seen_at, last_checked_at, raw
                UNIQUE(source, external_id)
price_points    id, listing_id, at, price, available     -- только при изменении
searches        id, name, query, sources(json), min_price, max_price, region,
                condition, sort, exclude_words(json), pages,
                interval_minutes, enabled, notify_new, notify_drop_pct,
                next_run_at, last_run_at, last_status, last_error,
                baseline_sources(json)  -- источники, уже записавшие базовую линию
search_hits     search_id, listing_id, first_seen_at, last_seen_at, last_price
watches         id, listing_id UNIQUE, target_price, drop_pct,
                last_price, last_available,  -- что видела эта проверка в прошлый раз
                interval_minutes, enabled, next_run_at, last_run_at,
                last_status, last_error
source_state    source PK, enabled, paused_until, failures, last_ok_at, last_error
events          id, at, kind, listing_id, search_id, text, delivered, error
```

Цены — целые копейки. Время — UTC ISO-8601. История пишется только при
изменении цены/наличия, поэтому таблица не растёт от «ничего не поменялось».

## Логика мониторинга

- **Сохранённый поиск:** первый успешный прогон каждого источника — его базовая
  линия, уведомлений нет (источник, бывший на паузе или добавленный позже, не
  выдаст всю выдачу как «новое»).
  Дальше: объявление, не встречавшееся в этом поиске → событие `new`;
  цена в выдаче упала на ≥ `notify_drop_pct` → `price_drop`. Новые объявления
  одного прогона уходят одним сообщением.
- **Отслеживаемая карточка:** `fetch` по расписанию; сравнение с тем, что видела
  прошлая проверка этой карточки (а не с полем объявления, которое мог обновить
  поиск); падение цены ≥ `drop_pct` или
  достижение `target_price` → уведомление; `NotFoundError` → статус `gone` +
  уведомление; `available` true→false → уведомление «нет в наличии».
- **Защита от ложных срабатываний:** падение цены больше чем вдвое
  уведомляется с пометкой «подозрительно» (может быть ошибка разбора или
  мошенническое объявление).
- **Паузы источника:** блокировка → `paused_until = now + base × 2^k` (base: WB
  10 мин, Avito 1 ч; не меньше `Retry-After`; до 12 ч), одно событие «источник
  ограничил доступ», задачи источника пропускаются без запросов, остальные
  источники работают. Успешный ответ сбрасывает счётчик.
- **Планировщик:** каждые 20 с выбирает задачи с `next_run_at <= now`, запускает
  по одной на источник (ограничение частоты внутри `http`), ставит
  `next_run_at = now + interval ± 10 %`.

## Выгодность

- `history_score` — насколько текущая цена ниже медианы собственной истории.
- `peer_score` — насколько цена ниже медианы цен той же выдачи (≥ 5 соседей).
- `suspicious` — цена < 40 % медианы соседей: частый признак мошенничества
  или неправильной категории.
- Сортировка «по выгодности» — по `peer_score`. Всё — обычная статистика, без LLM.

## REST API (`/api/v1`)

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/health` | живость, состояние источников |
| GET | `/sources` | источники, их ограничения и паузы |
| GET | `/search?q=&sources=&min_price=&max_price=&region=&condition=&sort=&exclude=` | разовый поиск по нескольким площадкам, объединённый и отсортированный |
| GET/POST | `/searches` | сохранённые поиски |
| GET/PATCH/DELETE | `/searches/{id}` | |
| POST | `/searches/{id}/run` | запустить сейчас |
| GET | `/searches/{id}/results` | объявления поиска с оценками |
| GET/POST | `/watches` | отслеживаемые карточки; POST принимает `url` или `listing_id` |
| PATCH/DELETE | `/watches/{id}` | |
| POST | `/watches/{id}/check` | проверить сейчас |
| GET | `/listings/{id}` | карточка + история цен |
| POST | `/listings/{id}/favorite` | избранное |
| GET | `/listings/{id}/history.{csv,json}` | экспорт истории |
| GET | `/events` | лента событий |
| POST | `/notify/test` | тестовое уведомление |

Опциональная базовая HTTP-авторизация на всё приложение (`SNIFLOT_AUTH_USER`/
`SNIFLOT_AUTH_PASSWORD`) — для домашней сети без reverse proxy.

## Веб-интерфейс

Серверный рендеринг Jinja2, один CSS-файл, без JS-сборки: дашборд (карточки,
поиски, последние события, состояние источников), поиск (таблица/карточки,
сортировки), страница поиска, карточка с SVG-графиком цены, избранное,
настройки (источники, Telegram-тест, bookmarklet). Адаптивная вёрстка на CSS grid.

## MCP

`python -m sniflot mcp` — stdio-сервер с инструментами `search_offers`,
`list_watches`, `add_watch`, `price_history`, `list_searches`. Это тонкая обёртка
над `services`; естественный язык разбирает сам агент, приложение получает
обычные параметры.
