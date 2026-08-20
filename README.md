# Payment Processing Service

Асинхронный сервис процессинга платежей, построенный на паттерне **Transactional Outbox** для надёжной доставки событий.

## Стек технологий

| Компонент | Технология |
|-----------|-----------|
| API | FastAPI 0.111, Python 3.11 |
| Брокер сообщений | FastStream 0.5 + RabbitMQ 3.13 |
| База данных | PostgreSQL 15 + SQLAlchemy 2.0 (asyncpg) |
| Миграции | Alembic |
| Контейнеризация | Docker + Docker Compose |
| Тесты | pytest-asyncio, pytest-httpx |
| Линтинг | Ruff |

---

## Архитектура

Сервис состоит из трёх независимых процессов, взаимодействующих через Postgres и RabbitMQ:

```
Клиент
  │
  ▼
┌─────────┐   Transactional   ┌──────────┐   RabbitMQ    ┌──────────────┐
│   API   │ ──── Outbox ────▶ │  Relay   │ ─────────────▶│   Consumer   │
│(FastAPI)│                   │ (poller) │  payments.new  │(FastStream)  │
└─────────┘                   └──────────┘                └──────────────┘
     │                              │                            │
     └──────────────────────────────┼────────────────────────────┘
                                    │
                              ┌─────┴──────┐
                              │ PostgreSQL  │
                              │  payments  │
                              │   outbox   │
                              └────────────┘
```

### Компоненты

- **`api`** — FastAPI-сервис. Принимает `POST /api/v1/payments`, в одной транзакции сохраняет `Payment` в таблицу `payments` и событие в `outbox`. Поддерживает идемпотентность через заголовок `Idempotency-Key`.
- **`relay`** — сервис-реле. Периодически (каждые 5 сек) опрашивает таблицу `outbox`, публикует события в RabbitMQ и удаляет их. Изолирует API от сбоев брокера, гарантирует доставку **at-least-once**.
- **`consumer`** — FastStream-подписчик. Получает `payment_id` из очереди `payments.new`, эмулирует обработку (90% успех), обновляет статус в БД и отправляет webhook клиенту. Неудачные сообщения уходят в DLQ `payments.new.dlq`.

### Схема взаимодействия

```
1. POST /api/v1/payments
       │
2.     ▼  (одна транзакция)
   payments ← INSERT Payment (status=pending)
   outbox   ← INSERT Outbox  (payload={payment_id})
       │
3.     ▼  Relay (каждые 5 сек, SELECT ... FOR UPDATE SKIP LOCKED)
   RabbitMQ ← PUBLISH {payment_id} → queue: payments.new
   outbox   ← DELETE
       │
4.     ▼  Consumer
   payments ← UPDATE status = succeeded | failed
   webhook  ← POST {payment_id, status, updated_at}
```

---

## Быстрый старт

### Требования

