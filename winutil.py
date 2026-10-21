"""窗口定位与面板跟随（Windows 专用，ctypes 实现，无第三方依赖）。

对外提供：
  list_windows()      -> [{hwnd, title, cls, pid, left, top, right, bottom, visible}]
  find_window(title, cls) -> hwnd 或 0
  monitor_rect(idx)   -> (left, top, right, bottom)
"""
import ctypes
import ctypes.wintypes as wt

user32 = ctypes.windll.user32

EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                ("right", wt.LONG), ("bottom", wt.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wt.DWORD)]


def _text(hwnd, kind="title"):
    n = 512
    buf = ctypes.create_unicode_buffer(n)
    if kind == "title":
        user32.GetWindowTextW(hwnd, buf, n)
    else:
        user32.GetClassNameW(hwnd, buf, n)
    return buf.value


def _rect(hwnd):
    r = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return (0, 0, 0, 0)
    return (r.left, r.top, r.right, r.bottom)


def list_windows(visible_only=True):
    out = []

    def cb(hwnd, _):
        if visible_only and not user32.IsWindowVisible(hwnd):
            return True
        title = _text(hwnd)
        cls = _text(hwnd, "class")
        if not title and not cls:
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        l, t, r, b = _rect(hwnd)
        out.append({"hwnd": int(hwnd), "title": title, "cls": cls, "pid": pid.value,
                    "left": l, "top": t, "right": r, "bottom": b,
                    "visible": bool(user32.IsWindowVisible(hwnd))})
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return out


def _score(win, title, cls):
    """返回匹配得分，0 表示不匹配。标题精确 > 标题包含 > 类名包含。"""
    s = 0
    if title:
        t = win["title"]
        if t == title:
            s += 100
        elif title.lower() in t.lower():
            s += 50
    if cls:
        c = win["cls"]
        if c == cls:
            s += 40
        elif cls.lower() in c.lower():
            s += 25
    return s


def find_window(title="", cls=""):
    """找窗口：title/cls 都是子串匹配，任一为空则忽略该条件。"""
    if not title and not cls:
        return 0
    best, best_s = 0, 0
    for w in list_windows():
        if w["right"] - w["left"] < 80 or w["bottom"] - w["top"] < 60:
            continue
        s = _score(w, title, cls)
        if s > best_s:
            best, best_s = w["hwnd"], s
    return best


def window_rect(hwnd):
    return _rect(hwnd)


def monitor_rect(idx=0):
    """返回第 idx 块显示器的 (left, top, right, bottom)，越界自动回退主屏。"""
    mons = []

    def cb(hmon, hdc, prc, data):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            mons.append((r.left, r.top, r.right, r.bottom))
        return True

    MonEnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC,
                                     ctypes.POINTER(RECT), wt.LPARAM)
    user32.EnumDisplayMonitors(0, 0, MonEnumProc(cb), 0)
    mons.sort()
    if not mons:
        return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    if 0 <= idx < len(mons):
        return mons[idx]
    return mons[0]


def monitor_count():
    n = [0]

    def cb(hmon, hdc, prc, data):
        n[0] += 1
        return True

    MonEnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC,
                                     ctypes.POINTER(RECT), wt.LPARAM)
    user32.EnumDisplayMonitors(0, 0, MonEnumProc(cb), 0)
    return n[0] or 1
