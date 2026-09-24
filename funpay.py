import asyncio
import base64
import html
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field

import requests
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from FunPayAPI import Account as _Account, Runner
from FunPayAPI.common import exceptions as fp_exceptions
from FunPayAPI.common.enums import OrderStatuses, EventTypes


class Account(_Account):
    """
    FunPay недавно добавил доп. защиту от ботов: сервер в ответ на некоторые запросы
    (в частности /runner/) требует куку `golden_seal`, которую нужно поймать из
    предыдущего ответа и пересылать дальше. Установленная версия FunPayAPI
    ничего не знает про эту куку и просто игнорирует её — из-за этого все запросы
    к /runner/ падали с 400 "Необходимая cookie отсутствует или устарела".

    Здесь переопределяем method(), чтобы запоминать ЛЮБЫЕ куки, которые сервер
    просит сохранить (Set-Cookie), и пересылать их во всех последующих запросах —
    как это делает обычный браузер.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._extra_cookies: dict[str, str] = {}

    def method(self, request_method, api_method, headers, payload,
               exclude_phpsessid=False, raise_not_200=False):
        headers = dict(headers)
        cookie = f"golden_key={self.golden_key}"
        if self.phpsessid and not exclude_phpsessid:
            cookie += f"; PHPSESSID={self.phpsessid}"
        for k, v in self._extra_cookies.items():
            cookie += f"; {k}={v}"
        headers["cookie"] = cookie
        if self.user_agent:
            headers["user-agent"] = self.user_agent

        link = api_method if api_method.startswith("https://funpay.com") else "https://funpay.com/" + api_method
        response = getattr(requests, request_method)(
            link, headers=headers, data=payload, timeout=self.requests_timeout, proxies=self.proxy or NO_PROXY
        )

        # запоминаем любые новые куки (например golden_seal) для последующих запросов
        if response.cookies:
            self._extra_cookies.update(response.cookies.get_dict())

        if response.status_code == 403:
            raise fp_exceptions.UnauthorizedError(response)
        elif response.status_code != 200 and raise_not_200:
            raise fp_exceptions.RequestFailedError(response)
        return response


# ================= НАСТРОЙКИ =================
# Токен твоего Telegram-бота. Берётся у @BotFather: /newbot → скопировать строку вида 123456:AA...
TELEGRAM_TOKEN = ""
# Твой числовой ID в Telegram. Узнать: написать @userinfobot. Только этот ID управляет ботом.
ADMIN_ID = 0
# Ключ сессии FunPay. Браузер → F12 → Application → Cookies → funpay.com → golden_key.
# НИКОМУ его не показывай: это полный доступ к аккаунту.
FUNPAY_GOLDEN_KEY = ""

# Пауза между запросами Runner'а к FunPay (сек). Чем меньше — тем быстрее бот
# видит новые сообщения и отвечает. 2 сек — безопасный минимум: ещё быстрее
# опрашивать не стоит, FunPay может посчитать это спамом запросов.
RUNNER_REQUEST_DELAY = 2
SESSION_REFRESH_SECONDS = 40 * 60  # обновлять сессию (PHPSESSID/csrf) не реже, чем раз в 40 минут

# Прокси для Telegram — заполни, если api.telegram.org недоступен напрямую (блокировка провайдером).
# Примеры: "http://127.0.0.1:1080" или "socks5://user:pass@host:port" (для socks5: pip install aiohttp-socks)
TELEGRAM_PROXY = ""

# --------- Fragment (автооплата звёзд) ---------
# Пока AUTO_PAY_FRAGMENT = False — после твоего «ДА» бот присылает данные для
# ручной оплаты на fragment.com и кнопку «✅ Оплатил». Когда заполнишь всё ниже,
# поставь True — появится полноценная автооплата.
AUTO_PAY_FRAGMENT = True
# hash из запросов fragment.com/api?hash=... (видно в DevTools -> Network, когда залогинен)
FRAGMENT_HASH = ""
# Куки залогиненного аккаунта fragment.com одной строкой: "stel_ssid=...; stel_dt=...; stel_token=..."
FRAGMENT_COOKIES = ""
# Прокси, через который браузер ходит на fragment.com (если сайт открыт через VPN/прокси).
# Fragment привязывает сессию к IP: если куки получены через VPN, бот должен ходить через него же.
# Формат: "http://127.0.0.1:8080" или "socks5://127.0.0.1:1080". Пусто "" = напрямую, без прокси.
FRAGMENT_PROXY = ""
# Сид-фраза TON-кошелька (24 слова через пробел), с которого оплачиваются звёзды
TON_SEED = ""
# API-ключ с tonapi.io (бесплатный тариф подходит)
TONAPI_KEY = ""


def check_config() -> None:
    """Проверить, что обязательные настройки заполнены.

    Запускаться с пустыми полями бессмысленно: лучше понятно сказать, что именно
    не заполнено, чем упасть где-то внутри с непонятной ошибкой.
    """
    problems = []
    if not TELEGRAM_TOKEN:
        problems.append("TELEGRAM_TOKEN — токен бота от @BotFather")
    if not ADMIN_ID:
        problems.append("ADMIN_ID — твой ID в Telegram (узнать у @userinfobot)")
    if not FUNPAY_GOLDEN_KEY:
        problems.append("FUNPAY_GOLDEN_KEY — кука golden_key с funpay.com")

    # Блок автовыдачи нужен только тем, кто включил AUTO_PAY_FRAGMENT.
    # Без него бот работает как уведомлялка и помощник — это нормальный режим.
    if AUTO_PAY_FRAGMENT:
        if not FRAGMENT_HASH:
            problems.append("FRAGMENT_HASH — hash из запросов fragment.com/api")
        if not FRAGMENT_COOKIES:
            problems.append("FRAGMENT_COOKIES — куки залогиненного fragment.com")
        if not TON_SEED.strip():
            problems.append("TON_SEED — 24 слова от TON-кошелька, с которого платишь")
        if not TONAPI_KEY:
            problems.append("TONAPI_KEY — бесплатный ключ с tonapi.io")

    if not problems:
        return

    print("")
    print("=" * 62)
    print("  БОТ НЕ ЗАПУЩЕН: не заполнены настройки в начале main.py")
    print("=" * 62)
    for item in problems:
        print("  ✗ " + item)
    print("")
    print("  Открой main.py в блокноте, заполни эти строки и запусти снова.")
    print("  Подробная инструкция — в файле README.txt рядом.")
    print("=" * 62)
    print("")
    raise SystemExit(1)


# --------- Анти-фрод ---------
MAX_USERNAME_ATTEMPTS = 8      # максимум попыток ввода юзернейма, дальше — только вручную
GREETING_COOLDOWN = 30 * 60    # «Я на месте!» не чаще раза в 30 минут на чат
MAX_STARS_PER_ORDER = 25000    # потолок звёзд на один заказ; больше — только вручную
FP_REPEAT_COOLDOWN = 120       # одинаковое сообщение в один чат — не чаще раза в 2 минуты
IN_PROGRESS_COOLDOWN = 10 * 60 # «заказ в обработке» — не чаще раза в 10 минут на чат
ADMIN_REPEAT_COOLDOWN = 120    # одинаковые уведомления админу — не чаще раза в 2 минуты
MAX_CONFIRM_ATTEMPTS = 8       # максимум непонятных ответов вместо ДА/НЕТ, дальше — заморозка
ORDER_RESOLVE_MAX_RETRIES = 5  # сколько раз пробовать открыть чат по заказу, прежде чем сдаться
AUTO_REFUND_ENABLED = True     # !возврат от покупателя — возвращать деньги автоматически
MENU_CACHE_SECONDS = 60        # как долго кэшировать балансы/лоты для главного меню
STALE_PAID_HOURS = 24          # сколько ждём подтверждения по выданному заказу, дальше — снимаем с контроля
STALE_FROZEN_HOURS = 72        # то же для замороженных сделок

# --------- Курс TON и автоцены ---------
FRAGMENT_TON_PER_STAR = 0.0107160   # сколько TON Fragment берёт за 1 звезду (сверяй раз в месяц)
TON_GAS_PER_ORDER = 0.01            # запас на комиссию сети на одну выдачу
AUTO_PRICING_ENABLED = True         # следить за курсом TON и двигать цены лотов
PRICING_DRY_RUN = True              # True = бот ТОЛЬКО показывает новые цены, но не меняет их.
                                    # Проверь /prices пару раз — и ставь False.
PRICE_UPDATE_SECONDS = 20 * 60      # как часто смотреть на курс
PRICE_RATE_TRIGGER = 0.01           # пересчитывать цены, если курс сдвинулся хотя бы на 1%
PRICE_TARGET_MARGIN = 0.05          # желаемая маржа (5% — режим теста)
PRICE_MIN_MARGIN = 0.02             # жёсткий пол: ниже себестоимости+2% не продаём НИКОГДА
PRICE_STEP_MIN_RUB = 1.0            # не дёргать цену ради копеек
PRICE_FOLLOW_MARKET = True          # учитывать цены конкурентов (только выдача по username)
PRICE_UNDERCUT_RUB = 1.0            # на сколько рублей подрезать самого дешёвого конкурента
PRICE_MAX_PER_STAR = 2.5            # потолок цены за 1⭐ (защита от диких значений)
TON_RATE_CACHE_SECONDS = 300        # как долго держать курс в кэше

# --------- Контроль запаса TON (стоп-лоты) ---------
AUTO_LOT_GUARD = True               # снимать с продажи лоты, на которые не хватает TON
LOT_GUARD_CHECK_SECONDS = 10 * 60   # как часто сверять баланс с лотами
TON_RESERVE = 0.05                  # неснижаемый остаток на кошельке (газ)
GUARD_HYSTERESIS = 0.1              # возвращать лот, когда запас на 10% больше нужного

# --------- Пауза бота ---------
PAUSE_HIDES_LOTS = False            # True = на паузе ещё и снимать все лоты с продажи
CONTROL_FILE = "bot_control.json"   # пауза/автоцены/снятые лоты переживают перезапуск

# --------- Фильтр рынка звёзд ---------
# По какому способу выдачи считать цены конкурентов: username / gift / link / all.
# Выдача подарком стоит дешевле и занижала статистику — теперь считаем только по юзернейму.
STARS_METHOD_FILTER = "username"
STARS_METHOD_KEEP_UNKNOWN = False   # брать ли в расчёт лоты, где способ выдачи не понятен

# --------- Чем платим на Fragment ---------
# Fragment умеет: GRAM, TON, USDT (ETH/TON/POL), USDC (ETH/Base/POL).
# ВНИМАНИЕ: не меняй вручную наугад. Сначала /pay_methods — он покажет,
# какое именно значение принимает внутренний API Fragment, и только потом ставь его.
FRAGMENT_PAYMENT_METHOD = "usdt_ton"     # после зонда ставить найденное значение для USDT
FRAGMENT_PAYMENT_CANDIDATES = [      # что перебирает /pay_methods
    "ton", "usdt_ton", "usdt-ton", "usdtton", "usdt", "USDT_TON", "gram",
]
USDT_GAS_TON = 0.08                  # сколько TON держать на газ под перевод жетона
USDT_DECIMALS = 6                    # у USDT на TON шесть знаков после запятой
JETTON_TRANSFER_OP = 0x0F8A7EA5      # op перевода жетона по TEP-74
USDT_PER_STAR = 0.015                # цена Fragment: 0.75 USDT за 50★ (проверить: /pay_methods)
SWAP_SPREAD = 0.032                  # переплата при свапе USDT->TON внутри кошелька.
                                     # Замеры разных дней дают от +0.5% до +3.2%, курс плавает.
                                     # Сверяй перед обменом и подставляй своё значение.
USDT_RUB_RATE = 86.911               # курс USDT на выводе FunPay (ДО комиссии)
USDT_GAS_COST_TON = 0.05             # сколько TON реально уходит на газ жетона (измерено)
FUNPAY_FEE_PCT = 0.14                # комиссия FunPay накручивается СВЕРХУ, её платит покупатель

# --------- Стоимость вывода денег (входит в себестоимость!) ---------
# Комиссия списывается рублями ДО конвертации, поэтому фактическая цена
# доллара всегда выше курса на сайте. При выводе 1000 ₽ доллар стоит ~102 ₽,
# при выводе 2500 ₽ и выше — ~92.5 ₽. Ниже 2500 ₽ выводить невыгодно.
WITHDRAW_FEE_PCT = 0.06              # комиссия FunPay за вывод
WITHDRAW_MIN_RUB = 150.0             # ...но не менее этой суммы за операцию
WITHDRAW_BATCH_RUB = 2500.0          # на какой партии считаем себестоимость
PAYMENT_AUTO_ROUTE = False           # на тесте платим только USDT: весь капитал в USDT.
# ВНИМАНИЕ: при курсах TON 122 ₽ / USDT 86.9 ₽ товар почти равноценен,
# а газ жетона в 5 раз дороже, поэтому TON выгоднее почти всегда.
# Мы всё равно платим USDT ради независимости от курса TON (разница ~5 ₽/заказ).
# Вернуть автовыбор: PAYMENT_AUTO_ROUTE = True (тогда нужен запас и TON, и USDT).
# Хочешь всегда USDT: PAYMENT_AUTO_ROUTE = False и FRAGMENT_PAYMENT_METHOD = "usdt_ton".

# --------- Живая цена Fragment (вместо статичной константы) ---------
FRAGMENT_LIVE_COST = True            # брать реальную цену звезды прямо с Fragment
FRAGMENT_QUOTE_SECONDS = 30 * 60     # как долго держать живую цену в кэше
FRAGMENT_QUOTE_STARS = 100           # на сколько звёзд делать пробную заявку
FRAGMENT_PROBE_USERNAME = ""  # юзернейм для пробных заявок (оплата не идёт).
# Важно: должен быть реальный пользователь, а не канал/бот. На этом же юзернейме
# работает живой запрос себестоимости (fragment_quote_ton_per_star).
                                     # (заявка только создаётся, оплата НЕ отправляется)

# --------- Склад в USDT ---------
USDT_JETTON_MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
USDT_BALANCE_CACHE_SECONDS = 120     # как долго держать баланс USDT в кэше

# --------- Доработки ---------
AUTO_RAISE_LOTS = True              # автоподнятие лотов FunPay (как ручное «Поднять», но само)
AUTO_RAISE_CHECK_SECONDS = 15 * 60  # как часто пробовать поднимать лоты
AUTO_CONFIRM_MAX_STARS = 0          # заказы до N⭐ оплачиваются БЕЗ кнопки подтверждения
                                    # (0 = выключено; работает только при AUTO_PAY_FRAGMENT = True)
LOW_BALANCE_TON = 0.3               # TON теперь ТОЛЬКО газ: 0.3 TON = ~6 заказов
REVIEW_REPLY_ENABLED = True         # автоответ-благодарность на отзыв после подтверждения заказа
REVIEW_REPLY_TEXT = "Спасибо за покупку! ⭐ Обращайтесь ещё!"
WATCHDOG_NOTIFY_COOLDOWN = 10 * 60  # «мониторинг падает» — не чаще раза в 10 минут
STATE_FILE = "bot_state.json"       # активные сделки (переживают перезапуск)
STATS_FILE = "bot_stats.json"       # история продаж для /stats
BLACKLIST_FILE = "blacklist.json"   # чёрный список покупателей
STARS_MARKET_URL = "https://funpay.com/lots/2418/"  # раздел Telegram Stars на FunPay
# пакеты звёзд, по которым группируется отчёт /stars_stats
STARS_PACKAGES = [50, 75, 85, 100, 150, 200, 250, 300, 350, 500, 1000, 2500]
STARS_TOP_LOTS = 4       # сколько ПЕРВЫХ лотов со страницы брать в расчёт по каждому пакету
STARS_SANE_MIN = 0.9     # адекватная цена за 1 звезду, руб.: ниже — скам или ошибка распознавания
STARS_SANE_MAX = 3.0     # адекватная цена за 1 звезду, руб.: выше — оверпрайс или ошибка, лот отбрасывается
# Ходить в интернет НАПРЯМУЮ, минуя системный прокси Windows / VPN-прокси.
# Лечит ошибку "ProxyError: Cannot connect to proxy" — когда в Windows прописан
# прокси (обычно остаток от VPN-программы), который не отвечает.
NO_PROXY = {"http": None, "https": None}
# Сколько секунд после запуска/переподключения игнорировать входящие сообщения FunPay:
# первая пачка событий от FunPay — это ИСТОРИЯ чатов (старые сообщения),
# отвечать на них нельзя — иначе бот разгребает всё, что написали до его включения.
RUNNER_STARTUP_GRACE = 10
# =============================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ %(levelname)s ] %(message)s",
    handlers=[
        logging.FileHandler("bot_debug.txt", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logging.getLogger("FunPayAPI.runner").setLevel(logging.DEBUG)
logging.getLogger("FunPayAPI.account").setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)


# ================= МОНИТОРИНГ ЦЕН КОНКУРЕНТОВ =================


def _parse_money(text) -> float | None:
    """Число из строки вида "1 250,50 ₽" / "1.25". None, если числа нет."""
    if text is None:
        return None
    cleaned = str(text).replace("\xa0", " ").replace("\u2009", " ")
    m = re.search(r"(\d+(?:[ ]\d{3})*(?:[.,]\d+)?)", cleaned)
    if not m:
        return None
    try:
        return float(m.group(1).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _parse_int(text) -> int | None:
    if text is None:
        return None
    cleaned = str(text).replace("\xa0", " ")
    m = re.search(r"(\d[\d ]*)", cleaned)
    if not m:
        return None
    try:
        return int(m.group(1).replace(" ", ""))
    except ValueError:
        return None


def _desc_pack_qty(desc: str) -> int | None:
    """Количество звёзд в лоте, если оно упомянуто РОВНО один раз.

    Если продавец выписал в описании целый прайс («50⭐ 100⭐ 500⭐…»), понять,
    к какому пакету относится цена, невозможно — именно из-за таких лотов
    раньше получались «100 звёзд за 1500 рублей».
    """
    desc_norm = str(desc).lower().replace("\xa0", " ")
    qtys = set()
    for qraw in re.findall(r"(\d[\d ]*)\s*(?:шт\.?\s*)?(?:зв[её]зд\w*|stars?|star|⭐|★)", desc_norm):
        val = _parse_int(qraw)
        if val and 1 <= val <= 1_000_000:
            qtys.add(val)
    if len(qtys) == 1:
        return qtys.pop()
    return None


def _desc_min_qty(desc: str) -> int | None:
    """Минимальный заказ из описания: «от 50 шт», «мин. 100⭐»."""
    desc_norm = str(desc).lower().replace("\xa0", " ")
    m = re.search(r"(?:от|мин\.?|минимум|min\.?)\s*(\d[\d ]*)", desc_norm)
    return _parse_int(m.group(1)) if m else None


def _lot_filter_attrs(item) -> dict:
    """Служебные атрибуты data-f-* — ими FunPay помечает значения фильтров лота.

    Именно тут живёт «способ выдачи» (по юзернейму / подарком / по ссылке),
    причём числовым id, который у каждой категории свой — поэтому мы его не зашиваем,
    а вычисляем по странице (см. _learn_method_map).
    """
    out = {}
    for key, value in (item.attrs or {}).items():
        if not key.startswith("data-f-"):
            continue
        if isinstance(value, (list, tuple)):
            value = " ".join(str(v) for v in value)
        out[key] = str(value).strip()
    return out


def _method_by_words(text: str) -> str:
    """Классификация способа выдачи по тексту лота: username / gift / link / unknown."""
    t = (text or "").lower().replace("\xa0", " ")

    gift = any(w in t for w in ("подарком", "подарок", "подарки", "gift", "\U0001f381"))
    username = any(w in t for w in (
        "юзернейм", "юзернейму", "юзернейму/", "username", "по нику", "по никнейму",
        "никнейм", "по логину", "login", "@username", "на юзер", "через юзер",
    ))
    link = any(w in t for w in ("по ссылке", "ссылкой", "invite", "вход в аккаунт", "входом"))

    if username and not gift:
        return "username"
    if gift and not username:
        return "gift"
    if link and not username and not gift:
        return "link"
    return "unknown"


def _learn_method_map(rows: list) -> dict:
    """Учимся на тех лотах, где способ выдачи понятен по тексту.

    Сопоставляем пары (атрибут фильтра, значение) со способом выдачи и потом по этой
    карте опознаём лоты, в описании которых способ не написан словами.
    """
    votes: dict = {}
    for row in rows:
        if row["method_words"] in ("username", "gift", "link"):
            for pair in row["attrs"].items():
                bucket = votes.setdefault(pair, {"username": 0, "gift": 0, "link": 0})
                bucket[row["method_words"]] += 1

    mapping = {}
    for pair, bucket in votes.items():
        total = sum(bucket.values())
        if total < 3:
            continue
        best_class = max(bucket, key=lambda k: bucket[k])
        if bucket[best_class] / total >= 0.8:
            mapping[pair] = best_class
    return mapping


def collect_stars_offers() -> tuple[list[dict], dict]:
    """Скачивает раздел Telegram Stars на FunPay и разбирает лоты конкурентов.

    Два ключевых момента:
    1. Цена берётся из атрибута data-s блока .tc-price (чистая цена ЗА ЕДИНИЦУ),
       а не из текста вместе с подписью «за шт.».
    2. Лоты разделяются по СПОСОБУ ВЫДАЧИ. Выдача подарком стоит заметно дешевле,
       и если смешать её с выдачей по юзернейму, средняя цена рынка занижается.
       Поэтому по умолчанию (STARS_METHOD_FILTER) берём только выдачу по юзернейму.

    Возвращает (список лотов в порядке страницы, статистика отсева).
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:
        raise RuntimeError("Нужен пакет beautifulsoup4: pip install beautifulsoup4") from e

    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "accept-language": "ru-RU,ru;q=0.9",
    }
    resp = requests.get(STARS_MARKET_URL, headers=headers, timeout=30, proxies=NO_PROXY)
    resp.raise_for_status()
    try:
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception:
        soup = BeautifulSoup(resp.text, "html.parser")

    stats = {
        "total": 0, "premium": 0, "no_price": 0, "insane": 0,
        "gift": 0, "link": 0, "unknown": 0, "username": 0, "filtered_out": 0,
        "method_filter": STARS_METHOD_FILTER,
    }

    # ---- первый проход: собираем сырые данные и черновую классификацию ----
    rows = []
    for item in soup.select("a.tc-item"):
        stats["total"] += 1
        desc_el = item.select_one(".tc-desc-text")
        price_el = item.select_one(".tc-price")
        seller_el = item.select_one(".media-user-name")
        amount_el = item.select_one(".tc-amount")
        desc = desc_el.get_text(" ", strip=True) if desc_el else ""
        title = item.get_text(" ", strip=True)
        seller = seller_el.get_text(" ", strip=True) if seller_el else "?"
        attrs = _lot_filter_attrs(item)

        rows.append({
            "desc": desc, "title": title, "seller": seller, "attrs": attrs,
            "price_el": price_el, "amount_el": amount_el,
            "method_words": _method_by_words(title + " " + desc),
        })

    method_map = _learn_method_map(rows)

    # ---- второй проход: цены + финальный способ выдачи ----
    offers: list[dict] = []
    for row in rows:
        desc, title = row["desc"], row["title"]
        blob = (title + " " + desc).lower()

        if "премиум" in blob or "premium" in blob:
            stats["premium"] += 1
            continue

        method = row["method_words"]
        if method == "unknown":
            guesses = {method_map[p] for p in row["attrs"].items() if p in method_map}
            if len(guesses) == 1:
                method = guesses.pop()
        stats[method] = stats.get(method, 0) + 1

        if STARS_METHOD_FILTER != "all" and method != STARS_METHOD_FILTER:
            # если способ так и не опознан — решает STARS_METHOD_KEEP_UNKNOWN
            if not (method == "unknown" and STARS_METHOD_KEEP_UNKNOWN):
                stats["filtered_out"] += 1
                continue

        price_el, amount_el = row["price_el"], row["amount_el"]

        # ---- цена за единицу товара ----
        unit_text = ""
        price_per_unit = None
        if price_el is not None:
            price_per_unit = _parse_money(price_el.get("data-s"))
            unit_el = price_el.select_one(".unit-desc") or price_el.select_one(".unit-item")
            if unit_el is not None:
                unit_text = unit_el.get_text(" ", strip=True)
            if price_per_unit is None:
                visible = price_el.get_text(" ", strip=True)
                if unit_text:
                    visible = visible.replace(unit_text, " ")
                price_per_unit = _parse_money(visible)
        if price_per_unit is None or price_per_unit <= 0:
            stats["no_price"] += 1
            continue

        unit_qty = 1
        mu = re.search(r"за\s+(\d[\d ]*)", unit_text.lower().replace("\xa0", " "))
        if mu:
            unit_qty = _parse_int(mu.group(1)) or 1

        pack_qty = _desc_pack_qty(desc)
        min_qty = _desc_min_qty(desc)
        stock = _parse_int(amount_el.get_text(" ", strip=True)) if amount_el is not None else None

        base = price_per_unit / unit_qty
        per_star = None
        kind = None
        if STARS_SANE_MIN <= base <= STARS_SANE_MAX:
            per_star = base
            kind = "per_star"
        elif pack_qty and STARS_SANE_MIN <= base / pack_qty <= STARS_SANE_MAX:
            per_star = base / pack_qty
            kind = "pack"
            if stock is not None:
                stock = stock * pack_qty
            min_qty = pack_qty
        else:
            stats["insane"] += 1
            continue

        offers.append({
            "seller": row["seller"],
            "desc": desc,
            "method": method,
            "per_star": per_star,
            "kind": kind,
            "pack_qty": pack_qty,
            "min_qty": min_qty,
            "stock": stock,
        })

    return offers, stats


