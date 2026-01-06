# Импорт библиотеки для работы с SQLite базами данных в асинхронном режиме
import aiosqlite  # Асинхронная версия sqlite3 для работы с БД без блокировки

# Имя файла базы данных
# SQLite хранит данные в файле на диске (club.db в корне проекта)
DB_NAME = "club.db"

async def init_db():
    """
    Инициализация базы данных.
    
    Создает таблицу bookings если её нет, и добавляет колонку api_booking_id
    для существующих баз данных (обратная совместимость).
    
    Вызывается при запуске бота для гарантии, что таблица существует.
    """
    # async with - автоматически закрывает соединение после использования
    # aiosqlite.connect() - открывает соединение с БД (создает файл если его нет)
    async with aiosqlite.connect(DB_NAME) as db:
        # Создаем таблицу bookings если её еще нет
        # CREATE TABLE IF NOT EXISTS - безопасная команда, не вызовет ошибку если таблица уже есть
        await db.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,  -- Автоинкрементный ID (1, 2, 3...)
            user_id INTEGER,                       -- ID пользователя в Telegram
            pc_number INTEGER,                     -- Номер ПК (1-26)
            date TEXT,                             -- Дата в формате "YYYY-MM-DD"
            time_from TEXT,                        -- Время начала в формате "HH:MM"
            time_to TEXT,                          -- Время окончания в формате "HH:MM"
            api_booking_id INTEGER                 -- ID брони в API приложения клуба (для синхронизации)
        )
        """)
        
        # Добавляем колонку api_booking_id если её нет (для существующих БД)
        # Это нужно для обратной совместимости со старыми версиями базы данных
        # Если БД была создана до добавления этой колонки - добавляем её
        try:
            # Пытаемся добавить колонку
            await db.execute("ALTER TABLE bookings ADD COLUMN api_booking_id INTEGER")
            await db.commit()  # Сохраняем изменения
        except aiosqlite.OperationalError:
            # Если колонка уже существует - получаем OperationalError
            # Игнорируем ошибку (это нормально, если БД уже была обновлена ранее)
            pass
        
        # Сохраняем все изменения в БД
        await db.commit()


async def is_pc_available(pc, date, time_from, time_to):
    """
    Проверяет доступность ПК на указанное время.
    
    Проверяет, нет ли пересечений с существующими бронями:
    - Тот же ПК
    - Та же дата
    - Временные интервалы пересекаются
    
    Args:
        pc: Номер ПК (1-26)
        date: Дата в формате "YYYY-MM-DD"
        time_from: Время начала в формате "HH:MM"
        time_to: Время окончания в формате "HH:MM"
    
    Returns:
        bool: True если ПК свободен, False если занят
    """
    async with aiosqlite.connect(DB_NAME) as db:
        # SQL запрос для проверки пересечений временных интервалов
        # Ищем брони, где:
        # - pc_number совпадает
        # - date совпадает
        # - Временные интервалы пересекаются
        #
        # Логика проверки пересечения:
        # Интервалы [A, B] и [C, D] пересекаются, если: A < D и C < B
        # В нашем случае:
        # - time_from < time_to (время начала новой брони < время конца существующей)
        # - time_to > time_from (время конца новой брони > время начала существующей)
        cursor = await db.execute("""
        SELECT 1 FROM bookings
        WHERE pc_number = ?        -- Тот же ПК
          AND date = ?             -- Та же дата
          AND time_from < ?        -- Время начала существующей брони < время конца новой
          AND time_to > ?          -- Время конца существующей брони > время начала новой
        """, (pc, date, time_to, time_from))
        # Параметры: pc, date, time_to, time_from (в порядке использования в запросе)
        
        # fetchone() возвращает одну строку или None
        result = await cursor.fetchone()
        
        # Если result is None - значит пересечений нет, ПК свободен
        # Если result не None - значит есть пересечение, ПК занят
        return result is None

async def add_booking(user_id, pc, date, time_from, time_to, api_booking_id=None):
    """
    Добавляет новую бронь в базу данных.
    
    Создает запись о бронировании в локальной БД.
    api_booking_id может быть None при создании, и обновиться позже после синхронизации с API.
    
    Args:
        user_id: ID пользователя в Telegram
        pc: Номер ПК (1-26)
        date: Дата в формате "YYYY-MM-DD"
        time_from: Время начала в формате "HH:MM"
        time_to: Время окончания в формате "HH:MM"
        api_booking_id: ID брони в API приложения клуба (опционально, может быть None)
    """
    async with aiosqlite.connect(DB_NAME) as db:
        # INSERT INTO - добавляем новую запись в таблицу bookings
        # VALUES (?, ?, ?, ?, ?, ?) - параметризованный запрос (защита от SQL инъекций)
        # ? заменяются на значения из кортежа (user_id, pc, date, time_from, time_to, api_booking_id)
        await db.execute(
            "INSERT INTO bookings (user_id, pc_number, date, time_from, time_to, api_booking_id) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, pc, date, time_from, time_to, api_booking_id)
        )
        # commit() - сохраняем изменения в БД
        # Без commit() изменения не будут сохранены
        await db.commit()

async def update_booking_api_id(booking_id, api_booking_id):
    """
    Обновляет api_booking_id для существующей брони.
    
    Вызывается после успешного создания брони в API приложения клуба.
    Сохраняет api_booking_id для синхронизации (чтобы можно было удалить бронь из API).
    
    Args:
        booking_id: ID брони в локальной БД (id из таблицы bookings)
        api_booking_id: ID брони в API приложения клуба (получен из ответа API)
    """
    async with aiosqlite.connect(DB_NAME) as db:
        # UPDATE - обновляем существующую запись
        # SET api_booking_id = ? - устанавливаем новое значение api_booking_id
        # WHERE id = ? - обновляем только запись с указанным id
        await db.execute(
            "UPDATE bookings SET api_booking_id = ? WHERE id = ?",
            (api_booking_id, booking_id)  # Параметры: сначала api_booking_id, потом booking_id
        )
        # Сохраняем изменения
        await db.commit()

async def get_last_booking(user_id):
    """
    Получает последнюю (самую новую) бронь пользователя.
    
    Используется для:
    - Отмены последней брони
    - Получения ID только что созданной брони для синхронизации с API
    
    Args:
        user_id: ID пользователя в Telegram
    
    Returns:
        tuple: Кортеж с данными брони (id, pc_number, date, time_from, time_to, api_booking_id)
               или None если броней нет
    """
    async with aiosqlite.connect(DB_NAME) as db:
        # SELECT - выбираем данные из таблицы
        # ORDER BY id DESC - сортируем по id в убывающем порядке (самая новая первая)
        # LIMIT 1 - берем только одну запись (самую новую)
        cursor = await db.execute("""
        SELECT id, pc_number, date, time_from, time_to, api_booking_id
        FROM bookings
        WHERE user_id = ?        -- Только брони этого пользователя
        ORDER BY id DESC         -- Сортируем по ID (самая новая первая)
        LIMIT 1                  -- Берем только одну запись
        """, (user_id,))
        # fetchone() - получаем одну строку или None
        return await cursor.fetchone()

async def delete_booking(booking_id):
    """
    Удаляет бронь из базы данных.
    
    Удаляет запись о бронировании по её ID.
    Используется при отмене брони пользователем.
    
    Args:
        booking_id: ID брони в локальной БД (id из таблицы bookings)
    """
    async with aiosqlite.connect(DB_NAME) as db:
        # DELETE FROM - удаляем запись из таблицы
        # WHERE id = ? - удаляем только запись с указанным id
        await db.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
        # Сохраняем изменения
        await db.commit()

async def get_user_bookings(user_id):
    """
    Получает все брони пользователя.
    
    Возвращает список всех броней пользователя, отсортированных по дате и времени
    (самые новые первыми).
    
    Args:
        user_id: ID пользователя в Telegram
    
    Returns:
        list: Список кортежей с данными броней
              Каждый кортеж: (id, pc_number, date, time_from, time_to, api_booking_id)
              Пустой список если броней нет
    """
    async with aiosqlite.connect(DB_NAME) as db:
        # SELECT - выбираем все брони пользователя
        # ORDER BY date DESC, time_from DESC - сортируем:
        #   - Сначала по дате (самые новые первыми)
        #   - Потом по времени начала (самые поздние первыми)
        cursor = await db.execute("""
        SELECT id, pc_number, date, time_from, time_to, api_booking_id
        FROM bookings
        WHERE user_id = ?                    -- Только брони этого пользователя
        ORDER BY date DESC, time_from DESC   -- Сортировка: новые даты и поздние времена первыми
        """, (user_id,))
        # fetchall() - получаем все строки результата
        return await cursor.fetchall()