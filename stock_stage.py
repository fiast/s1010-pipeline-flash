#!/usr/bin/env python3
"""
Работа со стоковым веб-интерфейсом роутера Ростелеком (Sercomm S1010).

Задачи:
    1) вход на http://192.168.0.1 (логин admin + пароль)
    2) чтение MAC-адреса роутера
    3) загрузка файла прошивки Breed

Особенности интерфейса, выясненные при разведке:

* Встроенный HTTP-сервер отдаёт «(null) 400 Bad Request» на почти любой
  запрос, если в заголовках НЕТ Accept-Language. С браузерным Accept-Language
  отдаёт нормальный HTTP/1.1 200. Заголовок Accept-Language обязателен.
* Страницы интерфейса строятся JavaScript'ом, HTML почти пустой, поэтому
  работа идёт через эндпоинты ./data/*.json, а не через обычные формы.
* Пароль не передаётся открытым текстом:
      hash1 = hex_hmac_sha256('$1$SERCOMM$', password)
      LoginPWD = hex_hmac_sha256(sys_encryption_key, hash1)
  Здесь sys_encryption_key пустой, но реализована полная схема.
* Каждый запрос принимается с токеном csrf_token из HTML страницы.
"""

import hashlib
import hmac
import json
import os
import re
import subprocess
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

STOCK_IP = os.environ.get("S1010_STOCK_IP") or "192.168.0.1"
STOCK_LOGIN = os.environ.get("S1010_STOCK_USER") or "admin"

# Каталог проекта - здесь ищем файлы прошивок по умолчанию
HERE = os.path.dirname(os.path.abspath(__file__))

# Заголовки обязательны: без Accept-Language сервер отвечает 400
HEADERS = {
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Connection": "keep-alive",
}

PASSWORD_KEY = "$1$SERCOMM$"
CSRF_RE = re.compile(r"csrf_token\s*=\s*'([0-9A-Za-z]+)'")
MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})\b")


def _rstr2binb(s: str) -> bytes:
    """str -> массив байт, как rstr2binb в js/sha.js (latin-1, а не UTF-8)."""
    return s.encode("latin-1", errors="replace")


def hex_hmac_sha256(key: str, data: str) -> str:
    """Точная копия hex_hmac_sha256(k, d) из js/sha.js роутера.

    Сверка с оригинальным JS (node + сам sha.js роутера) показала, что
    несмотря на Array(16) в исходнике, результат совпадает со стандартным
    HMAC-SHA256. Используется стандартная реализация.
    """
    return hmac.new(_rstr2binb(key), _rstr2binb(data), hashlib.sha256).hexdigest()


def hash_password(password: str, enc_key: str = "") -> str:
    """Хеширование пароля так же, как это делает веб-интерфейс.

    В login.js:
        hash1_pass   = hex_hmac_sha256('$1$SERCOMM$', password)
        LoginPWD     = hex_hmac_sha256(sys_encryption_key, hash1_pass)
    Здесь sys_encryption_key пустой, но полная схема поддержана.
    """
    stage1 = hex_hmac_sha256(PASSWORD_KEY, password)
    return hex_hmac_sha256(enc_key, stage1)


def normalize_mac(raw):
    clean = re.sub(r"[\s:\-.]", "", str(raw or "")).upper()
    if not re.fullmatch(r"[0-9A-F]{12}", clean):
        return None
    return clean


def mac_is_invalid(mac):
    if mac in ("000000000000", "FFFFFFFFFFFF"):
        return True
    return (int(mac[0:2], 16) & 1) == 1


def find_mac_in_text(text):
    """Ищет корректный unicast MAC в произвольном тексте."""
    for cand in MAC_RE.findall(text or ""):
        norm = normalize_mac(cand)
        if norm and not mac_is_invalid(norm):
            return cand.replace("-", ":").upper() if "-" in cand else cand.upper()
    return None


