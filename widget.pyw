"""Значок инвертора у часов (область уведомлений), как батарея на ноутбуке.

Зелёный - сеть есть, красный - сети нет (работаем от АКБ), жёлтый - инвертор в аварии
(резерв не сработает), серый - свежих показаний нет. В подсказке и в панели по левому щелчку -
нагрузка, напряжение АКБ и на сколько её хватит. События журнала мониторинга (свет пропал / дали,
авария, АКБ садится) всплывают уведомлениями.

Менеджер питания: сеть устойчиво пропала - включает экономию (режим «Экономия энергии» через утилиту
«Режим питания», таймеры гашения экрана, дисков, гибернации или сна и яркость монитора - как выбраны
в панели, блок «Экономия без света», хранятся в outage.json); сеть вернулась - возвращает всё как было.
Что было, лежит в power_state.json, поэтому возврат переживает перезапуск значка и ПК. Режим можно
сменить и руками (панель, меню): автоматика действует только в момент пропажи и возврата сети и ручной
выбор не перебивает. Ползунок яркости в панели ставит яркость монитора по DDC/CI; если его тронули во
время отключения, с сетью прежнюю яркость поверх ручной не возвращаем.

Данные берёт у monitor.py по http://127.0.0.1 (тот опрашивает инвертор раз в 5 с);
сам в инвертор и модуль ничего не шлёт.
Запуск: ярлык «Виджет света» рядом (pythonw widget.pyw; сам .pyw в Windows ни с чем не связан).
Переменная окружения DRY_RUN=1 - сухой прогон: настройки питания не трогает, только пишет в журнал,
что изменил бы. Левая кнопка по значку - панель, правая - меню.
"""
import ctypes
import json
import os
import pathlib
import subprocess
import threading
import time
import urllib.request
import uuid
import winreg
from ctypes import wintypes as wt

HERE = pathlib.Path(__file__).resolve().parent
CONFIG = pathlib.Path(os.environ.get("CONFIG", HERE / "config.json")).resolve()
CFG = json.loads(CONFIG.read_text(encoding="utf-8"))
STATE = CONFIG.parent / "power_state.json"      # как было до отключения; журнал - тоже рядом с конфигом
DRY = bool(os.environ.get("DRY_RUN"))
PAGE = f"http://127.0.0.1:{CFG.get('web_port', 8900)}/{CFG['web_token']}/"
STALE = 60          # с: показание старше - считаем, что данных нет
COLORS = {True: (46, 160, 67), False: (218, 54, 51), "fault": (210, 153, 34), None: (125, 133, 144)}
TITLES = {True: "Свет есть", False: "Света нет", "fault": "Инвертор в аварии", None: "Нет данных"}
BOLT = [(18.5, 4), (9, 17.5), (15, 17.5), (13, 28), (23, 13.5), (17, 13.5)]   # молния в сетке 32x32
# событие журнала -> (заголовок уведомления, значок: 1 сведения, 2 внимание, 3 ошибка)
NOTES = {"fault": ("Авария инвертора", 3), "fault_clear": ("Авария снята", 1), "grid_lost": ("Света нет", 2),
         "grid_back": ("Свет дали", 1), "bat_low": ("АКБ садится", 2), "bat_ok": ("АКБ в норме", 1),
         "link_lost": ("Инвертор молчит", 2), "link_back": ("Данные снова идут", 1),
         "warn": ("Предупреждение инвертора", 2), "warn_clear": ("Предупреждение снято", 1),
         "setting": ("Настройка инвертора изменена", 1)}

# схемы питания Windows и утилита владельца, которая их переключает
SAVER, HIGH = "a1841308-3541-4fab-bc81-f71556f20b4a", "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
MODES = {SAVER: "экономия энергии", HIGH: "высокая производительность"}
AHK = r"C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe"
POWER_MODE = next((p for p in (HERE / "PowerMode.ahk", pathlib.Path.home() / "Documents" / "PowerMode" / "PowerMode.ahk")
                   if p.exists()), HERE / "PowerMode.ahk")
# таймеры простоя: (подгруппа, настройка). Меняем значения «от сети» в схеме «Экономия энергии»:
# батареи у настольного ПК для Windows нет, а в «Высокой» таймеры остаются обычными.
TIMERS = {"экран": ("7516b95f-f776-4464-8c53-06167f40cc99", "3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e"),
          "диски": ("0012ee47-9041-4b5d-9b77-535fba8b1442", "6738e2c4-e8a5-4a42-b16a-e040e769756e"),
          "сон": ("238c9fa8-0aad-41ed-83f4-97be242c8f20", "29f6c1db-86da-48c5-9fdb-f2b67b1f44da"),
          "гибернация": ("238c9fa8-0aad-41ed-83f4-97be242c8f20", "9d7815a6-7ee4-497e-8888-515a05f02364")}
# экономия без сети: секунды простоя (0 - никогда) и яркость монитора; владелец меняет их в панели
OUTAGE = CONFIG.parent / "outage.json"
OUTAGE_DEFAULT = {"screen": 60, "disks": 60, "sleep": 600, "brightness": CFG.get("outage_brightness", 30)}
STEPS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 0]   # выбор таймера в панели: 1, 2, 5, 10, 15, 30 мин, 1, 2 ч, никогда
BRIGHT_STEPS = list(range(0, 101, 10))
BRIGHT_LOCK = threading.Lock()                      # монитор по DDC/CI - по одному запросу за раз


def log(text):
    with open(CONFIG.parent / "widget.log", "a", encoding="utf-8") as f:
        f.write(time.strftime("%d.%m %H:%M:%S ") + text + "\n")


def duration(sec):
    m = int(sec) // 60
    return f"{m // 60} ч {m % 60:02d} мин" if m >= 60 else f"{m} мин" if m else "меньше минуты"


def left_time(d):
    """Остаток времени словами; больше часа - с точностью до 10 минут, точнее мы не знаем."""
    return duration(round(d["left_s"] / 600) * 600 if d["left_s"] >= 3600 else d["left_s"])


def num(v, digits=1):
    return f"{v:.{digits}f}".replace(".", ",")


def fetch():
    """Свежие показания от monitor.py -> (словарь или None, почему их нет, последнее событие журнала)."""
    try:
        with urllib.request.urlopen(PAGE + "api/live", timeout=2) as r:
            d = json.load(r)
    except (OSError, ValueError):
        return None, "мониторинг не отвечает", None
    if not d["ts"] or d["now"] - d["ts"] > STALE:
        return None, "инвертор молчит" + (time.strftime(" с %H:%M", time.localtime(d["ts"])) if d["ts"] else ""), d["event"]
    return d, "", d["event"]


def describe(d):
    """-> (вид значка, строки подсказки); вид: True - сеть есть, False - нет, "fault" - авария."""
    if d["in_fault"]:
        return "fault", [f"ошибка {d['fault'] or '?'}",
                         f"Сеть {d['grid_v']:.0f} В · АКБ {num(d['bat_v'])} В · резерв не сработает"]
    text = f"{d['grid_v']:.0f} В" if d["grid_on"] else "работаем от АКБ"
    if d["grid_since"]:
        text += " · уже " + duration(d["now"] - d["grid_since"])
    lines = [text, f"АКБ {num(d['bat_v'])} В, {d['bat_a']:+.1f} А".replace(".", ",") + f" · нагрузка {d['out_w']:.0f} Вт"]
    if d.get("left_s"):
        lines.append(("От АКБ хватит ≈ " if d["grid_on"] else "Осталось ≈ ") + left_time(d)
                     + (" (грубо)" if d["ah_rough"] else ""))
    return d["grid_on"], lines


