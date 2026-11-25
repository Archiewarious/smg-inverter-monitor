"""Мониторинг инвертора GSIKE (SMG II) через Wi-Fi-модуль EyBond.

Модуль сам подключается к этому сервису по TCP; сервис раз в poll_seconds читает регистры
(ТОЛЬКО Modbus fn 0x03, запись в инвертор в коде отсутствует), пишет в SQLite, отдаёт
веб-страницу и (если настроено) шлёт уведомления в Telegram.

  python monitor.py                  - запуск сервиса
  python monitor.py --test-telegram  - отправить тестовое уведомление и выйти
"""
import asyncio
import csv
import hmac
import io
import json
import os
import socket
import sqlite3
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import eybond

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.environ.get("CONFIG", os.path.join(HERE, "config.json")), encoding="utf-8"))
DB_PATH = os.environ.get("DB", os.path.join(HERE, "inverter.db"))
if CFG.get("log_file"):                       # под pythonw консоли нет: лог в файл рядом
    sys.stdout = sys.stderr = open(os.path.join(HERE, CFG["log_file"]), "a", encoding="utf-8", buffering=1)
TZ = ZoneInfo(CFG.get("tz", "Europe/Kyiv"))
POLL = CFG.get("poll_seconds", 30)           # как часто писать измерение в базу
LIVE = CFG.get("live_seconds", 5)            # как часто опрашивать инвертор (для виджета и событий)
LINK_TIMEOUT = CFG.get("link_timeout_s", 600)
BAT_LOW = CFG.get("bat_low_v", 12.0)
SETTINGS_EVERY = CFG.get("settings_seconds", 600)
GAP = max(90, POLL * 3)                       # дольше этого между измерениями = разрыв в данных

MODES = {0: "Включение", 1: "Ожидание", 2: "От сети", 3: "От АКБ", 4: "Байпас", 5: "Заряд", 6: "Авария"}
MODE_BATTERY, MODE_FAULT = 3, 6

# Текущие показатели, регистры 201-234 (SMG RS232 Modbus protocol V1.0.1; карта сверена с живыми
# данными 22.11.2026). Поле: (имя в базе, регистр, делитель, название, единица).
STATUS = [
    ("grid_v", 202, 10, "Напряжение сети", "В"),
    ("grid_hz", 203, 100, "Частота сети", "Гц"),
    ("grid_w", 204, 1, "Мощность из сети", "Вт"),
    ("inv_v", 205, 10, "Напряжение инвертора", "В"),
    ("inv_a", 206, 10, "Ток инвертора", "А"),
    ("inv_hz", 207, 100, "Частота инвертора", "Гц"),
    ("inv_w", 208, 1, "Мощность инвертора (минус - заряд)", "Вт"),
    ("chg_w", 209, 1, "Мощность заряда от сети", "Вт"),
    ("chg_a", 233, 10, "Ток заряда от сети", "А"),
    ("out_v", 210, 10, "Напряжение на выходе", "В"),
    ("out_a", 211, 10, "Ток на выходе", "А"),
    ("out_hz", 212, 100, "Частота на выходе", "Гц"),
    ("out_w", 213, 1, "Нагрузка, активная мощность", "Вт"),
    ("out_va", 214, 1, "Нагрузка, полная мощность", "ВА"),
    ("load", 225, 1, "Загрузка инвертора", "%"),
    ("bat_v", 215, 10, "Напряжение АКБ", "В"),
    ("bat_a", 232, 10, "Ток АКБ (плюс - заряд, минус - разряд)", "А"),
    ("bat_w", 217, 1, "Мощность АКБ", "Вт"),
    ("soc", 229, 1, "Заряд АКБ по оценке инвертора", "%"),
    ("pv_v", 219, 10, "Напряжение панелей", "В"),
    ("pv_a", 220, 10, "Ток панелей", "А"),
    ("pv_w", 223, 1, "Мощность панелей", "Вт"),
    ("pv_chg_w", 224, 1, "Мощность заряда от панелей", "Вт"),
    ("pv_chg_a", 234, 10, "Ток заряда от панелей", "А"),
    ("t_dc", 226, 1, "Температура DC-модуля", "°C"),
    ("t_inv", 227, 1, "Температура инвертора", "°C"),
]
FIELDS = ["mode", "fault", "warn"] + [f[0] for f in STATUS]

