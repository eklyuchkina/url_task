# URL Shortener API

Сервис сокращения длинных ссылок (по аналогии с tinyurl/bitly). Реализован на FastAPI, для хранения используется PostgreSQL, Redis — для кэширования часто используемых редиректов.

Документация API в Swagger: `/docs` после запуска.

---

## Запуск локально

Нужен Docker. В папке проекта:

```bash
docker-compose up --build
```

Сервис откроется на http://localhost:8000

---

## API

### Регистрация и авторизация

`POST /auth/register` — создание пользователя
```json
{"username": "user1", "password": "password123"}
```

`POST /auth/login` — получение токена
```json
{"username": "user1", "password": "password123"}
```
Ответ содержит `access_token`, его передаём в заголовке: `Authorization: Bearer {token}`

### Ссылки

`POST /links/shorten` — создать короткую ссылку. Можно указать свой alias или время истечения.
```json
{
  "original_url": "https://example.com",
  "custom_alias": "myLink",
  "expires_at": "2025-12-31T23:59:00"
}
```
Только `original_url` обязателен. Если пользователь авторизован — добавляем заголовок Authorization.

`GET /links/{code}` — редирект на оригинальный URL

`GET /links/{code}/stats` — статистика: URL, дата создания, кол-во переходов, дата последнего использования

`GET /links/search?original_url={url}` — поиск по оригинальному URL

`DELETE /links/{code}` и `PUT /links/{code}` — удаление/изменение. Только владелец ссылки, нужна авторизация. Для PUT тело запроса:
```json
{"original_url": "https://new-url.com"}
```

---

## База данных

PostgreSQL. Схема:

- **users**: id, username, hashed_password, created_at  
- **links**: id, short_code, original_url, custom_alias, user_id, created_at, expires_at, click_count, last_used_at

Redis кэширует redirect и stats, при обновлении или удалении ссылки кэш инвалидируется.

---

## Деплой

Сервис развёрнут на Render: [https://url-task-gaaz.onrender.com]

PostgreSQL и Redis поднимаются отдельными сервисами, переменные окружения: DATABASE_URL, REDIS_URL, SECRET_KEY.
