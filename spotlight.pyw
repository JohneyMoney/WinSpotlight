"""
WinSpotlight – en Spotlight-lignende søkelinje for Windows.

Ctrl+Mellomrom åpner/lukker. Søk etter apper, filer og mapper,
regn ut regnestykker, åpne nettsider eller søk på nettet.

  Enter         åpne valgt treff
  Ctrl+Enter    vis i Utforsker
  Opp/Ned/Tab   flytt markering
  Esc           lukk

Start:              pythonw spotlight.pyw
Start med Windows:  python spotlight.pyw --autostart
Fjern autostart:    python spotlight.pyw --no-autostart
Krever:             pip install pywebview
"""

import ast
import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import operator
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
import winreg
import zlib

import webview

# ---------------------------------------------------------------- innstillinger

HOTKEY_MODS = 0x0002          # MOD_CONTROL  (Alt = 0x0001, Shift = 0x0004, Win = 0x0008)
HOTKEY_VK = 0x20              # VK_SPACE
WIDTH = 720                   # CSS-piksler
SEARCH_URL = "https://www.google.com/search?q="

APP_NAME = "WinSpotlight"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
ICON_DIR = os.path.join(DATA_DIR, "icons")
LOG_FILE = os.path.join(DATA_DIR, "feillogg.txt")
CREATE_NO_WINDOW = 0x08000000

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32
shell32 = ctypes.windll.shell32
ole32 = ctypes.windll.ole32
dwmapi = ctypes.windll.dwmapi

user32.GetForegroundWindow.restype = wt.HWND
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]

# --------------------------------------------------------------- autostart-cli