def panel_view(d, why, kind):
    """Что показать в панели по левому щелчку: заголовок, строка под ним, две плитки (подпись, значение,
    мелкая строка), остаток времени (подпись, значение), сноска, режим питания."""
    state = load_state()
    view = {"kind": kind, "title": TITLES[kind], "alert": "", "tiles": [("Нагрузка", "—", ""), ("АКБ", "—", "")],
            "left": ("От АКБ хватит примерно на", "—"), "note": "", "scheme": active_scheme(),
            "auto": "Сети нет: включена экономия, с сетью всё вернётся." if state and state["active"] else ""}
    if d is None:
        view["alert"] = why[:1].upper() + why[1:]
        return view
    if d["grid_on"]:
        view["title"] += f" · {d['grid_v']:.0f} В"
    elif d["grid_since"]:
        view["title"] += " · уже " + duration(d["now"] - d["grid_since"])
    amps = d["bat_a"]
    view["tiles"] = [("Нагрузка", f"{d['out_w']:.0f} Вт", f"загрузка {d['load']:.0f} %" if d.get("load") is not None else ""),
                     ("АКБ", f"{num(d['bat_v'])} В", f"заряд {num(amps)} А" if amps > 0 else
                      f"разряд {num(-amps)} А" if amps < 0 else "ток 0 А")]
    if d["in_fault"]:
        view["alert"] = f"Ошибка {d['fault'] or '?'}. Резерв не сработает: пропадёт сеть - погаснет и выход."
    view["left"] = ("От АКБ хватит примерно на" if d["grid_on"] else "АКБ хватит ещё примерно на",
                    left_time(d) if d.get("left_s") else "—")
    if d.get("ah_left") is not None:
        view["note"] = (f"В АКБ около {d['ah_left']:.0f} А·ч до отсечки {num(d['cutoff_v'])} В."
                        + (" Оценка грубая, по напряжению: уточнится после полного заряда." if d["ah_rough"] else ""))
    return view


# ---------- Win32 ----------
user32, shell32, kernel32, gdi32 = (ctypes.WinDLL(name) for name in ("user32", "shell32", "kernel32", "gdi32"))
powrprof, dxva2, dwmapi, gdiplus = (ctypes.WinDLL(name) for name in ("powrprof", "dxva2", "dwmapi", "gdiplus"))
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.c_void_p, wt.LPARAM)
WM_DESTROY, WM_ACTIVATE, WM_PAINT, WM_CLOSE, WM_ERASEBKGND = 0x0002, 0x0006, 0x000F, 0x0010, 0x0014
WM_COMMAND, WM_TIMER, WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0111, 0x0113, 0x0200, 0x0201, 0x0202
WM_RBUTTONUP, WM_MOUSEWHEEL, WM_MOUSELEAVE, WM_TRAY, WM_TEST, WM_BRIGHT = 0x0205, 0x020A, 0x02A3, 0x8001, 0x8002, 0x8003
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_BASE, NIF_INFO, NIIF_NOSOUND = 7, 0x10, 0x10      # 7 = сообщение + значок + подсказка
CLASS_NAME, PANEL_CLASS = "SvetWidget", "SvetPanel"
ID_EXIT, ID_PAGE, ID_NOTE, ID_ECO, ID_HIGH = 1, 2, 3, 4, 5


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON)]


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [("handle", wt.HANDLE), ("description", wt.WCHAR * 128)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", wt.RECT), ("fRestore", wt.BOOL),
                ("fIncUpdate", wt.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("dwFlags", wt.DWORD), ("hwndTrack", wt.HWND), ("dwHoverTime", wt.DWORD)]


class GDIPLUS_STARTUP(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("callback", ctypes.c_void_p), ("no_thread", wt.BOOL),
                ("no_codecs", wt.BOOL)]


# типы обязательны: на 64 битах дескрипторы и LPARAM не влезают в int по умолчанию
GUIDS = (ctypes.c_char_p,) * 3                        # схема, подгруппа, настройка - GUID как 16 байт
for func, restype, argtypes in (
        (kernel32.GetModuleHandleW, wt.HMODULE, (wt.LPCWSTR,)),
        (kernel32.LocalFree, ctypes.c_void_p, (ctypes.c_void_p,)),
        (kernel32.GetTickCount, wt.DWORD, ()),
        (user32.DefWindowProcW, ctypes.c_ssize_t, (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)),
        (user32.RegisterClassW, wt.ATOM, (ctypes.POINTER(WNDCLASS),)),
        (user32.CreateWindowExW, wt.HWND, (wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD) + (ctypes.c_int,) * 4
         + (wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)),
        (user32.FindWindowW, wt.HWND, (wt.LPCWSTR, wt.LPCWSTR)),
        (user32.DestroyWindow, wt.BOOL, (wt.HWND,)),
        (user32.SetForegroundWindow, wt.BOOL, (wt.HWND,)),
        (user32.ShowWindow, wt.BOOL, (wt.HWND, ctypes.c_int)),
        (user32.IsWindowVisible, wt.BOOL, (wt.HWND,)),
        (user32.SetWindowPos, wt.BOOL, (wt.HWND, wt.HWND) + (ctypes.c_int,) * 4 + (wt.UINT,)),
        (user32.InvalidateRect, wt.BOOL, (wt.HWND, wt.LPVOID, wt.BOOL)),
        (user32.BeginPaint, wt.HDC, (wt.HWND, ctypes.POINTER(PAINTSTRUCT))),
        (user32.EndPaint, wt.BOOL, (wt.HWND, ctypes.POINTER(PAINTSTRUCT))),
        (user32.GetDC, wt.HDC, (wt.HWND,)),
        (user32.ReleaseDC, ctypes.c_int, (wt.HWND, wt.HDC)),
        (user32.FillRect, ctypes.c_int, (wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH)),
        (user32.DrawTextW, ctypes.c_int, (wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(wt.RECT), wt.UINT)),
        (user32.DrawIconEx, wt.BOOL, (wt.HDC, ctypes.c_int, ctypes.c_int, wt.HICON, ctypes.c_int, ctypes.c_int, wt.UINT,
                                      wt.HBRUSH, wt.UINT)),
        (user32.TrackMouseEvent, wt.BOOL, (ctypes.POINTER(TRACKMOUSEEVENT),)),
        (user32.PostMessageW, wt.BOOL, (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)),
        (user32.SetCapture, wt.HWND, (wt.HWND,)),
        (user32.ReleaseCapture, wt.BOOL, ()),
        (user32.KillTimer, wt.BOOL, (wt.HWND, ctypes.c_size_t)),
        (user32.SystemParametersInfoW, wt.BOOL, (wt.UINT, wt.UINT, wt.LPVOID, wt.UINT)),
        (user32.LoadCursorW, wt.HANDLE, (wt.HINSTANCE, ctypes.c_void_p)),
        (user32.GetLastInputInfo, wt.BOOL, (ctypes.POINTER(LASTINPUTINFO),)),
        (user32.EnumDisplayMonitors, wt.BOOL, (wt.HDC, ctypes.c_void_p, MONITORENUMPROC, wt.LPARAM)),
        (user32.SetTimer, ctypes.c_size_t, (wt.HWND, ctypes.c_size_t, wt.UINT, wt.LPVOID)),
        (user32.GetMessageW, wt.BOOL, (ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)),
        (user32.DispatchMessageW, ctypes.c_ssize_t, (ctypes.POINTER(wt.MSG),)),
        (user32.GetCursorPos, wt.BOOL, (ctypes.POINTER(wt.POINT),)),
        (user32.CreateIcon, wt.HICON, (wt.HINSTANCE, ctypes.c_int, ctypes.c_int, ctypes.c_ubyte, ctypes.c_ubyte,
                                       ctypes.c_char_p, ctypes.c_char_p)),
        (user32.CreatePopupMenu, wt.HMENU, ()),
        (user32.AppendMenuW, wt.BOOL, (wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR)),
        (user32.TrackPopupMenu, wt.BOOL, (wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND,
                                          wt.LPVOID)),
        (user32.DestroyMenu, wt.BOOL, (wt.HMENU,)),
        (shell32.Shell_NotifyIconW, wt.BOOL, (wt.DWORD, ctypes.POINTER(NOTIFYICONDATA))),
        (gdi32.CreateFontW, wt.HANDLE, (ctypes.c_int,) * 5 + (wt.DWORD,) * 8 + (wt.LPCWSTR,)),
        (gdi32.CreateCompatibleDC, wt.HDC, (wt.HDC,)),
        (gdi32.CreateCompatibleBitmap, wt.HANDLE, (wt.HDC, ctypes.c_int, ctypes.c_int)),
        (gdi32.SelectObject, wt.HANDLE, (wt.HDC, wt.HANDLE)),
        (gdi32.DeleteObject, wt.BOOL, (wt.HANDLE,)),
        (gdi32.CreateSolidBrush, wt.HBRUSH, (wt.DWORD,)),
        (gdi32.SetBkMode, ctypes.c_int, (wt.HDC, ctypes.c_int)),
        (gdi32.SetTextColor, wt.DWORD, (wt.HDC, wt.DWORD)),
        (gdi32.BitBlt, wt.BOOL, (wt.HDC,) + (ctypes.c_int,) * 4 + (wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD)),
        (dwmapi.DwmSetWindowAttribute, ctypes.c_long, (wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD)),
        (gdiplus.GdiplusStartup, ctypes.c_int, (ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(GDIPLUS_STARTUP),
                                                ctypes.c_void_p)),
        (gdiplus.GdipCreateFromHDC, ctypes.c_int, (wt.HDC, ctypes.POINTER(ctypes.c_void_p))),
        (gdiplus.GdipSetSmoothingMode, ctypes.c_int, (ctypes.c_void_p, ctypes.c_int)),
        (gdiplus.GdipCreateSolidFill, ctypes.c_int, (ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p))),
        (gdiplus.GdipCreatePath, ctypes.c_int, (ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))),
        (gdiplus.GdipAddPathArc, ctypes.c_int, (ctypes.c_void_p,) + (ctypes.c_float,) * 6),
        (gdiplus.GdipClosePathFigure, ctypes.c_int, (ctypes.c_void_p,)),
        (gdiplus.GdipFillPath, ctypes.c_int, (ctypes.c_void_p,) * 3),
        (gdiplus.GdipDeleteBrush, ctypes.c_int, (ctypes.c_void_p,)),
        (gdiplus.GdipDeletePath, ctypes.c_int, (ctypes.c_void_p,)),
        (gdiplus.GdipDeleteGraphics, ctypes.c_int, (ctypes.c_void_p,)),
        (powrprof.PowerGetActiveScheme, wt.DWORD, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))),
        (powrprof.PowerSetActiveScheme, wt.DWORD, (ctypes.c_void_p, ctypes.c_char_p)),
        (powrprof.PowerReadACValueIndex, wt.DWORD, (ctypes.c_void_p,) + GUIDS + (ctypes.POINTER(wt.DWORD),)),
        (powrprof.PowerWriteACValueIndex, wt.DWORD, (ctypes.c_void_p,) + GUIDS + (wt.DWORD,)),
        (powrprof.IsPwrHibernateAllowed, ctypes.c_ubyte, ()),
        (dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR, wt.BOOL, (wt.HMONITOR, ctypes.POINTER(wt.DWORD))),
        (dxva2.GetPhysicalMonitorsFromHMONITOR, wt.BOOL, (wt.HMONITOR, wt.DWORD, ctypes.POINTER(PHYSICAL_MONITOR))),
        (dxva2.GetVCPFeatureAndVCPFeatureReply, wt.BOOL, (wt.HANDLE, ctypes.c_ubyte) + (ctypes.POINTER(wt.DWORD),) * 3),
        (dxva2.SetVCPFeature, wt.BOOL, (wt.HANDLE, ctypes.c_ubyte, wt.DWORD)),
        (dxva2.DestroyPhysicalMonitor, wt.BOOL, (wt.HANDLE,))):
    func.restype, func.argtypes = restype, argtypes