class StockSession:
    """Сессия к стоковому веб-интерфейсу роутера Ростелекома."""

    def __init__(self, ip=STOCK_IP, login=STOCK_LOGIN, password="",
                 verbose=True):
        self.ip = ip
        self.base = f"http://{ip}"
        self.login = login
        self.password = password
        self.verbose = verbose
        self.s = requests.Session()
        self.s.verify = False
        self.s.headers.update(HEADERS)
        self.csrf = None
        self.logged_in = False

    def say(self, msg):
        if self.verbose:
            print(msg)

    # ---------- служебное ----------
    def fetch_csrf(self, path="/login.html"):
        """Достаёт csrf_token из HTML страницы."""
        try:
            r = self.s.get(self.base + path, timeout=15)
        except requests.RequestException as e:
            self.say(f"[-] Роутер не отвечает: {e}")
            return None
        m = CSRF_RE.search(r.text)
        self.csrf = m.group(1) if m else None
        if not self.csrf:
            self.say("[-] csrf_token на странице не найден")
        return self.csrf

    def data_url(self, name, extra=""):
        stamp = int(time.time() * 1000)
        url = f"{self.base}/data/{name}.json?_={stamp}"
        if self.csrf:
            url += f"&csrf_token={self.csrf}"
        return url + extra

    def is_alive(self):
        try:
            r = self.s.get(self.base + "/login.html", timeout=8)
            return r.status_code == 200
        except requests.RequestException:
            return False

    # ---------- вход ----------
    def resolve_breed_file(self, given):
        """Находит файл образа Breed.

        Имя может быть указано с любым вариантом написания
        (breed_s1010.img / breed-s1010.img / breed-rt-fl-1.img), поэтому
        при неудаче ищем в каталоге проекта по шаблону.
        """
        if given and os.path.isabs(given) and os.path.exists(given):
            return given
        if given and not os.path.isabs(given):
            cand = os.path.join(HERE, given)
            if os.path.exists(cand):
                return cand
            # вариант с другим разделителем
            alt = given.replace("-", "_").replace("_", "-")
            for name in {given, alt}:
                cand = os.path.join(HERE, name)
                if os.path.exists(cand):
                    return cand
        # автопоиск в каталоге проекта
        try:
            entries = sorted(os.listdir(HERE))
        except OSError:
            return None
        for name in entries:
            low = name.lower()
            if not low.startswith("breed"):
                continue
            if low.endswith((".img", ".bin")):
                return os.path.join(HERE, name)
        return None

    def _get_encryption_material(self):
        """Забирает encryption_key и delay_time из user_lang.json.

        Ключ выдаётся сервером и меняется при каждом обращении, поэтому
        его нужно брать непосредственно перед попыткой входа.
        """
        ref = {"Referer": self.base + "/login.html", "Origin": self.base}
        try:
            r = self.s.get(self.data_url("user_lang"), headers=ref, timeout=20)
            data = json.loads(r.text)
        except (requests.RequestException, ValueError) as e:
            self.say(f"[-] user_lang.json не получен: {e}")
            return None

        def pick(key):
            for item in data:
                if isinstance(item, dict) and key in item:
                    return item[key]
            return None

        key = pick("encryption_key")
        delay = pick("delay_time")
        if key is None:
            return None
        return key, float(delay or 0)

    def login_router(self):
        """Выполняет вход. True - авторизация прошла.

        Схема входа повторяет js/login.js:
          1) GET  /login.html           - забрать csrf_token
          2) GET  data/user_lang.json    - encryption_key + delay_time
          3) POST data/login.json        - LoginName + LoginPWD
        Ключ шифрования выдаётся сервером на каждой странице, поэтому
        пароль хешируется именно тем ключом, который получен сейчас.
        """
        if not self.password:
            self.say("[-] Пароль не задан")
            return False
        if not self.fetch_csrf():
            return False

        material = self._get_encryption_material()
        if not material:
            self.say("[-] Не удалось получить ключ шифрования")
            return False
        enc_key, delay = material
        if delay:
            time.sleep(delay)      # сервер требует паузу перед входом

        ref = {"Referer": self.base + "/login.html", "Origin": self.base}
        pwd = hash_password(self.password, enc_key)
        try:
            r = self.s.post(
                self.data_url("login"),
                data={"LoginName": self.login, "LoginPWD": pwd},
                headers=ref, timeout=25,
            )
        except requests.RequestException as e:
            self.say(f"[-] Ошибка входа: {e}")
            return False

        body = (r.text or "").strip()
        self.logged_in = self._login_succeeded(body)
        if self.logged_in:
            self.say(f"[+] Вход выполнен, пользователь {self.login}")
        else:
            self.say(f"[-] Вход не выполнен, код: {body[:60]}")
        return self.logged_in

    @staticmethod
    def _login_succeeded(body):
        """Коды ответа data/login.json (см. js/login.js):
           "1" - успех, "2" - уже залогинен, "3"/"4" - неверный логин/пароль.
        """
        code = body.strip().strip('"')[:1]
        if code in ("1", "2"):
            return True
        return False

    # ---------- чтение MAC ----------
    # Роутер отдаёт несколько MAC, и они не равны базовому:
    #   14:2E:5E:8B:51:BA - базовый LAN/RF1 (реальный L2, виден в ARP)
    #   ...:BB              - WLAN 2.4     (base+1)
    #   ...:BC              - WLAN 5      (base+2)  это wifi_mac_address
    #   ...:C4              - WAN         (base+10) это settings_wan2.wan_mac
    # В Breed пишется базовый, от него считаются WLAN. Поэтому берём
    # именно базовый, а не wan_mac: он отличается на +10.
    MAC_OFFSETS = {
        "arp": 0,          # фактический L2-адрес устройства
        "status": 2,       # wifi_mac_address = base+2
        "wan2": 10,        # settings_wan2.wan_mac = base+10
    }

    def read_mac_from_arp(self):
        """Базовый MAC из ARP-таблицы хоста - самый прямой источник."""
        import subprocess
        for cmd in (["ip", "neigh", "show"],
                    ["arp", "-n"],
                    ["arp", "-a"]):
            try:
                p = subprocess.run(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=8, text=True)
            except (OSError, subprocess.SubprocessError):
                continue
            for line in (p.stdout or "").splitlines():
                if self.ip not in line:
                    continue
                m = re.search(r"(?:lladdr\s+|at\s+)?([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})",
                              line)
                if m:
                    return normalize_mac(m.group(1))
        return None

    def read_mac_from_wifi_field(self):
        """base+2 из поля wifi_mac_address (страница status-and-support)."""
        ref = {"Referer": self.base + "/status-and-support.html", "Origin": self.base}
        url = (f"{self.base}/statusandsupport/status.html"
               f"?_={int(time.time() * 1000)}&csrf_token={self.csrf}")
        try:
            r = self.s.get(url, headers=ref, timeout=20)
        except requests.RequestException:
            return None
        m = re.search(r'id="wifi_mac_address"[^>]*>\s*([0-9A-Fa-f:]{17})',
                      r.text or "")
        return normalize_mac(m.group(1)) if m else None

    def read_mac_from_wan2(self):
        """Сырое значение settings_wan2.wan_mac (= base+10), без поправки."""
        return self._wan2_mac()

    def _wan2_mac(self):
        ref = {"Referer": self.base + "/settings.html", "Origin": self.base}
        try:
            r = self.s.get(self.data_url("settings_wan2"), headers=ref, timeout=20)
            data = json.loads(r.text)
        except (requests.RequestException, ValueError):
            return None
        for item in data:
            if isinstance(item, dict) and "wan_mac" in item:
                norm = normalize_mac(item["wan_mac"])
                if norm and not mac_is_invalid(norm):
                    return norm
        return None

    def read_mac(self):
        """Определяет базовый MAC роутера, сверяя несколько источников.

        Каждый источник даёт свой MAC со своим смещением относительно
        базового. Базовый получается вычитанием смещения. Результаты
        сверяются между собой - это защита от тихой ошибки, когда
        читается не тот адрес.
        """
        candidates = [
            ("ARP (базовый)", self.read_mac_from_arp(), 0),
            ("wifi_mac_address", self.read_mac_from_wifi_field(), 2),
            ("settings_wan2.wan_mac", self.read_mac_from_wan2(), 10),
        ]

        results = {}
        for name, raw, offset in candidates:
            if not raw:
                self.say(f"  [i] источник {name}: не получен")
                continue
            base = f"{int(raw, 16) - offset:012X}"
            results[name] = base
            self.say(f"  [i] {name} = {raw} (base+{offset}) -> базовый {base}")

        if not results:
            self.say("[-] Не удалось определить MAC роутера")
            return None

        unique = set(results.values())
        if len(unique) > 1:
            self.say("[-] Источники дают РАЗНЫЕ базовые MAC - данные не сходятся:")
            for name, val in results.items():
                self.say(f"      {name:24} = {val}")
            return None

        mac = unique.pop()
        if mac_is_invalid(mac):
            self.say(f"[-] Получен некорректный MAC: {mac}")
            return None

        pretty = ":".join(mac[i:i + 2] for i in range(0, 12, 2))
        self.say(f"[+] Базовый MAC роутера: {pretty} (источники сошлись)")
        return mac

    # ---------- загрузка прошивки ----------
    UPLOAD_CGI = "/upload.cgi"
    MAX_FILE_SIZE = 102476800      # ограничение формы, из settings_fw_update.js
    BOUNDARY = "----S1010BreedUploadBoundary"

    def _multipart_body(self, path):
        """Собирает multipart/form-data вручную, целиком в памяти.

        Зачем вручную: при files={...} requests отправляет multipart
        через Transfer-Encoding: chunked, без Content-Length. Встроенный
        httpd роутера читает Content-Length и при его отсутствии
        отвечает "CONTENT_LENGTH is NULL", отклоняя файл.
        """
        name = os.path.basename(path)
        with open(path, "rb") as f:
            payload = f.read()

        sep = b"\r\n"
        parts = []
        for key, val in (("MAX_FILE_SIZE", str(self.MAX_FILE_SIZE)),
                         ("uploadType", "image")):
            parts.append(
                f"--{self.BOUNDARY}".encode() + sep
                + f'Content-Disposition: form-data; name="{key}"'.encode() + sep
                + sep
                + str(val).encode() + sep)
        parts.append(
            f"--{self.BOUNDARY}".encode() + sep
            + f'Content-Disposition: form-data; name="uploadedfile"; '
              f'filename="{name}"'.encode() + sep
            + b"Content-Type: application/octet-stream" + sep + sep
            + payload + sep)
        parts.append(f"--{self.BOUNDARY}--".encode() + b"\r\n")

        return b"".join(parts), len(payload)

    def _session_cookie_header(self):
        """Собирает заголовок Cookie из сессии requests для передачи в curl."""
        parts = []
        for c in self.s.cookies:
            parts.append(f"{c.name}={c.value}")
        return "; ".join(parts)

    def _upload_via_curl(self, path, timeout):
        """Загрузка файла системным curl.

        Зачем curl: встроенный httpd роутера капризен к multipart от
        requests и отвечает "CONTENT_LENGTH is NULL". curl всегда
        собирает тело целиком и отправляет с заголовком Content-Length,
        плюс сам обрабатывает multipart-разделители.
        """
        url = f"{self.base}/upload.cgi"
        cmd = [
            "curl", "-s", "-i",
            "--max-time", str(timeout),
            "-X", "POST", url,
            "-H", f"Accept-Language: {HEADERS['Accept-Language']}",
            "-H", f"Referer: {self.base}/settings.html",
            "-H", f"Origin: {self.base}",
            "-H", f"Cookie: {self._session_cookie_header()}",
            "-F", f"MAX_FILE_SIZE={self.MAX_FILE_SIZE}",
            "-F", "uploadType=image",
            "-F", f"uploadedfile=@{path}",
        ]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL,
                                  timeout=timeout + 15)
        except subprocess.TimeoutExpired:
            return None, "таймаут отправки через curl"
        return (proc.stdout or b"").decode("utf-8", "ignore"), None

    def upload_firmware(self, filename, timeout=600, verbose=True):
        """Загружает файл прошивки (Breed) через upload.cgi.

        Форма описана в js/settings_fw_update.js:
            <form enctype="multipart/form-data" action="upload.cgi" method="POST">
              <input name="MAX_FILE_SIZE" value="102476800">
              <input name="uploadType"    value="image">
              <input name="uploadedfile"  type="file">
        Возвращает (ok, detail).
        """
        path = filename if os.path.isabs(filename) else os.path.join(
            os.path.dirname(os.path.abspath(__file__)), filename)
        if not os.path.exists(path):
            return False, f"файл {filename} не найден"
        size = os.path.getsize(path)
        if size > self.MAX_FILE_SIZE:
            return False, f"файл {size} байт больше лимита {self.MAX_FILE_SIZE}"

        body, err = self._upload_via_curl(path, timeout)
        if err:
            return False, err
        if body is None:
            body = ""

        head = body[:200]
        low = body.lower()
        if "content_length is null" in low:
            return False, ("роутер не увидел Content-Length "
                           f"(тело {size} байт)")
        if resp_ok := ("200 ok" in head.lower()):
            if verbose:
                self.say(f"[+] {os.path.basename(path)} ({size} байт) залит")
            return True, "файл принят устройством"
        if "error" in low or "400" in head or "500" in head:
            clean = " ".join(body.split())[:160]
            return False, f"устройство отклонило файл: {clean}"
        if verbose:
            self.say(f"[+] {os.path.basename(path)} ({size} байт) отправлен, "
                     f"ответ: {' '.join(body.split())[:120]}")
        return True, "файл отправлен"
