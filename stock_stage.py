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
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

STOCK_IP = os.environ.get("S1010_STOCK_IP") or "192.168.0.1"
STOCK_LOGIN = os.environ.get("S1010_STOCK_USER") or "admin"

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
    def read_mac(self, endpoint="settings_wan2", field="wan_mac"):
        """Читает MAC роутера из data/<endpoint>.json.

        В провайдерской прошивке MAC хранится в settings_wan2.json
        в поле wan_mac; запасные источники - lan_mac и общий разбор.
        """
        ref = {"Referer": self.base + "/settings.html", "Origin": self.base}
        try:
            r = self.s.get(self.data_url(endpoint), headers=ref, timeout=20)
            data = json.loads(r.text)
        except (requests.RequestException, ValueError) as e:
            self.say(f"[-] {endpoint}.json недоступен: {e}")
            return None

        found = {}
        for item in data:
            if isinstance(item, dict):
                for key, val in item.items():
                    norm = normalize_mac(val)
                    if norm and not mac_is_invalid(norm):
                        found.setdefault(key, norm)

        mac = found.get(field)
        if mac is None:
            # берём первый осмысленный MAC
            for key in ("lan_mac", "wan_mac", "mac", "mac_address"):
                if key in found:
                    mac = found[key]
                    break
        if mac is None and found:
            mac = next(iter(found.values()))

        if mac:
            pretty = ":".join(mac[i:i + 2] for i in range(0, 12, 2))
            self.say(f"[+] MAC роутера: {pretty}")
        else:
            self.say(f"[-] MAC в {endpoint}.json не найден")
        return mac

    # ---------- загрузка прошивки ----------
    UPLOAD_CGI = "/upload.cgi"
    MAX_FILE_SIZE = 102476800      # ограничение формы, из settings_fw_update.js

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

        ref = {"Referer": self.base + "/settings.html", "Origin": self.base}
        try:
            with open(path, "rb") as f:
                resp = self.s.post(
                    self.base + self.UPLOAD_CGI,
                    files={"uploadedfile": (os.path.basename(path), f,
                                            "application/octet-stream")},
                    data={"MAX_FILE_SIZE": str(self.MAX_FILE_SIZE),
                          "uploadType": "image"},
                    headers=ref, timeout=timeout,
                )
        except requests.RequestException as e:
            return False, f"ошибка отправки файла: {e}"
        except OSError as e:
            return False, f"ошибка чтения файла: {e}"

        body = resp.text or ""
        low = body.lower()
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        for bad in ("error", "fail", "invalid", "not supported", "wrong"):
            if bad in low:
                return False, f"устройство отклонило файл: {body.strip()[:120]}"
        if verbose:
            self.say(f"[+] Файл {os.path.basename(path)} "
                     f"({size} байт) отправлен, HTTP {resp.status_code}")
            if body.strip():
                self.say(f"    ответ: {body.strip()[:150]}")
        return True, "файл принят устройством"
