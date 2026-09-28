#!/usr/bin/env python3
"""
Стенд прошивки роутера Sercomm S1010: сток Ростелеком -> Breed -> Wive-NG.

Полный цикл, от роутера со стоковой прошивкой до принятого Wive-NG:

  1. Ждём стоковую прошивку на 192.168.0.1
  2. Входим в веб-интерфейс (admin + пароль), см. stock_stage.py
  3. Читаем MAC роутера и запоминаем его
  4. Заливаем в стоковую прошивку образ Breed
  5. Ждём загрузки Breed на 192.168.1.1
  6. Записываем в Breed MAC-адреса (+0/+1/+2 от прочитанного)
  7. Перезагружаем роутер средствами Breed
  8. Заливаем Wive-NG, затем из него U-Boot
  9. Приёмка: вход в Wive-NG и сверка MAC

Порядок шагов 8 важен: сначала прошивка, затем U-Boot последним.
Иначе после записи U-Boot Breed уже заменён, и прошивку писать некуда
(ровно эта ошибка стоила нам неудачного прогона).

Запуск:
    python3 station.py --password 'пароль'
    python3 station.py --password 'пароль' --mac 749D7987B86E
    python3 station.py --password 'пароль' --breed-file breed-s1010.img
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from stock_stage import StockSession, normalize_mac, mac_is_invalid  # noqa: E402

C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_RED = "\033[91m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_CYAN = "\033[96m"

STOCK_IP = "192.168.0.1"
BREED_IP = "192.168.1.1"

REQUIRED_FILES = ["wive-ng-s1010.bin", "uboot-s1010-wive.bin"]

# Обработчик перезагрузки в веб-интерфейсе Breed (проверено на Breed 1.0):
#   1) GET  /reboot.html     - страница с формой, в ней hidden-поле magic
#   2) POST /rebooting.html  - submit=Reboot&magic=<значение из формы>
# Токен magic случайный, поэтому читается со страницы перед отправкой.
REBOOT_FORM_PATH = "/reboot.html"
REBOOT_ACTION_PATH = "/rebooting.html"
REBOOT_MAGIC_RE = re.compile(r'name="magic"\s+value="(\d+)"')

_stop = False


def handle_signal(signum, frame):
    global _stop
    _stop = True
    print()


def c(text, color):
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"{color}{text}{C_RESET}"


def fmt(mac):
    return ":".join(mac[i:i + 2] for i in range(0, 12, 2))


def ping_host(ip, count=1, timeout=1):
    proc = subprocess.run(
        ["ping", "-c", str(count), "-W", str(timeout), "-n", ip.split(":")[0]],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def step_banner(title):
    print()
    print(c("=" * 62, C_BOLD))
    print(f" {title}")
    print(c("=" * 62, C_BOLD))
    sys.stdout.flush()


def wait_for_host(ip, what, interval=1.0, timeout=300):
    """Ждёт появления узла по ping. False - по таймауту или прерыванию."""
    print(f"[*] Жду {what} по адресу {ip} (пинг раз в {interval:g} с)...")
    deadline = time.time() + timeout
    dots = 0
    while not _stop and time.time() < deadline:
        if ping_host(ip):
            print(c(f"[+] {what} отвечает на ping!", C_GREEN))
            time.sleep(1.0)
            return True
        dots = (dots + 1) % 4
        sys.stdout.write(f"\r    ждём {'.' * dots:<20}")
        sys.stdout.flush()
        time.sleep(interval)
    print()
    if _stop:
        return False
    print(c(f"[-] {what} не появился за {timeout:.0f} с", C_RED))
    return False


def wait_gone(ip, timeout=25):
    """Ждёт, пока узел пропадёт из сети. True - пропал."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _stop:
            return False
        if not ping_host(ip):
            return True
        time.sleep(0.5)
    return False