# Предупреждения, регистры 108-109: номер бита с нуля.
WARNINGS = {1: "форма напряжения сети не в норме", 3: "низкое напряжение сети", 4: "высокая частота сети",
            5: "низкая частота сети", 6: "нет напряжения панелей", 7: "перегрев", 8: "низкое напряжение АКБ",
            9: "АКБ не подключена", 10: "перегрузка", 11: "идёт выравнивающий заряд",
            12: "АКБ разряжена и не восстановилась", 13: "мощность выхода ограничена",
            14: "вентилятор заблокирован", 15: "мало энергии от панелей", 16: "нет связи с параллельным инвертором"}
WARN_QUIET = 1 << 6 | 1 << 15                 # панелей нет: эти два горят всегда, в события не идут
WARN_GRID = 1 << 0 | 1 << 1 | 1 << 3 | 1 << 4 | 1 << 5   # про сеть: без сети мигают, хватает «Сеть пропала»
FAULTS = {3: "перенапряжение АКБ", 7: "перегрузка", 13: "перегрузка по току", 14: "низкое напряжение шины"}

# Настройки, регистры 300-337. Третий элемент: словарь значений или (делитель, единица).
ONOFF = {0: "выключено", 1: "включено"}
SETTINGS = [
    (301, "Приоритет питания нагрузки", {0: "UTI: сеть в приоритете", 1: "SOL: панели, сеть, АКБ",
                                         2: "SBU: панели, АКБ, сеть"}),
    (331, "Приоритет заряда", {0: "сеть в приоритете", 1: "панели в приоритете", 2: "SNU: сеть и панели вместе",
                               3: "OSO: только панели, от сети не заряжать"}),
    (332, "Макс. ток заряда", (10, "А")),
    (333, "Макс. ток заряда от сети", (10, "А")),
    (322, "Тип АКБ", {0: "AGM", 1: "FLD (залитая)", 2: "USER (свои напряжения)"}),
    (324, "Напряжение основного заряда (bulk)", (10, "В")),
    (325, "Напряжение поддержки (float)", (10, "В")),
    (323, "Защита от перенапряжения АКБ", (10, "В")),
    (326, "Возврат на АКБ в режиме сети", (10, "В")),
    (327, "Отсечка по разряду в режиме сети", (10, "В")),
    (329, "Отсечка по разряду в автономе", (10, "В")),
    (302, "Диапазон входного напряжения", {0: "APL (широкий)", 1: "UPS (узкий)"}),
    (320, "Выходное напряжение", (10, "В")),
    (321, "Выходная частота", (100, "Гц")),
    (300, "Режим выхода", {0: "одиночный"}),
    (307, "Энергосбережение", ONOFF),
    (308, "Автоперезапуск после перегрузки", ONOFF),
    (309, "Автоперезапуск после перегрева", ONOFF),
    (310, "Байпас при перегрузке", ONOFF),
    (303, "Звуковой сигнал", {0: "выключен", 1: "при смене источника, предупреждениях и авариях",
                              2: "при предупреждениях и авариях", 3: "только при авариях"}),
    (305, "Подсветка экрана", {0: "гаснет по таймеру", 1: "всегда включена"}),
    (306, "Возврат на главный экран", {0: "нет", 1: "через минуту"}),
    (313, "Выравнивающий заряд (EQ)", ONOFF),
    (334, "Напряжение выравнивания", (10, "В")),
    (335, "Время выравнивания", (1, "мин")),
    (336, "Тайм-аут выравнивания", (1, "мин")),
    (337, "Интервал выравнивания", (1, "дн")),
]


def log(*a):
    print(datetime.now(TZ).strftime("%d.%m %H:%M:%S"), *a, flush=True)