def _fits_pack(offer: dict, pack: int) -> bool:
    """Можно ли у этого продавца реально купить ровно pack звёзд."""
    if offer["kind"] == "pack":
        return offer["pack_qty"] == pack
    min_qty = offer["min_qty"]
    if min_qty and min_qty > pack:
        return False
    stock = offer["stock"]
    if stock is not None and stock < pack:
        return False
    return True


_market_cache: dict = {"ts": 0.0, "offers": None, "stats": None}


def get_market_offers(max_age: float = 600.0) -> tuple[list[dict], dict]:
    """Кэшированный рынок — чтобы автоцены не дёргали FunPay каждую минуту."""
    now = time.time()
    if _market_cache["offers"] is not None and now - float(_market_cache["ts"]) < max_age:
        return _market_cache["offers"], _market_cache["stats"]
    offers, stats = collect_stars_offers()
    _market_cache.update({"ts": now, "offers": offers, "stats": stats})
    return offers, stats


def market_best_per_star(pack: int, offers: list | None = None) -> float | None:
    """Самая низкая цена за 1⭐ среди конкурентов, у кого реально можно купить pack."""
    if offers is None:
        try:
            offers, _ = get_market_offers()
        except Exception:
            logger.warning("Автоцены: не удалось получить цены конкурентов", exc_info=True)
            return None
    group = [o for o in offers if _fits_pack(o, pack)]
    if not group:
        return None
    return min(o["per_star"] for o in group)


def stars_method_debug() -> str:
    """Отчёт для проверки: как бот разложил лоты по способам выдачи."""
    saved = globals().get("STARS_METHOD_FILTER")
    globals()["STARS_METHOD_FILTER"] = "all"
    try:
        offers, stats = collect_stars_offers()
    finally:
        globals()["STARS_METHOD_FILTER"] = saved

    lines = ["РАЗБОР ЛОТОВ ПО СПОСОБУ ВЫДАЧИ", f"Всего на странице: {stats['total']}", ""]
    for method, title in (("username", "ПО ЮЗЕРНЕЙМУ"), ("gift", "����ОДАРКОМ"),
                          ("link", "ПО ССЫЛКЕ / С ВХОДОМ"), ("unknown", "НЕ ОПОЗНАНО")):
        group = [o for o in offers if o["method"] == method]
        if not group:
            lines.append(f"--- {title}: 0 лотов")
            lines.append("")
            continue
        prices = sorted(o["per_star"] for o in group)
        lines.append(f"--- {title}: {len(group)} лотов, от {prices[0]:.3f} до {prices[-1]:.3f} ₽/⭐, "
                     f"медиана {prices[len(prices) // 2]:.3f} ₽/⭐")
        for o in group[:5]:
            lines.append(f"    {o['per_star']:.3f} ₽/⭐ | {o['seller']} | {o['desc'][:70]}")
        lines.append("")
    lines.append(f"Сейчас в расчёт берётся: {saved} "
                 f"(меняется в настройке STARS_METHOD_FILTER: username / gift / link / all)")
    return "\n".join(lines)


