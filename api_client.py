# Импорты для работы с HTTP запросами, SSL, асинхронностью, датами и логированием
import aiohttp          # Библиотека для асинхронных HTTP запросов
import ssl              # Для работы с SSL сертификатами
import asyncio          # Для работы с асинхронным кодом
from datetime import datetime, timedelta  # Для работы с датами/временем
from typing import Optional, Dict, Any, List  # Типы для подсказок
import logging          # Для логирования

# Создаем логгер для этого модуля (для записи ошибок и информации)
logger = logging.getLogger(__name__)

class ClubAPI:
    """
    Класс для работы с API приложения клуба.
    Обеспечивает аутентификацию, создание/удаление бронирований и работу с хостами.
    """
    def __init__(self, base_url: str, username: str, password: str, 
                 branch_id: Optional[int] = None, register_id: Optional[int] = None):
        """
        Конструктор класса - инициализация при создании объекта.
        
        Args:
            base_url: Базовый URL API (например, "https://127.0.0.1:8443")
            username: Логин для аутентификации
            password: Пароль для аутентификации
            branch_id: ID филиала (опционально)
            register_id: ID регистра (опционально)
        """
        # Сохраняем настройки подключения
        self.base_url = base_url.rstrip('/')  # Убираем / в конце URL если есть
        self.username = username               # Сохраняем логин
        self.password = password               # Сохраняем пароль
        self.branch_id = branch_id            # ID филиала (опционально)
        self.register_id = register_id         # ID регистра (опционально)
        
        # Токены для аутентификации (пока не получены)
        self.token: Optional[str] = None      # Токен доступа (будет получен при аутентификации)
        self.refresh_token: Optional[str] = None  # Токен обновления (будет получен при аутентификации)
        
        # HTTP сессия для переиспользования соединений (создается при первом запросе)
        self.session: Optional[aiohttp.ClientSession] = None
        
        # Кэш маппинга: номер ПК (number) -> hostId (id)
        # Например: {1: 5, 2: 6, 3: 7} означает ПК 1 имеет hostId 5
        self._hosts_cache: Optional[Dict[int, int]] = None
        
        # Настройка SSL контекста
        # Для внешних серверов используем стандартную проверку сертификатов
        # Для localhost можно отключить проверку
        self.ssl_context = ssl.create_default_context()
        # Определяем, является ли URL localhost или внутренним адресом
        is_local = "127.0.0.1" in base_url or "localhost" in base_url or "0.0.0.0" in base_url
        
        # Проверяем переменную окружения для принудительного отключения проверки SSL
        # (может быть полезно для серверов с самоподписанными сертификатами)
        import os
        disable_ssl_verify = os.getenv("CLUB_API_DISABLE_SSL_VERIFY", "false").lower() == "true"
        
        if is_local or disable_ssl_verify:
            # Для localhost или при отключенной проверке SSL отключаем проверку сертификатов
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE
            if disable_ssl_verify:
                logger.warning("Проверка SSL сертификатов отключена (CLUB_API_DISABLE_SSL_VERIFY=true)")
        # Для внешних серверов используем стандартную проверку (по умолчанию)
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """
        Создает или возвращает существующую HTTP сессию.
        
        Сессия переиспользуется для всех запросов, что повышает производительность.
        Если сессии нет или она закрыта - создается новая.
        
        Returns:
            aiohttp.ClientSession: HTTP сессия для запросов
        """
        if self.session is None or self.session.closed:
            # Если сессии нет или она закрыта - создаем новую
            connector = aiohttp.TCPConnector(ssl=self.ssl_context)  # Создаем коннектор с SSL настройками
            self.session = aiohttp.ClientSession(connector=connector)  # Создаем сессию
        return self.session  # Возвращаем существующую или только что созданную сессию
    
    async def close(self):
        """
        Закрывает HTTP сессию.
        
        Вызывается при завершении работы бота для корректного закрытия соединений.
        """
        if self.session and not self.session.closed:
            await self.session.close()  # Закрываем сессию асинхронно
    
    async def authenticate(self, max_retries: int = 5) -> bool:
        """
        Получение токена доступа через API с механизмом повторных попыток.
        
        Отправляет GET запрос к /api/v2.0/auth/accesstoken с логином и паролем,
        получает токен доступа и refresh токен, сохраняет их для дальнейших запросов.
        При временных ошибках (504, таймауты) выполняет повторные попытки.
        
        Args:
            max_retries: Максимальное количество попыток (по умолчанию 5)
        
        Returns:
            bool: True если аутентификация успешна, False при ошибке
        """
        # Подготовка параметров для запроса
        params = {
            "Username": self.username,  # Логин
            "Password": self.password   # Пароль
        }
        # Добавляем опциональные параметры если они заданы
        if self.branch_id is not None:
            params["BranchId"] = self.branch_id
        if self.register_id is not None:
            params["RegisterId"] = self.register_id
        
        # Список HTTP статусов, при которых стоит повторить попытку
        retryable_statuses = {500, 502, 503, 504}  # Временные ошибки сервера
        
        for attempt in range(1, max_retries + 1):
            try:
                session = await self._get_session()  # Получаем HTTP сессию
                
                logger.info(f"Попытка аутентификации {attempt}/{max_retries} к API: {self.base_url}/api/v2.0/auth/accesstoken")
                
                # Отправляем GET запрос к API для получения токена
                # УВЕЛИЧЕН таймаут до 90 секунд для очень медленных соединений и перегруженных серверов
                async with session.get(
                    f"{self.base_url}/api/v2.0/auth/accesstoken",  # URL эндпоинта
                    params=params,  # Параметры в URL (?Username=...&Password=...)
                    timeout=aiohttp.ClientTimeout(total=90, connect=30)  # Таймаут: общий 90 сек, подключение 30 сек
                ) as response:
                    if response.status == 200:  # Успешный ответ
                        data = await response.json()  # Парсим JSON ответ
                        
                        # ИСПРАВЛЕНО: API возвращает токен в объекте result
                        # Структура: {"result": {"token": "...", "refreshToken": "..."}, ...}
                        result = data.get("result", {})
                        
                        # Извлекаем токен из объекта result
                        self.token = result.get("token")
                        self.refresh_token = result.get("refreshToken")
                        
                        if not self.token:
                            logger.error("Токен не получен в ответе API")
                            logger.error(f"Структура ответа: {data}")
                            return False
                        
                        logger.info("Успешная аутентификация в API")
                        return True
                    elif response.status in retryable_statuses:
                        # Временная ошибка сервера - можно повторить попытку
                        error_text = f"HTTP {response.status}"
                        try:
                            error_data = await response.json()
                            error_text = f"HTTP {response.status}: {error_data}"
                        except:
                            try:
                                error_text = f"HTTP {response.status}: {await response.text()}"
                            except:
                                pass
                        
                        if attempt < max_retries:
                            # Вычисляем задержку с экспоненциальным backoff (увеличена базовая задержка)
                            # 5, 10, 20, 40 секунд между попытками для 504 ошибок
                            delay = 5 * (2 ** (attempt - 1))
                            logger.warning(f"Временная ошибка сервера ({error_text}). Повтор через {delay} сек...")
                            await asyncio.sleep(delay)
                            continue  # Повторяем попытку
                        else:
                            # Последняя попытка не удалась
                            logger.error(f"Ошибка аутентификации после {max_retries} попыток: {error_text}")
                            return False
                    else:  # Другая ошибка (не 200 и не временная)
                        # Пытаемся получить JSON с описанием ошибки
                        try:
                            error_data = await response.json()
                            logger.error(f"Ошибка аутентификации (HTTP {response.status}): {error_data}")
                        except:
                            # Если не JSON, получаем текст ошибки
                            error_text = await response.text()
                            logger.error(f"Ошибка аутентификации (HTTP {response.status}): {error_text}")
                        return False
                        
            except aiohttp.ClientConnectorError as e:
                # Ошибка подключения (сервер недоступен, неправильный адрес и т.д.)
                if attempt < max_retries:
                    delay = 5 * (2 ** (attempt - 1))  # Увеличенная задержка: 5, 10, 20, 40 сек
                    logger.warning(f"Ошибка подключения к API {self.base_url}: {e}. Повтор через {delay} сек...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(f"Не удалось подключиться к API {self.base_url} после {max_retries} попыток: {e}")
                    logger.error(f"Тип ошибки: {type(e).__name__}")
                    logger.warning("Проверьте:")
                    logger.warning("  1. Доступность сервера по адресу (проверьте в браузере)")
                    logger.warning("  2. Правильность URL (должен начинаться с http:// или https://)")
                    logger.warning("  3. Наличие интернет-соединения")
                    logger.warning("  4. Настройки файрвола/прокси")
                    return False
            except asyncio.TimeoutError:
                # Таймаут (сервер не отвечает в течение установленного времени)
                if attempt < max_retries:
                    delay = 5 * (2 ** (attempt - 1))  # Увеличенная задержка: 5, 10, 20, 40 сек
                    logger.warning(f"Таймаут при подключении к API {self.base_url}. Повтор через {delay} сек...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(f"Таймаут при подключении к API {self.base_url} после {max_retries} попыток")
                    logger.warning("API сервер не отвечает в течение 90 секунд")
                    logger.warning("Проверьте доступность сервера и скорость интернет-соединения")
                    logger.warning("Возможно, сервер перегружен или недоступен")
                    return False
            except aiohttp.ClientSSLError as e:
                # Ошибка SSL сертификата - не повторяем попытку
                logger.error(f"Ошибка SSL при подключении к API {self.base_url}: {e}")
                logger.warning("Проблема с SSL сертификатом. Проверьте настройки SSL или используйте HTTP вместо HTTPS")
                return False
            except Exception as e:
                # Любая другая неожиданная ошибка
                if attempt < max_retries:
                    delay = 5 * (2 ** (attempt - 1))  # Увеличенная задержка: 5, 10, 20, 40 сек
                    logger.warning(f"Исключение при аутентификации ({type(e).__name__}): {e}. Повтор через {delay} сек...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(f"Исключение при аутентификации после {max_retries} попыток: {type(e).__name__}: {e}")
                    return False
        
        # Если дошли сюда - все попытки исчерпаны
        return False
    
    async def _ensure_authenticated(self) -> bool:
        """
        Проверяет наличие токена и получает его при необходимости.
        
        Эта функция вызывается перед каждым запросом к API, чтобы убедиться,
        что у нас есть валидный токен доступа.
        
        Returns:
            bool: True если токен есть или успешно получен, False при ошибке
        """
        if not self.token:  # Если токена нет
            return await self.authenticate()  # Получаем его
        return True  # Токен есть, все ОК
    
    def _calculate_duration_minutes(self, time_from: str, time_to: str) -> int:
        """
        Вычисляет длительность бронирования в минутах из времени начала и конца.
        
        Args:
            time_from: Время начала в формате "HH:MM" (например, "14:00")
            time_to: Время конца в формате "HH:MM" (например, "17:00")
        
        Returns:
            int: Длительность в минутах (например, 180 для 3 часов)
        """
        try:
            # Парсим строки времени в объекты datetime
            start = datetime.strptime(time_from, "%H:%M")  # "14:00" -> datetime
            end = datetime.strptime(time_to, "%H:%M")      # "17:00" -> datetime
            
            # Если конец раньше начала - значит переход через полночь
            # Например: 23:00 -> 01:00 (следующий день)
            if end < start:
                end += timedelta(days=1)  # Добавляем один день
            
            # Вычисляем разницу в секундах и переводим в минуты
            duration = (end - start).total_seconds() / 60
            return int(duration)  # Возвращаем целое число минут
        except Exception as e:
            logger.error(f"Ошибка вычисления длительности: {e}")
            return 60  # По умолчанию возвращаем 1 час (60 минут) при ошибке 
    
    def _format_datetime(self, date: str, time: str) -> str:
        """
        Форматирует дату и время в ISO 8601 формат для API.
        
        Args:
            date: Дата в формате "YYYY-MM-DD" (например, "2025-01-15")
            time: Время в формате "HH:MM" (например, "14:00")
        
        Returns:
            str: Дата и время в формате ISO 8601 (например, "2025-01-15T14:00:00.000Z")
        """
        try:
            # Объединяем дату и время и парсим в datetime объект
            dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
            # "2025-01-15 14:00" -> datetime объект
            
            # Форматируем в ISO 8601 формат (требуется API)
            return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            # datetime -> "2025-01-15T14:00:00.000Z"
        except Exception as e:
            logger.error(f"Ошибка форматирования даты: {e}")
            # Fallback: возвращаем текущее время в ISO формате
            return datetime.now().isoformat() + "Z"
    
    async def get_hosts(self) -> Optional[List[Dict[str, Any]]]:
        """
        Получает список хостов (ПК) из API.
        
        Хост - это компьютер в клубе. Каждый хост имеет:
        - id: уникальный идентификатор в системе (hostId)
        - number: номер ПК (1, 2, 3... 26)
        - name: название
        - и другие поля
        
        Returns:
            Optional[List[Dict[str, Any]]]: Список хостов или None при ошибке
        """
        # Проверяем, что у нас есть токен доступа
        if not await self._ensure_authenticated():
            logger.error("Не удалось аутентифицироваться")
            return None
        
        try:
            session = await self._get_session()
            # Заголовок с токеном для авторизации
            headers = {
                "Authorization": f"Bearer {self.token}"  # Токен в заголовке Authorization
            }
            
            # ИСПРАВЛЕНО: Преобразуем булево значение в строку для параметров URL
            # aiohttp не принимает булевы значения в params, нужно строковое представление
            params = {"IsDeleted": "false"}  # Строка вместо булева значения
            
            # Отправляем GET запрос для получения списка хостов
            async with session.get(
                f"{self.base_url}/api/v2.0/hosts",  # URL эндпоинта
                headers=headers,  # Заголовок с токеном
                params=params,  # Параметры (только активные хосты)
                timeout=aiohttp.ClientTimeout(total=30, connect=15)  # Таймаут: общий 30 сек, подключение 15 сек
            ) as response:
                if response.status == 200:  # Успешный ответ
                    data = await response.json()  # Парсим JSON ответ
                    
                    # ИСПРАВЛЕНО: Проверяем структуру ответа (может быть в result)
                    if isinstance(data, dict) and "result" in data:
                        # Если ответ в формате {"result": {...}, ...}
                        result = data.get("result", {})
                        hosts = result.get("data", []) if isinstance(result, dict) else []
                    else:
                        # Стандартный формат
                        hosts = data.get("data", []) if isinstance(data, dict) else data if isinstance(data, list) else []
                    
                    logger.info(f"Получено {len(hosts)} хостов из API")
                    return hosts
                else:  # Ошибка
                    error_data = await response.json()
                    logger.error(f"Ошибка получения хостов: {response.status} - {error_data}")
                    return None
                    
        except Exception as e:
            logger.error(f"Исключение при получении хостов: {e}")
            return None
    
    async def _load_hosts_cache(self) -> bool:
        """
        Загружает кэш маппинга номер ПК (number) -> hostId (id) из API.
        
        Создает словарь для быстрого поиска hostId по номеру ПК.
        Например: {1: 5, 2: 6, 3: 7} означает:
        - ПК 1 имеет hostId 5
        - ПК 2 имеет hostId 6
        - ПК 3 имеет hostId 7
        
        Returns:
            bool: True если кэш успешно загружен, False при ошибке
        """
        if self._hosts_cache is not None:
            return True  # Кэш уже загружен, ничего делать не нужно
        
        # Получаем список хостов из API
        hosts = await self.get_hosts()
        if hosts is None:
            return False  # Не удалось получить хосты
        
        # Создаем пустой словарь для кэша
        self._hosts_cache = {}
        
        # Проходим по всем хостам и создаем маппинг
        for host in hosts:
            number = host.get("number")    # Номер ПК (1, 2, 3...)
            host_id = host.get("id")       # ID хоста в системе (hostId)
            
            # Если оба значения есть - сохраняем в кэш
            if number is not None and host_id is not None:
                self._hosts_cache[number] = host_id  # Сохраняем маппинг
                logger.debug(f"Маппинг: ПК {number} -> hostId {host_id}")
        
        logger.info(f"Кэш хостов загружен: {len(self._hosts_cache)} записей")
        return True
    
    async def _map_pc_to_host_id(self, pc_number: int) -> int:
        """
        Преобразует номер ПК в hostId используя кэш из API.
        
        В боте пользователь выбирает ПК по номеру (1, 2, 3... 26),
        но API требует hostId (который может быть другим числом).
        Эта функция находит правильный hostId для выбранного номера ПК.
        
        Args:
            pc_number: Номер ПК выбранный пользователем (1-26)
        
        Returns:
            int: hostId для использования в API (или pc_number как fallback)
        """
        # Загружаем кэш если он еще не загружен
        await self._load_hosts_cache()
        
        # Ищем номер ПК в кэше
        if self._hosts_cache and pc_number in self._hosts_cache:
            host_id = self._hosts_cache[pc_number]  # Нашли в кэше
            logger.debug(f"Маппинг ПК {pc_number} -> hostId {host_id}")
            return host_id
        
        # Не нашли в кэше - используем pc_number как hostId (fallback)
        # Это может работать, если в системе номер ПК совпадает с hostId
        logger.warning(f"ПК {pc_number} не найден в кэше хостов, используем pc_number как hostId")
        return pc_number
    
    def _map_telegram_user_to_user_id(self, telegram_user_id: int) -> int:
        """
        Преобразует Telegram user_id в userId системы клуба.
        
        По умолчанию использует telegram_user_id напрямую как userId.
        Если в системе клуба нужен другой userId, можно:
        1. Создать пользователя через POST /api/v2.0/users
        2. Найти существующего пользователя через GET /api/v2.0/users
        3. Использовать один общий userId для всех броней из Telegram
        
        Args:
            telegram_user_id: ID пользователя в Telegram
        
        Returns:
            int: userId для использования в API
        """
        return telegram_user_id  # По умолчанию используем telegram_user_id как userId
    
    async def create_booking(self, telegram_user_id: int, pc_number: int, 
                           date: str, time_from: str, time_to: str,
                           contact_phone: str = "", contact_email: str = "") -> Optional[Dict[str, Any]]:
        """
        Создает бронирование через API приложения клуба.
        
        Args:
            telegram_user_id: ID пользователя в Telegram
            pc_number: Номер ПК (1-26)
            date: Дата в формате "YYYY-MM-DD" (например, "2025-01-15")
            time_from: Время начала в формате "HH:MM" (например, "14:00")
            time_to: Время конца в формате "HH:MM" (например, "17:00")
            contact_phone: Телефон для связи (опционально)
            contact_email: Email для связи (опционально)
        
        Returns:
            Optional[Dict[str, Any]]: Данные созданной брони (включая id) или None при ошибке
        """
        # Проверяем, что у нас есть токен доступа
        if not await self._ensure_authenticated():
            logger.error("Не удалось аутентифицироваться")
            return None
        
        try:
            session = await self._get_session()
            
            # Подготовка данных для запроса
            # Преобразуем номер ПК в hostId (например, ПК 1 -> hostId 5)
            host_id = await self._map_pc_to_host_id(pc_number)
            
            # Преобразуем Telegram user_id в userId системы клуба
            user_id = self._map_telegram_user_to_user_id(telegram_user_id)
            
            # Вычисляем длительность в минутах (например, "14:00"-"17:00" -> 180 минут)
            duration = self._calculate_duration_minutes(time_from, time_to)
            
            # Форматируем дату и время в ISO 8601 (например, "2025-01-15 14:00" -> "2025-01-15T14:00:00.000Z")
            date_time = self._format_datetime(date, time_from)
            
            # Формируем JSON тело запроса (payload)
            payload = {
                "userId": user_id,              # ID пользователя в системе клуба
                "date": date_time,              # Дата и время начала в ISO 8601
                "duration": duration,           # Длительность в минутах
                "contactPhone": contact_phone or "",  # Телефон (пустая строка если не указан)
                # ИСПРАВЛЕНО: Если email невалидный - используем пустую строку
                "contactEmail": contact_email if contact_email and "@" in contact_email and "." in contact_email.split("@")[1] else "",  # Email только если валидный
                "note": f"Бронь из Telegram бота (ПК {pc_number})",  # Заметка о источнике брони
                "pin": "",                     # PIN код (не используется)
                "status": 0,                   # Статус: 0 = активный
                "hosts": [{"hostId": host_id}],  # Массив хостов (один ПК)
                "users": [{"userId": user_id}]   # Массив пользователей (один пользователь)
            }
            
            # Заголовки запроса
            headers = {
                "Authorization": f"Bearer {self.token}",  # Токен для авторизации
                "Content-Type": "application/json"        # Тип данных - JSON
            }
            
            # Отправляем POST запрос для создания бронирования
            async with session.post(
                f"{self.base_url}/api/v2.0/reservations",  # URL эндпоинта
                json=payload,      # JSON тело запроса
                headers=headers,   # Заголовки с токеном
                timeout=aiohttp.ClientTimeout(total=30, connect=15)  # Таймаут: общий 30 сек, подключение 15 сек
            ) as response:
                if response.status == 200:  # Успешный ответ
                    data = await response.json()  # Парсим JSON ответ
                    
                    # ИСПРАВЛЕНО: Проверяем структуру ответа (может быть в result)
                    if isinstance(data, dict) and "result" in data:
                        # Если ответ в формате {"result": {...}, ...}
                        result = data.get("result", {})
                        booking_data = result if isinstance(result, dict) else {"id": result} if result else {}
                    else:
                        # Стандартный формат
                        booking_data = data
                    
                    logger.info(f"Бронь создана в API: {booking_data}")
                    return booking_data  # Возвращаем данные брони (включая id для синхронизации)
                else:  # Ошибка (не 200)
                    # Пытаемся получить JSON с описанием ошибки
                    try:
                        error_data = await response.json()
                        logger.error(f"Ошибка создания брони: {response.status} - {error_data}")
                    except:
                        # Если не JSON, получаем текст ошибки
                        error_text = await response.text()
                        logger.error(f"Ошибка создания брони: {response.status} - {error_text}")
                    return None
                    
        except Exception as e:
            logger.error(f"Исключение при создании брони: {e}")
            return None
    
    async def delete_booking(self, booking_id: int) -> bool:
        """
        Удаляет бронирование через API приложения клуба.
        
        Args:
            booking_id: ID бронирования в системе клуба (api_booking_id)
        
        Returns:
            bool: True если удаление успешно, False при ошибке
        """
        # Проверяем, что у нас есть токен доступа
        if not await self._ensure_authenticated():
            logger.error("Не удалось аутентифицироваться")
            return False
        
        try:
            session = await self._get_session()
            # Заголовок с токеном для авторизации
            headers = {
                "Authorization": f"Bearer {self.token}"
            }
            
            # Отправляем DELETE запрос для удаления бронирования
            # ID бронирования передается в URL: /api/v2.0/reservations/{booking_id}
            async with session.delete(
                f"{self.base_url}/api/v2.0/reservations/{booking_id}",  # URL с ID брони
                headers=headers,   # Заголовок с токеном
                timeout=aiohttp.ClientTimeout(total=30, connect=15)  # Таймаут: общий 30 сек, подключение 15 сек
            ) as response:
                if response.status == 200:  # Успешный ответ
                    logger.info(f"Бронь {booking_id} удалена из API")
                    # Успешный ответ может быть пустым объектом {} или содержать данные
                    return True
                else:  # Ошибка (не 200)
                    # Пытаемся получить JSON с описанием ошибки
                    try:
                        error_data = await response.json()
                        logger.error(f"Ошибка удаления брони: {response.status} - {error_data}")
                    except:
                        # Если не JSON, получаем текст ошибки
                        error_text = await response.text()
                        logger.error(f"Ошибка удаления брони: {response.status} - {error_text}")
                    return False
                    
        except Exception as e:
            logger.error(f"Исключение при удалении брони: {e}")
            return False