# ---------- питание: схема и таймеры Windows, яркость монитора ----------
def guid(text):
    return uuid.UUID(text).bytes_le


def active_scheme():
    p = ctypes.c_void_p()
    if powrprof.PowerGetActiveScheme(None, ctypes.byref(p)):
        return None
    text = str(uuid.UUID(bytes_le=ctypes.string_at(p, 16)))
    kernel32.LocalFree(p)
    return text


def set_scheme(scheme):
    return powrprof.PowerSetActiveScheme(None, guid(scheme)) == 0


def set_mode(eco):
    """Режим питания - через утилиту владельца: в «Экономии» она заодно отключает турбобуст."""
    try:
        return subprocess.run([AHK, str(POWER_MODE), "eco" if eco else "high"], timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def timer(name, seconds=None, scheme=SAVER):
    """Таймер простоя «от сети» в схеме (по умолчанию «Экономия энергии»): прочитать или (если задано seconds) записать."""
    ids = [guid(g) for g in (scheme,) + TIMERS[name]]
    if seconds is not None:
        return powrprof.PowerWriteACValueIndex(None, *ids, seconds) == 0
    value = wt.DWORD()
    if powrprof.PowerReadACValueIndex(None, *ids, ctypes.byref(value)):
        raise OSError(f"не читается таймер «{name}»")
    return value.value


def brightness(new=None):
    """Яркость монитора по DDC/CI (код 0x10): прочитать или (если задано new) поставить и перечитать.

    -> яркость на мониторе после вызова; None - монитор не ответил (спит, выключен). Успеху записи верим
    только по контрольному чтению: монитор может принять команду и не применить её (ECO Mode в его меню).
    """
    handles = []

    def each(hmon, hdc, rect, lparam):
        n = wt.DWORD()
        if dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(n)) and n.value:
            found = (PHYSICAL_MONITOR * n.value)()
            if dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, n.value, found):
                handles.extend(m.handle for m in found)
        return True

    def read(handle):
        seen = []
        for _ in range(3):                            # монитор отвечает медленно: пауза и три чтения, иначе мусор
            time.sleep(0.1)
            now, top, kind = wt.DWORD(), wt.DWORD(), wt.DWORD()
            if dxva2.GetVCPFeatureAndVCPFeatureReply(handle, 0x10, ctypes.byref(kind), ctypes.byref(now), ctypes.byref(top)):
                seen.append(now.value)
        return max(set(seen), key=seen.count) if seen else None

    with BRIGHT_LOCK:
        return _brightness(new, handles, each, read)


def _brightness(new, handles, each, read):
    result = None
    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(each), 0)
    for handle in handles:
        if result is None:
            result = read(handle)
            if result is not None and new is not None:
                for _ in range(3):
                    time.sleep(0.3)                   # сразу после чтения запись не проходит
                    if dxva2.SetVCPFeature(handle, 0x10, new):
                        break
                time.sleep(0.35)
                result = read(handle)
        dxva2.DestroyPhysicalMonitor(handle)
    return result


