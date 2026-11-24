"""Приём подключения Wi-Fi-модуля EyBond и чтение регистров инвертора.

Модуль сам звонит на наш TCP-порт. У модулей EyBond два диалекта, поддержаны оба:
  at - сервер шлёт «AT+DTUPN?», дальше голый Modbus RTU;
  eb - кадры с 8-байтным заголовком (tid, devcode, len, devaddr, fcode), Modbus внутри fcode 4.
На молчащем сокете пробуется один диалект; не подошёл - при следующем подключении другой.

Только чтение: единственный Modbus-запрос здесь - fn 0x03. Команд записи в модуль (fcode 3,
AT+...=) в коде нет.
"""
import asyncio
import struct
import time

SLAVE = 1
HEARTBEAT_S = 60
MAX_PENDING = 20
NET_ERRORS = (asyncio.IncompleteReadError, ConnectionError, OSError)


class NoLink(Exception):
    """Модуль сейчас не подключён."""


def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def rtu_read_request(start, count):
    body = struct.pack(">BBHH", SLAVE, 0x03, start, count)
    return body + struct.pack("<H", crc16(body))


def rtu_parse(frame, count):
    if len(frame) >= 5 and frame[1] & 0x80:
        raise ValueError(f"инвертор ответил ошибкой Modbus {frame[2]}")
    if len(frame) < 5 or frame[0] != SLAVE or frame[1] != 0x03 or frame[2] != 2 * count:
        raise ValueError("непонятный ответ " + frame[:16].hex())
    frame = frame[:5 + frame[2]]              # часть прошивок дописывает нули после кадра
    if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        raise ValueError("не сошлась контрольная сумма")
    return list(struct.unpack(f">{count}H", frame[3:-2]))


class Session:
    def __init__(self, reader, writer):
        self.r, self.w = reader, writer
        self.tid, self.dialect, self.pn, self.last_hb = 0, None, "", 0.0

    def close(self):
        self.w.close()

    async def _eb(self, devcode, devaddr, fcode, payload, timeout):
        self.tid = self.tid % 0xFFFF + 1
        self.w.write(struct.pack(">HHHBB", self.tid, devcode, len(payload) + 2, devaddr, fcode) + payload)
        await self.w.drain()

        async def rx():
            while True:
                tid, _, wlen, _, fc = struct.unpack(">HHHBB", await self.r.readexactly(8))
                body = await self.r.readexactly(max(0, wlen - 2))
                if fc == 1:                    # heartbeat (в том числе непрошеный) несёт PN модуля
                    self.pn = body[:14].rstrip(b"\0").decode("ascii", "replace")
                if fc == fcode and tid == self.tid:
                    return body
        return await asyncio.wait_for(rx(), timeout)

    async def _heartbeat(self):
        t = time.gmtime()
        stamp = bytes([t.tm_year - 2000, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec])
        await self._eb(0, 1, 1, stamp + struct.pack(">H", HEARTBEAT_S), 5)
        self.last_hb = time.monotonic()

    async def identify(self, silent_guess):
        try:
            first = await asyncio.wait_for(self.r.read(256), 2)
        except asyncio.TimeoutError:
            first = b""
        if first[:3] == b"AT+":
            self.dialect = "at"
        elif len(first) >= 22 and first[7] == 1:
            self.dialect = "eb"
        else:
            self.dialect = silent_guess
        if self.dialect == "at":
            self.w.write(b"AT+DTUPN?\r\n")
            await self.w.drain()
            while True:
                line = (await asyncio.wait_for(self.r.readuntil(b"\n"), 5)).strip()
                if line.startswith(b"AT+DTUPN:"):
                    self.pn = line.split(b":", 1)[1][:64].decode("ascii", "replace").strip()
                    return
        await self._heartbeat()

    async def quiet(self, seconds):
        """Выбросить запоздавшие байты, чтобы следующий ответ не съехал."""
        try:
            while await asyncio.wait_for(self.r.read(4096), seconds):
                pass
        except asyncio.TimeoutError:
            pass

    async def read(self, start, count, timeout=8):
        request = rtu_read_request(start, count)
        if self.dialect == "eb":
            if time.monotonic() - self.last_hb > HEARTBEAT_S:
                await self._heartbeat()
            return rtu_parse(await self._eb(1, 0xFF, 4, request, timeout), count)
        await self.quiet(0.05)
        self.w.write(request)
        await self.w.drain()

        async def rx():
            head = await self.r.readexactly(3)
            return head + await self.r.readexactly(2 if head[1] & 0x80 else head[2] + 2)
        return rtu_parse(await asyncio.wait_for(rx(), timeout), count)


class Link:
    """Держит одно живое подключение модуля; новое подключение заменяет старое."""

    def __init__(self, log, module_pn=""):
        self.log, self.module_pn = log, module_pn
        self.session = None
        self.lock = asyncio.Lock()
        self.guess = {}                        # ip -> диалект для молчащего сокета
        self.pending = 0                       # сколько подключений сейчас опознаётся

    @property
    def connected(self):
        return self.session is not None

    async def listen(self, port, host="0.0.0.0"):
        self.server = await asyncio.start_server(self._accept, host, port)

    def _pn_ok(self, pn):
        """Свой модуль: PN совпадает или один - часть другого (диалект eb отдаёт только 14 байт)."""
        want = self.module_pn
        return not want or (len(pn) >= 10 and (pn in want or want in pn))

    async def _accept(self, reader, writer):
        ip = writer.get_extra_info("peername")[0]
        s = Session(reader, writer)
        if self.pending >= MAX_PENDING:        # порт открыт в интернет: не дать забить сервис соединениями
            s.close()
            return
        guess = self.guess.get(ip, "at")
        self.pending += 1
        try:
            await asyncio.wait_for(s.identify(guess), 10)
        except Exception as e:                 # любой мусор от сканеров: закрыть и забыть
            if len(self.guess) > 200:
                self.guess.clear()
            self.guess[ip] = "eb" if guess == "at" else "at"
            self.log(f"подключение {ip} не опознано ({guess}): {type(e).__name__}")
            s.close()
            return
        finally:
            self.pending -= 1
        if not self._pn_ok(s.pn):
            self.log(f"подключение {ip} отклонено: чужой PN {s.pn[:32]!r}")
            s.close()
            return
        async with self.lock:
            old, self.session = self.session, s
        if old:
            old.close()
        self.log(f"модуль подключён: {ip}, диалект {s.dialect}, PN {s.pn}")

    async def read(self, start, count):
        async with self.lock:
            s = self.session
            if s is None:
                raise NoLink()
            try:
                return await s.read(start, count)
            except (asyncio.TimeoutError, ValueError):
                await s.quiet(0.5)             # инвертор промолчал: сессию держим
                raise
            except NET_ERRORS as e:
                self.log(f"модуль отключился: {e!r}")
                self.session = None
                s.close()
                raise NoLink()