def fetch_stars_market() -> str:
    """Готовый текстовый отчёт по ценам конкурентов. Вызывать в executor'е (блокирующая)."""
    offers, stats = collect_stars_offers()

    method_names = {"username": "по юзернейму", "gift": "подарком",
                    "link": "по ссылке/с входом", "all": "все способы"}

    lines = []
    lines.append("ОТЧЁТ ПО ЦЕНАМ НА ЗВЁЗДЫ (FunPay)")
    lines.append(f"Время: {time.strftime('%d.%m.%Y %H:%M:%S')}")
    lines.append(f"Раздел: {STARS_MARKET_URL}")
    lines.append(f"Способ выдачи в расчёте: {method_names.get(STARS_METHOD_FILTER, STARS_METHOD_FILTER)}")
    lines.append(f"Лотов на странице: {stats['total']}, в расчёт попало: {len(offers)}")
    lines.append(
        "Раскладка по способам: по юзернейму — {u}, подарком — {g}, по ссылке — {l}, "
        "не опознано — {unk}".format(
            u=stats.get("username", 0), g=stats.get("gift", 0),
            l=stats.get("link", 0), unk=stats.get("unknown", 0),
        )
    )
    lines.append(
        "Отсеяно: другой способ выдачи — {out}, премиум — {premium}, без цены — {no_price}, "
        "цена вне диапазона {mn}–{mx} ₽/⭐ — {insane}".format(
            out=stats.get("filtered_out", 0), premium=stats["premium"], no_price=stats["no_price"],
            insane=stats["insane"], mn=STARS_SANE_MIN, mx=STARS_SANE_MAX,
        )
    )
    lines.append("=" * 62)
    lines.append("")

    if not offers:
        lines.append("Ни одного подходящего лота не нашлось. Если так каждый раз — проверь /stars_debug: "
                     "возможно, FunPay поменял вёрстку или все лоты ушли в 'не опознано'.")
        return "\n".join(lines)

    cheapest = sorted(offers, key=lambda o: o["per_star"])[:10]
    lines.append("\U0001f525 САМЫЕ ДЕШЁВЫЕ ПРЕДЛОЖЕНИЯ (цена за 1⭐)")
    for i, o in enumerate(cheapest, 1):
        stock_text = f", в наличии {o['stock']}" if o["stock"] is not None else ""
        lines.append(f"  {i:>2}. {o['per_star']:.3f} ₽/⭐ — {o['seller']}{stock_text}")
    lines.append("")
    lines.append("=" * 62)
    lines.append("")

    rate = get_ton_rate()
    for pack in STARS_PACKAGES:
        group = [o for o in offers if _fits_pack(o, pack)]
        top = group[:STARS_TOP_LOTS]
        lines.append(f"\U0001f4e6 {pack} звёзд — подходящих лотов: {len(group)}, в расчёте: {len(top)}")
        if not top:
            lines.append("   Ни один продавец со страницы не продаёт такой объём.")
            lines.append("")
            continue
        for i, o in enumerate(top, 1):
            lines.append(
                f"   {i}. {o['per_star'] * pack:>9.2f} ₽  ({o['per_star']:.3f} ₽/⭐)  — {o['seller']}"
            )
        best = min(top, key=lambda o: o["per_star"])
        avg_per_star = sum(o["per_star"] for o in top) / len(top)
        lines.append(
            f"   ↓ Минимум: {best['per_star'] * pack:.2f} ₽ ({best['per_star']:.3f} ₽/⭐) — {best['seller']}"
        )
        lines.append(
            f"   ≈ Средняя по первым {len(top)}: {avg_per_star * pack:.2f} ₽ ({avg_per_star:.3f} ₽/⭐)"
        )
        if rate:
            cost = star_cost_rub(pack, rate)
            lines.append(
                f"   \U0001f4b0 Твоя себестоимость: {cost:.2f} ₽ — маржа по минимальной цене рынка: "
                f"{(best['per_star'] * pack - cost) / (best['per_star'] * pack) * 100:.1f}%"
            )
        lines.append("")

    all_per_star = sorted(o["per_star"] for o in offers)
    mid = all_per_star[len(all_per_star) // 2]
    lines.append("=" * 62)
    lines.append("ИТОГО ПО РЫНКУ (за 1⭐)")
    lines.append(f"  Минимум: {all_per_star[0]:.3f} ₽")
    lines.append(f"  Медиана: {mid:.3f} ₽")
    lines.append(f"  Средняя: {sum(all_per_star) / len(all_per_star):.3f} ₽")
    lines.append(f"  Максимум: {all_per_star[-1]:.3f} ₽")
    if rate:
        lines.append(f"  Курс TON: {rate:.2f} ₽ → себестоимость {star_cost_rub(1000, rate) / 1000:.4f} ₽/⭐")
    return "\n".join(lines)


def stars_market_short() -> str:
    """Короткая сводка для сообщения в Telegram (полный отчёт идёт файлом)."""
    offers, _stats = collect_stars_offers()
    if not offers:
        return "Ни одного лота с адекватной ценой не распознал."
    rows = []
    for pack in (100, 500, 1000):
        group = [o for o in offers if _fits_pack(o, pack)][:STARS_TOP_LOTS]
        if not group:
            continue
        best = min(group, key=lambda o: o["per_star"])
        rows.append(f"{pack}⭐ — от {best['per_star'] * pack:.2f} ₽ ({best['per_star']:.3f} ₽/⭐)")
    return "\n".join(rows)


# ================= БАЛАНС FUNPAY =================

def format_fp_balance(balance) -> str:
    """Аккуратно форматируем баланс FunPay (разные версии — разные типы/поля)."""
    if balance is None:
        return "—"
    if isinstance(balance, bool):
        return "—"
    if isinstance(balance, (int, float)):
        return f"{float(balance):.2f} ₽"
    if isinstance(balance, str):
        return balance.strip() or "—"
    parts = []
    for attrs, sign in (
        (("total_rub", "rub", "total_rubles"), "₽"),
        (("total_usd", "usd", "total_dollars"), "$"),
        (("total_eur", "eur", "total_euro"), "€"),
    ):
        for attr in attrs:
            value = getattr(balance, attr, None)
            if value not in (None, ""):
                try:
                    parts.append(f"{float(value):.2f} {sign}")
                except (TypeError, ValueError):
                    pass
                break
    available = getattr(balance, "available_rub", None)
    if available not in (None, "") and parts:
        try:
            parts[0] += f" (доступно {float(available):.2f} ₽)"
        except (TypeError, ValueError):
            pass
    return ", ".join(parts) if parts else str(balance)


# ================= БАЛАНС FUNPAY =================
# В разных версиях FunPayAPI get_balance() ведёт себя по-разному: где-то его нет вовсе,
# где-то он требует id СВОЕГО лота (и с чужим id по-умолчанию падает), где-то возвращает
# объект с другими полями. Поэтому баланс берём с нескольких попыток и, если библиотека
# не справляется, парсим страницу FunPay своими руками через ту же сессию.
_MONEY_RE = re.compile(r"(\d[\d\s\u00a0]*(?:[.,]\d+)?)\s*(₽|\$|€)")
_fp_balance_error = {"text": ""}


def _own_lot_ids(limit: int = 3) -> list[int]:
    """id своих лотов — нужны версиям get_balance(lot_id)."""
    acc = holder.account
    if acc is None:
        return []
    try:
        lots = list(acc.get_user(acc.id).get_lots() or [])
    except Exception:
        logger.warning("Баланс: не удалось получить свои лоты для get_balance(lot_id)", exc_info=True)
        return []
    ids = []
    for lot in lots:
        for attr in ("id", "lot_id", "offer_id"):
            value = getattr(lot, attr, None)
            try:
                if value is not None:
                    ids.append(int(value))
                    break
            except (TypeError, ValueError):
                continue
        if len(ids) >= limit:
            break
    return ids


def _funpay_page(path: str) -> str | None:
    """Получаем HTML страницы FunPay текущей сессией (с куками и golden_seal)."""
    acc = holder.account
    if acc is None:
        return None
    try:
        response = acc.method("get", path, {}, {}, raise_not_200=True)
        return response.text
    except Exception:
        logger.warning("Баланс: страница %s не открылась", path or "/", exc_info=True)
        return None


def _money_pairs(text: str) -> list[tuple[float, str]]:
    out = []
    for m in _MONEY_RE.finditer(text or ""):
        raw = m.group(1).replace("\u00a0", "").replace(" ", "").replace(",", ".")
        try:
            out.append((float(raw), m.group(2)))
        except ValueError:
            continue
    return out


def _parse_balance_html(html: str) -> str | None:
    """Ищем баланс в разметке FunPay: сначала блоки баланса, потом бейдж в шапке."""
    if not html:
        return None
    soup = None
    try:
        from bs4 import BeautifulSoup
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")
    except ImportError:
        soup = None

    chunks: list[str] = []
    if soup is not None:
        for selector in (
            ".balances-value", ".balance-value", ".balances .param-item",
            ".balances-list", ".badge-balance", ".menu-item-balance",
        ):
            for el in soup.select(selector):
                chunk = el.get_text(" ", strip=True)
                if chunk:
                    chunks.append(chunk)
    for chunk in chunks:
        pairs = _money_pairs(chunk)
        if pairs:
            best: dict[str, float] = {}
            for value, sign in pairs:
                best[sign] = max(best.get(sign, value), value) if sign in best else value
            order = ["₽", "$", "€"]
            return ", ".join(f"{best[s]:.2f} {s}" for s in order if s in best)
    return None


def fetch_fp_balance() -> tuple[str | None, str]:
    """Возвращает (текст баланса, причина неудачи)."""
    acc = holder.account
    if acc is None:
        return None, "сессия FunPay ещё поднимается"

    errors: list[str] = []

    # 1) Штатный метод библиотеки без аргументов.
    get_balance = getattr(acc, "get_balance", None)
    if get_balance is not None:
        try:
            result = format_fp_balance(get_balance())
            if result and result != "—":
                return result, ""
            errors.append("get_balance() вернул пустоту")
        except Exception as e:
            errors.append(f"get_balance(): {type(e).__name__}: {e}")

        # 2) Версии, где нужно передать id СВОЕГО лота (по-умолчанию там чужой id — и ошибка).
        for lot_id in _own_lot_ids():
            try:
                result = format_fp_balance(get_balance(lot_id))
                if result and result != "—":
                    return result, ""
            except Exception as e:
                errors.append(f"get_balance({lot_id}): {type(e).__name__}: {e}")
    else:
        errors.append("в этой версии FunPayAPI нет get_balance()")

    # 3) Парсим сами: страница баланса, потом ��юбая страница (бейдж в шапке).
    for path in ("account/balance", ""):
        parsed = _parse_balance_html(_funpay_page(path) or "")
        if parsed:
            return parsed, ""
    errors.append("свой парсер тоже не нашёл сумму на странице баланса"
                  " (возможно, сессия golden_key просрочена)")

    reason = "; ".join(errors)
    _fp_balance_error["text"] = reason
    logger.warning("Баланс FunPay не получен: %s", reason)
    return None, reason


# ================= АВТОВОЗВРАТ =================

def funpay_refund(order_id: str) -> tuple[bool, str]:
    """Возврат денег по заказу через FunPay. Возвращает (успех, текст ошибки)."""
    acc = holder.account
    if acc is None:
        return False, "сессия FunPay ещё не поднята"
    refund = getattr(acc, "refund", None)
    if refund is None:
        return False, "в этой версии FunPayAPI нет метода refund()"
    try:
        refund(order_id)
        return True, ""
    except Exception as e:
        logger.error("Не удалось оформить возврат по з��казу %s", order_id, exc_info=True)
        return False, f"{type(e).__name__}: {e}"


def find_paid_order_by_buyer(nickname: str):
    """Ищет оплаченный заказ по нику покупателя (если сделка потерялась)."""
    acc = holder.account
    if acc is None or not nickname:
        return None
    get_sells = getattr(acc, "get_sells", None)
    if get_sells is None:
        return None
    try:
        result = get_sells()
        sells = result[1] if isinstance(result, tuple) else result
    except Exception:
        logger.warning("Возврат: не удалось получить список продаж", exc_info=True)
        return None
    for o in sells or []:
        if getattr(o, "status", None) != OrderStatuses.PAID:
            continue
        buyer = str(getattr(o, "buyer_username", "") or "").strip().lower()
        if buyer and buyer == str(nickname).strip().lower():
            return o
    return None


# ================= ТЕКСТЫ =================
MSG_GREETING = (
    "🤖 Я на месте! Оплачивайте товар — после оплаты бот пришлёт подробную инструкцию."
)
MSG_ASK_USERNAME = (
    "✅ Заказ #{order_id} принят!\n"
    "Привет! Укажи свой юзернейм в Telegram, на который придут звёзды.\n"
    "При��ер: @username"
)
MSG_CONFIRM_USERNAME = (
    "⚠️ ВНИМАТЕЛЬНО проверьте юзернейм: @{username}\n\n"
    "Если в юзернейме есть ошибка — звёзды придут другому пользователю. "
    "Мы НЕ делаем возврат, если вы ошиблись при вводе юзернейма и звёзды пришли не вам.\n\n"
    "Есл�� уверены, что юзернейм ваш — напишите: ДА\n"
    "Если хотите изменить юзернейм — напишите: НЕТ"
)
MSG_ASK_USERNAME_AGAIN = "Хорошо! Введите юзернейм ещё раз.\nПример: @username"
MSG_CONFIRM_USERNAME_FROM_ORDER = (
    "✅ Заказ #{order_id} приня��!\n"
    "При оплате вы указали юзернейм: @{username}\n\n"
    "⚠️ ВНИМАТЕЛЬНО проверьте его! Если в юзернейме ошибка — звёзды придут другому "
    "пользователю. Мы НЕ делаем возврат, если юзернейм был указан неверно.\n\n"
    "Если юзернейм ваш — напишите: ДА\n"
    "Если нужен другой — напишите: НЕТ"
)
MSG_BAD_USERNAME = (
    "❌ Юзернейм выглядит некорректно. Он должен начинаться с буквы, состоять из "
    "латинских букв, цифр и _, длина 5-32 символа.\nПример: @username"
)
MSG_CONFIRM_HINT = "Напишите, пожалуйста, только ДА или НЕТ 🙂"
MSG_WAIT_PAYMENT = "⏳ Принято! Заявка передана на выдачу звёзд, обычно это занимает несколько минут."
MSG_IN_PROGRESS = "⏳ Ваш заказ в обработке, ожидайте — бот напишет, как только звёзды будут отправлены."
MSG_PAID = "✅ Ваш заказ оплачен! Проверьте свои звёзды и подтвердите заказ на FunPay 🙌"
MSG_THANKS = "❤️ Спасибо за покупку! Напишите, пожалуйста, отзыв 🙏"
MSG_MANUAL = "⏳ Ваш заказ передан продавцу, он выдаст звёзды вручную в ближайшее время."
MSG_REFUND_NO_ORDER = (
    "❗ Вы не оплатили заказ для возврата! Оплатите заказ, после чего бот даст вам "
    "подробные инструкции для выполнения. Если заказ всё же оплачен — напишите !помогите."
)
MSG_REFUND_DONE = (
    "↩️ Готово! Деньги за заказ возвращены на ваш баланс FunPay. "
    "Спасибо, что зашли, будем рады видеть вас снова 🙌"
)
MSG_REFUND_LATER = (
    "⏳ Заявка на возврат принята. По этому заказу выдача уже могла начаться, "
    "поэтому продавец проверит её и оформит возврат вручную — обычно это быстро."
)
MSG_REFUND_FAIL = (
    "⚠️ Не получилось оформить возврат автоматически. Продавец уже уведомлён "
    "и вернёт деньги вручную в ближайшее время."
)
# ==========================================


# ================= СОСТОЯНИЯ СДЕЛКИ =================
WAIT_USERNAME = "WAIT_USERNAME"    # ждём юзернейм от покупателя
WAIT_CONFIRM = "WAIT_CONFIRM"      # ждём ДА/НЕТ от покупателя
WAIT_ADMIN = "WAIT_ADMIN"          # ждём кнопку от админа в ТГ
PAYING = "PAYING"                  # идёт автооплата на Fragment
WAIT_MANUAL = "WAIT_MANUAL"        # админ оплачивает вручную
PAID = "PAID"                      # звёзды отправлены, ждём подтверждения заказа
FROZEN = "FROZEN"                  # что-то пошло не так — только ручное вмешательство


@dataclass
class Deal:
    order_id: str
    chat_id: int
    buyer: str
    stars: int | None            # количество звёзд, СПАРШЕННОЕ ИЗ ЗАКАЗА, не из чата!
    price: str
    description: str
    state: str = WAIT_USERNAME
    username: str | None = None
    recipient_name: str | None = None
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    product: str = "stars"       # "stars" | "premium"
    months: int | None = None    # срок Premium (3/6/12 мес), только для product == "premium"
    confirm_attempts: int = 0    # сколько раз не поняли ДА/НЕТ
    pay_stage: str = "none"      # где именно находится оплата: none/recipient/tx_built/sending/sent
    pay_req_id: str | None = None   # id заявки Fragment — чтобы не создавать вторую вслепую
    pay_tx_hash: str | None = None  # hash отправленной TON-транзакции
    refund_done: bool = False       # деньги уже возвращены


# Активные сделки: chat_id -> Deal.
# ВАЖНО (анти-фрод): сделка создаётся ТОЛЬКО по событию NEW_ORDER от сервера FunPay.
# Никакие сообщения в чате («я оплатил», поддельные системные уведомления и т.п.)
# создать сделку или изменить количество звёзд НЕ могут.
deals: dict[int, Deal] = {}
deals_lock = threading.RLock()
_order_resolve_attempts: dict[str, int] = {}  # заказ -> сколько раз не смогли открыть чат

processed_orders: set[str] = set()
notified_returns: set[str] = set()
notified_help: set[str] = set()
last_greeting: dict[int, float] = {}
last_in_progress: dict[int, float] = {}
_fp_last_sent: dict[int, tuple[str, float]] = {}
_last_admin_notify: dict[str, float] = {}
processed_messages: set[str] = set()  # id уже обработанных сообщений — защита от дублей событий


# ================= СОХРАНЕНИЕ СОСТОЯНИЯ / СТАТИСТИКА / ЧС =================

def _data_path(name: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def save_state():
    """Сохраняем активные сделки на диск — переживают перезапуск бота."""
    try:
        with deals_lock:
            data = {
                "deals": [asdict(d) for d in deals.values()],
                "processed_orders": list(processed_orders)[-2000:],
            }
        tmp = _data_path(STATE_FILE + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _data_path(STATE_FILE))
    except Exception:
        logger.error("Не удалось сохранить состояние сделок", exc_info=True)


def load_state():
    """Восстанавливаем сделки после перезапуска."""
    try:
        with open(_data_path(STATE_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception:
        logger.error("Не удалось прочитать сохранённое состояние", exc_info=True)
        return
    restored = 0
    with deals_lock:
        processed_orders.update(str(x) for x in data.get("processed_orders", []))
        for d in data.get("deals", []):
            try:
                deal = Deal(
                    order_id=str(d["order_id"]), chat_id=int(d["chat_id"]), buyer=str(d.get("buyer", "")),
                    stars=d.get("stars"), price=str(d.get("price", "")), description=str(d.get("description", "")),
                    state=str(d.get("state", WAIT_USERNAME)), username=d.get("username"),
                    recipient_name=d.get("recipient_name"), attempts=int(d.get("attempts", 0)),
                    created_at=float(d.get("created_at", time.time())),
                    product=str(d.get("product", "stars")), months=d.get("months"),
                    confirm_attempts=int(d.get("confirm_attempts", 0)),
                    pay_stage=str(d.get("pay_stage", "none")), pay_req_id=d.get("pay_req_id"),
                    pay_tx_hash=d.get("pay_tx_hash"), refund_done=bool(d.get("refund_done", False)),
                )
                if deal.state == PAYING:
                    # Если бот упал уже после отправки TON — не предлагаем обычную кнопку «ДА»,
                    # иначе можно оплатить заказ второй раз.
                    deal.state = FROZEN if deal.pay_stage in ("sending", "sent") else WAIT_ADMIN
                deals[deal.chat_id] = deal
                restored += 1
            except Exception:
                logger.error("Не удалось восстановить сделку: %s", d, exc_info=True)
    if restored:
        logger.info("♻️ Восстановлено активных сделок после перезапуска: %s", restored)


stats_lock = threading.Lock()


def load_stats() -> list:
    try:
        with open(_data_path(STATS_FILE), encoding="utf-8") as f:
            return list(json.load(f))
    except FileNotFoundError:
        return []
    except Exception:
        logger.error("Не удалось прочитать статистику", exc_info=True)
        return []


def record_sale(deal: Deal):
    """Пишем подтверждённую продажу в историю для /stats."""
    rec = {
        "ts": time.time(), "order_id": deal.order_id, "buyer": deal.buyer,
        "product": deal.product, "stars": deal.stars, "months": deal.months, "price": deal.price,
    }
    try:
        with stats_lock:
            sales = load_stats()
            sales.append(rec)
            with open(_data_path(STATS_FILE), "w", encoding="utf-8") as f:
                json.dump(sales, f, ensure_ascii=False, indent=1)
    except Exception:
        logger.error("Не удалось записать продажу в статистику", exc_info=True)


blacklist: set[str] = set()


def load_blacklist():
    global blacklist
    try:
        with open(_data_path(BLACKLIST_FILE), encoding="utf-8") as f:
            blacklist = {str(x).lower() for x in json.load(f)}
        if blacklist:
            logger.info("🚫 Чёрный список: %s ников", len(blacklist))
    except FileNotFoundError:
        pass
    except Exception:
        logger.error("Не удалось прочитать чёрный список", exc_info=True)


def save_blacklist():
    try:
        with open(_data_path(BLACKLIST_FILE), "w", encoding="utf-8") as f:
            json.dump(sorted(blacklist), f, ensure_ascii=False, indent=1)
    except Exception:
        logger.error("Не удалось сохранить чёрный список", exc_info=True)


class AccountHolder:
    """Текущий аккаунт FunPay (пересоздаётся при обновлении сессии)."""
    def __init__(self):
        self.account: Account | None = None


holder = AccountHolder()


def esc(value) -> str:
    """Безопасно вставляем произвольный текст (в т.ч. от покупателя) в HTML-разметку Telegram."""
    return html.escape(str(value if value is not None else ""))


_fp_sent_echo: dict[int, list] = {}  # последние отправленные ботом тексты — отсечение «эха»
_ECHO_TTL = 3600.0

# Юзернеймы, присланные в чат ДО того, как бот узнал о заказе (гонка событий
# FunPay: сообщение покупателя может прийти раньше события NEW_ORDER).
_pre_deal_usernames: dict = {}  # chat_id -> (ts, username)
_PRE_DEAL_TTL = 900.0  # секунд помним свои сообщения


def _norm_echo(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().lower()[:300]


def _remember_sent(chat_id: int, text: str) -> None:
    lst = _fp_sent_echo.setdefault(chat_id, [])
    lst.append((_norm_echo(text), time.time()))
    if len(lst) > 20:
        del lst[:-20]


def _is_own_echo(chat_id: int, text: str) -> bool:
    lst = _fp_sent_echo.get(chat_id)
    if not lst:
        return False
    norm = _norm_echo(text)
    now = time.time()
    return any(t == norm and now - ts < _ECHO_TTL for t, ts in lst)


def fp_send(chat_id: int, text: str, force: bool = False) -> bool:
    """Отправка со��бщения в чат FunPay (синхронно, из любого потока).

    force=True отключает антиспам — для важных сообщений (оплачено, возврат и т.п.),
    которые ни в коем случае нельзя «проглотить».
    Возвращает True, только если сообщение реально ушло покупателю.
    """
    now = time.time()
    prev = _fp_last_sent.get(chat_id)
    if not force and prev and prev[0] == text and now - prev[1] < FP_REPEAT_COOLDOWN:
        logger.info("Анти-спам: не повторяю одинаковое сообщение в чат %s", chat_id)
        return False
    try:
        acc = holder.account
        if acc is None:
            logger.error("Аккаунт FunPay ещё не готов, не могу написать в чат %s", chat_id)
            return False
        acc.send_message(chat_id, text)
        _remember_sent(chat_id, text)
        _fp_last_sent[chat_id] = (text, now)
        return True
    except Exception:
        logger.error("Не удалось отправить сообщение в чат FunPay %s", chat_id, exc_info=True)
        return False


# Очередь уведомлений админу: если Telegram временно недоступен (обрыв сети,
# блокировка api.telegram.org), сообщения НЕ теряются, а ждут в очереди
# и отправляются, как только связь восстановится.
admin_outbox: asyncio.Queue | None = None


def notify_admin_threadsafe(main_loop: asyncio.AbstractEventLoop, bot: Bot, text: str,
                             kb: InlineKeyboardMarkup | None = None):
    """Ставит уведомление админу в очередь (вызывается из потока Runner'а)."""
    if admin_outbox is None:
        logger.error("Очередь уведомлений ещё не создана, сообщение не отправлено: %s", text[:100])
        return
    # Анти-спам: одинаковые уведомления — не чаще раза в ADMIN_REPEAT_COOLDOWN.
    now = time.time()
    if now - _last_admin_notify.get(text, 0) < ADMIN_REPEAT_COOLDOWN:
        logger.info("Анти-спам: пропускаю повторное уведомление админу: %s", text[:60])
        return
    _last_admin_notify[text] = now
    main_loop.call_soon_threadsafe(admin_outbox.put_nowait, (text, kb))


async def admin_sender_worker(bot: Bot):
    """Фоновая задача: отправляет уведомления админу с повторами при сбоях сети."""
    delay = 5
    while True:
        text, kb = await admin_outbox.get()
        while True:
            try:
                await bot.send_message(ADMIN_ID, text, reply_markup=kb, parse_mode=ParseMode.HTML)
                delay = 5
                break
            except TelegramNetworkError:
                logger.warning(
                    "Telegram недоступен, повторю отправку через %s сек (в очереди ещё %s)",
                    delay, admin_outbox.qsize(),
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
            except Exception:
                logger.error("Не удал��с�� отправить сообщение админу (пропускаю его)", exc_info=True)
                break


# ================= WATCHDOG =================
_monitor_fail_state = {"failing": False, "last_notify": 0.0}


def _notify_monitor_failure(main_loop: asyncio.AbstractEventLoop, bot: Bot):
    now = time.time()
    first = not _monitor_fail_state["failing"]
    _monitor_fail_state["failing"] = True
    if first or now - _monitor_fail_state["last_notify"] > WATCHDOG_NOTIFY_COOLDOWN:
        _monitor_fail_state["last_notify"] = now
        notify_admin_threadsafe(
            main_loop, bot,
            "🚨 <b>Мониторинг FunPay падает с ошибкой</b> — перезапускаю каждые 20 сек.\n"
            "Подробности в bot_debug.txt (частые причины: слетел golden_key, нет сети до funpay.com).",
        )


def _notify_monitor_recovered(main_loop: asyncio.AbstractEventLoop, bot: Bot):
    if _monitor_fail_state["failing"]:
        _monitor_fail_state["failing"] = False
        notify_admin_threadsafe(main_loop, bot, "✅ Мониторинг FunPay восстановился.")


def chat_kb(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔗 ЧАТ", url=f"https://funpay.com/chat/?node={chat_id}")
    ]])


# ================= ПАРСИНГ КОЛИЧЕСТВА ЗВЁЗД =================
STAR_WORDS = r"(?:⭐|★|зв[её]зд\w*|stars?)"


def parse_stars_amount(order) -> int | None:
    """Определяем количество звёзд ТОЛЬКО из данных оплаченного заказа FunPay.

    Анти-фрод: количество звёзд никогда не берётся из сообщений покупателя.
    Если однозначно определить не удалось — возвращаем None, и заказ уходит
    админу на ручную обработку.
    """
    description = str(getattr(order, "description", "") or "")

    # 1) число рядом со словом «звёзд»/stars/⭐: «500 звёзд», «Stars 250», «⭐50»
    m = re.search(rf"(\d{{1,6}})\s*{STAR_WORDS}", description, re.IGNORECASE)
    if not m:
        m = re.search(rf"{STAR_WORDS}\s*[:\-–—]?\s*(\d{{1,6}})", description, re.IGNORECASE)
    base = int(m.group(1)) if m else None

    # 2) количество единиц лота (если лот «1 звезда», покупают amount штук)
    amount = getattr(order, "amount", None)
    try:
        amount = int(amount) if amount else None
    except (TypeError, ValueError):
        amount = None

    stars = None
    if base is not None and amount and amount > 1:
        stars = base * amount  # лот «50 звёзд» x 3 шт = 150
    elif base is not None:
        stars = base
    elif amount and amount > 1:
        stars = None  # только количество штук без числа в названии — неоднозначно

    if stars is not None and not (1 <= stars <= MAX_STARS_PER_ORDER):
        logger.warning("Подозрительное количество звёзд (%s) в заказе %s", stars, getattr(order, "id", "?"))
        return None
    return stars


PREMIUM_WORDS = r"(?:premium|премиум)"


def parse_product(order) -> tuple[str, int | None]:
    """Определяем товар из описания заказа: звёзды или Telegram Premium.

    Для Premium возвращаем срок в месяцах (Fragment продаёт только 3/6/12).
    Как и со звёздами — данные берём ТОЛЬКО из оплаченного заказа, не из чата.
    """
    description = str(getattr(order, "description", "") or "")
    if re.search(PREMIUM_WORDS, description, re.IGNORECASE):
        m = re.search(r"(\d{1,2})\s*(?:мес|month)", description, re.IGNORECASE)
        months = int(m.group(1)) if m else None
        if months not in (3, 6, 12):
            months = None
        return "premium", months
    return "stars", None


USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")


# невидимые символы, которые часто попадают в текст при копировании/вставке
INVISIBLE_CHARS_RE = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\u2060\ufeff]")


def extract_username(text: str) -> str | None:
    """Достаёт юзернейм TG из сообщения. Понимает @username, t.me/username,
    юзернейм внутри фразы и мусорные невидимые символы после копипаста."""
    text = INVISIBLE_CHARS_RE.sub("", str(text)).replace("\xa0", " ").strip()
    # ссылка t.me/username — где угодно в тексте
    m = re.search(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,31})\b", text, re.IGNORECASE)
    if not m:
        # сообщение целиком = один юзернейм (с @ или без)
        m = USERNAME_RE.match(text)
    if not m:
        # @username внутри более длинного текста («мой ник @vasya_123», лишние строки и т.п.)
        m = re.search(r"@([A-Za-z][A-Za-z0-9_]{3,31})\b", text)
    candidate = m.group(1) if m else None
    if candidate is None:
        return None
    if len(candidate) < 5:  # у Telegram минимум 5 символов
        return None
    return candidate


# названия полей заказа, в которых обычно лежит юзернейм
ORDER_USERNAME_KEY_HINTS = ("user", "юзер", "ник", "telegram", "телеграм", "тг", "tg")


def extract_username_from_order(order, acc) -> str | None:
    """Достаёт TG-юзернейм из данных самого заказа FunPay.

    При оплате лота FunPay просит покупателя заполнить поля (например
    «Telegram username») — они лежат в полном заказе (get_order). Если ничего
    не нашли — вернём None, и бот спросит юзернейм в чате, как раньше."""
    candidates: list[tuple[str, str]] = []  # (название поля, значение)

    full = None
    oid = getattr(order, "id", None)
    if acc is not None and oid:
        try:
            full = acc.get_order(oid)
        except Exception:
            logger.warning("Не смог загрузить полный заказ #%s для поиска юзернейма", oid, exc_info=True)

    for src in (full, order):
        if src is None:
            continue
        for attr in ("buyer_params", "lot_params", "parameters", "params", "fields"):
            d = getattr(src, attr, None)
            if isinstance(d, dict):
                for k, v in d.items():
                    if v:
                        candidates.append((str(k), str(v)))

    # 1) поля, в названии которых есть «юзернейм/телеграм», — самое надёжное
    for key, value in candidates:
        k = key.lower()
        if any(h in k for h in ORDER_USERNAME_KEY_HINTS):
            u = extract_username(value)
            if u:
                return u
    # 2) остальные поля — только если в значении явно виден @ или ссылка t.me
    for _key, value in candidates:
        if "@" in value or "t.me" in value.lower():
            u = extract_username(value)
            if u:
                return u
    # 3) запасной вариант: @username в тексте описания заказа
    for src in (full, order):
        if src is None:
            continue
        for attr in ("full_description", "description", "title"):
            text_val = getattr(src, attr, None)
            if text_val:
                m = re.search(r"@([A-Za-z][A-Za-z0-9_]{4,31})\b", str(text_val))
                if m:
                    return m.group(1)
    return None


# ================= FRAGMENT =================
class FragmentError(Exception):
    pass


FRAGMENT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:141.0) Gecko/20100101 Firefox/141.0"

# Актуальный api-hash. Стартуем с FRAGMENT_HASH из конфига,
# дальше бот обновляет его сам со страницы fragment.com при Access denied.
_fragment_hash: str = FRAGMENT_HASH


def _fragment_proxies() -> dict:
    return {"http": FRAGMENT_PROXY, "https": FRAGMENT_PROXY} if FRAGMENT_PROXY else NO_PROXY


def fragment_refresh_hash() -> tuple[str | None, bool]:
    """Тянем свежий api-hash со страницы покупки звёзд.

    Возвращает (hash или None, залогинены ли мы по мнению Fragment).
    Hash у Fragment привязан к сессии и периодически меняется,
    поэтому захардкоженный устаревает.
    """
    headers = {
        "Cookie": FRAGMENT_COOKIES,
        "User-Agent": FRAGMENT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3",
    }
    resp = requests.get(
        "https://fragment.com/stars/buy",
        headers=headers,
        timeout=30,
        proxies=_fragment_proxies(),
    )
    logged_in = "logout" in resp.text.lower()
    m = re.search(r"api\?hash=([a-fA-F0-9]+)", resp.text)
    new_hash = m.group(1) if m else None
    logger.debug("Fragment hash refresh: hash=%s, logged_in=%s", new_hash, logged_in)
    return new_hash, logged_in


def fragment_api(method: str, **data) -> dict:
    """Запрос к api fragment.com от имени залогиненного аккаунта.

    При Access denied один раз автоматически обновляет api-hash
    со страницы и повторяет запрос.
    """
    global _fragment_hash
    if not (_fragment_hash and FRAGMENT_COOKIES):
        raise FragmentError("Fragment не настроен (FRAGMENT_HASH / FRAGMENT_COOKIES)")
    headers = {
        "Cookie": FRAGMENT_COOKIES,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": FRAGMENT_UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3",
        "Origin": "https://fragment.com",
        "Referer": "https://fragment.com/stars/buy",
    }
    last_error = ""
    for attempt in (1, 2):
        resp = requests.post(
            "https://fragment.com/api?hash=" + _fragment_hash,
            data={"method": method, **data},
            headers=headers,
            timeout=30,
            proxies=_fragment_proxies(),
        )
        logger.debug("Fragment %s (попытка %s) -> %s %s", method, attempt, resp.status_code, resp.text[:500])
        try:
            j = resp.json()
        except ValueError:
            raise FragmentError(f"Fragment вернул не-JSON (HTTP {resp.status_code}) — возможно, слетели куки")
        # Успех: либо ok=true, либо просто нет поля error
        # (initBuyStarsRequest, например, возвращает {"req_id": ...} без "ok")
        if j.get("ok") or (isinstance(j, dict) and "error" not in j):
            return j
        last_error = str(j.get("error", j))
        if attempt == 1 and "denied" in last_error.lower():
            new_hash, logged_in = fragment_refresh_hash()
            if not logged_in:
                raise FragmentError(
                    f"Fragment ошибка в {method}: {last_error} — Fragment видит тебя РАЗЛОГИНЕННЫМ. "
                    "Куки протухли: зайди на fragment.com заново и обнови FRAGMENT_COOKIES"
                )
            if new_hash and new_hash != _fragment_hash:
                logger.info("Fragment: hash устарел, обновил %s -> %s, повторяю", _fragment_hash, new_hash)
                _fragment_hash = new_hash
                continue
        break
    raise FragmentError(f"Fragment ошибка в {method}: {last_error}")


def fragment_check_recipient(username: str, stars: int) -> tuple[str, str]:
    """Ищем получателя на Fragment. Возвращает (recipient_token, имя получателя)."""
    j = fragment_api("searchStarsRecipient", query=username, quantity=stars)
    found = j.get("found") or {}
    recipient = found.get("recipient")
    if not recipient:
        raise FragmentError(f"Получатель @{username} не найден на Fragment")
    return recipient, str(found.get("name") or "")


def fragment_build_transaction(recipient: str, stars: int,
                               method: str | None = None) -> tuple[dict, str]:
    """Создаём заявку на покупку и получаем (TON-транзакция, req_id)."""
    j = fragment_api("initBuyStarsRequest", recipient=recipient, quantity=stars,
                     payment_method=(method or FRAGMENT_PAYMENT_METHOD))
    req_id = j.get("req_id")
    if not req_id:
        raise FragmentError(f"initBuyStarsRequest не вернул req_id: {j}")
    j = fragment_api("getBuyStarsLink", transaction="1", id=req_id, show_sender="0")
    messages = (j.get("transaction") or {}).get("messages") or []
    if not messages:
        raise FragmentError(f"getBuyStarsLink не вернул транзакцию: {j}")
    return messages[0], str(req_id)  # {address, amount (нанотоны), payload (base64 boc)}


def fragment_check_premium_recipient(username: str, months: int) -> tuple[str, str]:
    """Ищем получателя подарка Telegram Premium на Fragment."""
    j = fragment_api("searchPremiumGiftRecipient", query=username, months=months)
    found = j.get("found") or {}
    recipient = found.get("recipient")
    if not recipient:
        raise FragmentError(f"Получатель @{username} не найден на Fragment")
    return recipient, str(found.get("name") or "")


def fragment_build_premium_transaction(recipient: str, months: int,
                                      method: str | None = None) -> tuple[dict, str]:
    """Создаём заявку на подарок Premium и получаем (TON-транзакция, req_id)."""
    j = fragment_api("initGiftPremiumRequest", recipient=recipient, months=months,
                     payment_method=(method or FRAGMENT_PAYMENT_METHOD))
    req_id = j.get("req_id")
    if not req_id:
        raise FragmentError(f"initGiftPremiumRequest не вернул req_id: {j}")
    j = fragment_api("getGiftPremiumLink", transaction="1", id=req_id, show_sender="0")
    messages = (j.get("transaction") or {}).get("messages") or []
    if not messages:
        raise FragmentError(f"getGiftPremiumLink не вернул транзакцию: {j}")
    return messages[0], str(req_id)


def _b64_any(data: str) -> bytes:
    """Декодируем base64 в любом виде: обычный и url-safe, с выравниванием и без.

    Зачем: Fragment отдаёт payload в url-safe алфавите ("-" и "_" вместо "+" и "/")
    и без хвостовых "=". Голый base64.b64decode в таком случае ��е падает,
    а МОЛЧА выкидывает непонятные ему символы и отдаёт битые байты.
    Именно это да��ало "Not enough bytes for cells data".
    """
    s = "".join(str(data or "").split())
    pad = "=" * (-len(s) % 4)
    last_err = None
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        for candidate in (s + pad, s):
            try:
                raw = decoder(candidate)
                if raw:
                    return raw
            except Exception as e:
                last_err = e
    raise ValueError(
        "не смог раскодировать base64 (%d символов): %s" % (len(s), last_err)
    )


def decode_jetton_transfer(payload_b64: str) -> dict:
    """Разбираем тело жетонного перевода (TEP-74) и достаём сумму.

    Ничего не отправляет и никуда не ходит — чисто локальный разбор байтов.

    Структура: op:uint32, query_id:uint64, amount:coins, destination:address, ...

    Возвращает {"ok": True, "amount_units": int, "amount": float} либо
    {"ok": False, "error": "..."}. Исключений не кидает никогда:
    это диагностика, она не должна ронять оплату.
    """
    if not payload_b64:
        return {"ok": False, "error": "payload пуст"}
    try:
        from pytoniq_core import Cell
    except Exception as e:
        return {"ok": False, "error": "нет pytoniq_core (%s)" % e}
    try:
        cell = Cell.one_from_boc(_b64_any(payload_b64))
        cs = cell.begin_parse()
        op = cs.load_uint(32)
        if op != JETTON_TRANSFER_OP:
            return {"ok": False, "error": "это не перевод жетона (op 0x%08X)" % op}
        cs.load_uint(64)  # query_id — не нужен
        # сумма в минимальных единицах жетона (VarUInteger 16)
        if hasattr(cs, "load_coins"):
            units = cs.load_coins()
        else:
            length = cs.load_uint(4)
            units = cs.load_uint(length * 8) if length else 0
        units = int(units or 0)
        return {
            "ok": True,
            "amount_units": units,
            "amount": units / (10 ** USDT_DECIMALS),
        }
    except Exception as e:
        s = "".join(str(payload_b64 or "").split())
        odd = "".join(sorted({c for c in s if not (c.isalnum() or c in "+/=")}))
        return {
            "ok": False,
            "error": "разбор не удался: %s [длина %d, остаток %d, нестандартные %r]"
                     % (e, len(s), len(s) % 4, odd),
        }


def fragment_probe_payment_methods(stars: int = 50,
                                   username: str | None = None) -> str:
    """Перебираем варианты payment_method и смотрим, что примет Fragment.

    Заявки только СОЗДАЮТСЯ. Ни одна подпись и ни один перевод не отправляются:
    оплата живёт отдельно, в send_ton_transaction, а его здесь нет.

    Главное в отчёте — поле amount:
      • большое (порядка цены звёзд) — платим самими TON;
      • маленькое (сотые TON) при наличии payload — это перевод жетона USDT,
        где TON уходит только на газ сети. Именно его мы ищем.
    """
    username = str(username or FRAGMENT_PROBE_USERNAME or "").lstrip("@").strip()
    if not username:
        return "Не задан юзернейм: /pay_methods @username"
    stars = max(int(stars or 50), 50)
    lines = [
        "Зо��д payment_method: %s star, получатель @%s" % (stars, username),
        "Оплата НЕ отправляется — только создание заявок.",
        "",
    ]
    try:
        recipient, _name = fragment_check_recipient(username, stars)
    except Exception as e:
        return (
            "Получателя @%s найти не удалось: %s\n\n"
            "Подбор способов оплаты не начался — Fragment отверг самого получателя.\n"
            "Нужен реальный пользователь Telegram (не канал, не бот, не группа).\n"
            "Попробуй свой же юзернейм или юзернейм из последнего успешного заказа."
        ) % (username, e)

    for method in FRAGMENT_PAYMENT_CANDIDATES:
        try:
            j = fragment_api("initBuyStarsRequest", recipient=recipient,
                             quantity=stars, payment_method=method)
            req_id = j.get("req_id")
            if not req_id:
                lines.append("%-10s | заявка без req_id: %s" % (method, str(j)[:110]))
                continue
            j2 = fragment_api("getBuyStarsLink", transaction="1",
                              id=req_id, show_sender="0")
            messages = (j2.get("transaction") or {}).get("messages") or []
            if not messages:
                lines.append("%-10s | req_id есть, транзакции нет: %s"
                             % (method, str(j2)[:110]))
                continue
            m0 = messages[0] or {}
            amount = int(m0.get("amount") or 0) / 1_000_000_000
            payload = m0.get("payload") or ""
            jetton = decode_jetton_transfer(payload) if payload else {"ok": False}
            if jetton.get("ok"):
                usdt = float(jetton["amount"])
                lines.append(
                    "%-10s | OK | газ %.4f TON | ЦЕНА %.6f USDT | %.6f USDT/★"
                    % (method, amount, usdt, usdt / stars if stars else 0.0)
                )
            else:
                kind = "жетон?" if (payload and amount < 0.2) else "чистый TON"
                lines.append(
                    "%-10s | OK | amount %.6f TON | %.6f TON/★ | payload %s | %s"
                    % (method, amount, amount / stars if stars else 0.0,
                       ("%d байт" % len(payload)) if payload else "нет", kind)
                )
                if payload and amount < 0.2 and jetton.get("error"):
                    lines.append("%-10s |    └ payload: %s" % ("", jetton["error"]))
        except Exception as e:
            lines.append("%-10s | ОШИБКА | %s" % (method, str(e)[:150]))

    lines.append("")
    lines.append("Где видна ЦЕНА в USDT — это реальная себестоимость в долларах,")
    lines.append("а газ — единственное, что ещё тратится в TON.")
    lines.append("Сравни USDT/★ с тем, сколько ты получаешь за звезду после вывода.")
    return "\n".join(lines)


def _ton_import_diag() -> str:
    """Диагностика TON-библиотек: какой питон, где лежит tonutils и что у него внутри."""
    import sys as _sys
    import pkgutil as _pkgutil
    lines = ["Python: " + str(_sys.executable)]
    for pkg_name in ("tonutils", "pytoniq_core"):
        try:
            pkg = __import__(pkg_name)
            ver = str(getattr(pkg, "__version__", "?"))
            path = str(getattr(pkg, "__file__", "?"))
            subs = sorted(m.name for m in _pkgutil.iter_modules(getattr(pkg, "__path__", [])))
            lines.append(pkg_name + " v" + ver + ": " + path)
            lines.append("  внутри: " + (", ".join(subs) if subs else "ПУСТО"))
        except Exception as e2:
            lines.append(pkg_name + ": не импортируется (" + str(e2) + ")")
    return "\n".join(lines)


async def send_ton_transaction(address: str, amount_nano: int, payload_b64: str) -> str:
    """Отправляем TON-транзакцию с кошелька из TON_SEED. Возвращает hash сообщения."""
    try:
        from pytoniq_core import Cell
        from tonutils.client import TonapiClient
        from tonutils.wallet import WalletV5R1
    except ImportError as e:
        diag = _ton_import_diag()
        logger.error("Импорт TON-библиотек не удался\n%s", diag, exc_info=True)
        raise FragmentError(
            f"Импорт TON-библиотек не удался: {e}\n{diag}\n"
            "Лечение (тем же питоном, которым запускаешь бота):\n"
            'py -m pip install "tonutils==0.5.6" pytoniq-core\n'
            "ВАЖНО: код написан под tonutils 1.x — версия 2.x НЕ подойдёт (другой API)"
        ) from e

    if not (TON_SEED and TONAPI_KEY):
        raise FragmentError("Fragment не настроен (TON_SEED / TONAPI_KEY)")

    client = TonapiClient(api_key=TONAPI_KEY)
    wallet, _, _, _ = WalletV5R1.from_mnemonic(client, TON_SEED.split())
    # Важно: _b64_any, а не base64.b64decode — payload бывает url-safe и без выравнивания.
    body = Cell.one_from_boc(_b64_any(payload_b64))
    tx_hash = await wallet.transfer(
        destination=address,
        amount=amount_nano / 1_000_000_000,  # нанотоны -> TON
        body=body,
    )
    return str(tx_hash)


_wallet_address_cache: str | None = None


def _get_wallet_address() -> str | None:
    global _wallet_address_cache
    if _wallet_address_cache:
        return _wallet_address_cache
    try:
        from tonutils.client import TonapiClient
        from tonutils.wallet import WalletV5R1
        client = TonapiClient(api_key=TONAPI_KEY)
        wallet, _, _, _ = WalletV5R1.from_mnemonic(client, TON_SEED.split())
        _wallet_address_cache = wallet.address.to_str()
        return _wallet_address_cache
    except Exception:
        logger.error("Не удалось получить адрес TON-кошелька\n%s", _ton_import_diag(), exc_info=True)
        return None


def get_ton_balance() -> float | None:
    """Баланс TON-кошелька через tonapi.io. None — если не настроено или ошибка."""
    if not (TON_SEED and TONAPI_KEY):
        return None
    address = _get_wallet_address()
    if not address:
        return None
    try:
        resp = requests.get(
            f"https://tonapi.io/v2/accounts/{address}",
            headers={"Authorization": f"Bearer {TONAPI_KEY}"},
            timeout=15,
            proxies=NO_PROXY,
        )
        return int(resp.json().get("balance", 0)) / 1_000_000_000
    except Exception:
        logger.error("Не удалось получить баланс TON-кошелька", exc_info=True)
        return None


async def fragment_pay(deal: Deal) -> str:
    """Полный цикл автооплаты. Возвращает hash транзакции. Бросает FragmentError."""
    loop = asyncio.get_running_loop()
    deal.pay_stage = "recipient"
    if deal.product == "premium":
        recipient, name = await loop.run_in_executor(
            None, fragment_check_premium_recipient, deal.username, deal.months
        )
        deal.recipient_name = name
        tx, req_id = await loop.run_in_executor(
            None, fragment_build_premium_transaction, recipient, deal.months
        )
        goods = f"Premium {deal.months} мес"
    else:
        recipient, name = await loop.run_in_executor(
            None, fragment_check_recipient, deal.username, deal.stars
        )
        deal.recipient_name = name
        pay_method = choose_payment_method(deal.stars)
        tx, req_id = await loop.run_in_executor(
            None, fragment_build_transaction, recipient, deal.stars, pay_method
        )
        goods = f"{deal.stars}⭐"
    deal.pay_req_id = req_id
    deal.pay_stage = "tx_built"
    save_state()
    amount_nano = int(tx["amount"])
    need_ton = amount_nano / 1_000_000_000

    # В режиме жетона tx["amount"] — ТОЛЬКО газ, а сами доллары лежат в payload.
    # Значит проверять надо ДВА баланса. Иначе проверка увидит "нужно 0.05 TON",
    # пропустит оплату без USDT на счёте, перевод провалится в сети,
    # газ спишется, звёзды не выдадутся, а сделка повиснет в PAYING.
    is_jetton = bool(tx.get("payload")) and need_ton < 0.2
    jetton = decode_jetton_transfer(tx.get("payload") or "") if is_jetton else {"ok": False}
    need_usdt = float(jetton["amount"]) if jetton.get("ok") else None

    balance = await loop.run_in_executor(None, get_ton_balance)

    if need_usdt is not None:
        # Платим долларами: нужен USDT на товар и немного TON на газ.
        if balance is not None and balance < need_ton + 0.02:
            raise FragmentError(
                f"Не хватает TON на газ: на кошельке {balance:.3f}, нужно ~{need_ton + 0.02:.3f} TON. "
                "Звёзды оплачиваются USDT, но без газа сеть не пропустит перевод."
            )
        usdt_balance = await loop.run_in_executor(None, get_usdt_balance)
        if usdt_balance is None:
            raise FragmentError(
                "Не удалось узнать баланс USDT. Оплату не отправляю: лучше задержать выдачу, "
                "чем пустить перевод наугад и потерять газ."
            )
        if usdt_balance < need_usdt:
            raise FragmentError(
                f"Недостаточно USDT: на кошельке {usdt_balance:.6f}, нужно {need_usdt:.6f}. "
                "Пополни USDT на сети TON."
            )
    elif is_jetton:
        # Похоже на жетон, но сумму прочитать не вышло — вслепую не рискуем.
        raise FragmentError(
            "Fragment прислал перевод жетона, но сумму в нём разобрать не удалось: "
            + str(jetton.get("error") or "причина неизвестна")
            + ". Оплату не отправляю — не могу проверить, хватает ли денег."
        )
    else:
        # Обычная оплата самими TON (запас 0.1 TON на комиссию сети).
        if balance is not None and balance < need_ton + 0.1:
            raise FragmentError(
                f"Недостаточно TON: на кошельке {balance:.3f}, нужно ~{need_ton + 0.1:.3f}. Пополни кошелёк."
            )

    logger.info("Fragment: оплата %s для @%s (%s), %s -> %s (req %s)",
                goods, deal.username, name,
                ("%.6f USDT + %.4f TON газ" % (need_usdt, need_ton))
                if need_usdt is not None else ("%.4f TON" % need_ton),
                tx["address"], req_id)
    # С этого момента повторная автооплата опасна: деньги могли уйти в сеть,
    # даже если ответ до бота не дошёл.
    deal.pay_stage = "sending"
    save_state()
    tx_hash = await send_ton_transaction(tx["address"], amount_nano, tx["payload"])
    deal.pay_tx_hash = tx_hash
    deal.pay_stage = "sent"
    save_state()

    # После оплаты предупреждаем, если кошелёк почти пуст
    balance = await loop.run_in_executor(None, get_ton_balance)
    if balance is not None and balance < LOW_BALANCE_TON and admin_outbox is not None:
        admin_outbox.put_nowait((
            f"⚠️ На TON-кошельке осталось <b>{balance:.3f} TON</b> — пополни, иначе автооплата встанет.",
            None,
        ))
    return tx_hash


async def auto_pay_deal(deal: Deal, bot: Bot):
    """Автооплата мелких заказов БЕЗ подтверждения админа (порог AUTO_CONFIRM_MAX_STARS)."""
    with deals_lock:
        if deal.state != WAIT_ADMIN:
            return
        deal.state = PAYING
        deal.pay_stage = "start"
    save_state()
    try:
        tx_hash = await fragment_pay(deal)
    except Exception as e:
        unsafe = deal.pay_stage in ("sending", "sent")
        with deals_lock:
            deal.state = FROZEN if unsafe else WAIT_ADMIN
        save_state()
        logger.error("Автооплата без подтверждения не удалась (шаг %s)", deal.pay_stage, exc_info=True)
        if admin_outbox is None:
            return
        if unsafe:
            # Транзакция могла уйти в сеть — НЕ предлагаем обычную кнопку оплаты.
            kb_retry = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔁 Проверил — оплатить заново",
                                      callback_data=f"payforce:{deal.chat_id}")],
                [InlineKeyboardButton(text="✅ Звёзды дошли — закрыть",
                                      callback_data=f"paidok:{deal.chat_id}")],
                [InlineKeyboardButton(text="🖐 Оплачу вручную", callback_data=f"manual:{deal.chat_id}")],
                [InlineKeyboardButton(text="🔗 ЧАТ", url=f"{{https://funpay.com/chat/?node={deal.chat_id}}}")],
            ])
            admin_outbox.put_nowait((
                f"⚠️ <b>Автооплата заказа #{esc(deal.order_id)} оборвалась на опасном шаге</b>\n"
                f"<code>{esc(e)}</code>\nШаг: <code>{esc(deal.pay_stage)}</code>, "
                f"заявка Fragment: <code>{esc(deal.pay_req_id)}</code>\n\n"
                f"Проверь историю TON-кошелька и fragment.com ПЕРЕД повтором: "
                f"если списание уже было, повторная оплата спишет деньги ВТОРОЙ раз.",
                kb_retry))
            return
        text_admin, kb = build_admin_confirm(deal)
        admin_outbox.put_nowait((
            f"❌ Автооплата заказа #{esc(deal.order_id)} не удалась:\n<code>{esc(e)}</code>\n"
            f"Деньги НЕ списаны (сбой на шаге <code>{esc(deal.pay_stage)}</code>).\n"
            f"Ниже — обычное подтверждение вручную.", None))
        admin_outbox.put_nowait((text_admin, kb))
        return
    with deals_lock:
        deal.state = PAID
    save_state()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, fp_send, deal.chat_id, MSG_PAID, True)
    goods = f"{deal.stars}⭐" if deal.product == "stars" else f"Premium {deal.months} мес"
    if admin_outbox is not None:
        admin_outbox.put_nowait((
            f"⚡ Заказ #{esc(deal.order_id)} оплачен АВТОМАТИЧЕСКИ (порог {AUTO_CONFIRM_MAX_STARS}⭐).\n"
            f"@{esc(deal.username)} +{esc(goods)}\nTX: <code>{esc(tx_hash)}</code>", None))