def wait_back(ip, timeout=180, label="роутер"):
    """Ждёт возвращения узла в сеть после перезагрузки."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _stop:
            return False
        if ping_host(ip):
            print(c(f"[+] {label} вернулся в сеть", C_GREEN))
            time.sleep(2.0)     # веб-сервер поднимается не мгновенно
            return True
        time.sleep(0.5)
    return False


# ---------- работа с Breed на 192.168.1.1 ----------

def breed_curl(args, timeout=20):
    return subprocess.run(
        ["curl", "-s", "--max-time", str(timeout)] + args,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=timeout + 5,
    )


def fetch_breed_magic(ip):
    """Читает токен magic со страницы ребута Breed."""
    proc = breed_curl([f"http://{ip}{REBOOT_FORM_PATH}"])
    match = REBOOT_MAGIC_RE.search((proc.stdout or b"").decode("utf-8", "ignore"))
    return match.group(1) if match else None


def reboot_via_breed(ip):
    """Перезагружает роутер средствами веб-интерфейса Breed.

    Протокол проверен на живом Breed 1.0: GET /reboot.html за токеном
    magic, затем POST /rebooting.html с submit=Reboot и этим токеном.
    Успех подтверждаем фактом - роутер должен пропасть из сети.
    """
    magic = fetch_breed_magic(ip)
    if not magic:
        print(c("  [-] токен magic со страницы ребута не получен", C_YELLOW))
        return False
    print(f"  токен magic получен ({len(magic)} символов)")
    try:
        breed_curl(["-X", "POST", f"http://{ip}{REBOOT_ACTION_PATH}",
                    "-d", f"submit=Reboot&magic={magic}"])
    except subprocess.TimeoutExpired:
        print(c("  Breed не успел ответить - вероятно, уже уходит в ребут", C_CYAN))
    if wait_gone(ip, timeout=25):
        print(c("  [+] Роутер ушёл в перезагрузку", C_GREEN))
        return True
    return False


def write_mac_to_breed(ip, base_mac):
    """Записывает MAC-адреса в Breed через POST /mac.html.

    Breed ждёт 54 параметра: три адреса вычисляются от базового
    (+0 LAN/RF1, +1 WLAN 2.4, +2 WLAN 5), остальные обнуляются.
    """
    base = int(base_mac, 16)

    def octets(offset):
        val = base + offset
        return [f"{val:012X}"[i:i + 2] for i in range(0, 12, 2)]

    parts = []
    for i in range(6):
        parts.append(f"wlan_mac1_mac{i}={octets(1)[i]}")
    for i in range(6):
        parts.append(f"mac1_1_mac{i}={octets(0)[i]}")
    for i in range(6):
        parts.append(f"mac1_2_mac{i}=FF")
    for i in range(6):
        parts.append(f"wlan_mac2_mac{i}={octets(2)[i]}")
    for i in range(6):
        parts.append(f"mac2_1_mac{i}=00")
    for i in range(6):
        parts.append(f"mac2_2_mac{i}=00")
    for i in range(6):
        parts.append(f"lan_mac_mac{i}=00")
    for i in range(6):
        parts.append(f"wan_mac_mac{i}=00")
    parts.append("submit=Modify")

    body = "&".join(parts)
    proc = breed_curl(["-i", "-X", "POST", f"http://{ip}/mac.html",
                       "-H", "Content-Type: application/x-www-form-urlencoded",
                       "-H", f"Referer: http://{ip}/mac.html",
                       "-H", "Origin: http://" + ip,
                       "--data-raw", body], timeout=25)
    text = (proc.stdout or b"").decode("utf-8", "ignore")
    if "200 OK" in text or "successfully" in text:
        print(c("  [+] MAC-адреса записаны в Breed", C_GREEN))
        return True
    print(c("  [-] Breed не подтвердил запись MAC", C_RED))
    print(c(f"      ответ: {text.strip()[:200]}", C_DIM))
    return False


def upload_to_breed(ip, path, field="firmware", timeout=900):
    """Загружает бинарник в Breed одной отправкой файла.

    Breed: POST /upload.html с полем boot_file для U-Boot,
           POST /            с полем firmware для прошивки.
    """
    name = os.path.basename(path)
    size = os.path.getsize(path)
    print(f"  отправляю {name} ({size} байт)...")
    url = f"http://{ip}/upload.html" if field == "boot_file" else f"http://{ip}/"
    try:
        proc = breed_curl([
            "-i", "-X", "POST", url,
            "-F", f"{field}=@{path}",
        ], timeout=timeout)
    except subprocess.TimeoutExpired:
        print(c(f"  [-] таймаут отправки {name} (устройство могло уйти в ребут)", C_YELLOW))
        return None
    text = (proc.stdout or b"").decode("utf-8", "ignore")
    if "200 OK" in text or "successfully" in text:
        print(c(f"  [+] {name} принят устройством", C_GREEN))
        return True
    if text.strip():
        print(c(f"  [!] неоднозначный ответ на {name}: {text.strip()[:150]}", C_YELLOW))
    return False


def run_step(title, script, router_ip, env_extra=None):
    """Запускает существующий скрипт шага, печатая вывод построчно."""
    path = os.path.join(HERE, script)
    env = dict(os.environ)
    env["S1010_ROUTER_IP"] = router_ip
    if env_extra:
        env.update(env_extra)

    step_banner(title)
    proc = subprocess.Popen(
        [sys.executable, path], cwd=HERE, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    tail = []
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        if len(tail) > 10:
            tail = tail[-10:]
        print(f"  {line}")
    proc.wait()
    if proc.returncode != 0 and tail:
        print(c(f"  [итог] ошибка: {tail[-1]}", C_RED))
    return proc.returncode == 0


def main():
    p = argparse.ArgumentParser(
        description="Стенд прошивки Sercomm S1010: сток -> Breed -> Wive-NG")
    p.add_argument("--password", help="пароль администратора стоковой прошивки")
    p.add_argument("--login", default="admin", help="логин (по умолчанию admin)")
    p.add_argument("--stock-ip", default=STOCK_IP, help="адрес стоковой прошивки")
    p.add_argument("--breed-ip", default=BREED_IP, help="адрес Breed")
    p.add_argument("--mac", help="задать MAC вручную, не читая с роутера")
    p.add_argument("--breed-file", default="breed-s1010.img",
                   help="файл образа Breed для заливки в стоковую прошивку")
    args = p.parse_args()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    password = args.password or os.environ.get("S1010_STOCK_PASSWORD") or ""
    if not password:
        try:
            import getpass
            password = getpass.getpass("Пароль администратора роутера: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 1

    print(c("=" * 62, C_BOLD))
    print(c(" СТЕНД ПРОШИВКИ SERCOMM S1010", C_BOLD))
    print(c("=" * 62, C_BOLD))
    print("  1. Ждём стоковую прошивку Ростелекома")
    print("  2. Входим, читаем MAC, заливаем Breed")
    print("  3. Breed: MAC, перезагрузка, Wive-NG и U-Boot")
    print("  4. Приёмка Wive-NG")

    for f in REQUIRED_FILES:
        if not os.path.exists(os.path.join(HERE, f)):
            print(f"[-] Не найден файл {f} в {HERE}")
            return 1

    # ---------- 1. Ждём стоковую прошивку ----------
    step_banner("ШАГ 1/5: ОЖИДАНИЕ СТОКОВОЙ ПРОШИВКИ")
    if not wait_for_host(args.stock_ip, "роутер на стоковой прошивке"):
        return 1

    # ---------- 2. Вход, MAC, заливка Breed ----------
    step_banner("ШАГ 2/5: ВХОД, ЧТЕНИЕ MAC, ЗАЛИВКА BREED")
    stock = StockSession(ip=args.stock_ip, login=args.login,
                         password=password, verbose=True)
    if not stock.login_router():
        print(c("[-] Не удалось войти в интерфейс роутера", C_RED))
        return 1

    if args.mac:
        mac = normalize_mac(args.mac)
        if not mac or mac_is_invalid(mac):
            print(f"[-] Некорректный MAC в --mac: {args.mac}")
            return 1
        print(f"[*] MAC задан аргументом: {fmt(mac)}")
    else:
        mac = stock.read_mac()
        if not mac:
            print(c("[-] Не удалось прочитать MAC роутера", C_RED))
            return 1
    print(f"[i] запомнил MAC роутера: {fmt(mac)}")

    breed_path = stock.resolve_breed_file(args.breed_file)
    if not breed_path:
        print(f"[-] Файл Breed не найден. Искали: {args.breed_file}")
        print("    Положите образ в каталог проекта или укажите --breed-file")
        return 1
    print(f"[*] Файл Breed: {breed_path} ({os.path.getsize(breed_path)} байт)")

    print(f"[*] Заливаю Breed: {os.path.basename(breed_path)}")
    ok, detail = stock.upload_firmware(breed_path)
    if not ok:
        print(c(f"[-] Breed не залит: {detail}", C_RED))
        return 1

    # ---------- 3. Ждём Breed ----------
    step_banner("ШАГ 3/5: ОЖИДАНИЕ ЗАГРУЗКИ BREED")
    print("[*] Роутер перезагружается с новым загрузчиком, это занимает время.")
    if not wait_for_host(args.breed_ip, "загрузчик Breed", timeout=420):
        print(c("[-] Breed не поднялся. Проверьте, что образ подходит модели.", C_RED))
        return 1

    # ---------- 4. Работа в Breed ----------
    step_banner("ШАГ 4/5: MAC, ПЕРЕЗАГРУЗКА, ПРОШИВКА WIVE-NG И U-BOOT")
    base = int(mac, 16)
    print("  Будет записано в Breed:")
    print(f"    RF1 MAC1 (+0): {fmt(mac)}")
    print(f"    RF1 WLAN   (+1): {fmt(f'{base + 1:012X}')}")
    print(f"    RF2 WLAN   (+2): {fmt(f'{base + 2:012X}')}")

    if not write_mac_to_breed(args.breed_ip, mac):
        return 1

    print("[*] Перезагружаю роутер средствами Breed...")
    if not reboot_via_breed(args.breed_ip):
        print(c("  [!] Breed не смог перезагрузить роутер.", C_YELLOW))
        print("      Нажмите питание на роутере и дождитесь возврата в сеть.")
        if not wait_back(args.breed_ip, timeout=180, label="роутер в Breed"):
            print(c("[-] Роутер не вернулся. Останов.", C_RED))
            return 1

    # ВАЖНО: сначала прошивка, затем U-Boot последним.
    # После записи U-Boot загрузчик Breed исчезает, и прошивку уже некуда писать.
    wive = os.path.join(HERE, "wive-ng-s1010.bin")
    uboot = os.path.join(HERE, "uboot-s1010-wive.bin")

    print("[*] Заливаю прошивку Wive-NG в Breed...")
    res = upload_to_breed(args.breed_ip, wive, field="firmware")
    if res is False:
        print(c("[-] Прошивка Wive-NG не залита", C_RED))
        return 1
    print("[*] Жду перезагрузки после записи прошивки...")
    if not wait_gone(args.breed_ip, timeout=60):
        print(c("  [!] роутер не пропал из сети", C_YELLOW))
    if not wait_back(args.breed_ip, timeout=300, label="роутер в Breed"):
        print(c("[-] Роутер не вернулся после записи прошивки", C_RED))
        return 1

    print("[*] Заливаю U-Boot последним шагом...")
    res = upload_to_breed(args.breed_ip, uboot, field="boot_file")
    if res is False:
        print(c("[-] U-Boot не залит", C_RED))
        return 1
    if res is None:
        wait_back(args.breed_ip, timeout=300, label="роутер")
    else:
        print("[*] Жду перезагрузки после записи U-Boot...")
        if not wait_gone(args.breed_ip, timeout=90):
            print(c("  [!] роутер не пропал из сети, жду возврата", C_YELLOW))
        if not wait_back(args.breed_ip, timeout=300, label="роутер"):
            print(c("[-] Роутер не вернулся после записи U-Boot", C_RED))
            return 1

    # ---------- 5. Приёмка ----------
    step_banner("ШАГ 5/5: ОЖИДАНИЕ WIVE-NG И ПРИЁМКА")
    print("[*] Роутер загружает Wive-NG, ждём 15 секунд...")
    for _ in range(15):
        if _stop:
            return 1
        time.sleep(1.0)
    if not run_step("ПРИЁМКА WIVE-NG", "check_wive.py", args.breed_ip):
        print(c("[-] Приёмка не пройдена. Проверьте роутер вручную.", C_RED))
        return 1

    print()
    print(c("=" * 62, C_BOLD))
    print(c(f" РОУТЕР {fmt(mac)} ПРОШИТ В WIVE-NG И ПРИНЯТ", C_GREEN))
    print(c("=" * 62, C_BOLD))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        print(c("[-] Прервано оператором.", C_YELLOW))
        sys.exit(1)