def put_brightness(level, was):
    """Поставить яркость и записать в журнал, что вышло.

    -> None - монитор не ответил (попробуем позже); "" - готово; иначе - что сказать владельцу.
    """
    now = level if DRY else brightness(level)
    if now is None:
        return None
    if now == level:
        log(("сухой прогон, изменил бы: " if DRY else "") + f"яркость монитора: {was} -> {level}")
        return ""
    log(f"НЕ УДАЛОСЬ: яркость монитора: {was} -> {level}, осталась {now}")
    return (f"Монитор принял команду, но яркость осталась {now} (нужно {level}). "
            "Скорее всего, в меню монитора включён ECO Mode - он блокирует яркость.")


def idle_seconds():
    """Сколько секунд не трогали мышь и клавиатуру."""
    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    user32.GetLastInputInfo(ctypes.byref(info))
    return (kernel32.GetTickCount() - info.dwTime) % 2 ** 32 / 1000


def act(text, func, *args):
    """Изменить настройку питания и записать это в журнал -> удалось ли. В сухом прогоне - только записать."""
    if DRY:
        log("сухой прогон, изменил бы: " + text)
        return True
    ok = func(*args)
    log(text if ok else "НЕ УДАЛОСЬ: " + text)
    return bool(ok)


# ---------- менеджер питания: экономия, пока сети нет ----------
def load_state():
    """Что запомнено о настройках до отключения; None - экономию мы не включали."""
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def save_state(state):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)                            # целиком или никак: файл не должен остаться обрезанным


def load_outage():
    """Настройки экономии без сети, выбранные в панели (или по умолчанию)."""
    try:
        saved = json.loads(OUTAGE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return {key: saved.get(key, value) for key, value in OUTAGE_DEFAULT.items()}


def save_outage(settings):
    tmp = OUTAGE.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=1), encoding="utf-8")
    os.replace(tmp, OUTAGE)


def outage_timers(settings):
    """Таймеры Windows на время без сети. Гибернация включена в Windows - она вместо сна."""
    hibernate = bool(powrprof.IsPwrHibernateAllowed())
    return {"экран": settings["screen"], "диски": settings["disks"], "сон": 0 if hibernate else settings["sleep"],
            "гибернация": settings["sleep"] if hibernate else 0}


def change_outage(key, value):
    """Владелец поменял настройку в панели. Экономия уже включена - применить сразу; прежние значения
    (до отключения) в power_state.json не трогаем, с сетью вернутся они. -> что сказать владельцу."""
    settings = load_outage()
    settings[key] = value
    save_outage(settings)
    log(f"настройка экономии без сети: {key} = {value}")
    state = load_state()
    if not (state and state["active"]):
        return ""
    if key == "brightness":
        if state.get("manual") or state["brightness"] is None:
            return ""                                 # яркость он уже ставил руками - не перебиваем
        problem = put_brightness(min(value, state["brightness"]), "экономная")
        if problem is not None:
            state["dimmed"] = True
            save_state(state)
        return problem or ""
    for name, seconds in outage_timers(settings).items():
        act(f"{name}: -> {seconds} с", timer, name, seconds)
    if not DRY and active_scheme() == SAVER:          # изменённая активная схема действует после повторного включения
        set_scheme(SAVER)
    return ""


HIGH_KEYS = {"screen": "экран", "disks": "диски", "sleep": "сон", "hibernate": "гибернация"}


def high_timers():
    """Таймеры схемы «Высокая производительность» - как они записаны в Windows сейчас."""
    return {key: timer(name, scheme=HIGH) for key, name in HIGH_KEYS.items()}


def change_high(key, seconds):
    """Владелец поменял таймер «Высокой» в панели: записать в схему; если она включена - применить сразу.
    Автоматика отключения пишет только в «Экономию энергии», эти значения она не трогает."""
    ok = act(f"«Высокая производительность», {HIGH_KEYS[key]}: -> {seconds} с", timer, HIGH_KEYS[key], seconds, HIGH)
    if ok and not DRY and active_scheme() == HIGH:
        set_scheme(HIGH)                              # изменённая активная схема действует после повторного включения
    return ok


def manual_brightness(level):
    """Яркость поставлена ползунком. -> (яркость на мониторе после записи или None, что сказать владельцу).

    Во время отключения ручной выбор главнее автоматики: с сетью прежнюю яркость поверх него не возвращаем.
    """
    now = level if DRY else brightness(level)
    log(f"яркость выбрана руками: {level}" + ("" if now == level else f", на мониторе {now}"))
    state = load_state()
    if state and now is not None:
        state["manual"] = True
        save_state(state)
    if now is None:
        return None, "Монитор не отвечает."
    if now != level:
        return now, f"Монитор оставил {now}: вероятно, в его меню включён ECO Mode."
    return now, ""


def economy(previous):
    """Сети нет: запомнить, как было, потом включить экономию.

    previous - запись от прошлого отключения, если яркость после него ещё не вернули (монитор спал).
    """
    new = outage_timers(load_outage())
    state = {"active": True, "since": int(time.time()), "scheme": active_scheme(),
             "timers": {name: timer(name) for name in TIMERS},
             "brightness": previous["brightness"] if previous else None,
             "dimmed": bool(previous and previous["dimmed"])}
    save_state(state)                                 # сначала запомнить, потом менять
    log(f"сети нет - включаю экономию; было: режим {MODES.get(state['scheme'], state['scheme'])}, "
        f"таймеры {state['timers']}")
    for name, seconds in new.items():
        act(f"{name}: {state['timers'][name]} -> {seconds} с", timer, name, seconds)
    # утилита включает схему - с этого момента новые таймеры действуют
    if not act("режим: экономия энергии (утилита «Режим питания»)", set_mode, True):
        act("режим: экономия энергии (напрямую)", set_scheme, SAVER)
    return dim(state)


def dim(state):
    """Убавить яркость монитора, запомнив прежнюю. Монитор молчит (спит) - повторим, когда проснётся.

    -> что сказать владельцу, если яркость не поддалась.
    """
    if state.get("manual"):
        return None
    if state["brightness"] is None:
        state["brightness"] = brightness()
        if state["brightness"] is None:
            return None
        save_state(state)
    problem = put_brightness(min(load_outage()["brightness"], state["brightness"]), state["brightness"])
    if problem is not None:
        state["dimmed"] = True                        # пробовали; с сетью вернём прежнюю яркость
        save_state(state)
    return problem


def restore(state):
    """Сеть вернулась: вернуть таймеры и режим, потом яркость. -> что сказать владельцу, если не всё вышло."""
    log("сеть вернулась - возвращаю как было")
    if not all([act(f"{name}: {seconds} с", timer, name, seconds) for name, seconds in state["timers"].items()]):
        return None                                   # не вышло - повторим на следующем опросе
    now = active_scheme()
    if DRY or now == SAVER:                           # та же схема применится заново - уже с прежними таймерами
        act(f"режим: {MODES.get(state['scheme'], state['scheme'])}", set_scheme, state["scheme"])
    else:
        log(f"режим не трогаю: сейчас «{MODES.get(now, now)}», его выбрали руками")
    state["active"] = False
    save_state(state)
    return undim(state)


def undim(state):
    """Вернуть яркость и забыть запись. Монитор молчит (спит) - запись остаётся, повторим, когда проснётся."""
    if state.get("manual"):
        log("яркость не возвращаю: во время отключения её поставили руками")
    problem = put_brightness(state["brightness"], "экономная") if state["dimmed"] and not state.get("manual") else ""
    if problem is not None:
        STATE.unlink()
    return problem


def manage(grid):
    """grid - устойчивое состояние сети: True / False; None - неизвестно, тогда режим и таймеры не трогаем.

    -> что сказать владельцу (яркость монитора не поддалась), иначе пусто.
    """
    state = load_state()
    if grid is False and not (state and state["active"]):
        return economy(state)
    if grid and state and state["active"]:
        return restore(state)
    if state and idle_seconds() < 60:                 # отложенная яркость: за ПК кто-то есть, монитор не спит
        if not state["active"]:
            return undim(state)
        if not state["dimmed"]:
            return dim(state)