# ================= ОБРАБОТКА ЗАКАЗОВ =================

def handle_new_order(order, main_loop: asyncio.AbstractEventLoop, bot: Bot):
    s_id = str(getattr(order, "id", "") or "")
    s_status = getattr(order, "status", None)
    buyer_username = getattr(order, "buyer_username", None)

    if not s_id or s_status != OrderStatuses.PAID or s_id in processed_orders:
        return

    logger.info("💰 Новый заказ #%s от %s", s_id, buyer_username)

    if is_paused():
        # На паузе заказ НЕ теряется: мы не помечаем его обработанным,
        # и после снятия с паузы страховочный поток подберёт его сам.
        if s_id not in _paused_orders_notified:
            _paused_orders_notified.add(s_id)
            notify_admin_threadsafe(
                main_loop, bot,
                f"⏸ <b>Заказ #{esc(s_id)}</b> от {esc(str(buyer_username))} пришёл, но бот на ПАУЗЕ.\n"
                f"Обработка начнётся сразу после команды /resume — или разберись вручную.",
            )
        logger.info("⏸ Заказ #%s пропущен: бот на паузе", s_id)
        return

    if buyer_username and str(buyer_username).lower() in blacklist:
        processed_orders.add(s_id)
        save_state()
        logger.warning("🚫 Заказ #%s от покупателя из чёрного списка (%s)", s_id, buyer_username)
        notify_admin_threadsafe(
            main_loop, bot,
            f"🚫 <b>Заказ #{esc(s_id)}</b> от покупателя из чёрного списка ({esc(buyer_username)}).\n"
            f"Бот его НЕ обрабатывает — разберись вручную (например, оформи возврат).",
        )
        return

    product, months = parse_product(order)
    stars = parse_stars_amount(order) if product == "stars" else None
    description = str(getattr(order, "description", "") or "")
    price = f"{getattr(order, 'price', '')} {getattr(order, 'currency', '') or ''}".strip()

    # чат покупателя резолвим по нику (у OrderShortcut нет chat_id)
    chat_id = None
    if buyer_username:
        try:
            acc = holder.account
            chat_shortcut = acc.get_chat_by_name(buyer_username, True) if acc else None
            if chat_shortcut is not None:
                chat_id = chat_shortcut.id
        except Exception:
            logger.error("Не удалось найти чат покупателя %s", buyer_username, exc_info=True)

    if chat_id is None:
        # Раньше заказ уже считался обработанным и терялся навсегда при любом сетевом
        # глюке. Теперь даём несколько попыток — страховочный поток подхватит его снова.
        tries = _order_resolve_attempts.get(s_id, 0) + 1
        _order_resolve_attempts[s_id] = tries
        gave_up = tries >= ORDER_RESOLVE_MAX_RETRIES
        if gave_up:
            processed_orders.add(s_id)
            save_state()
        tail = (
            f"⚠️ Не смог открыть чат с покупателем ({tries} попыток). "
            f"Больше не пробую — обработай вручную."
            if gave_up else
            f"⚠️ Не смог открыть чат с покупателем (попытка {tries} из "
            f"{ORDER_RESOLVE_MAX_RETRIES}). Пробую ещё автоматически, вмешиваться пока не надо."
        )
        notify_admin_threadsafe(
            main_loop, bot,
            f"💰 <b>НОВЫЙ ЗАКАЗ #{esc(s_id)}</b>\n"
            f"Покупатель: {esc(buyer_username)}\nЛот: {esc(description)}\nСумма: {esc(price)}\n\n"
            + tail,
        )
        return

    _order_resolve_attempts.pop(s_id, None)
    processed_orders.add(s_id)
    save_state()

    with deals_lock:
        existing = deals.get(chat_id)
        if existing is not None:
            # Анти-фрод: не перезаписываем активную сделку — иначе покупатель мог бы
            # оплатить дешёвый заказ поверх дорогого и запутать бота.
            notify_admin_threadsafe(
                main_loop, bot,
                f"⚠️ <b>Заказ #{esc(s_id)}</b> от {esc(buyer_username)}: у покупателя уже есть "
                f"активная сделка (#{esc(existing.order_id)}, {existing.state}). Обработай вручную.",
                chat_kb(chat_id),
            )
            return
        deal = Deal(
            order_id=s_id, chat_id=chat_id, buyer=str(buyer_username),
            stars=stars, price=price, description=description,
            product=product, months=months,
        )
        deals[chat_id] = deal
    save_state()

    if product == "premium":
        stars_text = f"Premium {months} мес" if months else "Premium ❓ (не смог о��ределить срок — вручную!)"
    else:
        stars_text = f"{stars}⭐" if stars else "❓ (не смог определить количество звёзд!)"

    # Пробуем достать юзернейм прямо из заказа (покупатель вводит его при оплате
    # лота). Если получилось — не просим вводить заново, а сразу просим подтвердить.
    found_username = None
    try:
        found_username = extract_username_from_order(order, holder.account)
    except Exception:
        logger.error("Ошибка при поиске юзернейма в заказе #%s", s_id, exc_info=True)

    # Гонка событий: покупатель мог прислать юзернейм в чат ДО того, как бот
    # узнал о заказе — тогда юзернейм уже ждёт в _pre_deal_usernames.
    _early = _pre_deal_usernames.pop(chat_id, None)
    if not found_username and _early and time.time() - _early[0] <= _PRE_DEAL_TTL:
        found_username = _early[1]
        logger.info("📥 Подхватил юзернейм @%s, присланный до появления заказа #%s", found_username, s_id)

    if found_username:
        with deals_lock:
            deal.username = found_username
            deal.state = WAIT_CONFIRM
        save_state()
        step_text = f"Нашёл юзернейм в заказе: @{esc(found_username)}. Жду подтверждения покупателя (ДА/НЕТ)."
    else:
        logger.info("Юзернейм в заказе #%s не нашёл — спрошу в чате", s_id)
        step_text = "Юзернейм в заказе не нашёл — запросил у покупателя в чате."

    notify_admin_threadsafe(
        main_loop, bot,
        f"💰 <b>НОВЫЙ ЗАКАЗ #{esc(s_id)}</b>\n"
        f"Покупатель: {esc(buyer_username)}\nЛот: {esc(description)}\n"
        f"Товар: {esc(stars_text)}\nСумма: {esc(price)}\n\n"
        f"{step_text}",
        chat_kb(chat_id),
    )
    if found_username:
        fp_send(chat_id, MSG_CONFIRM_USERNAME_FROM_ORDER.format(order_id=s_id, username=found_username))
    else:
        fp_send(chat_id, MSG_ASK_USERNAME.format(order_id=s_id))


def handle_order_status_changed(order, main_loop: asyncio.AbstractEventLoop, bot: Bot):
    """Покупатель подтвердил заказ (или произошёл возврат)."""
    s_id = str(getattr(order, "id", "") or "")
    s_status = getattr(order, "status", None)

    with deals_lock:
        deal = next((d for d in deals.values() if d.order_id == s_id), None)

    if s_status == OrderStatuses.CLOSED:
        logger.info("✅ Заказ #%s подтверждён покупателем", s_id)
        if deal is not None:
            fp_send(deal.chat_id, MSG_THANKS)
            with deals_lock:
                deals.pop(deal.chat_id, None)
            save_state()
            record_sale(deal)
            notify_admin_threadsafe(
                main_loop, bot,
                f"🎉 Заказ #{esc(s_id)} подтверждён покупателем {esc(deal.buyer)}. Сделка закрыта.",
            )
        # Автоответ-благодарность на отзыв (если покупатель оставил его при подтверждении)
        if REVIEW_REPLY_ENABLED:
            try:
                acc = holder.account
                if acc is not None and hasattr(acc, "send_review"):
                    acc.send_review(s_id, REVIEW_REPLY_TEXT)
                    logger.info("💬 Оставил ответ на отзыв по заказу %s", s_id)
            except Exception as e:
                # отзыва может не быть вовсе — это нормально, не шумим
                logger.debug("Ответ на отзыв по заказу %s не отправлен: %s", s_id, e)
    elif s_status == OrderStatuses.REFUNDED and deal is not None:
        with deals_lock:
            deals.pop(deal.chat_id, None)
        save_state()
        notify_admin_threadsafe(
            main_loop, bot,
            f"↩️ По заказу #{esc(s_id)} оформлен возврат, сделка снята с контроля.",
        )


# ================= ОБРАБОТКА СООБЩЕНИЙ =================

def build_admin_confirm(deal: Deal) -> tuple[str, InlineKeyboardMarkup]:
    recipient = f"@{deal.username}"
    if deal.recipient_name:
        recipient += f" ({deal.recipient_name})"
    goods = f"{esc(deal.stars)}⭐" if deal.product == "stars" else f"Premium {esc(deal.months)} мес"
    text = (
        f"💫 Пользователь <b>{esc(deal.buyer)}</b> оплатил заказ "
        f"<b>#{esc(deal.order_id)}</b> на <b>{goods}</b>\n"
        f"Лот: {esc(deal.description)}\n"
        f"Сумма: {esc(deal.price)}\n"
        f"Получатель: <code>{esc(recipient)}</code>\n\n"
        f"Оплатить заказ на Fragment?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ ДА (автооплата)", callback_data=f"pay:{deal.chat_id}"),
            InlineKeyboardButton(text="❌ НЕТ", callback_data=f"no:{deal.chat_id}"),
        ],
        [InlineKeyboardButton(text="🖐 Оплачу вручную", callback_data=f"manual:{deal.chat_id}")],
        [InlineKeyboardButton(text="🔗 ЧАТ", url=f"https://funpay.com/chat/?node={deal.chat_id}")],
    ])
    return text, kb


def handle_deal_message(deal: Deal, text: str, main_loop: asyncio.AbstractEventLoop, bot: Bot):
    """FSM сделки. Вызывается только для сообщений интерлокьютора в чате сделки."""
    answer = text.strip()

    if deal.state == WAIT_USERNAME:
        username = extract_username(answer)
        if username is None:
            logger.warning("Не распознал юзернейм в сделке #%s, сырой текст: %r", deal.order_id, text)
            # ВАЖНО: попытка засчитывается только если покуп��тел�� реально увидел объяснени��:
            # раньше антиспам мог съесть ответ, а попытки всẹ́ равно сгорали.
            delivered = fp_send(deal.chat_id, MSG_BAD_USERNAME)
            if delivered:
                deal.attempts += 1
            if deal.attempts >= MAX_USERNAME_ATTEMPTS:
                deal.state = FROZEN
                save_state()
                fp_send(deal.chat_id, MSG_MANUAL, True)
                notify_admin_threadsafe(
                    main_loop, bot,
                    f"🧊 Сделка #{esc(deal.order_id)} ({esc(deal.buyer)}) заморожена: "
                    f"слишком много неудачных попыток ввода юзернейма. Разберись вручную.",
                    chat_kb(deal.chat_id),
                )
                return
            save_state()
            return
        deal.username = username
        deal.state = WAIT_CONFIRM
        save_state()
        fp_send(deal.chat_id, MSG_CONFIRM_USERNAME.format(username=username), True)
        return

    if deal.state == WAIT_CONFIRM:
        norm = answer.lower().strip("!. ")
        # вытаскиваем слова из сообщения — терпим мусорные символы и знаки препинания вокруг
        words = set(re.findall(r"[а-яёa-z]+", norm))
        has_yes = bool({"да", "yes", "lf"} & words)
        has_no = bool({"нет", "no", "ytn"} & words)
        if has_yes and not has_no:
            deal.state = WAIT_ADMIN
            # Если Fragment настроен — сразу проверяем, что получатель существует.
            if FRAGMENT_HASH and FRAGMENT_COOKIES and (deal.stars or deal.months):
                try:
                    if deal.product == "premium":
                        _, name = fragment_check_premium_recipient(deal.username, deal.months)
                    else:
                        _, name = fragment_check_recipient(deal.username, deal.stars)
                    deal.recipient_name = name
                except FragmentError as e:
                    logger.warning("Fragment: получатель не найден: %s", e)
                    deal.state = WAIT_USERNAME
                    deal.attempts += 1
                    save_state()
                    fp_send(
                        deal.chat_id,
                        "❌ Такой юзернейм не найден в Telegram. Проверьте и введите ещё раз.\nПример: @username",
                    )
                    return
                except Exception:
                    logger.error("Fragment: ошибка проверки получателя", exc_info=True)
            save_state()
            fp_send(deal.chat_id, MSG_WAIT_PAYMENT, True)
            # Мелкие заказы звёзд можно оплачивать сразу, без кнопки подтверждения
            if (AUTO_PAY_FRAGMENT and AUTO_CONFIRM_MAX_STARS > 0 and deal.product == "stars"
                    and deal.stars and deal.stars <= AUTO_CONFIRM_MAX_STARS):
                asyncio.run_coroutine_threadsafe(auto_pay_deal(deal, bot), main_loop)
                return
            text_admin, kb = build_admin_confirm(deal)
            notify_admin_threadsafe(main_loop, bot, text_admin, kb)
            return
        if has_no and not has_yes:
            deal.state = WAIT_USERNAME
            deal.username = None
            save_state()
            fp_send(deal.chat_id, MSG_ASK_USERNAME_AGAIN)
            return
        logger.warning("Не распознал ДА/НЕТ в сделке #%s, сырой текст: %r", deal.order_id, text)
        # Симметрично WAIT_USERNAME: лимит попыток, чтобы сделка не висела вечно.
        if fp_send(deal.chat_id, MSG_CONFIRM_HINT):
            deal.confirm_attempts += 1
        if deal.confirm_attempts >= MAX_CONFIRM_ATTEMPTS:
            deal.state = FROZEN
            save_state()
            fp_send(deal.chat_id, MSG_MANUAL, True)
            notify_admin_threadsafe(
                main_loop, bot,
                f"🧊 Сделка #{esc(deal.order_id)} ({esc(deal.buyer)}) заморожена: "
                f"покупатель так и не ответил ДА/НЕТ. Разберись вручную.",
                chat_kb(deal.chat_id),
            )
            return
        save_state()
        return

    if deal.state in (WAIT_ADMIN, PAYING, WAIT_MANUAL):
        now = time.time()
        if now - last_in_progress.get(deal.chat_id, 0) > IN_PROGRESS_COOLDOWN:
            last_in_progress[deal.chat_id] = now
            fp_send(deal.chat_id, MSG_IN_PROGRESS)
        return
    # PAID / FROZEN — молчим, чтобы не спамить; админ уже в курсе.


# Статусы, в которых точно ничего ещё не выдано — возврат безопасен.
REFUNDABLE_STATES = (WAIT_USERNAME, WAIT_CONFIRM, WAIT_ADMIN, FROZEN)