- Docker & Docker Compose
- (для локальной разработки) Python 3.11+, [Poetry](https://python-poetry.org/)

### 1. Настройка окружения

```bash
cp .env.example .env
# Отредактируйте .env при необходимости
```

Переменные окружения:

| Переменная | Описание | Пример |
|------------|----------|--------|
| `POSTGRES_USER` | Пользователь PostgreSQL | `user` |
| `POSTGRES_PASSWORD` | Пароль PostgreSQL | `password` |
| `POSTGRES_DB` | Имя базы данных | `payments` |
| `RABBITMQ_DEFAULT_USER` | Пользователь RabbitMQ | `user` |
| `RABBITMQ_DEFAULT_PASS` | Пароль RabbitMQ | `password` |
| `API_KEY` | Ключ авторизации API (`X-API-Key`) | `super-secret-api-key` |

### 2. Запуск через Docker Compose

```bash
docker-compose up --build
```

Это запустит все сервисы: Postgres, RabbitMQ, API, Relay, Consumer.
Миграции применяются автоматически при старте `api`.

**Адреса сервисов после запуска:**

| Сервис | URL |
|--------|-----|
| API | http://localhost:8000 |
| Swagger UI | http://localhost:8000/docs |
| ReDoc | http://localhost:8000/redoc |
| RabbitMQ Management UI | http://localhost:15672 |

### 3. Остановка

```bash
docker-compose down          # остановить контейнеры
docker-compose down -v       # + удалить volumes (данные БД)
```

---

## Локальная разработка

### Установка зависимостей

```bash
poetry install
```

### Применение миграций

```bash
# Убедитесь что Postgres запущен (например через docker-compose up postgres)
poetry run alembic upgrade head
```

### Создание новой миграции

```bash
poetry run alembic revision --autogenerate -m "описание изменения"
```

### Запуск сервисов локально

```bash
# API
poetry run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload

# Consumer
poetry run faststream run src.consumer.main:app

# Relay
poetry run python -m src.relay.main
```

---

## Тестирование

### Запуск тестов локально (без Postgres/RabbitMQ)

Тесты используют **SQLite in-memory** — внешние сервисы не нужны:

```bash
# Установить зависимости (включая aiosqlite)
poetry install
pip install aiosqlite   # если не установлен

# Запустить все тесты
poetry run pytest

# С подробным выводом
poetry run pytest -v

# Только API-тесты
poetry run pytest tests/api/ -v

# Только юнит-тесты (consumer + relay)
poetry run pytest tests/consumer/ tests/relay/ -v
```

### Запуск тестов в Docker (с реальным Postgres)

```bash
docker-compose --profile test up --build --abort-on-container-exit
```

### Структура тестов

```
tests/
├── conftest.py              # Глобальные фикстуры: БД, клиенты, фабрики
├── api/
│   └── test_payments.py    # Интеграционные тесты API (23 теста)
├── consumer/
│   └── test_consumer.py    # Юнит-тесты consumer-логики (11 тестов)
└── relay/
    └── test_relay.py       # Юнит-тесты relay-сервиса (8 тестов)
```

### Линтинг и форматирование

```bash
# Проверка
poetry run ruff check .
poetry run ruff format --check .

# Автоисправление
poetry run ruff check . --fix
poetry run ruff format .
```

---

## API Reference

### Авторизация

Все запросы к `/api/v1/*` требуют заголовок `X-API-Key`.

### Endpoints

#### `POST /api/v1/payments` — Создать платёж

**Заголовки:**

| Заголовок | Обязателен | Описание |
|-----------|-----------|----------|
| `X-API-Key` | ✅ | API-ключ авторизации |
| `Idempotency-Key` | ✅ | Уникальный ключ для идемпотентности (UUID) |

**Тело запроса:**

```json
{
  "amount": 1500.50,
  "currency": "RUB",
  "description": "Оплата заказа #123",
  "webhook_url": "https://example.com/webhook",
  "extra_data": {"customer_id": "user-123"}
}
```

| Поле | Тип | Описание |
|------|-----|----------|
| `amount` | `decimal` | Сумма платежа, > 0 |
| `currency` | `enum` | `RUB`, `USD`, `EUR` |
| `description` | `string?` | Описание (до 255 символов) |
| `webhook_url` | `url` | URL для отправки результата |
| `extra_data` | `object?` | Произвольные метаданные |

**Ответы:**

- `202 Accepted` — платёж принят в обработку (новый)
- `200 OK` — платёж уже существует с таким `Idempotency-Key` (возвращает существующий)
- `401 Unauthorized` — неверный API-ключ
- `422 Unprocessable Entity` — ошибка валидации

#### `GET /api/v1/payments/{payment_id}` — Получить платёж

- `200 OK` — платёж найден
- `404 Not Found` — платёж не существует

#### `GET /health` — Healthcheck

```json
{"status": "ok"}
```

### Статусы платежа

| Статус | Описание |
|--------|----------|
| `pending` | Принят, ожидает обработки |
| `succeeded` | Успешно обработан |
| `failed` | Обработка завершилась ошибкой |

---

## Примеры запросов

### Создание платежа

```bash
curl -X POST "http://localhost:8000/api/v1/payments" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: super-secret-api-key" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{
    "amount": 1500.50,
    "currency": "RUB",
    "description": "Оплата заказа #123",
    "webhook_url": "https://webhook.site/YOUR_UNIQUE_ID",
    "extra_data": {"customer_id": "user-123"}
  }'
```

**Ответ (202):**
```json
{
  "payment_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "pending",
  "created_at": "2026-08-21T01:00:00Z"
}
```

### Получение статуса платежа

```bash
curl -X GET "http://localhost:8000/api/v1/payments/3fa85f64-5717-4562-b3fc-2c963f66afa6" \
  -H "X-API-Key: super-secret-api-key"
```

**Ответ (200):**
```json
{
  "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "amount": "1500.50",
  "currency": "RUB",
  "status": "succeeded",
  "description": "Оплата заказа #123",
  "extra_data": {"customer_id": "user-123"},
  "webhook_url": "https://webhook.site/YOUR_UNIQUE_ID",
  "created_at": "2026-08-21T01:00:00Z",
  "updated_at": "2026-08-21T01:00:05Z"
}
```

### Тестирование webhook

Для тестирования webhook используйте [webhook.site](https://webhook.site/) или [beeceptor.com](https://beeceptor.com/).

Consumer отправит POST на `webhook_url` с телом:
```json
{
  "payment_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "succeeded",
  "updated_at": "2026-08-21T01:00:05Z"
}
```