# ---------- панель по левому щелчку: рисуем сами, в цветах темы Windows ----------
PANEL_W = 400                                         # ширина панели в точках при 100 %
# цвета 0xRRGGBB: фон, плитка, плитка под курсором и нажатая, текст, второстепенный текст, тревога, рамка окна
THEMES = {True: dict(bg=0x202020, card=0x2D2D2D, hover=0x3B3B3B, press=0x343434, text=0xFFFFFF, dim=0xA6A6A6,
                     warn=0xE3B341, border=0x454545, knob=0x454545, chip=0x383838),
          False: dict(bg=0xF3F3F3, card=0xFFFFFF, hover=0xE8E8E8, press=0xEEEEEE, text=0x1B1B1B, dim=0x5E5E5E,
                      warn=0x8A5A00, border=0xC4C4C4, knob=0xFFFFFF, chip=0xF0F0F0)}
# шрифты: (размер в пунктах, насыщенность, гарнитура)
FONTS = {"title": (15, 600, "Segoe UI Semibold"), "value": (18, 600, "Segoe UI Semibold"),
         "big": (24, 600, "Segoe UI Semibold"), "label": (10, 400, "Segoe UI"), "button": (11, 400, "Segoe UI"),
         "current": (11, 600, "Segoe UI Semibold")}