def handle_refund_request(c_id: int, chat_name: str, text: str,
                          main_loop: asyncio.AbstractEventLoop, bot: Bot):
    """Обработка !возврат: бот сам возвращает деньги, если это безопасно."""
    with deals_lock:
        deal = deals.get(c_id)

    # 1) Есть активная сделка, звёзды ещё НЕ выдавались — возвращаем автоматически.
    if deal is not None and deal.state in REFUNDABLE_STATES and not deal.refund_done:
        if not AUTO_REFUND_ENABLED:
            fp_send(c_id, MSG_REFUND_LATER, True)
            notify_admin_threadsafe(
                main_loop, bot,
                f"↩️ <b>Запрос возврата</b> по заказу #{esc(deal.order_id)} от {esc(chat_name)}.\n"
                f"Автовозврат выключен (AUTO_REFUND_ENABLED = False) — оформи вручную.",
                chat_kb(c_id),
            )
            return
        ok, err = funpay_refund(deal.order_id)
        if ok:
            with deals_lock:
                deal.refund_done = True
                deals.pop(c_id, None)
            save_state()
            fp_send(c_id, MSG_REFUND_DONE, True)
            notify_admin_threadsafe(
                main_loop, bot,
                f"↩️ <b>АВТОВОЗВРАТ</b> по заказу #{esc(deal.order_id)}\n"
                f"👤 {esc(chat_name)}\n💸 {esc(deal.price)}\n"
                f"Звёзды не выдавались (был статус {deal.state}), деньги вернул сам.",
                chat_kb(c_id),
            )
        else:
            fp_send(c_id, MSG_REFUND_FAIL, True)
            notify_admin_threadsafe(
                main_loop, bot,
                f"⚠️ <b>Не смог вернуть деньги</b> по заказу #{esc(deal.order_id)} ({esc(chat_name)}):\n"
                f"<code>{esc(err)}</code>\nОформи возврат вручную.",
                chat_kb(c_id),
            )
        return

    # 2) Сделка есть, но выдача уже шла/прошла — автовозврат запрещён.
    if deal is not None:
        fp_send(c_id, MSG_REFUND_LATER, True)
        notify_admin_threadsafe(
            main_loop, bot,
            f"⚠️ <b>ЗАПРОС ВОЗВРАТА</b> по заказу #{esc(deal.order_id)} ({esc(chat_name)})\n"
            f"Статус сделки: <b>{deal.state}</b> — товар мог быть уже выдан, "
            f"поэтому деньги сам НЕ возвращаю. Проверь и реши вручную.\n"
            f"💬 <code>{esc(text)}</code>",
            chat_kb(c_id),
        )
        return

    # 3) Активной сделки нет — смотрим, есть ли вообще оплаченный заказ этого покупателя.
    order = find_paid_order_by_buyer(chat_name)
    if order is not None:
        fp_send(c_id, MSG_REFUND_LATER, True)
        notify_admin_threadsafe(
            main_loop, bot,
            f"⚠️ <b>ЗАПРОС ВОЗВРАТА</b> от {esc(chat_name)}\n"
            f"Активной сделки у бота нет, но есть оплаченный заказ "
            f"#{esc(getattr(order, 'id', '?'))} — звёзды могли быть выданы вручную.\n"
            f"Сам деньги НЕ возвращаю. Если возврат нужен: "
            f"<code>/refund {esc(getattr(order, 'id', ''))}</code>\n💬 <code>{esc(text)}</code>",
            chat_kb(c_id),
        )
        return

    # 4) Оплаченного заказа нет вообще.
    fp_send(c_id, MSG_REFUND_NO_ORDER, True)
    logger.info("!возврат без оплаченного заказа от %s (чат %s)", chat_name, c_id)


def handle_new_message(message, main_loop: asyncio.AbstractEventLoop, bot: Bot):
    acc = holder.account
    author_id = getattr(message, "author_id", None)

    # свои сообщения и системные сообщения FunPay (author_id == 0) в FSM не пускаем.
    # Анти-фрод: поддельные «системные» тексты от покупателя сюда тоже не влияют —
    # сделка создаётся только по событию NEW_ORDER от сервера FunPay.
    if acc is not None and author_id is not None and str(author_id) == str(acc.id):
        return
    # Страховка от петли: сообщения от имени нашего же аккаунта тоже отбрасываем
    author_name = getattr(message, "author", None)
    if acc is not None and author_name and str(author_name) == str(getattr(acc, "username", "")):
        return

    text = getattr(message, "text", None)
    if not text:
        return
    text = INVISIBLE_CHARS_RE.sub("", str(text))[:1000]

    c_id = message.chat_id
    m_id = message.id
    chat_name = getattr(message, "chat_name", "") or ""
    lower_text = text.lower()

    # Защита от дублей: FunPay-ран��ер может прислать одно и то же сообщение
    # дважды (например, после того как бот сам написал в чат и чат «обновился»).
    # Повторная обработка ломает FSM: дубли ответов, ложные переходы состояний.
    if m_id is not None:
        if str(m_id) in processed_messages:
            logger.info("⏭ Повторное событие для сообщения %s — пропускаю", m_id)
            return
        processed_messages.add(str(m_id))
        if len(processed_messages) > 4000:
            processed_messages.clear()
            processed_messages.add(str(m_id))

    logger.info("📨 Сообщение %s | автор id=%r ник=%r | чат %s | %r",
                m_id, author_id, author_name, c_id, text[:80])

    # Эхо собственных сообщений: FunPay-раннер может вернуть наш же текст
    # без author_id/автора — тогда фильтры выше его не ловят. Сверка по тексту
    # не зависит от полей автора и гарантированно рубит петлю (спам ДА/НЕТ,
    # ложные 🆘, самопроизвольные переходы сделки).
    if _is_own_echo(c_id, text):
        logger.info("⏭ Эхо собственного сообщения в чате %s — пропускаю", c_id)
        return

    # --- чёрный список: полностью игнорируем ---
    if chat_name and chat_name.lower() in blacklist:
        logger.info("🚫 Игнорирую сообщение от %s (чёрный список)", chat_name)
        return

    # --- !ВОЗВРАТ ---
    if "!возврат" in lower_text:
        msg_key = f"{c_id}_{m_id}"
        if msg_key in notified_returns:
            return
        notified_returns.add(msg_key)
        logger.warning("❗ Найдено: !возврат в чате %s (msg %s)", c_id, m_id)
        handle_refund_request(c_id, chat_name, text, main_loop, bot)
        return

    # --- !ПОМОГИТЕ ---
    if "!помогите" in lower_text or "!помощь" in lower_text:
        msg_key = f"{c_id}_{m_id}"
        if msg_key in notified_help:
            return
        notified_help.add(msg_key)
        logger.warning("🆘 Запрос помощи в чате %s (msg %s)", c_id, m_id)
        fp_send(c_id, "🆘 Продавец уже уведомлён и скоро подключится к чату!")
        notify_admin_threadsafe(
            main_loop, bot,
            f"🆘 <b>НУЖНА ПОМОЩЬ</b>\n👤: <code>{esc(chat_name)}</code>\n💬: <code>{esc(text)}</code>",
            chat_kb(c_id),
        )
        return

    if author_id == 0:
        return  # системные уведомления FunPay — не повод для авто-ответов

    # --- активная сделка в этом чате ---
    with deals_lock:
        deal = deals.get(c_id)
    if deal is not None:
        # В FSM сделки пускаем только сообщения самого покупателя —
        # ни свои, ни системные, ни чьи-либо ещё тексты не должны двигать сделку.
        if author_name and deal.buyer and str(author_name).strip().lower() != str(deal.buyer).strip().lower():
            logger.info("⏭ Сообщение в чате сделки #%s не от покупателя (%r) — игнорирую",
                        deal.order_id, author_name)
            return
        handle_deal_message(deal, text, main_loop, bot)
        return

    # --- нет сделки: гонка событий — покупатель мог прислать юзернейм раньше,
    # чем FunPay отдал боту событие о новом заказе. Запоминаем юзернейм —
    # обработчик заказа его подхватит, а «оплачивайте» в этом случае не шлём.
    now = time.time()
    early_username = extract_username(text)
    if early_username:
        _pre_deal_usernames[c_id] = (now, early_username)
        if len(_pre_deal_usernames) > 300:
            _cut = now - _PRE_DEAL_TTL
            for _k in [k for k, v in _pre_deal_usernames.items() if v[0] < _cut]:
                _pre_deal_usernames.pop(_k, None)
        logger.info("💾 Ранний юзернейм @%s в чате %s — сделки ещё нет, жду событие заказа", early_username, c_id)
        return

    if now - last_greeting.get(c_id, 0) > GREETING_COOLDOWN:
        last_greeting[c_id] = now
        fp_send(c_id, MSG_GREETING)


# ================= КУРС TON, АВТОЦЕНЫ, СТОП-ЛОТЫ, ПАУЗА =================

control_lock = threading.RLock()
_control: dict = {
    "paused": False,          # бот на паузе (новые заказы не обрабатываются)
    "paused_at": None,
    "pricing": AUTO_PRICING_ENABLED,   # включены ли автоцены (можно гасить из ТГ)
    "guard_disabled": {},     # лоты, снятые из-за нехватки TON: {lot_id: звёзд}
    "pause_disabled": [],     # лоты, снятые паузой (чтобы вернуть именно их)
}


def load_control():
    """Состояние паузы/автоцен/снятых лотов переживает перезапуск бота."""
    if not os.path.exists(CONTROL_FILE):
        return
    try:
        with open(CONTROL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        with control_lock:
            _control.update({k: v for k, v in data.items() if k in _control})
            _control["guard_disabled"] = {str(k): v for k, v in (_control.get("guard_disabled") or {}).items()}
            _control["pause_disabled"] = [str(x) for x in (_control.get("pause_disabled") or [])]
        logger.info("Состояние управления загружено: пауза=%s, автоцены=%s",
                    _control["paused"], _control["pricing"])
    except Exception:
        logger.error("Не удалось прочитать %s", CONTROL_FILE, exc_info=True)


def save_control():
    try:
        with control_lock:
            data = dict(_control)
        with open(CONTROL_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.error("Не удалось сохранить %s", CONTROL_FILE, exc_info=True)


def is_paused() -> bool:
    with control_lock:
        return bool(_control["paused"])


def pricing_enabled() -> bool:
    with control_lock:
        return bool(_control["pricing"])


# ---------- курс TON ----------

_ton_rate_cache: dict = {"ts": 0.0, "rate": None, "source": ""}


def get_ton_rate(max_age: float | None = None) -> float | None:
    """Курс TON в рублях. Два источника подряд, результат кэшируется."""
    max_age = TON_RATE_CACHE_SECONDS if max_age is None else max_age
    now = time.time()
    if _ton_rate_cache["rate"] and now - _ton_rate_cache["ts"] < max_age:
        return _ton_rate_cache["rate"]

    rate = None
    source = ""
    # 1) tonapi.io — тот же ключ, что и для баланса кошелька
    if TONAPI_KEY:
        try:
            resp = requests.get(
                "https://tonapi.io/v2/rates",
                params={"tokens": "ton", "currencies": "rub"},
                headers={"Authorization": f"Bearer {TONAPI_KEY}"},
                timeout=15, proxies=NO_PROXY,
            )
            value = resp.json().get("rates", {}).get("TON", {}).get("prices", {}).get("RUB")
            if value:
                rate = float(value)
                source = "tonapi.io"
        except Exception:
            logger.debug("Курс TON через tonapi не получился", exc_info=True)

    # 2) запасной вариант — CoinGecko (без ключа)
    if rate is None:
        try:
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "the-open-network", "vs_currencies": "rub"},
                timeout=15, proxies=NO_PROXY,
            )
            value = resp.json().get("the-open-network", {}).get("rub")
            if value:
                rate = float(value)
                source = "coingecko.com"
        except Exception:
            logger.debug("Курс TON через coingecko не получился", exc_info=True)

    if rate and rate > 0:
        _ton_rate_cache.update({"ts": now, "rate": rate, "source": source})
        return rate

    logger.warning("Не удалось узнать курс TON ни из одного источника")
    return _ton_rate_cache["rate"]  # пусть лучше старый курс, чем никакого


def ton_rate_source() -> str:
    return _ton_rate_cache.get("source") or "?"


_ton_usd_cache: dict = {"ts": 0.0, "price": None, "source": ""}


def get_ton_usd(max_age: float | None = None) -> float | None:
    """Цена TON в долларах.

    Нужна, чтобы сравнивать TON и USDT в одной валюте. Без неё бот
    сравнивал TON по рыночному рублёвому курсу, а USDT — по нашему
    дорогому доллару с комиссией вывода, и TON казался дешевле без причины.
    """
    max_age = TON_RATE_CACHE_SECONDS if max_age is None else max_age
    now = time.time()
    if _ton_usd_cache["price"] and now - _ton_usd_cache["ts"] < max_age:
        return _ton_usd_cache["price"]

    price = None
    source = ""
    if TONAPI_KEY:
        try:
            resp = requests.get(
                "https://tonapi.io/v2/rates",
                params={"tokens": "ton", "currencies": "usd"},
                headers={"Authorization": f"Bearer {TONAPI_KEY}"},
                timeout=15, proxies=NO_PROXY,
            )
            value = resp.json().get("rates", {}).get("TON", {}).get("prices", {}).get("USD")
            if value:
                price = float(value)
                source = "tonapi.io"
        except Exception:
            logger.debug("Цена TON в $ через tonapi не получилась", exc_info=True)

    if price is None:
        try:
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "the-open-network", "vs_currencies": "usd"},
                timeout=15, proxies=NO_PROXY,
            )
            value = resp.json().get("the-open-network", {}).get("usd")
            if value:
                price = float(value)
                source = "coingecko.com"
        except Exception:
            logger.debug("Цена TON в $ через coingecko не получилась", exc_info=True)

    # полоса правдоподобия: TON никогда не стоил ни $0.05, ни $100
    if price and 0.05 <= price <= 100.0:
        _ton_usd_cache.update({"ts": now, "price": price, "source": source})
        return price

    if price:
        logger.warning("Неправдоподобная цена TON в долларах: %s", price)
    return _ton_usd_cache["price"]


_quote_cache: dict = {"ton_per_star": None, "ts": 0.0, "source": "", "stars": 0}
_quote_lock = threading.RLock()


def fragment_quote_ton_per_star(force: bool = False) -> float | None:
    """Реальная цена 1 звезды в TON — пробная заявка на Fragment БЕЗ оплаты.

    Создаём заявку (initBuyStarsRequest + getBuyStarsLink) и смотрим, сколько
    Fragment просит в нанотонах. Деньги никуда не уходят: перевод отправляется
    отдельно, в fragment_pay. Ровно то же, что делает /fragment_test.
    None — если не получилось (тогда вызывающий берёт константу).
    """
    if not FRAGMENT_LIVE_COST:
        return None
    with _quote_lock:
        cached = _quote_cache.get("ton_per_star")
        age = time.time() - (_quote_cache.get("ts") or 0.0)
        if cached and not force and age < FRAGMENT_QUOTE_SECONDS:
            return cached
    stars = max(int(FRAGMENT_QUOTE_STARS or 100), 50)
    username = (FRAGMENT_PROBE_USERNAME or "").lstrip("@").strip()
    if not username:
        return None
    try:
        recipient, _name = fragment_check_recipient(username, stars)
        # КРИТИЧНО: здесь всегда спрашиваем ИМЕННО TON-маршрут.
        # Если подставится FRAGMENT_PAYMENT_METHOD = "usdt_ton", Fragment вернёт
        # жетонный перевод, где amount — это ГАЗ (0.05 TON), а не цена товара.
        # Тогда замер даёт 0.05/100 = 0.0005 TON/⭐ и валится в проверку правдоподобия.
        tx, _req_id = fragment_build_transaction(recipient, stars, "ton")
        amount_nano = int(tx.get("amount") or 0)
        if amount_nano <= 0:
            raise FragmentError("Fragment вернул нулевую сумму")
        per_star = (amount_nano / 1_000_000_000) / stars
        if not (0.001 <= per_star <= 0.1):
            raise FragmentError(
                "неправдоподобная цена %.6f TON за звезду "
                "(заявка на %d⭐, пришло %d нанотон, маршрут ton). "
                "Если цифра около 0.0005 — Fragment отдал жетонный перевод вместо чистого TON"
                % (per_star, stars, amount_nano))
    except Exception:
        logger.warning("Живую цену Fragment получить не удалось, беру константу", exc_info=True)
        return None
    with _quote_lock:
        _quote_cache.update({
            "ton_per_star": per_star,
            "ts": time.time(),
            "source": "fragment.com",
            "stars": stars,
        })
    logger.info("Fragment: живая цена %.7f TON/star (заявка на %s star, без оплаты)",
                per_star, stars)
    return per_star


def live_ton_per_star() -> float:
    """TON за 1 звезду: живая цена с Fragment, если получилась, иначе константа."""
    return fragment_quote_ton_per_star() or FRAGMENT_TON_PER_STAR


def ton_per_star_source() -> str:
    """Откуда взята цена звезды — для честных отчётов в Telegram."""
    with _quote_lock:
        if _quote_cache.get("ton_per_star"):
            return _quote_cache.get("source") or "fragment.com"
    return "константа в коде"


_usdt_cache: dict = {"amount": None, "ts": 0.0}


def get_usdt_balance() -> float | None:
    """Баланс USDT (жетон на сети TON) на кошельке бота. None — если ошибка."""
    if not (TON_SEED and TONAPI_KEY):
        return None
    if (_usdt_cache.get("amount") is not None
            and time.time() - (_usdt_cache.get("ts") or 0.0) < USDT_BALANCE_CACHE_SECONDS):
        return _usdt_cache["amount"]
    address = _get_wallet_address()
    if not address:
        return None
    try:
        resp = requests.get(
            "https://tonapi.io/v2/accounts/" + address + "/jettons/" + USDT_JETTON_MASTER,
            headers={"Authorization": "Bearer " + TONAPI_KEY},
            timeout=20,
        )
        j = resp.json()
        raw = j.get("balance")
        if raw is None:
            return None
        decimals = int(((j.get("jetton") or {}).get("decimals")) or 6)
        amount = int(raw) / float(10 ** decimals)
    except Exception:
        logger.warning("Баланс USDT получить не удалось", exc_info=True)
        return None
    _usdt_cache.update({"amount": amount, "ts": time.time()})
    return amount


def usdt_per_star() -> float:
    """Сколько USDT Fragment берёт за одну звезду."""
    try:
        value = float(USDT_PER_STAR)
        if 0.005 <= value <= 0.05:
            return value
    except Exception:
        pass
    return 0.015


def route_costs_rub(stars: int) -> dict:
    """Стоимость заказа в рублях по каждому из двух путей оплаты.

    В TON-пути от курса зависит всё. В USDT-пути — только газ,
    товар оплачивается долларами по фиксированной цене.
    Пустой dict — если курс TON узнать не удалось.
    """
    stars = max(int(stars or 0), 0)
    rate = get_ton_rate()
    if not rate:
        return {}
    # ОБА пути считаем в ОДНОЙ линейке — в рублях НАШЕГО капитала.
    # Раньше была ошибка: TON оценивался по рыночному курсу (tonapi),
    # а USDT — по нашему дорогому доллару с комиссией вывода. Из-за этого
    # TON казался дешевле на ~37 рублей с заказа, чего в реальности нет:
    # Fragment берёт одинаково в долларах любым маршрутом (~$0.015 за звезду).
    # Товар оплачивается капиталом в USDT, а TON покупается за эти же USDT
    # через свап со спредом — значит и стоит он как наши доллары плюс спред.
    usd_rub = usdt_cost_rub()
    ton_usd = get_ton_usd()
    if ton_usd:
        ton_rub = ton_usd * usd_rub * (1.0 + SWAP_SPREAD)
        basis = "TON по %.4f $ + свап %.1f%%" % (ton_usd, SWAP_SPREAD * 100)
    else:
        # цену TON в долларах не узнали — не угадываем, честно говорим
        ton_rub = rate
        basis = "рыночный курс (цена TON в $ неизвестна, сравнение приблизительное)"
    ton_route = stars * live_ton_per_star() * ton_rub + TON_GAS_PER_ORDER * ton_rub
    # газ берём ИЗМЕРЕННЫЙ (USDT_GAS_COST_TON = 0.05), а не резерв
    # USDT_GAS_TON = 0.08: резерв — сколько держать на кошельке, а не сколько
    # спишется. На мелких заказах лишние 0.03 TON переворачивали вывод.
    usdt_route = stars * usdt_per_star() * usd_rub + USDT_GAS_COST_TON * ton_rub
    return {
        "ton": ton_route,
        "usdt_ton": usdt_route,
        "rate": rate,
        "ton_rub": ton_rub,
        "usd_rub": usd_rub,
        "ton_usd": ton_usd,
        "basis": basis,
        "cheaper": "usdt_ton" if usdt_route < ton_route else "ton",
        "saving": abs(ton_route - usdt_route),
    }


def choose_payment_method(stars: int) -> str:
    """Чем платить за этот заказ: TON или USDT.

    USDT дешевле на самих звёздах, но газ жетона в пять раз дороже.
    На мелких заказах лишний газ съедает выгоду, на крупных — нет.
    Если курс неизвестен — не угадываем, берём настройку из конфига.
    """
    if not PAYMENT_AUTO_ROUTE:
        return FRAGMENT_PAYMENT_METHOD
    try:
        costs = route_costs_rub(stars)
    except Exception as e:
        logger.warning("choose_payment_method: %s", e)
        return FRAGMENT_PAYMENT_METHOD
    if not costs:
        return FRAGMENT_PAYMENT_METHOD
    return costs["cheaper"]


def cost_table(packages=None) -> str:
    """Прайс-лист: себестоимость, цена к получению и цена на витрине."""
    packs = list(packages or STARS_PACKAGES)
    try:
        rate = get_ton_rate()
    except Exception:
        rate = None
    eff = usdt_cost_rub()
    out = [
        "USDT: курс %.3f ₽, фактически %.2f ₽ (вывод %.0f%%, мин %.0f ₽)"
        % (USDT_RUB_RATE, eff, WITHDRAW_FEE_PCT * 100, WITHDRAW_MIN_RUB),
        "Маржа %.0f%%, жёсткий минимум %.0f%%"
        % (PRICE_TARGET_MARGIN * 100, PRICE_MIN_MARGIN * 100),
        "",
        "%6s %9s %8s %9s" % ("звёзд", "себест.", "ставлю", "витрина"),
    ]
    for s in packs:
        cost = star_cost_rub(s, rate)
        if cost is None:
            out.append("%6d %9s" % (s, "нет курса"))
            continue
        price = int(cost * (1 + PRICE_TARGET_MARGIN) + 0.999999)
        shelf = price * (1 + FUNPAY_FEE_PCT)
        out.append("%6d %9.2f %8d %9.0f" % (s, cost, price, shelf))
    out.append("")
    out.append("Ставлю = вбиваешь в лот. Витрина = что увидит покупатель.")
    return "\n".join(out)


def payment_route_table(packages=None) -> str:
    """Таблица для /route: что дешевле на каждом пакете звёзд."""
    packages = packages or STARS_PACKAGES
    first = route_costs_rub(packages[0] if packages else 100)
    if not first:
        return "Не удалось узнать курс TON — сравнить пути оплаты невозможно."
    lines = [
        "Курс TON %.2f ₽ | USDT %.3f ₽ | %.6f TON/★ | %.4f USDT/★"
        % (first["rate"], USDT_RUB_RATE, live_ton_per_star(), usdt_per_star()),
        "",
        "Оценка TON: %s" % first.get("basis", "?"),
        "Доллар нашего капитала: %.2f ₽ | TON в расчёте: %.2f ₽"
        % (first.get("usd_rub") or 0.0, first.get("ton_rub") or 0.0),
        "",
        "Звёзды |    TON ₽ |   USDT ₽ | дешевле",
    ]
    for stars in packages:
        c = route_costs_rub(stars)
        if not c:
            continue
        mark = "USDT" if c["cheaper"] == "usdt_ton" else "TON "
        lines.append(
            "%6d | %8.2f | %8.2f | %s (+%.2f ₽)"
            % (stars, c["ton"], c["usdt_ton"], mark, c["saving"])
        )
    lines.append("")
    lines.append(
        "Автовыбор: %s. Газ в расчёте: TON %.2f / USDT %.2f TON "
        "(на кошельке держим %.2f TON запаса)."
        % ("вкл" if PAYMENT_AUTO_ROUTE else "выкл", TON_GAS_PER_ORDER,
           USDT_GAS_COST_TON, USDT_GAS_TON)
    )
    return "\n".join(lines)


def stars_ton_cost(stars: int) -> float:
    """Сколько TON реально уйдёт на выдачу stars звёзд (вместе с газом)."""
    return stars * live_ton_per_star() + TON_GAS_PER_ORDER


def usdt_cost_rub(batch_rub: float | None = None) -> float:
    """Во сколько РЕАЛЬНО обходится 1 USDT с учётом вывода с FunPay.

    Комиссия списывается рублями ДО конвертации, поэтому цена доллара
    выше курса на сайте. Формула сверена с реальным выводом:
    1147 ₽ при курсе 85.158 дали 11.7 USDT — сошлось точно.
    """
    batch = float(batch_rub or WITHDRAW_BATCH_RUB)
    if batch <= 0:
        return USDT_RUB_RATE / max(1.0 - WITHDRAW_FEE_PCT, 1e-9)
    fee = max(WITHDRAW_FEE_PCT * batch, WITHDRAW_MIN_RUB)
    if fee >= batch:
        # вся сумма ушла бы в комиссию — считаем по большой партии
        return USDT_RUB_RATE / max(1.0 - WITHDRAW_FEE_PCT, 1e-9)
    return USDT_RUB_RATE * batch / (batch - fee)