def startup_link():
    return os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup", APP_NAME + ".lnk")


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def set_autostart(enable):
    """Flere uavhengige sikkerhetsnett, så appen alltid kjører:
      1. Planlagt oppgave ved innlogging
      2. … og hver gang PC-en låses opp
      3. … og hvert minutt (gjør ingenting hvis den allerede kjører)
      4. Windows sin vanlige autostart-liste (Run i registeret)
    Appen avslutter seg selv hvis den allerede kjører, så dobbel start er ufarlig."""
    if os.path.exists(startup_link()):              # gammel autostart-metode
        os.remove(startup_link())
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not enable:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Unregister-ScheduledTask -TaskName '{APP_NAME}' -Confirm:$false -ErrorAction SilentlyContinue"])
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, APP_NAME)
        except OSError:
            pass
        print("Autostart fjernet.")
        return
    q = lambda s: s.replace("'", "''")
    ps = f"""
$ErrorActionPreference = 'Stop'
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute '{q(pythonw)}' -Argument '"{q(os.path.abspath(__file__))}"' -WorkingDirectory '{q(HERE)}'
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$cls = Get-CimClass -Namespace ROOT\\Microsoft\\Windows\\TaskScheduler -ClassName MSFT_TaskSessionStateChangeTrigger
$unlock = New-CimInstance -CimClass $cls -ClientOnly -Property @{{StateChange = 8; UserId = $user}}
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 1)
$every.Repetition.Duration = ''          # gjenta for alltid
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 `
            -MultipleInstances IgnoreNew -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName '{APP_NAME}' -Action $action -Trigger $logon,$unlock,$every -Settings $settings -Principal $principal -Force | Out-Null
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f'"{pythonw}" "{os.path.abspath(__file__)}"')
    print("Autostart lagt til (innlogging, opplåsing, sjekk hvert minutt og Windows-autostart).")


# ------------------------------------------------------------------- ikoner
# Henter ekte Windows-ikoner (og miniatyrbilder for bilder) via
# IShellItemImageFactory og koder dem som PNG – uten eksterne pakker.


class GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]


def _guid(s):
    g = GUID()
    ctypes.memmove(ctypes.byref(g), uuid.UUID(s).bytes_le, 16)
    return g


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
                ("bmWidthBytes", ctypes.c_long), ("bmPlanes", ctypes.c_ushort), ("bmBitsPixel", ctypes.c_ushort),
                ("bmBits", ctypes.c_void_p)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", ctypes.c_ushort), ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


IID_IShellItemImageFactory = _guid("bcc18b79-ba16-442f-80c4-8a59c30c463b")
shell32.SHCreateItemFromParsingName.argtypes = [wt.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
_GetImage = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, SIZE, ctypes.c_int, ctypes.POINTER(wt.HBITMAP))
_Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
gdi32.GetObjectW.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p]
gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
gdi32.DeleteObject.argtypes = [wt.HANDLE]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]


def _png(w, h, rgba):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    stride = w * 4
    raw = b"".join(b"\x00" + bytes(rgba[y * stride:(y + 1) * stride]) for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def shell_image_png(parsing_name, size=64, thumbnail=False):
    ole32.CoInitialize(None)
    p = ctypes.c_void_p()
    if shell32.SHCreateItemFromParsingName(parsing_name, None, ctypes.byref(IID_IShellItemImageFactory), ctypes.byref(p)) != 0 or not p:
        return None
    vtbl = ctypes.cast(ctypes.cast(p, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    hbmp = wt.HBITMAP()
    flags = 0x1 if thumbnail else 0x4          # BIGGERSIZEOK / ICONONLY
    hr = _GetImage(vtbl[3])(p, SIZE(size, size), flags, ctypes.byref(hbmp))
    _Release(vtbl[2])(p)
    if hr != 0 or not hbmp:
        return None
    try:
        bm = BITMAP()
        gdi32.GetObjectW(hbmp, ctypes.sizeof(bm), ctypes.byref(bm))
        w, h = bm.bmWidth, abs(bm.bmHeight)
        bih = BITMAPINFOHEADER(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = (ctypes.c_ubyte * (w * h * 4))()
        hdc = user32.GetDC(None)
        gdi32.GetDIBits(hdc, hbmp, 0, h, buf, ctypes.byref(bih), 0)
        user32.ReleaseDC(None, hdc)
    finally:
        gdi32.DeleteObject(hbmp)
    px = bytearray(buf)
    px[0::4], px[2::4] = px[2::4], px[0::4]           # BGRA -> RGBA
    alpha = px[3::4]
    if not any(alpha):
        px[3::4] = b"\xff" * (w * h)
    elif all(px[i] <= px[i | 3] and px[i + 1] <= px[i | 3] and px[i + 2] <= px[i | 3] for i in range(0, len(px), 4)):
        for i in range(0, len(px), 4):               # fjern premultiplisert alfa
            a = px[i + 3]
            if 0 < a < 255:
                px[i] = px[i] * 255 // a
                px[i + 1] = px[i + 1] * 255 // a
                px[i + 2] = px[i + 2] * 255 // a
    return _png(w, h, px)


IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".heic", ".tif", ".tiff", ".mp4", ".mov", ".mkv", ".psd"}
UNIQUE_EXT = {".exe", ".lnk", ".url", ".ico", ".appref-ms", ".msc"}


class IconCache:
    def __init__(self):
        self.mem = {}
        self.lock = threading.Lock()
        os.makedirs(ICON_DIR, exist_ok=True)

    def key_for(self, it):
        if it.kind == "uwp":
            return "uwp:" + it.target, "shell:AppsFolder\\" + it.target, False
        if it.kind == "folder":
            return "folder", it.target, False
        ext = os.path.splitext(it.target)[1].lower()
        if it.kind == "app" or ext in UNIQUE_EXT:
            return "path:" + it.target.lower(), it.target, False
        if ext in IMAGE_EXT:
            return "thumb:" + it.target.lower(), it.target, True
        return "ext:" + ext, it.target, False

    def get(self, it):
        key, name, thumb = self.key_for(it)
        with self.lock:
            if key in self.mem:
                return self.mem[key]
        fn = os.path.join(ICON_DIR, hashlib.sha1(key.encode()).hexdigest() + ".png")
        data = None
        try:
            if os.path.exists(fn):
                with open(fn, "rb") as f:
                    data = f.read()
            else:
                data = shell_image_png(name, 64, thumb)
                if data:
                    with open(fn, "wb") as f:
                        f.write(data)
        except Exception:
            data = None
        uri = "data:image/png;base64," + base64.b64encode(data).decode() if data else None
        with self.lock:
            self.mem[key] = uri
        return uri


shell32.SHParseDisplayName.argtypes = [wt.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong, ctypes.c_void_p]
shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong]
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]


def reveal_in_explorer(path):
    """Åpner mappen i Utforsker med filen markert."""
    ole32.CoInitialize(None)
    pidl = ctypes.c_void_p()
    if shell32.SHParseDisplayName(os.path.normpath(path), None, ctypes.byref(pidl), 0, None) == 0 and pidl:
        try:
            user32.AllowSetForegroundWindow(-1)         # la Utforsker komme foran
            if shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0:
                return
        finally:
            ole32.CoTaskMemFree(pidl)
    subprocess.Popen(f'explorer.exe /select,"{os.path.normpath(path)}"')


REVEAL_KINDS = ("file", "folder", "app", "uwp")
CMD_COPY = {"Innstillinger": "ms-settings:", "Filutforsker": os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "explorer.exe")}

shell32.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wt.DWORD, wt.HANDLE, ctypes.POINTER(ctypes.c_void_p)]

_APPX_PS = r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$fam, $app = $env:WS_APPID.Split('!', 2)
$p = Get-AppxPackage | Where-Object { $_.PackageFamilyName -eq $fam } | Select-Object -First 1
if ($p) {
  $m = [xml](Get-Content -LiteralPath (Join-Path $p.InstallLocation 'AppxManifest.xml'))
  $a = @($m.Package.Applications.Application) | Where-Object { $_.Id -eq $app } | Select-Object -First 1
  if ($a -and $a.Executable) { Join-Path $p.InstallLocation $a.Executable } else { $p.InstallLocation }
}
"""