def windows_dark():
    """Тёмная ли тема приложений в настройках Windows; узнать не удалось - считаем, что тёмная."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return True


def theme():
    """Цвета панели под нынешнюю тему Windows; акцент - из палитры Windows, как у её собственных кнопок."""
    dark = windows_dark()
    colors = dict(THEMES[dark], dark=dark, accent=0x60CDFF if dark else 0x005FB8,
                  on_accent=0x000000 if dark else 0xFFFFFF)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent") as key:
            shades = winreg.QueryValueEx(key, "AccentPalette")[0]     # 8 оттенков по 4 байта: R, G, B, 0
        r, g, b = shades[4:7] if dark else shades[16:19]              # на тёмном - светлый оттенок, на светлом - тёмный
        colors["accent"] = r << 16 | g << 8 | b
    except (OSError, ValueError):
        pass
    # выбранный режим - приглушённый акцент: заливка и обводка, подмешанные к фону
    base = colors["card"]
    colors["sel"] = mix(colors["accent"], base, 0.30 if dark else 0.14)
    colors["sel_edge"] = mix(colors["accent"], base, 0.60 if dark else 0.45)
    colors["sel_text"] = colors["text"] if dark else colors["accent"]
    return colors


def mix(a, b, share):
    """Цвет a, подмешанный к b в доле share (0xRRGGBB)."""
    return sum(round((a >> s & 255) * share + (b >> s & 255) * (1 - share)) << s for s in (16, 8, 0))


def panel_fonts(scale):
    return {name: gdi32.CreateFontW(-round(pt * 96 / 72 * scale), 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, face)
            for name, (pt, weight, face) in FONTS.items()}


def paint_panel(dc, fonts, scale, view, colors, icon, hot=None, down=False):
    """Нарисовать панель в dc от левого верхнего угла.

    hot - номер кнопки под курсором, down - зажата ли кнопка мыши.
    -> (высота в точках экрана, {номер кнопки: (лево, верх, право, низ)} - куда можно нажать).
    """
    shapes, texts, buttons = [], [], {}               # размеры ниже - в точках при 100 %, px() переводит в экранные

    def px(v):
        return round(v * scale)

    def text(string, font, color, x, y, width, height=0, flags=0x24):
        """Запомнить надпись. Без height - с переносом по словам, высота по тексту. -> высота."""
        if not height:
            flags = 0x10
            box = wt.RECT(0, 0, px(width), 0)
            gdi32.SelectObject(dc, fonts[font])
            height = user32.DrawTextW(dc, string, -1, ctypes.byref(box), 0xC10) / scale       # только измерить
        texts.append((string, font, color, wt.RECT(px(x), px(y), px(x + width), px(y + height)), flags | 0x800))
        return height

    pad, inner = 16, PANEL_W - 32
    y = 16
    text(view["title"], "title", colors["text"], pad + 36, y, inner - 36, 28)
    y += 38
    if view["alert"]:
        y += text(view["alert"], "label", colors["warn" if view["kind"] == "fault" else "dim"], pad, y, inner) + 8
    half = (inner - 8) / 2
    for i, (label, value, sub) in enumerate(view["tiles"]):
        x = pad + i * (half + 8)
        shapes.append((x, y, half, 90, 7, colors["card"]))
        text(label, "label", colors["dim"], x + 14, y + 10, half - 28, 18)
        text(value, "value", colors["text"], x + 14, y + 30, half - 28, 32)
        text(sub, "label", colors["dim"], x + 14, y + 63, half - 28, 18)
    y += 98
    label, value = view["left"]
    height = 82
    text(label, "label", colors["dim"], pad + 14, y + 10, inner - 28, 18)
    text(value, "big", colors["text"], pad + 14, y + 30, inner - 28, 42)
    if view["note"]:
        height += text(view["note"], "label", colors["dim"], pad + 14, y + 78, inner - 28) + 8
    shapes.append((pad, y, inner, height, 7, colors["card"]))
    y += height + 16

    # яркость монитора: ползунок; пишется в монитор, когда отпустили
    level = view.get("bright")
    text("Яркость монитора", "label", colors["dim"], pad, y, inner - 120, 20)
    text(view.get("bright_note") or ("…" if level is None else f"{level} %"), "button" if view.get("bright_note") else
         "current", colors["warn" if view.get("bright_note") else "text"], pad + 100, y, inner - 100, 20, 0x26)
    y += 26
    left, right = pad + 11, pad + inner - 11          # центр бегунка: края бегунка - вровень с плитками
    middle = y + 12
    share = (level or 0) / 100
    shapes.append((pad, middle - 2, inner, 4, 2, colors["border"]))
    if level is not None:
        shapes.append((pad, middle - 2, left - pad + (right - left) * share, 4, 2, colors["accent"]))
        knob = left + (right - left) * share
        grow = 1 if hot == "slider" else 0
        shapes.append((knob - 11, middle - 11, 22, 22, 11, colors["border"]))
        shapes.append((knob - 10, middle - 10, 20, 20, 10, colors["knob"]))
        shapes.append((knob - 5 - grow, middle - 5 - grow, 10 + 2 * grow, 10 + 2 * grow, 5 + grow, colors["accent"]))
        buttons["slider"] = (px(left), px(y), px(right), px(y + 24))
    y += 36

    text("Режим питания" + ("" if view["scheme"] in MODES else ": другая схема"), "label", colors["dim"], pad, y, inner, 18)
    y += 24
    if view["auto"]:
        y += text(view["auto"], "label", colors["dim"], pad, y, inner) + 8

    def button(ident, name, current=False):
        nonlocal y
        fill = "accent" if current else ("press" if down else "hover") if hot == ident else "card"
        shapes.append((pad, y, inner, 36, 5, colors[fill]))
        text(name, "current" if current else "button", colors["on_accent" if current else "text"], pad, y, inner, 36, 0x25)
        if not current:                               # нынешний режим выделен цветом и не нажимается
            buttons[ident] = (px(pad), px(y), px(pad + inner), px(y + 36))
        y += 42

    # режим: переключатель на две половины, выбранная - приглушённым акцентом
    shapes.append((pad, y, inner, 38, 6, colors["card"]))
    half = inner / 2
    for i, (ident, name, scheme) in enumerate(((ID_ECO, "Экономия", SAVER), (ID_HIGH, "Производительность", HIGH))):
        x = pad + i * half
        current = view["scheme"] == scheme
        if current:
            shapes.append((x + 3, y + 3, half - 6, 32, 4, colors["sel_edge"]))
            shapes.append((x + 4, y + 4, half - 8, 30, 4, colors["sel"]))
        elif hot == ident:
            shapes.append((x + 3, y + 3, half - 6, 32, 4, colors["press" if down else "hover"]))
        text(name, "current" if current else "button", colors["sel_text" if current else "text"], x, y, half, 38, 0x25)
        if not current:                               # нынешний режим не нажимается
            buttons[ident] = (px(x), px(y), px(x + half), px(y + 38))
    y += 46

    # настройки экономии без света: раскрываются щелчком по заголовку
    # таймеры: «Экономия без света» (свои настройки, outage.json) и «Высокая производительность» (сама схема Windows)
    blocks = []
    if view.get("outage"):
        blocks.append(("expand", "Экономия без света", view.get("expanded"), view["outage"], "",
                       (("screen", "Гасить экран через"), ("disks", "Отключать диски через"),
                        ("sleep", "Гибернация через" if view.get("hibernate") else "Сон через"),
                        ("brightness", "Яркость без света")),
                       "Света нет - изменения действуют сразу; с сетью вернутся прежние настройки."
                       if view.get("active") else "Включится само, когда пропадёт свет; с сетью всё вернётся."))
    if view.get("high"):
        blocks.append(("expand_high", "Высокая производительность", view.get("expanded_high"), view["high"], "high:",
                       (("screen", "Гасить экран через"), ("disks", "Отключать диски через"),
                        ("sleep", "Сон через"), ("hibernate", "Гибернация через")),
                       "Таймеры самой схемы Windows: действуют в этом режиме, автоматика их не трогает."))
    for expand, title, expanded, values, prefix, rows, note in blocks:
        y += 6 * (expand != blocks[0][0])
        button(expand, ("▾  " if expanded else "▸  ") + title)
        if expanded:
            top = y
            for key, label in rows:
                value = values[key]
                key = prefix + key
                shown = (f"{value} %" if key == "brightness" else "никогда" if not value else
                         f"{value // 3600} ч" if value % 3600 == 0 else duration(value))
                text(label, "button", colors["text"], pad + 14, y, inner - 170, 36)
                for sign, x in (("−", pad + inner - 150), ("+", pad + inner - 40)):
                    ident = (sign, key)
                    shapes.append((x + 2, y + 5, 26, 26, 13, colors[("press" if down else "hover") if hot == ident
                                                                      else "chip"]))
                    text(sign, "current", colors["text"], x + 2, y + 4, 26, 26, 0x25)
                    buttons[ident] = (px(x - 4), px(y), px(x + 34), px(y + 36))
                text(shown, "current", colors["text"], pad + inner - 120, y, 76, 36, 0x25)
                y += 38
            shapes.append((pad, top - 4, inner, y - top + 6, 7, colors["card"]))
            shapes.insert(0, shapes.pop())            # подложка под строками, а не поверх
            y += 8
            y += text(note, "label", colors["dim"], pad, y, inner) + 10
    # «Графики и журнал» - скромная ссылка справа внизу
    link = "Графики и журнал  ›"
    gdi32.SelectObject(dc, fonts["button"])
    box = wt.RECT(0, 0, 0, 0)
    user32.DrawTextW(dc, link, -1, ctypes.byref(box), 0xC20)    # только измерить ширину
    link_w = box.right / scale + 8
    text(link, "current" if hot == ID_PAGE else "button", colors["accent"], pad + inner - link_w, y, link_w, 26, 0x26)
    buttons[ID_PAGE] = (px(pad + inner - link_w), px(y), px(pad + inner), px(y + 26))
    y += 30
    total = px(y + 8)

    brush = gdi32.CreateSolidBrush(bgr(colors["bg"]))
    user32.FillRect(dc, ctypes.byref(wt.RECT(0, 0, px(PANEL_W), total)), brush)
    gdi32.DeleteObject(brush)
    graphics = ctypes.c_void_p()                      # плитки со сглаженными углами - через GDI+
    gdiplus.GdipCreateFromHDC(dc, ctypes.byref(graphics))
    gdiplus.GdipSetSmoothingMode(graphics, 4)
    for x, top, width, height, radius, color in shapes:
        path, fill = ctypes.c_void_p(), ctypes.c_void_p()
        gdiplus.GdipCreatePath(0, ctypes.byref(path))
        x, top, right, bottom, d = px(x), px(top), px(x + width), px(top + height), px(radius) * 2
        for left, up, angle in ((x, top, 180), (right - d, top, 270), (right - d, bottom - d, 0), (x, bottom - d, 90)):
            gdiplus.GdipAddPathArc(path, left, up, d, d, angle, 90)
        gdiplus.GdipClosePathFigure(path)
        gdiplus.GdipCreateSolidFill(0xFF000000 | color, ctypes.byref(fill))
        gdiplus.GdipFillPath(graphics, fill, path)
        gdiplus.GdipDeleteBrush(fill)
        gdiplus.GdipDeletePath(path)
    gdiplus.GdipDeleteGraphics(graphics)
    user32.DrawIconEx(dc, px(pad), px(18), icon, px(24), px(24), 0, None, 3)
    gdi32.SetBkMode(dc, 1)                            # надписи без своего фона
    for string, font, color, box, flags in texts:
        gdi32.SelectObject(dc, fonts[font])
        gdi32.SetTextColor(dc, bgr(color))
        user32.DrawTextW(dc, string, -1, ctypes.byref(box), flags)
    return total, buttons


def bgr(rgb):
    """0xRRGGBB -> COLORREF Windows (0x00BBGGRR)."""
    return (rgb & 0xFF) << 16 | rgb & 0xFF00 | rgb >> 16


# ---------- значок ----------
def in_bolt(x, y):
    hit = False
    for (x1, y1), (x2, y2) in zip(BOLT, BOLT[1:] + BOLT[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def make_icon(rgb, n):
    """Круг цвета rgb с белой молнией, n x n точек со сглаживанием -> HICON."""
    xor, mask = bytearray(), bytearray()
    steps = [(i + 0.5) / 4 * 32 / n for i in range(4)]            # 4x4 пробы на точку, в сетке 32x32
    for y in range(n):
        row = 0
        for x in range(n):
            pts = [(x * 32 / n + dx, y * 32 / n + dy) for dy in steps for dx in steps]
            disc = [p for p in pts if (p[0] - 16) ** 2 + (p[1] - 16) ** 2 <= 256]
            row = row << 1 | (not disc)
            if disc:
                white = sum(in_bolt(*p) for p in disc) / len(disc)
                xor += bytes([round(c + (255 - c) * white) for c in reversed(rgb)] + [len(disc) * 255 // 16])  # BGRA
            else:
                xor += bytes(4)
        mask += (row << -n % 16).to_bytes((n + 15) // 16 * 2, "big")   # строки маски - кратно 16 битам
    return user32.CreateIcon(kernel32.GetModuleHandleW(None), n, n, 1, 32, bytes(mask), bytes(xor))


def main():
    if user32.FindWindowW(CLASS_NAME, None):          # уже запущен - второй значок не нужен
        return
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    size = user32.GetSystemMetrics(49)                # SM_CXSMICON: размер значков у часов
    scale = user32.GetDpiForSystem() / 96
    icons = {kind: make_icon(rgb, size) for kind, rgb in COLORS.items()}
    taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
    nid = NOTIFYICONDATA(cbSize=ctypes.sizeof(NOTIFYICONDATA), uID=1, uFlags=NIF_BASE, uCallbackMessage=WM_TRAY)
    shown, seen = [None], [None]                      # что сейчас на значке; номер последнего показанного события
    latest = [None, "ещё нет данных", None]           # последние показания: словарь, почему их нет, вид значка
    grid_first = [None, None]                         # состояние сети и время измерения, где его увидели впервые
    # панель: окно, когда спрятана, цвета темы, высота, кнопка под курсором, зажата ли мышь, куда можно нажать
    panel = {"hidden_at": 0.0, "colors": None, "height": 0, "hot": None, "down": False, "buttons": {},
             # яркость: на мониторе (или выбранная), что не так, куда тянут, идёт ли запрос, тянут ли ползунок
             "bright": None, "bright_note": "", "want": None, "busy": False, "result": None, "drag": False,
             "expanded": False, "expanded_high": False}
    width = round(PANEL_W * scale)
    big_icons = {kind: make_icon(rgb, round(24 * scale)) for kind, rgb in COLORS.items()}
    fonts = panel_fonts(scale)
    gdiplus.GdiplusStartup(ctypes.byref(ctypes.c_void_p()), ctypes.byref(GDIPLUS_STARTUP(1)), None)
    screen = user32.GetDC(None)
    canvas = gdi32.CreateCompatibleDC(screen)         # панель рисуется сюда, в окно копируется готовой - без мерцания
    gdi32.SelectObject(canvas, gdi32.CreateCompatibleBitmap(screen, width, round(900 * scale)))
    user32.ReleaseDC(None, screen)

    def notify(title, text, level):
        """Всплывающее уведомление Windows; звук - только у «внимание» и «ошибка»."""
        nid.uFlags = NIF_BASE | NIF_INFO
        nid.szInfoTitle, nid.szInfo = title[:63], text[:255]
        nid.dwInfoFlags = level | (NIIF_NOSOUND if level == 1 else 0)
        ok = shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        nid.uFlags = NIF_BASE                         # иначе каждое обновление значка покажет его снова
        log(f"уведомление {'показано' if ok else 'НЕ принято Windows'}: {title}")

    def stable_grid(d):
        """Сеть по данным сервиса (дребезг он уже отсеял), подтверждённая вторым измерением; иначе None."""
        grid = d.get("grid_ok") if d else None
        if grid is None or grid != grid_first[0]:
            grid_first[:] = [grid, d and d["ts"]]
            return None
        return grid if d["ts"] != grid_first[1] else None

    def draw_panel():
        """Нарисовать панель по последним показаниям; изменилась высота - подвинуть окно (правый нижний угол)."""
        d, why, kind = latest
        view = panel_view(d, why, kind)
        state = load_state()
        view.update(bright=panel["bright"], bright_note=panel["bright_note"], outage=load_outage(),
                    expanded=panel["expanded"], hibernate=bool(powrprof.IsPwrHibernateAllowed()),
                    high=high_timers(), expanded_high=panel["expanded_high"],
                    active=bool(state and state["active"]))
        height, panel["buttons"] = paint_panel(canvas, fonts, scale, view, panel["colors"],
                                               big_icons[kind], panel["hot"], panel["down"])
        if height != panel["height"]:
            panel["height"] = height
            area = wt.RECT()
            user32.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0)      # рабочая область без панели задач
            gap = round(12 * scale)
            user32.SetWindowPos(panel["hwnd"], None, area.right - width - gap, area.bottom - height - gap,
                                width, height, 0x14)
        user32.InvalidateRect(panel["hwnd"], None, False)

    def toggle_panel():
        if user32.IsWindowVisible(panel["hwnd"]):
            user32.ShowWindow(panel["hwnd"], 0)
        elif time.time() - panel["hidden_at"] > 0.4:  # иначе щелчок по значку, закрывший панель, откроет её снова
            colors = panel["colors"] = theme()        # тему Windows смотрим при каждом открытии
            for attribute, value in ((20, colors["dark"]), (33, 2), (34, bgr(colors["border"]))):
                # тёмная рамка, скруглённые углы, цвет рамки; старая Windows этого не умеет - не страшно
                dwmapi.DwmSetWindowAttribute(panel["hwnd"], attribute, ctypes.byref(ctypes.c_int(value)), 4)
            panel.update(height=0, hot=None, down=False, drag=False, bright_note="", expanded=False,
                         expanded_high=False)
            draw_panel()
            user32.ShowWindow(panel["hwnd"], 5)
            user32.SetForegroundWindow(panel["hwnd"])
            ddc()                                     # узнать нынешнюю яркость монитора

    def ddc():
        """Запрос к монитору в отдельном потоке (DDC/CI медленный): записать выбранную яркость, а если
        выбирать нечего - прочитать нынешнюю. Ответ придёт сообщением WM_BRIGHT."""
        if panel["busy"]:
            return                                    # закончится нынешний - запустим снова
        level, panel["want"], panel["busy"] = panel["want"], None, True

        def work():
            panel["result"] = manual_brightness(level) if level is not None else (brightness(), "")
            user32.PostMessageW(panel["hwnd"], WM_BRIGHT, 0, 0)

        threading.Thread(target=work, daemon=True).start()

    def aim(level):
        """Ползунок поставили на level: показать сразу, записать в монитор, когда его оставят на 0,3 с."""
        level = max(0, min(100, level))
        if level != panel["bright"] or panel["drag"]:
            panel.update(bright=level, want=level, bright_note="")
            draw_panel()
            user32.SetTimer(panel["hwnd"], 2, 300, None)

    def step(key, sign):
        """Кнопки − и + в «Экономии без света»."""
        values = BRIGHT_STEPS if key == "brightness" else STEPS
        high = key.startswith("high:")
        now = high_timers()[key[5:]] if high else load_outage()[key]
        order = sorted(values, key=lambda v: v or 10 ** 9)        # «никогда» - в конце
        i = min(range(len(order)), key=lambda j: abs((order[j] or 10 ** 9) - (now or 10 ** 9)))
        i = max(0, min(len(order) - 1, i + (1 if sign == "+" else -1)))
        if order[i] != now and high:
            change_high(key[5:], order[i])
        elif order[i] != now:
            problem = change_outage(key, order[i])
            if problem:
                notify("Яркость монитора не изменилась", problem, 1)
            if key == "brightness" and not panel["busy"]:
                ddc()                                 # могла поменяться яркость на мониторе
        draw_panel()

    def choose(eco):
        """Режим питания выбран руками; автоматика его не перебьёт: она действует только при смене состояния сети."""
        act("режим выбран руками: " + MODES[SAVER if eco else HIGH], set_mode, eco)
        if user32.IsWindowVisible(panel["hwnd"]):
            draw_panel()

    def press(ident):
        """Нажата кнопка панели."""
        if ident in (ID_ECO, ID_HIGH):
            choose(ident == ID_ECO)
        elif ident == ID_PAGE:
            user32.ShowWindow(panel["hwnd"], 0)
            os.startfile(PAGE)
        elif ident in ("expand", "expand_high"):
            name, other = ("expanded", "expanded_high") if ident == "expand" else ("expanded_high", "expanded")
            panel[name], panel[other] = not panel[name], False    # раскрыт один блок: оба сразу не влезут по высоте
            draw_panel()
        elif isinstance(ident, tuple):
            step(ident[1], ident[0])

    def update(add=False):
        d, why, event = fetch()
        kind, lines = describe(d) if d else (None, [why])
        latest[:] = [d, why, kind]
        tip = f"{TITLES[kind]} · " + "\n".join(lines)
        if add or (kind, tip) != shown[0]:
            shown[0] = (kind, tip)
            nid.hIcon, nid.szTip = icons[kind], tip[:127]
            shell32.Shell_NotifyIconW(NIM_ADD if add else NIM_MODIFY, ctypes.byref(nid))
        if event and event["id"] != seen[0]:
            fresh = seen[0] is not None and time.time() - event["ts"] < 120   # старое при запуске не показывать
            seen[0] = event["id"]
            if fresh:
                title, level = NOTES.get(event["kind"], ("Инвертор", 1))
                notify(title, event["text"], level)
        try:
            problem = manage(stable_grid(d))
            if problem:
                notify("Яркость монитора не изменилась", problem, 1)
        except Exception as e:                        # значок из-за менеджера питания падать не должен
            log(f"менеджер питания: ошибка {e!r}")
        if user32.IsWindowVisible(panel["hwnd"]):
            draw_panel()

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_TIMER:
            update()
        elif msg == taskbar_created:                  # проводник перезапустился (или только что загрузился)
            update(add=True)
        elif msg == WM_TEST:
            notify("Проверка", "Уведомления виджета инвертора работают.", 1)
        elif msg == WM_TRAY and lparam == WM_LBUTTONUP:
            toggle_panel()
        elif msg == WM_TRAY and lparam == WM_RBUTTONUP:
            scheme = active_scheme()
            menu = user32.CreatePopupMenu()
            user32.AppendMenuW(menu, 8 * (scheme == SAVER), ID_ECO, "Режим: экономия энергии")       # 8 = галочка
            user32.AppendMenuW(menu, 8 * (scheme == HIGH), ID_HIGH, "Режим: высокая производительность")
            user32.AppendMenuW(menu, 0x800, 0, None)  # разделитель
            user32.AppendMenuW(menu, 0, ID_PAGE, "Графики и журнал")
            user32.AppendMenuW(menu, 0, ID_NOTE, "Проверить уведомление")
            user32.AppendMenuW(menu, 0x800, 0, None)
            user32.AppendMenuW(menu, 0, ID_EXIT, "Выход")
            pt = wt.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(hwnd)          # иначе меню не закроется щелчком мимо
            choice = user32.TrackPopupMenu(menu, 0x0102, pt.x, pt.y, 0, hwnd, None)   # вернуть выбранный пункт
            user32.DestroyMenu(menu)
            if choice == ID_EXIT:
                user32.DestroyWindow(hwnd)
            elif choice == ID_PAGE:
                os.startfile(PAGE)
            elif choice == ID_NOTE:
                notify("Проверка", "Уведомления виджета инвертора работают.", 1)
            elif choice in (ID_ECO, ID_HIGH):
                choose(choice == ID_ECO)
        elif msg == WM_DESTROY:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            user32.PostQuitMessage(0)
        else:
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        return 0

    def panel_proc(hwnd, msg, wparam, lparam):
        if msg == WM_PAINT:
            ps = PAINTSTRUCT()
            gdi32.BitBlt(user32.BeginPaint(hwnd, ctypes.byref(ps)), 0, 0, width, panel["height"], canvas, 0, 0, 0xCC0020)
            user32.EndPaint(hwnd, ctypes.byref(ps))
        elif msg == WM_ERASEBKGND:
            return 1                                  # фон рисуем сами
        elif msg == WM_BRIGHT:                        # монитор ответил
            panel["busy"] = False
            now, note = panel["result"]
            if panel["want"] is not None:
                ddc()                                 # пока писали, ползунок сдвинули ещё
            elif not panel["drag"]:
                panel.update(bright=now if now is not None else panel["bright"],
                             bright_note=note or ("" if now is not None else "Монитор не отвечает"))
                if user32.IsWindowVisible(hwnd):
                    draw_panel()
        elif msg == WM_TIMER and wparam == 2:         # ползунок оставили в покое - пишем в монитор
            user32.KillTimer(hwnd, 2)
            ddc()
        elif msg == WM_MOUSEWHEEL and panel["hot"] == "slider" and panel["bright"] is not None:
            aim(panel["bright"] + (5 if ctypes.c_short(wparam >> 16 & 0xFFFF).value > 0 else -5))
        elif msg == WM_LBUTTONDOWN and panel["hot"] == "slider" or panel["drag"] and msg in (WM_MOUSEMOVE, WM_LBUTTONUP):
            left, _, right, _ = panel["buttons"].get("slider", (0, 0, 1, 0))
            x = ctypes.c_short(lparam & 0xFFFF).value
            if msg == WM_LBUTTONDOWN:
                panel["drag"] = True
                user32.SetCapture(hwnd)
            elif msg == WM_LBUTTONUP:
                panel["drag"] = False
                user32.ReleaseCapture()
            aim(round((x - left) / (right - left) * 100))
            if msg == WM_LBUTTONUP:                   # отпустили - пишем сразу
                user32.KillTimer(hwnd, 2)
                ddc()
        elif msg in (WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSELEAVE):
            x, y = lparam & 0xFFFF, lparam >> 16 & 0xFFFF
            hot = None if msg == WM_MOUSELEAVE else next(
                (ident for ident, (left, top, right, bottom) in panel["buttons"].items()
                 if left <= x < right and top <= y < bottom), None)
            clicked = msg == WM_LBUTTONUP and panel["down"]
            down = msg == WM_LBUTTONDOWN or msg == WM_MOUSEMOVE and panel["down"]
            if msg == WM_MOUSEMOVE:                   # чтобы узнать, когда курсор уйдёт с панели
                user32.TrackMouseEvent(ctypes.byref(TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), 2, hwnd, 0)))
            if (hot, down) != (panel["hot"], panel["down"]):
                panel.update(hot=hot, down=down)
                draw_panel()
            if clicked and hot:
                press(hot)
        elif msg == WM_COMMAND:                       # то же нажатие, но сообщением (для проверок)
            press(wparam & 0xFFFF)
        elif msg == WM_ACTIVATE and not wparam & 0xFFFF or msg == WM_CLOSE:   # щёлкнули мимо - панель прячется
            user32.ShowWindow(hwnd, 0)
            panel["hidden_at"] = time.time()
        else:
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        return 0

    def guarded(proc):
        """Ошибку в обработчике - в журнал (под pythonw её больше никто не увидит), окно живёт дальше."""
        def call(hwnd, msg, wparam, lparam):
            try:
                return proc(hwnd, msg, wparam, lparam)
            except Exception as e:
                log(f"ошибка в окне (сообщение 0x{msg:04X}): {e!r}")
                return 0
        return call

    procs = WNDPROC(guarded(wndproc)), WNDPROC(guarded(panel_proc))   # держим ссылки, пока идёт цикл сообщений
    hinst = kernel32.GetModuleHandleW(None)
    user32.RegisterClassW(ctypes.byref(WNDCLASS(lpfnWndProc=procs[0], hInstance=hinst, lpszClassName=CLASS_NAME)))
    user32.RegisterClassW(ctypes.byref(WNDCLASS(style=0x20000, lpfnWndProc=procs[1], hInstance=hinst,       # с тенью
                                                lpszClassName=PANEL_CLASS, hCursor=user32.LoadCursorW(None, 32512))))
    # обычное скрытое окно, не message-only: иначе «TaskbarCreated» до него не дойдёт
    nid.hWnd = user32.CreateWindowExW(0, CLASS_NAME, "Свет", 0, 0, 0, 0, 0, None, None, hinst, None)
    # панель: всплывающее окно без рамки поверх остальных, без кнопки на панели задач
    panel["hwnd"] = user32.CreateWindowExW(0x88, PANEL_CLASS, "Инвертор", 0x80000000, 0, 0, 0, 0, None, None, hinst, None)
    update(add=True)
    user32.SetTimer(nid.hWnd, 1, 5000, None)
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