def star_cost_rub(stars: int, rate: float | None = None) -> float | None:
    """Себестоимость stars звёзд в рублях по ФАКТИЧЕСКОМУ маршруту оплаты.

    Включает три слагаемых, а не одно:
      * цену товара на Fragment,
      * газ сети,
      * комиссию за вывод денег с FunPay.
    Без третьего слагаемого себестоимость занижена на 6-15%, и бот
    спокойно выставит цену ниже затрат, считая её выгодной.
    """
    rate = get_ton_rate() if rate is None else rate
    if not rate:
        return None
    try:
        method = choose_payment_method(stars)
    except Exception:
        method = FRAGMENT_PAYMENT_METHOD
    if method == "ton":
        return stars_ton_cost(stars) * rate
    goods = stars * usdt_per_star() * usdt_cost_rub()
    return goods + USDT_GAS_COST_TON * rate


def target_price_rub(stars: int, rate: float | None = None,
                     market_best_per_star: float | None = None) -> dict | None:
    """Сколько должен стоить лот на stars звёзд.

    Цена = себестоимость + целевая маржа, но:
      • если рынок ниже — подрезаем конкурента на PRICE_UNDERCUT_RUB;
      • ниже минимальной маржи не падаем НИКОГДА (лучше не продать, чем в минус).
    Цена считается «к получению» (то, что ты ставишь в лоте), комиссию FunPay платит покупатель.
    """
    rate = get_ton_rate() if rate is None else rate
    if not rate:
        return None
    cost = star_cost_rub(stars, rate)
    if cost is None:
        return None
    floor = cost * (1 + PRICE_MIN_MARGIN)
    price = cost * (1 + PRICE_TARGET_MARGIN)
    reason = "целевая маржа"

    if market_best_per_star:
        rival = market_best_per_star * stars - PRICE_UNDERCUT_RUB
        if rival < price:
            if rival >= floor:
                price = rival
                reason = "подрезаем рынок"
            else:
                price = floor
                reason = "рынок ниже нашей минимальной маржи"

    price = max(price, floor)
    if PRICE_MAX_PER_STAR:
        price = min(price, PRICE_MAX_PER_STAR * stars)
    price = round(price + 1e-9, 2)
    return {
        "stars": stars,
        "price": price,
        "cost": cost,
        "floor": floor,
        "margin": (price - cost) / price if price else 0.0,
        "reason": reason,
        "rate": rate,
        "market": market_best_per_star,
    }


# ---------- работа со своими лотами ----------

def own_lots() -> list:
    """Список своих лотов (LotShortcut). Пустой список — если аккаунт ещё не готов."""
    acc = holder.account
    if acc is None:
        return []
    try:
        return list(acc.get_user(acc.id).get_lots())
    except Exception:
        logger.error("Не удалось получить свои лоты", exc_info=True)
        return []


def lot_text(lot) -> str:
    parts = []
    for attr in ("title", "title_ru", "description", "description_ru", "desc"):
        value = getattr(lot, attr, None)
        if value:
            parts.append(str(value))
    return " ".join(parts)


def lot_stars_qty(lot) -> int | None:
    """Сколько звёзд в лоте. None — если это не звёздный лот или число не найдено."""
    text = lot_text(lot)
    low = text.lower()
    if not any(w in low for w in ("звезд", "звёзд", "star", "⭐")):
        return None
    best = None
    for m in re.finditer(r"(\d[\d \u00a0]{0,9})\s*(?:шт|⭐|зв|star)", low):
        value = _parse_int(m.group(1))
        if value and 10 <= value <= MAX_STARS_PER_ORDER:
            best = value if best is None else max(best, value)
    if best is None:
        for m in re.finditer(r"(\d[\d \u00a0]{1,9})", low):
            value = _parse_int(m.group(1))
            if value and 10 <= value <= MAX_STARS_PER_ORDER:
                best = value if best is None else max(best, value)
    return best


def _lot_fields(lot_id):
    """Поля редактирования лота. Разные версии FunPayAPI называют метод по-разному."""
    acc = holder.account
    if acc is None:
        raise RuntimeError("FunPay-аккаунт ещё не авторизован")
    for name in ("get_lot_fields", "get_lot", "get_lot_page"):
        method = getattr(acc, name, None)
        if callable(method):
            return method(int(lot_id))
    raise RuntimeError("В установленной FunPayAPI нет метода чтения полей лота")


def _save_lot_fields(fields) -> None:
    acc = holder.account
    if acc is None:
        raise RuntimeError("FunPay-аккаунт ещё не авторизован")
    renew = getattr(fields, "renew_fields", None)
    if callable(renew):
        try:
            renew()
        except Exception:
            logger.debug("renew_fields() не сработал, сохраняем ��ак есть", exc_info=True)
    for name in ("save_lot", "update_lot", "save_lot_fields"):
        method = getattr(acc, name, None)
        if callable(method):
            method(fields)
            return
    raise RuntimeError("В установленной FunPayAPI нет метода сохранения лота")


def _fields_get_price(fields) -> float | None:
    value = getattr(fields, "price", None)
    if value is None:
        raw = getattr(fields, "fields", None)
        if isinstance(raw, dict):
            value = raw.get("price")
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _fields_set_price(fields, price: float) -> None:
    text = f"{price:.2f}"
    try:
        fields.price = float(text)
        return
    except Exception:
        pass
    raw = getattr(fields, "fields", None)
    if isinstance(raw, dict):
        raw["price"] = text
        return
    raise RuntimeError("Не понял, куда записать цену лота")


def _fields_set_active(fields, active: bool) -> None:
    try:
        fields.active = bool(active)
        return
    except Exception:
        pass
    raw = getattr(fields, "fields", None)
    if isinstance(raw, dict):
        if active:
            raw["active"] = "on"
        else:
            raw.pop("active", None)
        return
    raise RuntimeError("Не понял, как включить/выключить лот")


def set_lot_active(lot_id, active: bool) -> bool:
    """Включить/снять лот с продажи. True — получилось."""
    try:
        fields = _lot_fields(lot_id)
        _fields_set_active(fields, active)
        _save_lot_fields(fields)
        logger.info("Лот %s %s", lot_id, "включён" if active else "снят с продажи")
        return True
    except Exception:
        logger.error("Не удалось %s лот %s", "включить" if active else "снять", lot_id, exc_info=True)
        return False


def set_lot_price(lot_id, price: float) -> bool:
    try:
        fields = _lot_fields(lot_id)
        _fields_set_price(fields, price)
        _save_lot_fields(fields)
        logger.info("Лот %s: новая цена %.2f ₽", lot_id, price)
        return True
    except Exception:
        logger.error("Не удалось поменять цену лота %s", lot_id, exc_info=True)
        return False


def lots_overview() -> list[dict]:
    """Сводка по своим лотам: звёзды, цена, активен ли, хватает ли TON."""
    rate = get_ton_rate()
    balance = get_ton_balance()
    usable = None if balance is None else max(balance - TON_RESERVE, 0.0)
    with control_lock:
        guard = dict(_control["guard_disabled"])

    rows = []
    for lot in own_lots():
        lot_id = str(getattr(lot, "id", "") or "")
        stars = lot_stars_qty(lot)
        price = getattr(lot, "price", None)
        need = stars_ton_cost(stars) if stars else None
        rows.append({
            "id": lot_id,
            "title": (getattr(lot, "title", None) or getattr(lot, "description", None) or "без названия"),
            "stars": stars,
            "price": price,
            "active": bool(getattr(lot, "active", True)),
            "need_ton": need,
            "affordable": None if (need is None or usable is None) else usable >= need,
            "guard_off": lot_id in guard,
            "cost": star_cost_rub(stars, rate) if stars else None,
        })
    return rows


# ---------- автоцены ----------

def plan_prices() -> dict:
    """Считает, какие цены должны быть у своих лотов при текущем курсе TON. Ничего не меняет."""
    rate = get_ton_rate()
    if not rate:
        return {"rate": None, "items": [], "error": "не удалось узнать курс TON"}

    offers = None
    if PRICE_FOLLOW_MARKET:
        try:
            offers, _ = get_market_offers()
        except Exception:
            logger.warning("Автоцены: рынок недоступен, считаем только от себестоимости", exc_info=True)

    items = []
    for lot in own_lots():
        stars = lot_stars_qty(lot)
        if not stars:
            continue
        best = market_best_per_star(stars, offers) if offers else None
        plan = target_price_rub(stars, rate, best)
        if not plan:
            continue
        current = getattr(lot, "price", None)
        try:
            current = float(current) if current is not None else None
        except (TypeError, ValueError):
            current = None
        plan.update({
            "lot_id": str(getattr(lot, "id", "") or ""),
            "title": (getattr(lot, "title", None) or "лот"),
            "current": current,
            "diff": None if current is None else round(plan["price"] - current, 2),
        })
        items.append(plan)

    items.sort(key=lambda x: x["stars"])
    return {"rate": rate, "items": items, "error": None}


def apply_prices(dry_run: bool | None = None) -> dict:
    """Применяет посчитанные цены к лотам. dry_run — только показать, ничего не менять."""
    dry_run = PRICING_DRY_RUN if dry_run is None else dry_run
    plan = plan_prices()
    changed, skipped, failed = [], [], []

    for item in plan["items"]:
        current, price = item["current"], item["price"]
        if current is not None and abs(price - current) < PRICE_STEP_MIN_RUB:
            skipped.append(item)
            continue
        if dry_run:
            changed.append(item)
            continue
        if set_lot_price(item["lot_id"], price):
            changed.append(item)
        else:
            failed.append(item)

    return {"rate": plan["rate"], "error": plan["error"], "dry_run": dry_run,
            "changed": changed, "skipped": skipped, "failed": failed}


def prices_report(result: dict) -> str:
    """Текст для Telegram по результату apply_prices()."""
    if result.get("error"):
        return f"⚠️ Автоцены: {esc(result['error'])}"
    rate = result["rate"]
    head = [
        f"\U0001f4b1 Курс TON: <b>{rate:.2f} ₽</b> ({esc(ton_rate_source())})",
        f"\U0001f4b8 Себестоимость: <b>{stars_ton_cost(1000) * rate / 1000:.4f} ₽/⭐</b> "
        f"(цель маржи {PRICE_TARGET_MARGIN * 100:.0f}%, минимум {PRICE_MIN_MARGIN * 100:.0f}%)",
    ]
    if result["dry_run"]:
        head.append("❗ Режим предпросмотра (PRICING_DRY_RUN = True) — цены НЕ изменены.")
    head.append("")

    if not result["changed"] and not result["failed"]:
        head.append("Все цены уже в порядке — менять нечего.")
        return "\n".join(head)

    verb = "Надо поставить" if result["dry_run"] else "Обновлено"
    head.append(f"<b>{verb}:</b>")
    for item in result["changed"][:20]:
        current = "?" if item["current"] is None else f"{item['current']:.2f}"
        arrow = "→"
        if item["current"] is not None:
            arrow = "\U0001f4c8" if item["price"] > item["current"] else "\U0001f4c9"
        head.append(
            f"  {arrow} {item['stars']}⭐: {current} ₽ → <b>{item['price']:.2f} ₽</b> "
            f"(себест. {item['cost']:.2f} ₽, маржа {item['margin'] * 100:.1f}%, {esc(item['reason'])})"
        )
    if result["failed"]:
        head.append("")
        head.append("⚠️ Не удалось изменить: " + ", ".join(f"{i['stars']}⭐" for i in result["failed"]))
    if result["skipped"]:
        head.append(f"Без изменений: {len(result['skipped'])} лот(ов).")
    return "\n".join(head)


def run_price_updater(main_loop: asyncio.AbstractEventLoop, bot: Bot):
    """Поток: следим за курсом TON и двигаем цены лотов вверх/вниз."""
    time.sleep(120)  # даём мониторингу авторизоваться
    last_rate = None
    while True:
        try:
            if not pricing_enabled() or is_paused():
                time.sleep(60)
                continue
            rate = get_ton_rate(max_age=0)
            if rate is None:
                time.sleep(PRICE_UPDATE_SECONDS)
                continue
            moved = last_rate is None or abs(rate - last_rate) / last_rate >= PRICE_RATE_TRIGGER
            if moved:
                result = apply_prices()
                last_rate = rate
                if result["changed"] or result["failed"]:
                    notify_admin_threadsafe(
                        main_loop, bot,
                        "\U0001f504 <b>Автоцены отработали</b>\n" + prices_report(result),
                    )
        except Exception:
            logger.error("Ошибка в потоке автоцен", exc_info=True)
        time.sleep(PRICE_UPDATE_SECONDS)


# ---------- стоп-лоты при нехватке TON ----------

def lot_need_and_stock(stars: int, ton_balance, usdt_balance):
    """Сколько нужно и сколько есть для лота на stars звёзд.

    Возвращает (нужно, есть, единица) в валюте того маршрута, которым
    бот реально будет платить. Для USDT-маршрута отдельно проверяется газ:
    без TON на газ платёж не уйдёт, даже если USDT полный склад.
    """
    try:
        method = choose_payment_method(stars)
    except Exception:
        method = FRAGMENT_PAYMENT_METHOD
    ton = float(ton_balance or 0.0)
    if method == "ton":
        return stars_ton_cost(stars), max(ton - TON_RESERVE, 0.0), "TON"
    need_usdt = stars * usdt_per_star()
    if ton < USDT_GAS_TON + TON_RESERVE:
        return need_usdt, 0.0, "USDT"  # газа нет — считаем, что выдать не сможем
    return need_usdt, max(float(usdt_balance or 0.0), 0.0), "USDT"


def guard_check_once(main_loop: asyncio.AbstractEventLoop | None = None, bot=None) -> dict:
    """Проверяет баланс TON и снимает/возвращает лоты под текущие деньги.

    Логика простая: если на кошельке не хватит TON, чтобы выдать заказ по лоту —
    лот снимается с продажи, чтобы никто его не купил. Как только баланс пополнился —
    бот сам возвращает именно те лоты, которые снимал сам (ручные не трогает).
    """
    balance = get_ton_balance()
    if balance is None:
        return {"balance": None, "disabled": [], "enabled": [], "error": "не удалось узнать баланс TON"}
    usdt_balance = get_usdt_balance()
    if usdt_balance is None and choose_payment_method(1000) != "ton":
        # НЕ гадаем. Лучше ничего не трогать, чем снять все лоты из-за сбоя API.
        return {"balance": balance, "usdt": None, "disabled": [], "enabled": [],
                "error": "не удалось узнать баланс USDT"}

    usable = max(balance - TON_RESERVE, 0.0)
    disabled, enabled = [], []

    with control_lock:
        guard = dict(_control["guard_disabled"])

    for lot in own_lots():
        lot_id = str(getattr(lot, "id", "") or "")
        stars = lot_stars_qty(lot)
        if not lot_id or not stars:
            continue
        need, usable, unit = lot_need_and_stock(stars, balance, usdt_balance)
        active = bool(getattr(lot, "active", True))

        if usable < need:
            if active:
                if set_lot_active(lot_id, False):
                    guard[lot_id] = stars
                    disabled.append((lot_id, stars, need))
            else:
                guard.setdefault(lot_id, stars)
        elif lot_id in guard and usable >= need * (1 + GUARD_HYSTERESIS):
            if set_lot_active(lot_id, True):
                guard.pop(lot_id, None)
                enabled.append((lot_id, stars, need))

    with control_lock:
        _control["guard_disabled"] = guard
    save_control()

    if (disabled or enabled) and main_loop is not None and bot is not None:
        if choose_payment_method(1000) == "ton":
            covered = int(max(balance - TON_RESERVE - TON_GAS_PER_ORDER, 0.0)
                          / max(live_ton_per_star(), 1e-9))
            stock_line = f"Запас: <b>{balance:.4f} TON</b>"
        else:
            covered = int(max(float(usdt_balance or 0.0), 0.0) / max(usdt_per_star(), 1e-9))
            gas_orders = int(balance / max(USDT_GAS_TON, 1e-9))
            stock_line = (f"Склад: <b>{(usdt_balance or 0.0):.2f} USDT</b> • "
                          f"газ: <b>{balance:.4f} TON</b> (на {gas_orders} заказов)")
        lines = [f"\U0001f48e <b>Контроль запаса</b>",
                 stock_line + f" — хватает примерно на <b>{covered}⭐</b>"]
        if disabled:
            lines.append("⛔ Сняты с продажи: " + ", ".join(f"{s}⭐" for _i, s, _n in disabled))
        if enabled:
            lines.append("✅ Вернул в продажу: " + ", ".join(f"{s}⭐" for _i, s, _n in enabled))
        notify_admin_threadsafe(main_loop, bot, "\n".join(lines))

    return {"balance": balance, "usable": usable, "disabled": disabled, "enabled": enabled, "error": None}


def run_lot_guard(main_loop: asyncio.AbstractEventLoop, bot: Bot):
    """Поток: раз в LOT_GUARD_CHECK_SECONDS сверяем баланс TON с лотами."""
    time.sleep(150)
    while True:
        try:
            if AUTO_LOT_GUARD and not is_paused():
                guard_check_once(main_loop, bot)
        except Exception:
            logger.error("Ошибка в потоке контроля запаса TON", exc_info=True)
        time.sleep(LOT_GUARD_CHECK_SECONDS)


# ---------- пауза ----------

def set_paused(value: bool, touch_lots: bool | None = None) -> dict:
    """Ставит бота на паузу / снимает с паузы.

    На паузе бот не берёт в работу новые заказы и ничего не оплачивает, только шлёт тебе
    уведомления. Заказы не теряются: после снятия с паузы страховочный поток подберёт их.
    Если PAUSE_HIDES_LOTS = True — лоты на время паузы снимаются с продажи.
    """
    touch_lots = PAUSE_HIDES_LOTS if touch_lots is None else touch_lots
    changed_lots = []

    if value:
        with control_lock:
            _control["paused"] = True
            _control["paused_at"] = time.time()
        if touch_lots:
            ids = []
            for lot in own_lots():
                lot_id = str(getattr(lot, "id", "") or "")
                if lot_id and bool(getattr(lot, "active", True)) and set_lot_active(lot_id, False):
                    ids.append(lot_id)
                    changed_lots.append(lot_id)
            with control_lock:
                _control["pause_disabled"] = ids
    else:
        with control_lock:
            _control["paused"] = False
            _control["paused_at"] = None
            ids = list(_control.get("pause_disabled") or [])
            guard = dict(_control["guard_disabled"])
        for lot_id in ids:
            if lot_id in guard:
                continue  # на этот лот всế равно не хватает TON — пусть решает контроль запаса
            if set_lot_active(lot_id, True):
                changed_lots.append(lot_id)
        with control_lock:
            _control["pause_disabled"] = []

    save_control()
    return {"paused": value, "lots": changed_lots}