def resolve_appid(appid):
    """Finner filbanen til en app fra Get-StartApps (Store-app, eller sti med kjent-mappe-GUID)."""
    m = re.match(r"^\{([0-9a-f-]{36})\}\\(.+)$", appid, re.I)
    if m:                                               # f.eks. {System32-GUID}\cmd.exe
        p = ctypes.c_void_p()
        if shell32.SHGetKnownFolderPath(ctypes.byref(_guid(m.group(1))), 0, None, ctypes.byref(p)) == 0:
            base = ctypes.wstring_at(p.value)
            ole32.CoTaskMemFree(p)
            return os.path.join(base, m.group(2))
        return None
    if "!" in appid:                                    # Store-app: PackageFamilyName!AppId
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", _APPX_PS], capture_output=True,
                                 timeout=15, creationflags=CREATE_NO_WINDOW, env={**os.environ, "WS_APPID": appid}
                                 ).stdout.decode("utf-8", "replace").strip()
            return out.splitlines()[-1].strip() if out else None
        except Exception:
            return None
    return appid if os.path.isabs(appid) else None

kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]


def set_clipboard(text):
    data = text.encode("utf-16-le") + b"\0\0"
    for _ in range(20):                      # utklippstavlen kan være opptatt et øyeblikk
        if user32.OpenClipboard(None):
            break
        time.sleep(0.025)
    else:
        raise OSError("Utklippstavlen er opptatt")
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(0x0002, len(data))     # GMEM_MOVEABLE
        ctypes.memmove(kernel32.GlobalLock(h), data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(13, h)                  # CF_UNICODETEXT
    finally:
        user32.CloseClipboard()


def resolve_shortcut(lnk):
    """Finner hva en .lnk-snarvei peker på. Faller tilbake til snarveien selv."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
             "(New-Object -ComObject WScript.Shell).CreateShortcut($env:WS_LNK).TargetPath"],
            capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW,
            env={**os.environ, "WS_LNK": lnk},
        ).stdout.decode("utf-8", "replace").strip()
        if out and os.path.exists(out):
            return out
    except Exception:
        pass
    return lnk


# ------------------------------------------------------------------- indeksering


class Item:
    __slots__ = ("kind", "name", "low", "sub", "target", "initials")

    def __init__(self, kind, name, sub, target):
        self.kind = kind          # app, uwp, folder, file, cmd
        self.name = name
        self.low = name.lower()
        self.sub = sub
        self.target = target
        self.initials = "".join(w[0] for w in re.split(r"[\s\-_.()]+", self.low) if w)


SKIP_WORDS = ("uninstall", "avinstaller", "readme", "documentation", "website", "release notes", "manual", "faq")
SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "appdata", "$recycle.bin", "site-packages", ".git"}


class Index:
    def __init__(self):
        self.apps, self.files = [], []
        self.lock = threading.Lock()

    def refresh(self):
        apps, files = self._index_apps(), self._index_files()
        with self.lock:
            self.apps, self.files = apps, files

    def _index_apps(self):
        found = {}
        roots = [
            os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs"),
            os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
        ]
        for root in roots:
            for dirpath, _, files in os.walk(root):
                for f in files:
                    stem, ext = os.path.splitext(f)
                    if ext.lower() not in (".lnk", ".url", ".appref-ms"):
                        continue
                    if any(w in stem.lower() for w in SKIP_WORDS):
                        continue
                    found.setdefault(stem.lower(), Item("app", stem, "Program", os.path.join(dirpath, f)))
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress"],
                capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW,
            ).stdout.decode("utf-8", "replace")
            data = json.loads(out) if out.strip() else []
            for a in [data] if isinstance(data, dict) else data:
                name, appid = a.get("Name"), a.get("AppID")
                if name and appid and not any(w in name.lower() for w in SKIP_WORDS):
                    found.setdefault(name.lower(), Item("uwp", name, "Program", appid))
        except Exception:
            pass
        return list(found.values())

    def _index_files(self):
        home = os.path.expanduser("~")
        roots = [os.path.join(home, s) for s in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")]
        if os.environ.get("OneDrive"):
            roots.append(os.environ["OneDrive"])
        items, seen, limit = [], set(), 60000

        def walk(path, depth):
            if len(items) >= limit or depth > 5:
                return
            try:
                with os.scandir(path) as it:
                    for e in it:
                        n = e.name
                        if n.startswith((".", "~$")) or n.lower() in ("desktop.ini", "thumbs.db"):
                            continue
                        try:
                            is_dir = e.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                        if e.path.lower() in seen:
                            continue
                        seen.add(e.path.lower())
                        if is_dir:
                            if n.lower() in SKIP_DIRS:
                                continue
                            items.append(Item("folder", n, short_path(path), e.path))
                            walk(e.path, depth + 1)
                        else:
                            items.append(Item("file", n, short_path(path), e.path))
                        if len(items) >= limit:
                            return
            except OSError:
                pass

        for r in roots:
            if os.path.isdir(r):
                walk(r, 0)
        return items


def short_path(p, maxlen=72):
    home = os.path.expanduser("~")
    if p.lower().startswith(home.lower()):
        p = "~" + p[len(home):]
    p = p.replace("\\", " › ").replace("~ › ", "~ › ")
    if len(p) > maxlen:
        p = "… " + p[-(maxlen - 2):]
    return p


# ------------------------------------------------------------------- søk/rangering


def match_score(q, it):
    n = it.low
    if n == q:
        return 1000
    if n.startswith(q):
        return 900 - len(n)
    pos = n.find(q)
    if pos > 0 and not n[pos - 1].isalnum():
        return 800 - len(n)
    if len(q) >= 2 and it.initials.startswith(q):
        return 750 - len(n)
    if pos > 0:
        return 600 - pos - len(n) // 4
    if it.kind in ("app", "uwp", "cmd") and len(q) >= 2:
        # løs match ("vsc" -> "visual studio code"): må starte på et ord og hoppe til ordstarter
        starts = [k for k in range(len(n)) if k == 0 or not n[k - 1].isalnum()]
        for st in starts:
            if n[st] != q[0]:
                continue
            i, last, jumps, ok = st + 1, st, 0, True
            for ch in q[1:]:
                if i < len(n) and n[i] == ch:
                    last, i = i, i + 1
                    continue
                nxt = next((k for k in starts if k >= i and n[k] == ch), None)
                if nxt is None:
                    ok = False
                    break
                jumps += 1
                last, i = nxt, nxt + 1
            if ok:
                return 500 - jumps * 20 - len(n) // 4
    return None


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
        ast.USub: operator.neg, ast.UAdd: operator.pos}


def calc(expr):
    if not re.fullmatch(r"[\d\s+\-*/().,%^x×÷]+", expr) or not re.search(r"\d", expr):
        return None
    if not re.search(r"[+\-*/%^x×÷]", expr.strip().lstrip("-")):
        return None
    py = expr.replace(",", ".").replace("^", "**").replace("x", "*").replace("×", "*").replace("÷", "/")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            l, r = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(r) > 100:
                raise ValueError
            return _OPS[type(node.op)](l, r)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError

    try:
        v = ev(ast.parse(py, mode="eval"))
    except Exception:
        return None
    if isinstance(v, float):
        v = int(v) if v.is_integer() and abs(v) < 1e15 else round(v, 10)
    if isinstance(v, int):
        return f"{v:,}".replace(",", "\u202f")
    return str(v).replace(".", ",")


ALL_APPS_WORDS = ("apper", "alle apper", "apps", "all apps", "programmer", "alle programmer", "programs")

URL_RE = re.compile(r"^(https?://)?[\w-]+(\.[\w-]+)*\.[a-z]{2,}(/\S*)?$", re.I)

# --------------------------------------------------------------------- tema


def _reg(path, name, default):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return default


def theme():
    light = _reg(r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "AppsUseLightTheme", 0) == 1
    abgr = _reg(r"Software\Microsoft\Windows\DWM", "AccentColor", 0xFFD77800)
    r, g, b = abgr & 0xFF, (abgr >> 8) & 0xFF, (abgr >> 16) & 0xFF
    return light, f"#{r:02x}{g:02x}{b:02x}"


# ----------------------------------------------------------------- vindusmagi


class ACCENTPOLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p), ("SizeOfData", ctypes.c_size_t)]


class MARGINS(ctypes.Structure):
    _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]


def dwm_set(hwnd, attr, value):
    v = ctypes.c_int(value)
    return dwmapi.DwmSetWindowAttribute(wt.HWND(hwnd), attr, ctypes.byref(v), ctypes.sizeof(v))


def apply_glass(hwnd, light):
    """Ekte Windows 11-akryl bak vinduet, avrundede hjørner og skygge."""
    dwm_set(hwnd, 20, 0 if light else 1)          # DWMWA_USE_IMMERSIVE_DARK_MODE
    dwm_set(hwnd, 33, 2)                          # DWMWA_WINDOW_CORNER_PREFERENCE = ROUND
    dwm_set(hwnd, 34, 0xFFFFFFFE)                 # DWMWA_BORDER_COLOR = NONE (CSS tegner kanten)
    m = MARGINS(-1, -1, -1, -1)
    dwmapi.DwmExtendFrameIntoClientArea(wt.HWND(hwnd), ctypes.byref(m))
    if dwm_set(hwnd, 38, 3) != 0:                 # DWMWA_SYSTEMBACKDROP_TYPE = ACRYLIC (Win11 22H2+)
        tint = 0x40F3F3F3 if light else 0x60202020  # eldre Windows: SetWindowCompositionAttribute
        ap = ACCENTPOLICY(4, 2, tint, 0)
        data = WINCOMPATTRDATA(19, ctypes.cast(ctypes.pointer(ap), ctypes.c_void_p), ctypes.sizeof(ap))
        user32.SetWindowCompositionAttribute(wt.HWND(hwnd), ctypes.byref(data))


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", ctypes.c_ulong)]


user32.MonitorFromPoint.restype = wt.HMONITOR
user32.MonitorFromPoint.argtypes = [POINT, ctypes.c_uint]
user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(MONITORINFO)]


def cursor_work_area():
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(user32.MonitorFromPoint(pt, 2), ctypes.byref(mi))
    return mi.rcWork


# --------------------------------------------------------------------- appen


class Api:
    """Metodene her kalles fra ui.html via window.pywebview.api."""

    def __init__(self, app):
        self._app = app

    def config(self):
        light, accent = theme()
        return {"light": light, "accent": accent, "hotkey": "Ctrl Mellomrom"}

    def search(self, q):
        return self._app.search(q)

    def open(self, rid, reveal=False):
        return self._app.open(rid, reveal)

    def action(self, rid, name):
        return self._app.action(rid, name)

    def hide(self):
        self._app.hide()

    def resize(self, h):
        self._app.resize(h)


class Spotlight:
    def __init__(self, demo=None):
        self.demo = demo
        self._lnk_cache = {}
        self.index = Index()
        self.icons = IconCache()
        self.history = self._load_history()
        self.last = {}
        self.visible = False
        self.shown_at = 0
        self.hwnd = None
        self.height = 56
        self.events = queue.Queue()
        self.commands = [
            Item("cmd", "Innstillinger", "Windows-innstillinger", lambda: os.startfile("ms-settings:")),
            Item("cmd", "Filutforsker", "Åpne Utforsker", lambda: subprocess.Popen(["explorer.exe"])),
            Item("cmd", "Oppdater søkeindeksen", APP_NAME, lambda: threading.Thread(target=self._refresh, daemon=True).start()),
            Item("cmd", "Start WinSpotlight på nytt", APP_NAME, lambda: os._exit(3)),
        ]
        self.window = webview.create_window(
            APP_NAME, url=os.path.join(HERE, "ui.html"), js_api=Api(self),
            width=WIDTH, height=self.height, min_size=(200, 40), frameless=True, on_top=True, hidden=True,
            resizable=False, background_color="#000000", shadow=True, focus=True,
        )
        self.window.events.loaded += self._on_loaded

    # ------------------------------------------------------- vindu

    def _on_loaded(self):
        if self.hwnd:
            return
        form = self.window.native
        self.hwnd = form.Handle.ToInt64()
        light, _ = theme()

        def setup():
            from System.Drawing import Color
            form.browser.webview.DefaultBackgroundColor = Color.Transparent
            try:    # la Ctrl+Shift+C osv. gå til appen i stedet for nettleserens egne snarveier
                form.browser.webview.CoreWebView2.Settings.AreBrowserAcceleratorKeysEnabled = False
            except Exception as ex:
                print("kunne ikke slå av nettleser-snarveier:", ex)
            ex = user32.GetWindowLongPtrW(self.hwnd, -20)
            user32.SetWindowLongPtrW(self.hwnd, -20, (ex | 0x80) & ~0x40000)   # TOOLWINDOW, ikke APPWINDOW
            apply_glass(self.hwnd, light)

        self._ui(setup)
        threading.Thread(target=self._hotkey_thread, daemon=True).start()
        threading.Thread(target=self._watch_focus, daemon=True).start()
        threading.Thread(target=self._event_loop, daemon=True).start()
        if self.demo is not None:
            time.sleep(1.5)
            self.show()
            self.window.evaluate_js(f"setQuery({json.dumps(self.demo)})")

    def _ui(self, fn):
        from System import Action
        self.window.native.Invoke(Action(fn))

    def _scale(self):
        try:
            return user32.GetDpiForWindow(wt.HWND(self.hwnd)) / 96.0
        except Exception:
            return 1.0

    def _place(self, show=False):
        wa = cursor_work_area()
        s = self._scale()
        w, h = int(WIDTH * s), int(self.height * s)
        x = wa.left + (wa.right - wa.left - w) // 2
        y = wa.top + int((wa.bottom - wa.top) * 0.22)
        flags = 0x0010 | (0x0040 if show else 0)        # NOACTIVATE | SHOWWINDOW
        user32.SetWindowPos(wt.HWND(self.hwnd), wt.HWND(-1), x, y, w, h, flags)

    def resize(self, h):
        h = max(40, min(int(h), 900))
        if h != self.height:
            self.height = h
            if self.hwnd:
                self._place()

    def show(self):
        if not self.hwnd:
            return
        self.window.evaluate_js("reset()")
        self.height = 56
        self._place()
        form = self.window.native

        def up():
            form.Show()
            form.Activate()
            form.browser.webview.Focus()

        self._ui(up)
        self._place()
        user32.SetForegroundWindow(wt.HWND(self.hwnd))
        self.window.evaluate_js("onShow()")
        self.visible = True
        self.shown_at = time.time()

    def hide(self):
        if self.hwnd and self.visible:
            self.visible = False
            self._ui(self.window.native.Hide)

    def toggle(self):
        self.hide() if self.visible else self.show()

    # ------------------------------------------------------- søk

    def search(self, raw):
        q = (raw or "").strip()
        ql = q.lower()
        res = []
        if not q:
            self.last = {}
            return res

        with self.index.lock:
            apps, files = self.index.apps, self.index.files

        if ql in ALL_APPS_WORDS:
            return self._publish(self._all_apps(apps), limit=None)

        val = calc(q)
        if val is not None:
            res.append({"kind": "calc", "group": "Kalkulator", "title": val, "sub": q, "payload": val.replace("\u202f", "")})
        scored = []
        for it in self.commands:
            s = match_score(ql, it)
            if s is not None:
                scored.append((s + 10, it))
        for it in apps:
            s = match_score(ql, it)
            if s is not None:
                scored.append((s + 50 + min(self.history.get(it.name, 0) * 25, 250), it))
        if len(ql) >= 2:
            fs = []
            for it in files:
                if ql in it.low:
                    s = match_score(ql, it)
                    if s is not None:
                        bonus = 10 if it.kind == "folder" else 0
                        fs.append((s - 120 + bonus + min(self.history.get(it.target, 0) * 25, 250), it))
            fs.sort(key=lambda t: -t[0])
            scored.extend(fs[:12])
        scored.sort(key=lambda t: -t[0])

        groups = {"app": "Programmer", "uwp": "Programmer", "cmd": "Handlinger", "folder": "Mapper", "file": "Dokumenter"}
        limits = {"Programmer": 5, "Handlinger": 2, "Mapper": 3, "Dokumenter": 4}
        top, buckets = None, {}
        for s, it in scored:
            if top is None and val is None and s >= 700:
                top = it
                continue
            g = groups[it.kind]
            if len(buckets.setdefault(g, [])) < limits[g]:
                buckets[g].append(it)

        def entry(it, group):
            sub = {"app": "Program", "uwp": "Program", "cmd": it.sub, "folder": it.sub, "file": it.sub}[it.kind]
            return {"kind": it.kind, "group": group, "title": it.name, "sub": sub,
                    "icon": self.icons.get(it) if it.kind != "cmd" else None, "item": it}

        if top:
            res.append(entry(top, "Topptreff"))
        for g in ("Programmer", "Handlinger", "Mapper", "Dokumenter"):
            for it in buckets.get(g, []):
                res.append(entry(it, g))
        if len(ql) >= 2 and any(w.startswith(ql) for w in ALL_APPS_WORDS):
            res.insert(1 if top else 0, {"kind": "showall", "group": "Handlinger", "title": "Alle apper",
                                         "sub": f"Vis alle {len(apps)} programmer"})
        if URL_RE.match(q) and " " not in q:
            res.insert(1 if res else 0, {"kind": "url", "group": "Nettside", "title": q, "sub": "Åpne i nettleseren", "payload": q})
        res.append({"kind": "web", "group": "Søk på nettet", "title": q, "sub": "Søk med Google", "payload": q})
        return self._publish(res)

    def _all_apps(self, apps):
        res = []
        for it in sorted(apps, key=lambda a: a.low):
            first = it.name[:1].upper()
            group = first if first.isalpha() else "#"
            res.append({"kind": it.kind, "group": group, "title": it.name, "sub": "",
                        "icon": self.icons.get(it), "item": it})
        return res

    def _publish(self, res, limit=14):
        self.last = {}
        out = []
        for i, r in enumerate(res[:limit]):
            rid = str(i)
            self.last[rid] = r
            out.append({k: v for k, v in r.items() if k not in ("item", "payload")}
                       | {"id": rid} | self._flags(r))
        return out

    def open(self, rid, reveal=False):
        r = self.last.get(str(rid))
        if not r:
            return False
        kind = r["kind"]
        try:
            if kind == "showall":
                return "showall"
            if kind == "calc":
                self._copy(r["payload"])
                return "copied"
            self.hide()
            if kind == "web":
                webbrowser.open(SEARCH_URL + urllib.parse.quote_plus(r["payload"]))
            elif kind == "url":
                p = r["payload"]
                webbrowser.open(p if "://" in p else "https://" + p)
            else:
                it = r["item"]
                if it.kind == "cmd":
                    it.target()
                    return True
                if it.kind == "uwp":
                    subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + it.target])
                else:
                    os.startfile(it.target)
                key = it.name if it.kind in ("app", "uwp") else it.target
                self.history[key] = self.history.get(key, 0) + 1
                self._save_history()
        except Exception as ex:
            user32.MessageBoxW(None, f"Kunne ikke åpne:\n{ex}", APP_NAME, 0x10)
        return True

    def action(self, rid, name):
        """Filplassering / kopier bane – for alt som har en bane eller adresse."""
        r = self.last.get(str(rid))
        if not r:
            return False
        try:
            if name == "copy":
                text = self._copy_text(r)
                if not text:
                    return False
                set_clipboard(text)
                return "copied"
            if name == "reveal":
                it = r.get("item")
                if not it or it.kind not in REVEAL_KINDS:
                    return False
                self.hide()
                path = self._real_path(it)
                if path and os.path.exists(path):
                    reveal_in_explorer(path)
                else:                                   # Store-app uten synlig mappe
                    subprocess.Popen(["explorer.exe", "shell:AppsFolder"])
                return True
        except Exception as ex:
            user32.MessageBoxW(None, f"Noe gikk galt:\n{ex}", APP_NAME, 0x10)
        return False

    def _copy_text(self, r):
        kind = r["kind"]
        if kind == "calc":
            return r["payload"]
        if kind == "url":
            p = r["payload"]
            return p if "://" in p else "https://" + p
        if kind == "web":
            return SEARCH_URL + urllib.parse.quote_plus(r["payload"])
        it = r.get("item")
        if not it:
            return None
        if it.kind == "cmd":
            return CMD_COPY.get(it.name)
        if it.kind == "uwp":
            return self._real_path(it) or "shell:AppsFolder\\" + it.target
        return self._real_path(it)

    def _real_path(self, it):
        """Programmer: filen snarveien peker på (f.eks. chrome.exe). Store-apper: .exe i installasjonsmappen."""
        if it.kind == "app" and it.target.lower().endswith(".lnk"):
            if it.target not in self._lnk_cache:
                self._lnk_cache[it.target] = resolve_shortcut(it.target)
            return self._lnk_cache[it.target]
        if it.kind == "uwp":
            if it.target not in self._lnk_cache:
                self._lnk_cache[it.target] = resolve_appid(it.target)
            return self._lnk_cache[it.target]
        return it.target

    def _flags(self, r):
        kind, it = r["kind"], r.get("item")
        if kind in ("calc", "url", "web"):
            return {"copy": True, "reveal": False}
        if not it:
            return {"copy": False, "reveal": False}
        if it.kind == "cmd":
            return {"copy": it.name in CMD_COPY, "reveal": False}
        return {"copy": True, "reveal": it.kind in REVEAL_KINDS}

    def _copy(self, text):
        set_clipboard(text)

    # ------------------------------------------------------- lagring

    def _load_history(self):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_history(self):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False)
        except Exception:
            pass

    # ------------------------------------------------------- bakgrunn

    def _refresh(self):
        try:
            self.index.refresh()
            for it in list(self.index.apps):          # forvarm app-ikoner
                self.icons.get(it)
        except Exception:
            pass

    def _index_loop(self):
        while True:
            self._refresh()
            time.sleep(600)

    def _hotkey_thread(self):
        # Rett etter oppstart kan snarveien være midlertidig opptatt – prøv i opptil 2 minutter
        for attempt in range(60):
            if user32.RegisterHotKey(None, 1, HOTKEY_MODS | 0x4000, HOTKEY_VK):
                break
            time.sleep(2)
        else:
            print(time.strftime("%H:%M:%S"), "Ctrl+Mellomrom er opptatt av et annet program – prøver igjen senere")
            os._exit(3)                         # vakten starter appen på nytt
        if attempt:
            print(time.strftime("%H:%M:%S"), f"fikk Ctrl+Mellomrom etter {attempt * 2} s")
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:   # WM_HOTKEY
                self.events.put("toggle")

    def _event_loop(self):
        while True:
            if self.events.get() == "toggle":
                self.toggle()

    def _watch_focus(self):
        while True:
            time.sleep(0.12)
            if self.visible and self.demo is None and time.time() - self.shown_at > 0.35:
                if user32.GetForegroundWindow() != self.hwnd:
                    self.hide()

    def run(self):
        threading.Thread(target=self._index_loop, daemon=True).start()
        webview.start(gui="edgechromium", storage_path=os.path.join(DATA_DIR, "webview"))


def supervise():
    """Kjører selve appen som underprosess og starter den på nytt hvis den krasjer."""
    kernel32.CreateMutexW(None, False, "Local\\WinSpotlightSingleInstance")
    if kernel32.GetLastError() == 183:      # kjører allerede
        return
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    quick_fails = 0
    while True:                             # gir aldri opp
        started = time.time()
        try:
            code = subprocess.call([pythonw, os.path.abspath(__file__), "--child"])
        except Exception as ex:
            code = repr(ex)
        print(time.strftime("%H:%M:%S"), "appen stoppet (kode", code, ") – starter på nytt")
        quick_fails = quick_fails + 1 if time.time() - started < 60 else 0
        time.sleep(min(60, 2 ** quick_fails))   # 2, 4, 8 … maks 60 sekunder mellom forsøk


def main():
    if "--autostart" in sys.argv:
        return set_autostart(True)
    if "--no-autostart" in sys.argv:
        return set_autostart(False)
    os.makedirs(DATA_DIR, exist_ok=True)
    if sys.stderr is None:                  # pythonw svelger feil – skriv dem til logg
        sys.stderr = sys.stdout = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    demo = sys.argv[sys.argv.index("--demo") + 1] if "--demo" in sys.argv else None
    if demo is None and "--child" not in sys.argv:
        print(time.strftime("\n--- %Y-%m-%d %H:%M:%S  WinSpotlight starter"))
        return supervise()
    Spotlight(demo).run()


if __name__ == "__main__":
    main()