def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    con = db()
    con.execute("CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY)")
    have = {r[1] for r in con.execute("PRAGMA table_info(samples)")}
    for f in FIELDS:                          # новые показатели добавляются к старой базе
        if f not in have:
            con.execute(f"ALTER TABLE samples ADD COLUMN {f} REAL")
    con.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, text TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS settings (reg INTEGER PRIMARY KEY, value INTEGER, ts INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
    con.commit()
    con.close()


# ---------- разбор регистров ----------
def s16(v):
    return v - 65536 if v > 32767 else v


def fault_text(fault):
    """Регистры 100-101: 32 бита, код ошибки на экране = номер бита + 1 (бит 2 -> «03»)."""
    codes = [b + 1 for b in range(32) if int(fault or 0) >> b & 1]
    return ", ".join(f"{c:02d}" + (f" ({FAULTS[c]})" if c in FAULTS else "") for c in codes)


def warn_names(warn):
    return [WARNINGS.get(b, f"бит {b}") for b in range(32) if int(warn or 0) >> b & 1]


def decode(r100, r108, r201):
    s = {"mode": r201[0], "fault": r100[0] << 16 | r100[1], "warn": r108[0] << 16 | r108[1]}
    for name, reg, div, _, _ in STATUS:
        v = s16(r201[reg - 201])
        s[name] = v / div if div > 1 else v
    return s


def setting_text(reg, raw):
    kind = next(k for r, _, k in SETTINGS if r == reg)
    if isinstance(kind, dict):
        return kind.get(raw, f"значение {raw}")
    div, unit = kind
    return f"{raw / div:g} {unit}"


# ---------- Telegram ----------
def tg_send(text):
    if not CFG.get("tg_token") or not CFG.get("tg_chat_id"):
        return False                          # Telegram не настроен: события только в журнале страницы
    data = urllib.parse.urlencode({"chat_id": CFG["tg_chat_id"], "text": text}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{CFG['tg_token']}/sendMessage", data, timeout=15) as r:
            return r.status == 200
    except Exception as e:  # сеть/неверный токен: сервис из-за этого падать не должен
        log("Telegram ошибка:", e)
        return False


# ---------- события и уведомления ----------
class Alerts:
    """Сообщает только о сменах состояния. Первое измерение после старта - тихо."""

    def __init__(self):
        self.state = {}       # fault / grid / bat / link -> bool, warn -> биты
        self.pending = {}     # дребезг: новое значение должно продержаться 2 опроса

    def event(self, kind, text):
        log("СОБЫТИЕ", kind, text)
        con = db()
        con.execute("INSERT INTO events (ts, kind, text) VALUES (?, ?, ?)", (int(time.time()), kind, text))
        con.commit()
        con.close()
        threading.Thread(target=tg_send, args=(text,), daemon=True).start()

    def flip(self, key, value, debounce=False):
        """True, если состояние key сменилось на value (и это не первое измерение)."""
        old = self.state.get(key)
        if old == value:
            self.pending.pop(key, None)
            return False
        if debounce and old is not None and self.pending.get(key) != value:
            self.pending[key] = value
            return False
        self.pending.pop(key, None)
        self.state[key] = value
        return old is not None

    def sample(self, s):
        bat = f"АКБ {s['bat_v']:.1f} В, сеть {s['grid_v']:.0f} В"
        if self.flip("link", True):
            self.event("link_back", f"🟢 Данные от инвертора снова идут. {bat}")
        fault = s["mode"] == MODE_FAULT or s["fault"] != 0
        if self.flip("fault", fault):
            if fault:
                code = fault_text(s["fault"]) or "?"
                self.event("fault", f"🔴 Инвертор в аварии (Failure Mode), ошибка {code}. "
                                    f"Резерв не сработает: при отключении света выход погаснет. {bat}")
            else:
                self.event("fault_clear", f"✅ Авария снята, режим: {MODES.get(s['mode'], s['mode'])}. {bat}")
        # гистерезис: пропала < 150 В, появилась > 170 В
        grid_now = self.state.get("grid")
        grid = s["grid_v"] > (150 if grid_now else 170) if grid_now is not None else s["grid_v"] > 160
        changed = self.flip("grid", grid, debounce=True)
        live["grid_ok"] = self.state["grid"]  # сеть без дребезга - по ней значок включает экономию
        if changed:
            self.event("grid_back" if grid else "grid_lost",
                       f"⚡ Сеть появилась ({s['grid_v']:.0f} В). АКБ {s['bat_v']:.1f} В" if grid else
                       f"⚠️ Сеть пропала. Режим: {MODES.get(s['mode'], s['mode'])}, АКБ {s['bat_v']:.1f} В"
                       + (". Инвертор в аварии - выход, скорее всего, погас!" if fault else ""))
        low_now = self.state.get("bat")
        low = s["bat_v"] < (BAT_LOW + 0.3 if low_now else BAT_LOW)
        if self.flip("bat", low, debounce=True):
            self.event("bat_low" if low else "bat_ok",
                       f"🪫 АКБ ниже порога: {s['bat_v']:.1f} В (порог {BAT_LOW} В)" if low else
                       f"🔋 АКБ восстановилась: {s['bat_v']:.1f} В")
        warn, old = s["warn"] & ~WARN_QUIET, self.state.get("warn")
        if not grid or not self.state["grid"]:
            warn &= ~WARN_GRID
        self.state["warn"] = warn
        if old is not None and warn != old:
            if warn & ~old:
                self.event("warn", "⚠️ Предупреждение инвертора: " + ", ".join(warn_names(warn & ~old)) + f". {bat}")
            if old & ~warn:
                self.event("warn_clear", "Предупреждение снято: " + ", ".join(warn_names(old & ~warn)))

    def link_lost(self, minutes):
        if self.flip("link", False):
            self.event("link_lost", f"⚫ Нет данных от инвертора {minutes} мин: "
                                    f"пропало питание, сеть или модуль отключился.")


# ---------- остаток АКБ ----------
BAT_AH = CFG.get("battery_ah", 200) * 0.9     # паспортная ёмкость минус 10 %: лучше пообещать меньше
EFFICIENCY, BAT_NOMINAL_V = 0.9, 12.8         # для прикидки тока разряда по нагрузке, пока сеть есть
# заряд LiFePO4 по напряжению под небольшой нагрузкой, очень грубо: (В, доля полного заряда)
VOLT_SOC = [(11.0, 0.0), (12.0, 0.07), (12.5, 0.12), (12.8, 0.17), (12.9, 0.22), (13.0, 0.3), (13.1, 0.45),
            (13.2, 0.7), (13.3, 0.9), (13.4, 1.0)]


def soc_by_volts(v):
    if v <= VOLT_SOC[0][0]:
        return VOLT_SOC[0][1]
    for (v1, s1), (v2, s2) in zip(VOLT_SOC, VOLT_SOC[1:]):
        if v <= v2:
            return s1 + (s2 - s1) * (v - v1) / (v2 - v1)
    return VOLT_SOC[-1][1]


class Battery:
    """Сколько ампер-часов осталось до отсечки инвертора и на сколько их хватит.

    После полного заряда - счёт по току АКБ (bat_a). Пока полного заряда не видели или в данных
    был перерыв (ПК спал, сервис стоял) - оценка грубая (rough): без заряда - по напряжению.
    Заряду «по оценке инвертора» (soc) не верим: он тоже считает по напряжению.
    """

    def __init__(self):
        con = db()
        row = con.execute("SELECT value FROM state WHERE key = 'battery'").fetchone()
        con.close()
        saved = json.loads(row[0]) if row else {}
        self.used = saved.get("used")         # А·ч, израсходовано от полного заряда; None - ещё не знаем
        self.rough = saved.get("rough", True)
        self.ts = saved.get("ts", 0)
        self.grid = saved.get("grid")         # была ли сеть в последнем измерении (для перерывов в данных)
        self.full_s = 0                      # сколько секунд подряд АКБ выглядит полной
        self.amps = 0.0                       # сглаженный ток разряда, А
        self.limits_ts = 0

    def read_limits(self):
        """Напряжение поддержки и отсечка в автономе - из прочитанных настроек инвертора."""
        con = db()
        regs = dict(con.execute("SELECT reg, value FROM settings WHERE reg IN (325, 329)"))
        con.close()
        self.float_v, self.cutoff_v = regs.get(325, 141) / 10, regs.get(329, 114) / 10

    def update(self, s, ts):
        """Учесть измерение -> поля для api/live."""
        if ts - self.limits_ts > SETTINGS_EVERY:
            self.limits_ts = ts
            self.read_limits()
        dt, self.ts = ts - self.ts, ts
        amps, volts = s["bat_a"], s["bat_v"]
        empty = soc_by_volts(self.cutoff_v)   # на отсечке в АКБ ещё что-то есть, но нам оно недоступно
        share = max(0.0, (soc_by_volts(volts - (0.3 if amps > 5 else 0)) - empty) / (1 - empty))
        if dt > 300:                          # перерыв в данных (ПК был выключен или спал)
            if self.grid and dt >= 3600:
                # перед перерывом сеть была: инвертор держал АКБ на подзарядке, считаем её полной.
                # Если свет пропал во время перерыва - считаем, что только что (ПК включают сразу).
                self.used, self.rough = 0.0, False
            elif not self.grid:
                self.rough = True             # перерыв посреди отключения: что было с АКБ, неизвестно
        if not 0 < dt <= 300:
            dt = 0
        self.grid = s["grid_v"] > 160
        # страховка: счёт говорит «больше половины», а напряжение под нагрузкой - «почти пусто»
        if not self.rough and amps < -1 and share < 0.15 and BAT_AH - self.used > BAT_AH * 0.5:
            self.used, self.rough = BAT_AH * (1 - share), True
        if self.used is None:
            self.used = BAT_AH * (1 - share)
        elif self.rough and amps <= 1:        # не заряжается: напряжению можно верить, подтягиваемся к нему
            self.used += (BAT_AH * (1 - share) - self.used) * min(1, dt / 120)
        else:
            self.used = min(max(self.used - amps * dt / 3600, 0.0), BAT_AH)
        # полный заряд: зарядник держит напряжение, а ток упал ниже 0,02C
        full = volts >= self.float_v - 0.3 and 0 <= amps <= BAT_AH * 0.02
        self.full_s = self.full_s + dt if full else 0
        if self.full_s >= 120:
            self.used, self.rough = 0.0, False
        if amps < -1:
            self.amps += (-amps - self.amps) * (min(1, dt / 60) if self.amps else 1)
        else:                                 # не разряжается: каким был бы ток при такой нагрузке
            self.amps = s["out_w"] / EFFICIENCY / BAT_NOMINAL_V
        left = BAT_AH - self.used
        return {"ah_left": round(left, 1), "ah_rough": self.rough, "cutoff_v": self.cutoff_v,
                "left_s": int(left / self.amps * 3600) if self.amps > 0.5 else None}

    def save(self, con):
        con.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('battery', ?)",
                    (json.dumps({"used": self.used, "rough": self.rough, "ts": self.ts, "grid": self.grid}),))


# ---------- опрос ----------
def send_redirect():
    """Один UDP-пакет модулю: «подключись к этому ПК». Адрес облака в модуле не меняется.

    Эта прошивка модуля после обрыва сама не перезванивает, поэтому без пакета связи не будет.
    """
    command = f"set>server={CFG['redirect_to']};".encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
        u.settimeout(3)
        try:
            u.sendto(command, (CFG["module_ip"], 58899))
            log("модулю отправлено", command.decode(), "ответ:", u.recvfrom(256)[0][:40])
        except OSError as e:
            log("модуль не ответил на set>server:", type(e).__name__)


async def fetch_settings(link):
    # блоки по 32 и 6 регистров: длина ответа не совпадает с блоком показателей, запоздавший
    # чужой ответ не будет принят за настройки
    regs = await link.read(300, 32) + await link.read(332, 6)
    return {reg: regs[reg - 300] for reg, _, _ in SETTINGS}


async def read_settings(link, alerts):
    """Прочитать настройки (только чтение) и отметить в журнале, если что-то поменяли."""
    new = await fetch_settings(link)
    con = db()
    old = dict(con.execute("SELECT reg, value FROM settings"))
    con.close()
    changed = [r for r in new if r in old and old[r] != new[r]]
    if changed and await fetch_settings(link) != new:
        return False                          # повторное чтение не совпало: не верим, перечитаем позже
    for reg in changed:
        name = next(n for r, n, _ in SETTINGS if r == reg)
        alerts.event("setting", f"⚙️ Настройка инвертора изменена: {name}: "
                                f"{setting_text(reg, old[reg])} → {setting_text(reg, new[reg])}")
    con = db()
    con.executemany("INSERT OR REPLACE INTO settings (reg, value, ts) VALUES (?, ?, ?)",
                    [(r, v, int(time.time())) for r, v in new.items()])
    con.commit()
    con.close()
    return True


live = {"sample": None, "ts": 0}              # последнее измерение - для виджета и шапки страницы


async def poll_loop(link, alerts):
    last_ok = time.time()
    last_redirect = time.time() - 50          # первый пакет через ~10 с после старта
    last_settings = stored_at = 0
    stored_key = None
    battery = Battery()
    while True:
        if CFG.get("auto_redirect") and not link.connected and time.time() - last_redirect > 60:
            last_redirect = time.time()       # только когда модуль НЕ подключён: пакет рвёт живую сессию
            await asyncio.to_thread(send_redirect)
            await asyncio.sleep(5)
        started = time.time()
        try:
            r201 = await link.read(201, 34)   # режим раньше кода ошибки: иначе на стыке «авария без кода»
            r100 = await link.read(100, 10)   # 100-101 ошибки, 108-109 предупреждения
            s = decode(r100[0:2], r100[8:10], r201)
            live.update(battery.update(s, started), sample=s, ts=int(started))
            # в базу - раз в POLL секунд, а смену режима, ошибки или сети - сразу
            key = (s["mode"], s["fault"], s["warn"], s["grid_v"] > 160)
            if started - stored_at >= POLL or key != stored_key:
                con = db()
                con.execute(f"INSERT OR REPLACE INTO samples (ts, {', '.join(FIELDS)}) VALUES (?{', ?' * len(FIELDS)})",
                            [int(started)] + [s[f] for f in FIELDS])
                battery.save(con)
                con.commit()
                con.close()
                stored_at, stored_key = started, key
            last_ok = started
            alerts.sample(s)
            if time.time() - last_settings > SETTINGS_EVERY and await read_settings(link, alerts):
                last_settings = time.time()
        except eybond.NoLink:
            pass
        except Exception as e:
            log("опрос не удался:", repr(e))
        if time.time() - last_ok > LINK_TIMEOUT:
            alerts.link_lost(round((time.time() - last_ok) / 60))
        await asyncio.sleep(max(1, LIVE - (time.time() - started)))


# ---------- сводка: энергия, отключения, циклы заряда ----------
_stats_cache = {}


def compute_stats(days):
    cached = _stats_cache.get(days)
    if cached and time.time() - cached[0] < 60:
        return cached[1]
    now = int(time.time())
    today = datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    starts = [int((today - timedelta(days=i)).timestamp()) for i in range(days - 1, -1, -1)]
    con = db()
    rows = con.execute("SELECT ts, mode, fault, grid_v, grid_w, out_w, chg_w, bat_a, bat_v FROM samples "
                       "WHERE ts >= ? ORDER BY ts", (starts[0],)).fetchall()
    con.close()
    acc = [dict(date=s, data_s=0, grid_wh=0.0, load_wh=0.0, chg_wh=0.0, chg_ah=0.0, dis_ah=0.0, outages=0,
                off_s=0, bat_s=0, fault_s=0, bat_v_min=None, bat_v_max=None) for s in starts]
    cycles, cur, di, prev_off = [], None, 0, None
    for i, (ts, mode, fault, grid_v, grid_w, out_w, chg_w, bat_a, bat_v) in enumerate(rows):
        while di + 1 < len(starts) and ts >= starts[di + 1]:
            di += 1
        d = acc[di]
        nxt = rows[i + 1][0] if i + 1 < len(rows) else None
        broken = nxt is None or nxt - ts > GAP          # дальше разрыв в данных (или конец)
        dt = POLL if broken else nxt - ts
        hours = dt / 3600
        bat_a = bat_a or 0
        d["data_s"] += dt
        d["grid_wh"] += (grid_w or 0) * hours
        d["load_wh"] += (out_w or 0) * hours
        d["chg_wh"] += (chg_w or 0) * hours
        d["chg_ah"] += max(bat_a, 0) * hours
        d["dis_ah"] += max(-bat_a, 0) * hours
        if bat_v is not None:
            d["bat_v_min"] = bat_v if d["bat_v_min"] is None else min(d["bat_v_min"], bat_v)
            d["bat_v_max"] = bat_v if d["bat_v_max"] is None else max(d["bat_v_max"], bat_v)
        off = grid_v is not None and grid_v < 150
        if off:
            d["off_s"] += dt
            d["outages"] += prev_off is not True         # новое отключение (или первое после разрыва)
        prev_off = None if broken else off
        d["bat_s"] += dt * (mode == MODE_BATTERY)
        d["fault_s"] += dt * (mode == MODE_FAULT)
        # цикл заряда: подряд идущие измерения, где в АКБ течёт больше 0,5 А
        charging = bat_a > 0.5
        if charging:
            if cur is None:
                cur = dict(start=ts, v0=bat_v, ah=0.0, wh=0.0, peak_a=0.0)
            cur["ah"] += bat_a * hours
            cur["wh"] += (chg_w or 0) * hours
            cur["peak_a"] = max(cur["peak_a"], bat_a)
            cur.update(end=ts + dt, v1=bat_v)
        if cur and (not charging or broken):
            # авария могла встать через одно-два измерения после остановки заряда: смотрим на 2 минуты вперёд
            failed = next((r for r in rows[i:i + 10] if r[0] - ts <= 120 and r[1] == MODE_FAULT), None)
            cur["why"] = ("идёт сейчас" if nxt is None and charging and now - ts < GAP else
                          "нет данных" if charging else
                          "прерван аварией " + (fault_text(failed[2]) or "?") if failed else
                          "сеть пропала" if off else "")
            if cur["end"] - cur["start"] >= 60:
                cycles.append(cur)
            cur = None
    result = {"days": acc[::-1], "cycles": cycles[::-1][:20]}
    _stats_cache[days] = (time.time(), result)
    return result


# ---------- веб ----------
SERIES = ("mode", "grid_v", "chg_w", "bat_v", "bat_a", "grid_w", "out_w", "t_dc", "t_inv")


def api_data(hours, link):
    now = int(time.time())
    frm = now - hours * 3600
    bucket = max(POLL, hours * 3600 // 600)
    con = db()
    rows = con.execute(
        f"SELECT (ts / {bucket}) * {bucket}, MAX(mode), "
        + ", ".join(f"ROUND(AVG({f}), 1)" for f in SERIES[1:])
        + " FROM samples WHERE ts >= ? GROUP BY 1 ORDER BY 1", (frm,)).fetchall()
    last = con.execute(f"SELECT ts, {', '.join(FIELDS)} FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
    events = con.execute("SELECT ts, kind, text FROM events ORDER BY ts DESC LIMIT 100").fetchall()
    settings = dict(con.execute("SELECT reg, value FROM settings"))
    settings_ts = con.execute("SELECT MAX(ts) FROM settings").fetchone()[0]
    con.close()
    out = {"now": now, "from": frm, "to": now, "gap": bucket * 3, "stale": max(LINK_TIMEOUT, POLL * 3),
           "link": link.connected, "events": [dict(zip(("ts", "kind", "text"), e)) for e in events], "last": None,
           "fields": [[name, title, unit] for name, _, _, title, unit in STATUS],
           "settings": [[name, setting_text(reg, settings[reg])] for reg, name, _ in SETTINGS if reg in settings],
           "settings_ts": settings_ts}
    for i, k in enumerate(("t",) + SERIES):
        out[k] = [r[i] for r in rows]
    if live["sample"]:                        # свежее, чем база: опрос идёт чаще записи
        last = [live["ts"]] + [live["sample"][f] for f in FIELDS]
    if last:
        out["last"] = dict(zip(["ts"] + FIELDS, last))
        out["last"]["fault_text"] = fault_text(last[2])
        out["last"]["warn_all"] = warn_names(last[3])
        out["last"]["warn_main"] = warn_names(int(last[3] or 0) & ~WARN_QUIET)
    return json.dumps(out).encode()


def grid_since(on):
    """С какого измерения длится нынешнее состояние сети (есть / нет)."""
    con = db()
    row = con.execute("SELECT MIN(ts) FROM samples WHERE ts > (SELECT ts FROM samples "
                      "WHERE (grid_v > 160) != ? ORDER BY ts DESC LIMIT 1)", (on,)).fetchone()
    con.close()
    return row[0]


def api_live(link):
    """Короткий ответ для значка у часов: последнее измерение и последнее событие журнала."""
    con = db()
    event = con.execute("SELECT id, ts, kind, text FROM events ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    s = live["sample"]
    out = {"now": int(time.time()), "link": link.connected, "ts": live["ts"] or None,
           "event": dict(zip(("id", "ts", "kind", "text"), event)) if event else None}
    if s:
        on = s["grid_v"] > 160
        if live.get("grid_on") != on:         # считать «с какого времени» только при смене состояния
            live.update(grid_on=on, grid_since=grid_since(on))
        out.update({k: s[k] for k in ("mode", "grid_v", "grid_w", "bat_v", "bat_a", "chg_w", "out_w", "load")},
                   mode_name=MODES.get(s["mode"], f"режим {s['mode']}"), grid_on=on, grid_since=live["grid_since"],
                   in_fault=s["mode"] == MODE_FAULT or s["fault"] != 0, fault=fault_text(s["fault"]),
                   warn=warn_names(s["warn"] & ~WARN_QUIET), grid_ok=live.get("grid_ok"),
                   **{k: live[k] for k in ("ah_left", "ah_rough", "left_s", "cutoff_v")})
    return json.dumps(out).encode()


def api_csv(hours):
    con = db()
    rows = con.execute(f"SELECT ts, {', '.join(FIELDS)} FROM samples WHERE ts >= ? ORDER BY ts",
                       (int(time.time()) - hours * 3600,)).fetchall()
    con.close()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Время", "Режим", "Ошибка", "Предупреждения"] + [f"{title}, {unit}" for _, _, _, title, unit in STATUS])
    for r in rows:
        w.writerow([datetime.fromtimestamp(r[0], TZ).strftime("%d.%m.%Y %H:%M:%S"), MODES.get(r[1], r[1]),
                    fault_text(r[2]), ", ".join(warn_names(r[3]))]
                   + ["" if v is None else f"{v:g}".replace(".", ",") for v in r[4:]])
    return ("﻿" + buf.getvalue()).encode()


class WebServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass                                  # сканеры и оборванные TLS-рукопожатия: без трассировок в лог


def make_handler(link):
    token = CFG["web_token"].encode()

    class H(BaseHTTPRequestHandler):
        timeout = 15                          # медленный клиент не держит поток вечно
        server_version, sys_version = "inverter", ""

        def log_message(self, *a):
            pass

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            parts = url.path.split("/", 2)    # "", токен, маршрут
            if len(parts) < 3 or not hmac.compare_digest(parts[1].encode(), token):
                self.send_error(404)
                return
            route = parts[2]
            q = urllib.parse.parse_qs(url.query)

            def number(key, default, most):
                try:
                    return min(most, max(1, int(q.get(key, [default])[0])))
                except ValueError:
                    return default

            hours = number("hours", 24, 24 * 90)
            if route == "":
                body, ctype = open(os.path.join(HERE, "index.html"), "rb").read(), "text/html; charset=utf-8"
            elif route == "api/data":
                body, ctype = api_data(hours, link), "application/json"
            elif route == "api/live":
                body, ctype = api_live(link), "application/json"
            elif route == "api/stats":
                body, ctype = json.dumps(compute_stats(number("days", 7, 31))).encode(), "application/json"
            elif route == "api/csv":
                body, ctype = api_csv(hours), "text/csv; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'")
            if route == "api/csv":
                self.send_header("Content-Disposition", 'attachment; filename="inverter-log.csv"')
            self.end_headers()
            self.wfile.write(body)

    return H


async def main():
    init_db()
    link = eybond.Link(log, CFG.get("module_pn", ""))
    await link.listen(CFG.get("listen_port", 8899), CFG.get("bind", "0.0.0.0"))
    web = WebServer((CFG.get("web_bind", "0.0.0.0"), CFG.get("web_port", 8080)), make_handler(link))
    if CFG.get("tls_cert"):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(CFG["tls_cert"], CFG["tls_key"])
        # рукопожатие - в потоке запроса (с таймаутом), а не в accept: иначе один клиент вешает всех
        web.socket = ctx.wrap_socket(web.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    log(f"запущен: модуль -> порт {CFG.get('listen_port', 8899)}, веб -> порт {CFG.get('web_port', 8080)}"
        f" ({'HTTPS' if CFG.get('tls_cert') else 'HTTP'})")
    await poll_loop(link, Alerts())


if __name__ == "__main__":
    if "--test-telegram" in sys.argv:
        ok = tg_send("✅ Тест: мониторинг инвертора может слать уведомления.")
        print("отправлено" if ok else "НЕ отправлено")
        sys.exit(0 if ok else 1)
    asyncio.run(main())