def pause_text() -> str:
    with control_lock:
        paused, since = _control["paused"], _control["paused_at"]
    if not paused:
        return "\U0001f7e2 Бот работает."
    if since:
        mins = int((time.time() - float(since)) // 60)
        return f"⏸ Бот на паузе (уже {mins} мин). Новые заказы не обрабатываются."
    return "⏸ Бот на паузе. Новые заказы не обрабатываются."



_paused_orders_notified: set[str] = set()  # о каком заказе уже предупредили во время паузы


# ================= АВТОПОДНЯТИЕ ЛОТОВ =================

def run_lot_raiser():
    """Отдельный поток: периодически поднимаем лоты во всех категориях.

    У FunPay свой кулдаун на поднятие — ошибки «ещё рано» это норма, просто ждём.
    """
    time.sleep(90)  # даём мониторингу авторизоваться
    while True:
        acc = holder.account
        if acc is None:
            time.sleep(30)
            continue
        try:
            profile = acc.get_user(acc.id)
            categories = {}
            for lot in profile.get_lots():
                subcat = getattr(lot, "subcategory", None)
                cat = getattr(subcat, "category", None) if subcat else None
                if cat is not None:
                    categories[cat.id] = cat
            for cat_id, cat in categories.items():
                try:
                    acc.raise_lots(cat_id)
                    logger.info("⬆️ Лоты подняты: %s", getattr(cat, "name", cat_id))
                except Exception as e:
                    # чаще всего это кулдаун FunPay («можно поднять через N минут»)
                    logger.debug("Категория %s пока не поднимается: %s", cat_id, e)
        except Exception:
            logger.error("Ошибка автоподнятия лотов", exc_info=True)
        time.sleep(AUTO_RAISE_CHECK_SECONDS)


# ================= RUNNER =================

def run_funpay_runner(main_loop: asyncio.AbstractEventLoop, bot: Bot):
    """Работает в отдельном потоке (все вызовы FunPayAPI синхронные/блокирующие)."""
    logger.info("✅ Мониторинг запущен. Ожидаю оплаты, возвраты и запросы помощи.")

    while True:
        session_started_at = time.time()
        try:
            account_manager = Account(FUNPAY_GOLDEN_KEY)
            account_manager.get(update_phpsessid=True)
            holder.account = account_manager
            logger.info("🔑 Сессия FunPay обновлена (пользователь: %s)", account_manager.username)
            _notify_monitor_recovered(main_loop, bot)

            runner = Runner(account_manager)

            listen_started_at = time.time()
            for event in runner.listen(requests_delay=RUNNER_REQUEST_DELAY, ignore_exceptions=True):
                try:
                    if event.type == EventTypes.NEW_MESSAGE:
                        # Первая пачка событий после запуска — старые сообщения из истории чатов.
                        # Игнорируем их, чтобы не отвечать на написанное до включения бота.
                        if time.time() - listen_started_at < RUNNER_STARTUP_GRACE:
                            logger.info("⏭ Пропускаю старое сообщение (написано до запуска бота)")
                            continue
                        handle_new_message(event.message, main_loop, bot)
                    elif event.type == EventTypes.NEW_ORDER:
                        handle_new_order(event.order, main_loop, bot)
                    elif event.type == getattr(EventTypes, "ORDER_STATUS_CHANGED", None):
                        handle_order_status_changed(event.order, main_loop, bot)
                except Exception:
                    logger.error("Ошибка при обработке события Runner'а", exc_info=True)

                if time.time() - session_started_at > SESSION_REFRESH_SECONDS:
                    logger.info("🔄 Плановое обновление сессии FunPay...")
                    break

        except Exception:
            _notify_monitor_failure(main_loop, bot)
            logger.error("Ошибка в цикле мониторинга FunPay, перезапуск через 20 сек", exc_info=True)
            time.sleep(20)


# ================= СТРАХОВКА ОТ ПОТЕРЯННЫХ ЗАКАЗОВ =================

# Как часто (сек) сверять список продаж FunPay с уже обработанными заказами.
# Если событие «новый заказ» потерялось из-за обрыва сети — этот поток его подхватит.
ORDER_CATCHUP_INTERVAL = 120.0


# Статусы, в которых сделка ещё требует работы бота/админа.
WORKING_STATES = (WAIT_USERNAME, WAIT_CONFIRM, WAIT_ADMIN, PAYING, WAIT_MANUAL)


def deal_counters() -> dict:
    """Счётчики сделок по смыслу, а не просто len(deals)."""
    with deals_lock:
        items = list(deals.values())
    return {
        "working": sum(1 for d in items if d.state in WORKING_STATES),
        "waiting_confirm": sum(1 for d in items if d.state == PAID),
        "frozen": sum(1 for d in items if d.state == FROZEN),
        "total": len(items),
    }


def _close_deal(deal: Deal, reason: str, as_sale: bool):
    with deals_lock:
        current = deals.get(deal.chat_id)
        if current is not deal:
            return False
        deals.pop(deal.chat_id, None)
    if as_sale:
        record_sale(deal)
    save_state()
    logger.info("🧹 Сделка #%s снята с контроля: %s", deal.order_id, reason)
    return True


def reconcile_deals(main_loop: asyncio.AbstractEventLoop | None = None,
                    bot: Bot | None = None, notify: bool = True) -> list[str]:
    """Сличаем свои сделки с реальными заказами FunPay и чистим зависшие.

    Причина зависаний: сделка удалялась только по событию ORDER_STATUS_CHANGED.
    Если бот в этот момент был выключен или событие потерялось, сделка
    оставалась в bot_state.json навсегда и висела в меню как «активная».
    Возвращает список текст��в��х описаний того, что было закрыто.
    """
    with deals_lock:
        items = list(deals.values())
    if not items:
        return []

    statuses: dict[str, object] = {}
    acc = getattr(holder, "account", None)
    if acc is not None:
        get_sells = getattr(acc, "get_sells", None)
        if get_sells is not None:
            try:
                result = get_sells()
                sells = result[1] if isinstance(result, tuple) else result
                for o in sells or []:
                    o_id = str(getattr(o, "id", "") or "")
                    if o_id:
                        statuses[o_id] = getattr(o, "status", None)
            except Exception:
                logger.warning("🧹 Сверка сделок: не удалось получить список продаж", exc_info=True)
                return []

    now = time.time()
    closed: list[str] = []
    for deal in items:
        status = statuses.get(deal.order_id, "unknown")
        age_h = (now - float(deal.created_at or now)) / 3600.0

        # 1) Заказ уже подтверждён покупателем — событие просто не дошло до бота.
        if status == OrderStatuses.CLOSED:
            if _close_deal(deal, "заказ подтверждён на FunPay", as_sale=(deal.state == PAID)):
                closed.append(f"#{deal.order_id} — заказ подтверждён покупателем")
            continue

        # 2) По заказу был возврат.
        if status == OrderStatuses.REFUNDED:
            if _close_deal(deal, "по заказу оформлен возврат", as_sale=False):
                closed.append(f"#{deal.order_id} — оформлен возврат")
            continue

        # 3) Заказа вообще нет в списке продаж (ушёл из активных) — держать нечего.
        if statuses and status == "unknown" and deal.state in (PAID, FROZEN):
            if _close_deal(deal, "заказа больше нет среди активных продаж", as_sale=False):
                closed.append(f"#{deal.order_id} — заказа больше нет среди активных на FunPay")
            continue

        # 4) Страховка по времени: выдали давно, а подтверждения так и нет.
        if deal.state == PAID and age_h >= STALE_PAID_HOURS:
            if _close_deal(deal, "нет подтверждения более %.0f ч" % age_h, as_sale=False):
                closed.append(f"#{deal.order_id} — выдано {age_h:.0f} ч назад, подтверждения нет")
            continue
        if deal.state == FROZEN and age_h >= STALE_FROZEN_HOURS:
            if _close_deal(deal, "заморожена более %.0f ч" % age_h, as_sale=False):
                closed.append(f"#{deal.order_id} — висела замороженной {age_h:.0f} ч")
            continue

    if closed and notify and main_loop is not None and bot is not None:
        notify_admin_threadsafe(
            main_loop, bot,
            "🧹 <b>Автоочистка сделок</b>\n" + esc("\n".join(closed)),
        )
    return closed


def run_order_catchup(main_loop: asyncio.AbstractEventLoop, bot: Bot):
    """Страховка: Runner может потерять событие заказа (обрыв сети, перезапуск).

    Раз в ORDER_CATCHUP_INTERVAL секунд сами запрашиваем список продаж и обрабатываем
    оплаченные заказы, о которых бот не узнал. Найденные ПРИ СТАРТЕ старые оплаченные
    заказы НЕ трогаем (могли быть выданы вручную до запуска) — только сообщаем админу.
    """
    time.sleep(30.0)  # даём Runner'у время поднять сессию
    baseline_done = False
    while True:
        acc = getattr(holder, "account", None)
        if acc is None:
            time.sleep(15.0)
            continue
        try:
            get_sells = getattr(acc, "get_sells", None)
            if get_sells is None:
                logger.warning("🛟 Страховка заказов: метод get_sells недоступен в этой версии FunPayAPI — поток выключен")
                return
            result = get_sells()
            sells = result[1] if isinstance(result, tuple) else result
        except Exception:
            logger.warning("🛟 Страховка заказов: не удалось получить список продаж, повторю позже", exc_info=True)
            time.sleep(ORDER_CATCHUP_INTERVAL)
            continue
        missed = []
        for o in sells or []:
            s_id = str(getattr(o, "id", "") or "")
            if not s_id or s_id in processed_orders:
                continue
            if getattr(o, "status", None) != OrderStatuses.PAID:
                continue
            missed.append((s_id, o))
        if not baseline_done:
            baseline_done = True
            if missed:
                for s_id, _o in missed:
                    processed_orders.add(s_id)
                save_state()
                ids = ", ".join("#" + s_id for s_id, _o in missed)
                notify_admin_threadsafe(
                    main_loop, bot,
                    "👀 При запуск�� нашёл оплаченные заказы, которые бот не обрабатывал: " + esc(ids) + ".\n"
                    "Я их НЕ трогаю (могли быть выданы вручную до запуска) — проверь глазами на FunPay.",
                )
        else:
            for s_id, o in missed:
                logger.warning("🛟 Подхватываю пропущенный заказ #%s (событие потерялось из-за сбоя сети)", s_id)
                notify_admin_threadsafe(
                    main_loop, bot,
                    "🛟 Подхватил пропущенный заказ #" + esc(s_id) + " — событие потерялось из-за обрыва сети. Обрабатываю как новый.",
                )
                try:
                    handle_new_order(o, main_loop, bot)
                except Exception:
                    logger.error("🛟 Ошибка при обработке пропущенного заказа #%s", s_id, exc_info=True)
        # Сверка сделок: закрываем те, что уже подтверждены/возвращены/протухли.
        try:
            reconcile_deals(main_loop, bot)
        except Exception:
            logger.error("🧹 Ошибка сверки сделок", exc_info=True)
        time.sleep(ORDER_CATCHUP_INTERVAL)


# ================= TELEGRAM =================

async def main():
    global admin_outbox
    admin_outbox = asyncio.Queue()
    load_state()
    load_blacklist()
    load_control()
    session = AiohttpSession(proxy=TELEGRAM_PROXY) if TELEGRAM_PROXY else None
    bot = Bot(token=TELEGRAM_TOKEN, session=session)
    dp = Dispatcher()

    def get_deal_for_callback(call: CallbackQuery) -> Deal | None:
        """Достаём сделку по callback_data и проверяем права."""
        if call.from_user is None or call.from_user.id != ADMIN_ID:
            return None  # анти-фрод: кнопки работают только у админа
        try:
            chat_id = int((call.data or "").split(":", 1)[1])
        except (IndexError, ValueError):
            return None
        with deals_lock:
            return deals.get(chat_id)

    # ================= ГЛАВНОЕ МЕНЮ =================
    _summary_cache: dict = {"ts": 0.0, "data": None}

    def collect_summary() -> dict:
        """Собираем данные для главного меню. Блокирующая — только в executor'е."""
        data = {
            "lots_active": None, "lots_total": None, "fp_balance": None,
            "fp_balance_error": "", "fp_username": None, "ton": None, "problems": [],
        }
        acc = holder.account
        if acc is None:
            data["problems"].append("сессия FunPay ещё поднимается")
        else:
            data["fp_username"] = getattr(acc, "username", None)
            try:
                profile = acc.get_user(acc.id)
                lots = list(profile.get_lots() or [])
                data["lots_total"] = len(lots)
                data["lots_active"] = sum(1 for lot in lots if getattr(lot, "active", True))
            except Exception:
                logger.error("Меню: не удалось получить лоты", exc_info=True)
                data["problems"].append("не удалось получить список лотов")
            try:
                balance_text_value, balance_error = fetch_fp_balance()
                data["fp_balance"] = balance_text_value
                data["fp_balance_error"] = balance_error
                if balance_text_value is None:
                    data["problems"].append("не удалось получить баланс FunPay")
            except Exception as e:
                logger.error("Меню: не удалось получить баланс FunPay", exc_info=True)
                data["fp_balance_error"] = f"{type(e).__name__}: {e}"
                data["problems"].append("не удалось получить баланс FunPay")
        try:
            data["ton"] = get_ton_balance()
        except Exception:
            logger.error("Меню: не удалось получить баланс TON", exc_info=True)
        return data

    async def get_summary(force: bool = False) -> dict:
        now = time.time()
        cached = _summary_cache["data"]
        if not force and cached is not None and now - float(_summary_cache["ts"]) < MENU_CACHE_SECONDS:
            return cached
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, collect_summary)
        _summary_cache["ts"] = now
        _summary_cache["data"] = data
        return data

    # Сообщения-вкладки: что бот отправил по кнопке меню и что можно удалить,
    # когда хозяин вернётся в меню. Ключ — chat_id.
    # ВСЁ меню живёт в ОДНОМ сообщении. Любая вкладка не отправляется
    # заново, а перерисовывает то же самое сообщение через edit_text:
    # меню -> выбрал функцию -> она открылась тут же -> назад -> снова меню.
    # _panels нужен только как страховка: если edit_text не сработал и пришлось
    # отправить новое сообщение, его id запомним и удалим при следующем шаге.
    _panels: dict[int, list[int]] = {}

    PANEL_TEXT_LIMIT = 3500

    def back_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="menu:back")
        ]])

    async def clear_panels(chat_id: int):
        """Убрать сообщения-хвосты, если они когда-то появились.

        Ошибки глушим: сообщение могли удалить вручную, а Telegram не даёт
        чистить сообщения старше 48 часов.
        """
        ids = _panels.pop(chat_id, [])
        for mid in ids:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                logger.debug("Не смог удалить сообщение %s", mid, exc_info=True)

    def fit_panel(text: str, limit: int = PANEL_TEXT_LIMIT) -> str:
        """Вкладка должна влезть в одно сообщение (лимит Telegram ~4096).

        Режем по строкам и честно пишем, что обрезали, чтобы не выглядело
        будто отчёт закончился.
        """
        if len(text) <= limit:
            return text
        lines = text.splitlines()
        if len(lines) < 2:
            # нет переводов строк — режем по символам, иначе потеряем весь текст
            return text[:limit] + "\n\n... обрезано: в одно сообщение не влезло."
        out: list[str] = []
        used = 0
        for line in lines:
            if used + len(line) + 1 > limit:
                break
            out.append(line)
            used += len(line) + 1
        shown = len(out)
        out.append("")
        out.append("... показано %d строк из %d — не влезло в одно сообщение." % (shown, len(lines)))
        return "\n".join(out)

    async def render(call, text: str, html: bool = True, pre: bool = False, keyboard=None):
        """Перерисовать ТО ЖЕ сообщение. Новые сообщения не плодим."""
        chat_id = call.message.chat.id
        await clear_panels(chat_id)
        body = fit_panel(text)
        if pre:
            body = "<pre>" + esc(body) + "</pre>"
        mode = ParseMode.HTML if (html or pre) else None
        kb = back_keyboard() if keyboard is None else keyboard
        try:
            await call.message.edit_text(body, parse_mode=mode, reply_markup=kb)
            return
        except Exception as e:
            # "message is not modified" — не ошибка: текст тот же, всё уже на экране
            if "not modified" in str(e).lower():
                return
            logger.debug("edit_text не вышел, отправляю заново", exc_info=True)
        m = await call.message.answer(body, parse_mode=mode, reply_markup=kb)
        _panels[chat_id] = [m.message_id]

    async def render_menu(call):
        """Вернуть главное меню в том же сообщении."""
        text = await build_menu_text(user_title(call.from_user), force=True)
        await render(call, text, keyboard=menu_keyboard())

    async def render_panel(call, worker, note: str, pre: bool = True):
        """Долгая вкладка: сначала показываем ожидание на том же экране,
        потом считаем в потоке и перерисовываем результатом.

        worker — блокирующая функция без аргументов, возвращает готовый текст.
        """
        await render(call, "⏳ " + note)
        loop = asyncio.get_running_loop()
        try:
            report = await loop.run_in_executor(None, worker)
        except Exception as e:
            logger.error("Вкладка не собралась: %s", note, exc_info=True)
            await render(call, "❌ Не получилось: %s: %s" % (type(e).__name__, esc(str(e))))
            return
        await render(call, report, pre=pre)

    def menu_keyboard() -> InlineKeyboardMarkup:
        paused = is_paused()
        pause_button = InlineKeyboardButton(
            text="▶️ Снять с паузы" if paused else "⏸ Пауза",
            callback_data="menu:resume" if paused else "menu:pause",
        )
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Цены конкурентов", callback_data="menu:stars_stats"),
             InlineKeyboardButton(text="🏷 Мои цены", callback_data="menu:prices")],
            [InlineKeyboardButton(text="🛠 Статус", callback_data="menu:status"),
             InlineKeyboardButton(text="📦 Сделки", callback_data="menu:deals")],
            [InlineKeyboardButton(text="📊 Статистика продаж", callback_data="menu:stats"),
             InlineKeyboardButton(text="💎 Балансы", callback_data="menu:balance")],
            [InlineKeyboardButton(text="🛒 Лоты и запас TON", callback_data="menu:lots"),
             InlineKeyboardButton(text="🧪 Fragment-тест", callback_data="menu:fragment_test")],
            [InlineKeyboardButton(text="🧾 Себестоимость", callback_data="menu:cost"),
             InlineKeyboardButton(text="🧭 Чем платить", callback_data="menu:route")],
            [pause_button,
             InlineKeyboardButton(text="🔄 Обновить", callback_data="menu:refresh")],
        ])

    async def build_menu_text(user_title: str, force: bool = False) -> str:
        data = await get_summary(force=force)
        counters = deal_counters()

        if data["lots_active"] is None:
            lots_line = "🛒 Лоты: не удалось узнать"
        elif data["lots_total"] and data["lots_total"] != data["lots_active"]:
            lots_line = f"🛒 Активных лотов: <b>{data['lots_active']}</b> из {data['lots_total']}"
        else:
            lots_line = f"🛒 Активных лотов: <b>{data['lots_active']}</b>"

        fp_line = f"💰 Баланс FunPay: <b>{esc(data['fp_balance'])}</b>" if data["fp_balance"] \
            else "💰 Баланс FunPay: не удалось узнать"

        _loop0 = asyncio.get_running_loop()
        usdt_bal = await _loop0.run_in_executor(None, get_usdt_balance)
        if usdt_bal is None:
            usdt_line = "💵 Склад USDT: не удалось узнать"
        else:
            _left = int(usdt_bal / max(usdt_per_star(), 1e-9))
            usdt_line = f"💵 Склад USDT: <b>{usdt_bal:.2f}</b> — хватит на <b>{_left}⭐</b>"
        if data["ton"] is None:
            ton_line = "⛽ Газ TON: не удалось узнать"
        else:
            warn = " ⚠️ мало, пополни!" if data["ton"] < LOW_BALANCE_TON else ""
            _ord = int(data["ton"] / max(USDT_GAS_TON, 1e-9))
            ton_line = f"⛽ Газ TON: <b>{data['ton']:.4f}</b> — на <b>{_ord}</b> заказов{warn}"
        ton_line = usdt_line + "\n" + ton_line

        loop = asyncio.get_running_loop()
        rate = await loop.run_in_executor(None, get_ton_rate)
        if rate:
            _c1000 = star_cost_rub(1000, rate)
            per_star = (_c1000 / 1000.0) if _c1000 else 0.0
            rate_line = (
                f"💱 Курс TON: <b>{rate:.2f} ₽</b> • себестоимость <b>{per_star:.4f} ₽/⭐</b> • "
                f"автоцены: <b>{'вкл' if pricing_enabled() else 'выкл'}</b>"
                + (" (только предпросмотр)" if PRICING_DRY_RUN else "")
            )
        else:
            rate_line = "💱 Курс TON: не удалось узнать"

        lines = [
            f"👋 Здравствуйте, <b>{esc(user_title)}</b>!",
            ("⏸ <b>Бот на паузе</b> — новые заказы не обрабатываются."
             if is_paused() else "🟢 Бот работает.") + (
                f" Аккаунт FunPay: <b>{esc(data['fp_username'])}</b>" if data["fp_username"] else ""
            ),
            "",
            lots_line,
            fp_line,
            ton_line,
            rate_line,
            "",
            f"📁 Сделок в работе: <b>{counters['working']}</b>"
            + (f" • ждут подтверждения: <b>{counters['waiting_confirm']}</b>"
               if counters["waiting_confirm"] else "")
            + (f" • заморожено: <b>{counters['frozen']}</b>" if counters["frozen"] else "")
            + f" • обработано заказов: <b>{len(processed_orders)}</b>",
            f"⚡ Автооплата Fragment: <b>{'вкл' if AUTO_PAY_FRAGMENT else 'выкл'}</b> • "
            f"автовозврат !возврат: <b>{'вкл' if AUTO_REFUND_ENABLED else 'выкл'}</b>",
        ]
        if data["problems"]:
            lines.append("")
            lines.append("⚠️ " + esc("; ".join(data["problems"])))
        lines.append("")
        lines.append(f"🕒 Обновлено: {time.strftime('%H:%M:%S')}")
        return "\n".join(lines)

    def user_title(user) -> str:
        if user is None:
            return "хозяин"
        if getattr(user, "username", None):
            return "@" + user.username
        return getattr(user, "full_name", None) or str(getattr(user, "id", ""))

    # ---------- тексты для команд (используются и командами, и кнопками меню) ----------

    def status_text() -> str:
        counters = deal_counters()
        return (
            "🛠 <b>Статус</b>\n"
            f"Обработано заказов: {len(processed_orders)}\n"
            f"Сделок в работе: {counters['working']} "
            f"(ждут подтверждения: {counters['waiting_confirm']}, "
            f"заморожено: {counters['frozen']})\n"
            f"Мониторинг FunPay: {'⚠️ падает' if _monitor_fail_state['failing'] else '✅ в норме'}\n"
            f"Автоподнятие лотов: {'вкл' if AUTO_RAISE_LOTS else 'выкл'}"
        )

    def deals_text() -> str:
        with deals_lock:
            items = list(deals.values())
        if not items:
            return "Активных сделок нет."
        now = time.time()
        lines = []
        for d in items:
            age_h = (now - float(d.created_at or now)) / 3600.0
            age = f"{age_h:.1f} ч" if age_h < 48 else f"{age_h / 24:.0f} д"
            hint = " — ждём подтверждения покупателя" if d.state == PAID else ""
            lines.append(
                f"#{esc(d.order_id)} | {esc(d.buyer)} | {esc(d.stars)}⭐ | "
                f"@{esc(d.username or '—')} | {d.state} | {age}{hint}"
            )
        lines.append("")
        lines.append("Зависшие можно снять кома��дой /cleanup")
        return "\n".join(lines)

    def stats_text() -> str:
        sales = load_stats()
        now = time.time()

        def bucket(seconds: int | None) -> str:
            rows = [s for s in sales if seconds is None or now - float(s.get("ts", 0)) <= seconds]
            stars_sum = sum(int(s.get("stars") or 0) for s in rows)
            revenue: dict[str, float] = {}
            for s in rows:
                m = re.match(r"([\d.,]+)\s*(\S*)", str(s.get("price") or ""))
                if not m:
                    continue
                try:
                    amount = float(m.group(1).replace(",", "."))
                except ValueError:
                    continue
                cur = m.group(2) or "?"
                revenue[cur] = revenue.get(cur, 0.0) + amount
            rev_text = ", ".join(f"{v:.2f} {k}" for k, v in revenue.items()) or "—"
            return f"{len(rows)} зак., {stars_sum}⭐, {rev_text}"

        return (
            "📊 Статистика (только подтверждённые покупателем заказы):\n"
            f"За 24 часа: {bucket(24 * 3600)}\n"
            f"За 7 дней: {bucket(7 * 24 * 3600)}\n"
            f"За 30 дней: {bucket(30 * 24 * 3600)}\n"
            f"За всё время: {bucket(None)}"
        )

    async def balance_text() -> str:
        data = await get_summary(force=True)
        lines = ["💳 <b>Балансы</b>"]
        if data["fp_balance"]:
            lines.append(f"FunPay: {esc(data['fp_balance'])}")
        else:
            lines.append("FunPay: не удалось узнать")
            if data.get("fp_balance_error"):
                lines.append(f"Причина: <code>{esc(data['fp_balance_error'])[:500]}</code>")
        if data["ton"] is None:
            lines.append("Кошелёк TON: не удалось узнать (смотри bot_debug.txt)")
        else:
            warn = " ⚠️ мало, пополни!" if data["ton"] < LOW_BALANCE_TON else ""
            lines.append(f"Кошелёк TON: {data['ton']:.4f} TON{warn}")
        return "\n".join(lines)

    async def send_stars_report(target: Message):
        await target.answer("🔍 Собираю цены конкурентов на FunPay, подожди несколько секунд...")
        loop = asyncio.get_running_loop()
        try:
            report = await loop.run_in_executor(None, fetch_stars_market)
        except Exception as e:
            logger.error("Не удалось собрать цены конкурентов", exc_info=True)
            await target.answer(f"❌ Не получилось собрать цены: {type(e).__name__}: {e}")
            return
        head = "\n".join(report.splitlines()[:6])
        doc = BufferedInputFile(report.encode("utf-8"), filename="stars_market.txt")
        await target.answer_document(
            doc,
            caption="⭐ Цены конкурентов на з��ёзды (FunPay)\n\n" + head[:800],
        )

    def build_lots_report() -> str:
        """Сводка по своим лотам: хватает ли TON на каждый из них. Блокирующая."""
        rows = lots_overview()
        balance = get_ton_balance()
        rate = get_ton_rate()
        lines = []
        if balance is None:
            lines.append("💎 Баланс TON: не удалось узнать")
        else:
            usable = max(balance - TON_RESERVE, 0.0)
            covered = int(max(usable - TON_GAS_PER_ORDER, 0) / FRAGMENT_TON_PER_STAR)
            lines.append(
                f"💎 Баланс TON: <b>{balance:.4f}</b> "
                f"(рабочий остаток {usable:.4f}) — хватает на <b>{covered}⭐</b>"
            )
        if rate:
            lines.append(f"💱 Курс TON: <b>{rate:.2f} ₽</b> ({esc(ton_rate_source())})")
        lines.append("")

        if not rows:
            lines.append("Лоты не найдены (или FunPay ещё не ответил).")
            return "\n".join(lines)

        for row in rows:
            if row["active"]:
                mark = "✅"
            elif row["guard_off"]:
                mark = "⛔"
            else:
                mark = "💤"
            stars = f"{row['stars']}⭐" if row["stars"] else "?⭐"
            price = f"{float(row['price']):.2f} ₽" if row["price"] not in (None, "") else "—"
            extra = ""
            if row["need_ton"]:
                extra = f" • нужно {row['need_ton']:.3f} TON"
            if row["cost"]:
                extra += f" • себест. {row['cost']:.2f} ₽"
            if row["affordable"] is False:
                extra += " • <b>денег не хватает</b>"
            lines.append(f"{mark} {stars} — {price}{extra}")

        lines.append("")
        lines.append("✅ в продаже • ⛔ снят ботом из-за нехватки TON • 💤 снят вручную")
        return "\n".join(lines)

    async def send_prices_report(target: Message, apply_now: bool = False):
        await target.answer("💱 Смотрю курс TON и считаю цены...")
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, apply_prices, False if apply_now else None)
        except Exception as e:
            logger.error("Ошибка расчёта цен", exc_info=True)
            await target.answer(f"❌ Не получилось посчитать цены: {type(e).__name__}: {e}")
            return
        await target.answer(prices_report(result), parse_mode=ParseMode.HTML)
        if result["dry_run"] and result["changed"]:
            await target.answer(
                "Чтобы бот реально поменял цены прямо сейчас — <code>/prices apply</code>.\n"
                "Если всё понравилось, поставь в настройках <code>PRICING_DRY_RUN = False</code>.",
                parse_mode=ParseMode.HTML,
            )

    async def send_lots_report(target: Message):
        loop = asyncio.get_running_loop()
        try:
            report = await loop.run_in_executor(None, build_lots_report)
        except Exception as e:
            logger.error("Ошибка сводки по лотам", exc_info=True)
            await target.answer(f"❌ Не получилось собрать лоты: {type(e).__name__}: {e}")
            return
        await target.answer(report, parse_mode=ParseMode.HTML)

    # ---------- команды ----------

    @dp.message(Command("start"))
    @dp.message(Command("menu"))
    async def cmd_menu(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        await clear_panels(message.chat.id)
        text = await build_menu_text(user_title(message.from_user))
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=menu_keyboard())

    @dp.callback_query(F.data.startswith("menu:"))
    async def cb_menu(call: CallbackQuery):
        if call.from_user is None or call.from_user.id != ADMIN_ID:
            await call.answer()
            return
        action = (call.data or "").split(":", 1)[1]

        if action in ("back", "refresh"):
            await call.answer("Обновляю..." if action == "refresh" else "")
            await render_menu(call)
            return

        if action == "status":
            await call.answer()
            await render(call, status_text())
            return

        if action == "deals":
            await call.answer()
            await render(call, deals_text())
            return

        if action == "stats":
            await call.answer()
            await render(call, stats_text(), html=False)
            return

        if action == "balance":
            await call.answer("Смотрю балансы...")
            await render(call, "⏳ Смотрю балансы...")
            await render(call, await balance_text())
            return

        if action == "cost":
            await call.answer("Считаю себестоимость...")
            await render_panel(call, lambda: cost_table(None), "Считаю себестоимость...")
            return

        if action == "route":
            await call.answer("Сравниваю пути оплаты...")
            await render_panel(call, lambda: payment_route_table(None), "Сравниваю пути оплаты...")
            return

        if action == "lots":
            await call.answer("Смотрю лоты...")
            await render_panel(call, build_lots_report, "Смотрю лоты и запас...", pre=False)
            return

        if action == "stars_stats":
            await call.answer("Собираю цены...")
            # таблица дли��ная: в экран влезет начало, целиком файлом — /stars_stats
            def _stars_panel():
                report = fetch_stars_market()
                return report + "\n\nВесь отчёт файлом: /stars_stats"

            await render_panel(call, _stars_panel, "Собираю цены конкурентов на FunPay...")
            return

        if action == "prices":
            await call.answer("Считаю цены...")

            def _prices_panel():
                result = apply_prices(None)
                text = prices_report(result)
                if result["dry_run"] and result["changed"]:
                    text += ("\n\nПрименить прямо сейчас: <code>/prices apply</code>\n"
                             "Всегда применять: <code>PRICING_DRY_RUN = False</code>")
                return text

            await render_panel(call, _prices_panel, "Смотрю курс и считаю цены...", pre=False)
            return

        if action in ("pause", "resume"):
            want_pause = action == "pause"
            await call.answer("Ставлю на паузу..." if want_pause else "Возвращаю в работу...")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, set_paused, want_pause)
            # состояние паузы видно в самом меню — отдельное сообщение не нужно
            await render_menu(call)
            return

        if action == "fragment_test":
            await call.answer()
            await render(
                call,
                "🧪 <b>Fragment-тест</b>\n\n"
                "Напиши <code>/fragment_test @username</code> — проверю доступ к Fragment "
                "без оплаты (деньги не спишутся).",
            )
            return

        await call.answer()

    @dp.message(Command("status"))
    async def cmd_status(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        await message.answer(status_text(), parse_mode=ParseMode.HTML)

    @dp.message(Command("deals"))
    async def cmd_deals(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        await message.answer(deals_text(), parse_mode=ParseMode.HTML)

    @dp.message(Command("cleanup"))
    async def cmd_cleanup(message: Message):
        """Ручная сверка сделок с FunPay и чистка зависших."""
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        loop = asyncio.get_running_loop()
        closed = await loop.run_in_executor(None, reconcile_deals, None, None, False)
        if not closed:
            await message.answer("✅ Зависших сделок нет — всё совпадает с FunPay.")
        else:
            await message.answer(
                "🧹 <b>Снято с контроля</b>\n" + esc("\n".join(closed)),
                parse_mode=ParseMode.HTML,
            )
        _summary_cache["ts"] = 0.0
        await message.answer(await build_menu_text(user_title(message.from_user), force=True),
                             parse_mode=ParseMode.HTML, reply_markup=menu_keyboard())

    @dp.message(Command("stats"))
    async def cmd_stats(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        await message.answer(stats_text())

    @dp.message(Command("balance"))
    async def cmd_balance(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        await message.answer(await balance_text(), parse_mode=ParseMode.HTML)

    @dp.message(Command("refund"))
    async def cmd_refund(message: Message):
        """Ручной возврат из Telegram: /refund номер_заказа"""
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        parts = (message.text or "").split()
        order_id = parts[1].lstrip("#").strip() if len(parts) > 1 else ""
        if not order_id:
            await message.answer("Использование: /refund номер_заказа")
            return
        loop = asyncio.get_running_loop()
        ok, err = await loop.run_in_executor(None, funpay_refund, order_id)
        if not ok:
            await message.answer(f"❌ Возврат по заказу #{esc(order_id)} не прошёл: {esc(err)}",
                                 parse_mode=ParseMode.HTML)
            return
        with deals_lock:
            found = next((d for d in deals.values() if d.order_id == order_id), None)
            if found is not None:
                deals.pop(found.chat_id, None)
        save_state()
        if found is not None:
            await loop.run_in_executor(None, fp_send, found.chat_id, MSG_REFUND_DONE, True)
        await message.answer(f"↩️ Возврат по заказу #{esc(order_id)} оформлен.",
                             parse_mode=ParseMode.HTML)

    @dp.message(Command("fragment_test"))
    async def cmd_fragment_test(message: Message):
        """Проверка доступа к Fragment БЕЗ оплаты: /fragment_test @username"""
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        parts = (message.text or "").split()
        username = extract_username(parts[1]) if len(parts) > 1 else None
        if not username:
            await message.answer(
                "Использование: /fragment_test @username\n"
                "Проверю доступ к Fragment без оплаты — деньги не спишутся."
            )
            return
        await message.answer(f"🔍 Проверяю Fragment на @{esc(username)} (50⭐, без оплаты)...")
        loop = asyncio.get_running_loop()
        try:
            recipient, name = await loop.run_in_executor(None, fragment_check_recipient, username, 50)
        except Exception as e:
            await message.answer(
                f"❌ Шаг 1 (поиск получателя): {esc(str(e))}\n"
                "Этот шаг работает даже без логина — если он упал, проблема с сетью или hash."
            )
            return
        try:
            msg_tx, req_id = await loop.run_in_executor(None, fragment_build_transaction, recipient, 50)
        except Exception as e:
            await message.answer(
                f"✅ Шаг 1: получатель найден ({esc(name)}).\n"
                f"❌ Шаг 2 (создание заявки): {esc(str(e))}\n\n"
                "Я уже попробовал автоматически обновить api-hash со страницы и повторить.\n"
                "Если всё равно Access denied:\n"
                "1) куки протухли — перелогинься на fragment.com и обнови FRAGMENT_COOKIES;\n"
                "2) бот и браузер выходят с разных IP — проверь VPN/FRAGMENT_PROXY;\n"
                "3) на fragment.com не подключён TON-кошелёк."
            )
            return
        amount_ton = int(msg_tx.get("amount", 0)) / 1_000_000_000
        await message.answer(
            f"✅ Fragment полностью работает!\n"
            f"Получатель: {esc(name)} (@{esc(username)})\n"
            f"Заявка на 50⭐ создана (req_id {esc(req_id)}), цена: {amount_ton:.4f} TON.\n"
            f"Оплату я НЕ отправлял — это только проверка доступа."
        )

    @dp.message(Command("route"))
    async def cmd_route(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        loop = asyncio.get_running_loop()
        try:
            report = await loop.run_in_executor(None, payment_route_table, None)
        except Exception as e:
            await message.answer(f"❌ Не смог сравнить пути оплаты: {esc(str(e))}")
            return
        await message.answer(f"<pre>{esc(report)}</pre>")

    @dp.message(Command("cost"))
    async def cmd_cost(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        loop = asyncio.get_running_loop()
        try:
            report = await loop.run_in_executor(None, cost_table, None)
        except Exception as e:
            await message.answer(f"❌ Не смог посчитать себестоимость: {esc(str(e))}")
            return
        await message.answer(f"<pre>{esc(report)}</pre>")

    @dp.message(Command("pay_methods"))
    async def cmd_pay_methods(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        parts = (message.text or "").split()
        username = extract_username(parts[1]) if len(parts) > 1 else None
        if not username:
            username = (FRAGMENT_PROBE_USERNAME or "").lstrip("@").strip() or None
        if not username:
            await message.answer(
                "Использование: /pay_methods @username\n"
                "Нужен реальный пользователь Telegram — оплата не отправляется."
            )
            return
        await message.answer(
            f"🔍 Перебираю способы оплаты на Fragment (получатель @{esc(username)}).\n"
            "Заявки только создаются, деньги не уходят. Может занять минуту..."
        )
        loop = asyncio.get_running_loop()
        try:
            report = await loop.run_in_executor(
                None, fragment_probe_payment_methods, 50, username
            )
        except Exception as e:
            await message.answer(f"❌ Зонд упал: {esc(str(e))}")
            return
        await message.answer(f"<pre>{esc(report)}</pre>")

    @dp.message(Command("fragment_debug"))
    async def cmd_fragment_debug(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        loop = asyncio.get_running_loop()

        def _collect() -> str:
            lines = []
            try:
                ip = requests.get(
                    "https://api.ipify.org", timeout=15, proxies=_fragment_proxies()
                ).text.strip()
                lines.append(f"IP бота: {ip}")
            except Exception as e:
                lines.append(f"IP бота: не удалось узнать ({e})")
            try:
                headers = {
                    "Cookie": FRAGMENT_COOKIES,
                    "User-Agent": FRAGMENT_UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3",
                }
                resp = requests.get(
                    "https://fragment.com/stars/buy",
                    headers=headers,
                    timeout=30,
                    proxies=_fragment_proxies(),
                )
                text = resp.text
                low = text.lower()
                m = re.search(r"api\?hash=([a-fA-F0-9]+)", text)
                page_hash = m.group(1) if m else "н�� найден"
                lines.append(f"Страница /stars/buy: HTTP {resp.status_code}, {len(text)} байт")
                lines.append(f"hash со страницы: {page_hash}")
                lines.append(f"hash в конфиге: {FRAGMENT_HASH}")
                lines.append(
                    "Кнопка выхода на странице (= залогинен): "
                    + ("ДА" if "logout" in low else "НЕТ")
                )
                lines.append(
                    "Кнопка Connect Telegram (= НЕ залогинен): "
                    + ("ДА" if "connect telegram" in low else "НЕТ")
                )
                if "just a moment" in low or "cf-chl" in low:
                    lines.append("⚠️ Похоже, вместо страницы пришла проверка Cloudflare")
            except Exception as e:
                lines.append(f"Страница не загрузилась: {e}")
            return "\n".join(lines)

        report = await loop.run_in_executor(None, _collect)
        await message.answer(
            "🔎 Диагностика Fragment:\n" + esc(report), parse_mode=ParseMode.HTML
        )

    @dp.message(Command("stars_stats"))
    async def cmd_stars_stats(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        await send_stars_report(message)

    @dp.message(Command("pause"))
    async def cmd_pause(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        if is_paused():
            await message.answer(pause_text())
            return
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, set_paused, True)
        extra = f" Снято с продажи лотов: {len(result['lots'])}." if result["lots"] else ""
        await message.answer(
            "⏸ <b>Бот на паузе.</b>" + extra + "\n"
            "Новые заказы не обрабатываются и ничего не оплачивается — я просто буду писать тебе.\n"
            "Заказы не потеряются: после <code>/resume</code> бот подберёт их сам.",
            parse_mode=ParseMode.HTML,
        )

    @dp.message(Command("resume"))
    async def cmd_resume(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        if not is_paused():
            await message.answer("🟢 Бот и так работает.")
            return
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, set_paused, False)
        extra = f" Вернул в продажу лотов: {len(result['lots'])}." if result["lots"] else ""
        await message.answer(
            "▶️ <b>Бот снова в работе.</b>" + extra + "\n"
            "Пропущенные оплаченные заказы бот подберёт в течение пары минут.",
            parse_mode=ParseMode.HTML,
        )

    @dp.message(Command("prices"))
    async def cmd_prices(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        parts = (message.text or "").split()
        apply_now = len(parts) > 1 and parts[1].strip().lower() in ("apply", "применить", "go", "yes")
        await send_prices_report(message, apply_now=apply_now)

    @dp.message(Command("rate"))
    async def cmd_rate(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        loop = asyncio.get_running_loop()
        rate = await loop.run_in_executor(None, get_ton_rate, 0)
        if not rate:
            await message.answer("❌ Курс TON узнать не удалось (ни tonapi, ни coingecko).")
            return
        lines = [
            f"💱 <b>Кур�� TON: {rate:.2f} ₽</b> ({esc(ton_rate_source())})",
            f"💸 Себестоимость: <b>{stars_ton_cost(1000) * rate / 1000:.4f} ₽/⭐</b>",
            "",
        ]
        for pack in (50, 100, 500, 1000, 2500):
            plan = target_price_rub(pack, rate)
            if plan:
                lines.append(
                    f"{pack}⭐: себест. {plan['cost']:.2f} ₽ → цена по цели <b>{plan['price']:.2f} ₽</b> "
                    f"(минимум {plan['floor']:.2f} ₽)"
                )
        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

    @dp.message(Command("lots"))
    async def cmd_lots(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        await send_lots_report(message)

    @dp.message(Command("ton_check"))
    async def cmd_ton_check(message: Message):
        """Ручная проверка запаса TON со снятием/возвратом лотов."""
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        await message.answer("💎 Проверяю запас TON и лоты...")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, guard_check_once, None, None)
        if result.get("error"):
            await message.answer(f"⚠️ {esc(result['error'])}", parse_mode=ParseMode.HTML)
            return
        parts = [f"💎 Баланс: <b>{result['balance']:.4f} TON</b>"]
        if result["disabled"]:
            parts.append("⛔ Снято: " + ", ".join(f"{s}⭐" for _i, s, _n in result["disabled"]))
        if result["enabled"]:
            parts.append("✅ Вернул: " + ", ".join(f"{s}⭐" for _i, s, _n in result["enabled"]))
        if not result["disabled"] and not result["enabled"]:
            parts.append("Изменений нет — всё в порядке.")
        await message.answer("\n".join(parts), parse_mode=ParseMode.HTML)

    @dp.message(Command("autoprice"))
    async def cmd_autoprice(message: Message):
        """Вкл/выкл автоцен без перезапуска бота."""
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        parts = (message.text or "").split()
        if len(parts) < 2 or parts[1].lower() not in ("on", "off", "вкл", "выкл"):
            await message.answer(
                f"Сейчас автоцены: <b>{'вкл' if pricing_enabled() else 'выкл'}</b>. "
                f"Использование: <code>/autoprice on</code> или <code>/autoprice off</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        value = parts[1].lower() in ("on", "вкл")
        with control_lock:
            _control["pricing"] = value
        save_control()
        await message.answer(f"🏷 Автоцены: <b>{'вкл' if value else 'выкл'}</b>.", parse_mode=ParseMode.HTML)

    @dp.message(Command("stars_debug"))
    async def cmd_stars_debug(message: Message):
        """Как бот разложил лоты конкурентов по способам выдачи."""
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        await message.answer("🔎 Разбираю лоты по способам выдачи...")
        loop = asyncio.get_running_loop()
        try:
            report = await loop.run_in_executor(None, stars_method_debug)
        except Exception as e:
            await message.answer(f"❌ Не получилось: {type(e).__name__}: {e}")
            return
        doc = BufferedInputFile(report.encode("utf-8"), filename="stars_methods.txt")
        await message.answer_document(doc, caption="⭐ Разбор лотов по способам выдачи")

    @dp.message(Command("ban"))
    async def cmd_ban(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.answer("Использование: /ban ник_на_funpay")
            return
        nick = parts[1].strip().lower()
        blacklist.add(nick)
        save_blacklist()
        await message.answer(
            f"🚫 {nick} в чёрном списке: бот игнорирует его сообщения, "
            f"а о его заказах только сообщает тебе (без автообработки)."
        )

    @dp.message(Command("unban"))
    async def cmd_unban(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.answer("Использование: /unban ник_на_funpay")
            return
        nick = parts[1].strip().lower()
        if nick in blacklist:
            blacklist.discard(nick)
            save_blacklist()
            await message.answer(f"✅ {nick} убран из чёрного списка.")
        else:
            await message.answer(f"{nick} и так не в чёрном списке.")

    @dp.message(Command("banlist"))
    async def cmd_banlist(message: Message):
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return
        if not blacklist:
            await message.answer("Чёрный список пуст.")
            return
        await message.answer("🚫 Чёрный список:\n" + "\n".join(sorted(blacklist)))

    async def delete_call_message(call: CallbackQuery):
        """Удаляем сообщение с кнопками после нажатия, чтобы не засорять чат."""
        try:
            await call.message.delete()
        except Exception:
            # если удалить нельзя (сообщению больше 48ч) — хотя бы убираем кнопки
            try:
                await call.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

    def pay_retry_keyboard(deal: Deal) -> InlineKeyboardMarkup:
        """Кнопки для случая, когда оплата оборвалась на неясном шаге."""
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Проверил — оплатить заново", callback_data=f"payforce:{deal.chat_id}")],
            [InlineKeyboardButton(text="✅ Звёзды дошли — закрыть", callback_data=f"paidok:{deal.chat_id}")],
            [InlineKeyboardButton(text="🖐 Оплачу вручную", callback_data=f"manual:{deal.chat_id}")],
            [InlineKeyboardButton(text="🔗 ЧАТ", url=f"https://funpay.com/chat/?node={deal.chat_id}")],
        ])

    async def run_payment(deal: Deal, call: CallbackQuery):
        """Общая логика автооплаты для кнопок «ДА» и «Проверил — оплатить заново»."""
        try:
            tx_hash = await fragment_pay(deal)
        except Exception as e:
            # ГЛАВНОЕ: если ошибка случилась после того, как транзакция ушла в сеть,
            # НЕЛЬЗЯ просто вернуть сделку в WAIT_ADMIN: повторное «ДА» могло бы
            # отправить вторую TON-транзакцию и оплатить заказ дважды.
            unsafe = deal.pay_stage in ("sending", "sent")
            with deals_lock:
                deal.state = FROZEN if unsafe else WAIT_ADMIN
            save_state()
            logger.error("Автооплата не удалась (шаг %s)", deal.pay_stage, exc_info=True)
            if unsafe:
                await call.message.answer(
                    f"⚠️ <b>Оплата заказа #{esc(deal.order_id)} оборвалась на опасном шаге</b>\n"
                    f"<code>{esc(e)}</code>\n\n"
                    f"Шаг: <code>{esc(deal.pay_stage)}</code>, заявка Fragment: <code>{esc(deal.pay_req_id)}</code>\n"
                    f"Транзакция МОГЛА уйти в сеть, просто ответ до бота не дошёл.\n\n"
                    f"<b>Прежде чем повторять</b> — пров��рь историю TON-кошелька и fragment.com: "
                    f"если списание уже было, повторная оплата спишет деньги ВТОРОЙ раз.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=pay_retry_keyboard(deal),
                )
            else:
                await call.message.answer(
                    f"❌ Автооплата по заказу #{esc(deal.order_id)} не удалась:\n<code>{esc(e)}</code>\n"
                    f"Деньги НЕ списаны (сбой на шаге <code>{esc(deal.pay_stage)}</code>) — можно повторить или оплатить вручную.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_admin_confirm(deal)[1],
                )
            return
        with deals_lock:
            deal.state = PAID
        save_state()
        goods = f"{esc(deal.stars)}⭐" if deal.product == "stars" else f"Premium {esc(deal.months)} мес"
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, fp_send, deal.chat_id, MSG_PAID, True)
        await delete_call_message(call)
        await call.message.answer(
            f"✅ Заказ #{esc(deal.order_id)} оплачен на Fragment!\n"
            f"@{esc(deal.username)} +{goods}\nTX: <code>{esc(tx_hash)}</code>\n"
            f"Покупатель уведомлён, ждём подтверждения заказа.",
            parse_mode=ParseMode.HTML,
        )

    @dp.callback_query(F.data.startswith("pay:"))
    async def cb_pay(call: CallbackQuery):
        deal = get_deal_for_callback(call)
        if deal is None:
            await call.answer("Сделка не найдена (возможно, уже закрыта).", show_alert=True)
            return
        if not AUTO_PAY_FRAGMENT:
            await call.answer(
                "Автооплата выключена (AUTO_PAY_FRAGMENT = False). Используй «Оплачу вручную».",
                show_alert=True,
            )
            return
        if (deal.product == "stars" and deal.stars is None) or (deal.product == "premium" and deal.months is None):
            await call.answer("Не определено количество/срок товара — только вручную.", show_alert=True)
            return
        # Идемпотентность: оплату можно запустить строго один раз.
        with deals_lock:
            if deal.state != WAIT_ADMIN:
                await call.answer(f"Уже в статусе {deal.state}, повторная оплата невозможна.", show_alert=True)
                return
            deal.state = PAYING
            deal.pay_stage = "start"
        save_state()
        await call.answer("Запускаю оплату...")
        await run_payment(deal, call)

    @dp.callback_query(F.data.startswith("payforce:"))
    async def cb_pay_force(call: CallbackQuery):
        """Пов��орная оплата после обрыва — только осознанно, через отдельную кнопку."""
        deal = get_deal_for_callback(call)
        if deal is None:
            await call.answer("Сделка не найдена (возможно, уже закрыта).", show_alert=True)
            return
        with deals_lock:
            if deal.state not in (FROZEN, WAIT_ADMIN):
                await call.answer(f"Уже в статусе {deal.state}.", show_alert=True)
                return
            deal.state = PAYING
            deal.pay_stage = "start"
            deal.pay_req_id = None
        save_state()
        await call.answer("Повторяю оплату...")
        await delete_call_message(call)
        await run_payment(deal, call)

    @dp.callback_query(F.data.startswith("paidok:"))
    async def cb_paid_ok(call: CallbackQuery):
        """Админ проверил блокчейн/Fragment: звёзды всё-таки дошли."""
        deal = get_deal_for_callback(call)
        if deal is None:
            await call.answer("Сделка не найдена (возможно, уже закрыта).", show_alert=True)
            return
        with deals_lock:
            deal.state = PAID
        save_state()
        await call.answer()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, fp_send, deal.chat_id, MSG_PAID, True)
        await delete_call_message(call)
        await call.message.answer(
            f"✅ Заказ #{esc(deal.order_id)} отмечен оплаченным, покупатель уведомлён.",
            parse_mode=ParseMode.HTML,
        )

    @dp.callback_query(F.data.startswith("manual:"))
    async def cb_manual(call: CallbackQuery):
        deal = get_deal_for_callback(call)
        if deal is None:
            await call.answer("Сделка не найдена (возможно, уже закрыта).", show_alert=True)
            return
        with deals_lock:
            if deal.state not in (WAIT_ADMIN, PAYING):
                await call.answer(f"Уже в статусе {deal.state}.", show_alert=True)
                return
            deal.state = WAIT_MANUAL
        save_state()
        await call.answer()
        await delete_call_message(call)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Оплатил — уведомить покупателя", callback_data=f"done:{deal.chat_id}"),
        ]])
        if deal.product == "premium":
            await call.message.answer(
                f"🖐 Ручная оплата заказа #{esc(deal.order_id)} (Telegram Premium):\n"
                f"1. Открой fragment.com/premium/gift\n"
                f"2. Получатель: <code>@{esc(deal.username)}</code>\n"
                f"3. Срок: <code>{esc(deal.months)}</code> мес\n\n"
                f"Когда оплатишь — жми кнопку.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
            return
        await call.message.answer(
            f"🖐 Ручная оплата заказа #{esc(deal.order_id)}:\n"
            f"1. Открой https://fragment.com/stars/buy\n"
            f"2. Получатель: <code>@{esc(deal.username)}</code>\n"
            f"3. Количество: <code>{esc(deal.stars)}</code>⭐\n\n"
            f"Когда оплатишь — жми кнопку.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )

    @dp.callback_query(F.data.startswith("done:"))
    async def cb_done(call: CallbackQuery):
        deal = get_deal_for_callback(call)
        if deal is None:
            await call.answer("Сделка не найдена (возможно, уже закрыта).", show_alert=True)
            return
        with deals_lock:
            if deal.state != WAIT_MANUAL:
                await call.answer(f"Уже в стату��е {deal.state}.", show_alert=True)
                return
            deal.state = PAID
        save_state()
        await call.answer()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, fp_send, deal.chat_id, MSG_PAID)
        await delete_call_message(call)
        await call.message.answer(
            f"✅ Отметил заказ #{esc(deal.order_id)} оплаченным, покупатель уведомлён.",
            parse_mode=ParseMode.HTML,
        )

    @dp.callback_query(F.data.startswith("no:"))
    async def cb_no(call: CallbackQuery):
        deal = get_deal_for_callback(call)
        if deal is None:
            await call.answer("Сделка не найдена (возможно, уж�� закрыта).", show_alert=True)
            return
        with deals_lock:
            deal.state = FROZEN
        save_state()
        await call.answer()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, fp_send, deal.chat_id,
            "⏳ Продавец проверяет ваш заказ и скоро свяжется с вами в этом чате.",
        )
        await delete_call_message(call)
        await call.message.answer(
            f"🧊 Заказ #{esc(deal.order_id)} заморожен. Разберись с ним вручную "
            f"(выдача/возврат через FunPay), покупатель предупреждён.",
            parse_mode=ParseMode.HTML,
        )

    main_loop = asyncio.get_running_loop()
    main_loop.run_in_executor(None, run_funpay_runner, main_loop, bot)
    main_loop.run_in_executor(None, run_order_catchup, main_loop, bot)
    if AUTO_RAISE_LOTS:
        main_loop.run_in_executor(None, run_lot_raiser)
    if AUTO_PRICING_ENABLED:
        main_loop.run_in_executor(None, run_price_updater, main_loop, bot)
    if AUTO_LOT_GUARD:
        main_loop.run_in_executor(None, run_lot_guard, main_loop, bot)
    asyncio.create_task(admin_sender_worker(bot))

    # Telegram может быть временно недоступен (обрыв сети, блокировки) —
    # не роняем бота, а ждём и пробуем снова. Мониторинг FunPay при этом уже работает,
    # а уведомления копятся в очереди и дойдут, когда связь появится.
    while True:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            break
        except TelegramNetworkError:
            logger.warning("Telegram недоступен на старте, повтор через 30 сек...")
            await asyncio.sleep(30)

    while True:
        try:
            await dp.start_polling(bot)
            break
        except TelegramNetworkError:
            logger.warning("Обрыв связи с Telegram в polling, перезапуск через 30 сек...")
            await asyncio.sleep(30)


if __name__ == "__main__":
    check_config()
    asyncio.run(main())
