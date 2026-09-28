#!/usr/bin/env python3
"""
Стенд прошивки одного роутера Sercomm S1010: Breed -> U-Boot -> Wive-NG.

Скрипт ждёт появления роутера по адресу 192.168.1.1 (роутер в Breed),
запрашивает MAC с корпуса, дальше отрабатывает пайплайн:

    1. change_mac_breed.py  - считает +0/+1/+2 от базового MAC и пишет в Breed
    2. ребут через Breed     - команда на перезагрузку, при неудаче жмём питание
    3. autoflash_wiveng.py  - заливка U-Boot, затем из него Wive-NG
    4. check_wive.py        - приёмка: логин в Wive-NG и сверка MAC

Подготовка (один раз на роутер, вручную): в веб-интерфейсе стоковой
прошивки Ростелекома http://192.168.0.1 (admin + пароль с корпуса)
залить Breed. Роутер встаёт в Breed на 192.168.1.1.

Запуск:
    python3 station.py
    python3 station.py --mac 142E5E7B8C72      # без интерактивного ввода
    python3 station.py --no-wait               # не ждать пинг
    python3 station.py --router-ip 192.168.1.1
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_RED = "\033[91m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_CYAN = "\033[96m"

REQUIRED_FILES = ["uboot-s1010-wive.bin", "wive-ng-s1010.bin"]

# Кандидаты обработчика перезагрузки в веб-интерфейсе Breed.
# Точный путь зависит от сборки, поэтому перебираем и проверяем фактом.
REBOOT_PATHS = ["/reboot", "/reboot.html", "/index.html?reboot", "/"]

_stop = False


def handle_signal(signum, frame):
    global _stop
    _stop = True
    print()


def c(text, color):
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"{color}{text}{C_RESET}"


def normalize_mac(raw):
    clean = re.sub(r"[\s:\-.]", "", str(raw or "")).upper()
    if not re.fullmatch(r"[0-9A-F]{12}", clean):
        return None
    return clean


def mac_is_invalid(mac):
    """Отсекаем нули, broadcast и multicast."""
    if mac == "000000000000" or mac == "FFFFFFFFFFFF":
        return True
    return (int(mac[0:2], 16) & 1) == 1


def fmt(mac):
    return ":".join(mac[i:i + 2] for i in range(0, 12, 2))


def ping_router(ip):
    # -n не резолвит имя; порт в адресе, если задан, отбрасываем
    host = ip.split(":")[0]
    proc = subprocess.run(
        ["ping", "-c", "1", "-W", "1", "-n", host],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def wait_for_router(ip, interval=1.0):
    """Ждёт появления роутера по ping. False - остановлено или не дождались."""
    print(f"[*] Жду роутер по адресу {ip} (пинг раз в {interval:g} с)...")
    started = time.time()
    dots = 0
    while not _stop:
        if ping_router(ip):
            print(f"\r{'[+] Роутер ответил на ping!':<62}")
            time.sleep(1.0)   # веб-сервер Breed поднимается не мгновенно
            return True
        dots = (dots + 1) % 4
        sys.stdout.write(f"\r    ждём роутер {'.' * dots:<20}")
        sys.stdout.flush()
        time.sleep(interval)
    print()
    return False


def try_reboot_via_breed(ip, timeout=15):
    """Пробует перезагрузить роутер средствами веб-интерфейса Breed.

    У Breed есть собственный обработчик перезагрузки, поэтому обходимся
    без нажатия кнопки питания. Точный путь зависит от сборки Breed,
    поэтому перебираем несколько кандидатов. Успех определяем не по
    коду ответа, а по факту: роутер должен пропасть из сети.
    """
    for path in REBOOT_PATHS:
        # ip может содержать порт ("192.168.1.1:8080") - тогда не добавляем
        # двоеточие лишний раз
        host = ip if ":" in ip else f"{ip}:80"
        url = f"http://{host}{path}"
        print(f"  пробую ребут через Breed: {path}")
        try:
            subprocess.run(
                ["curl", "-s", "-o", os.devnull, "--max-time", "5",
                 "-X", "POST", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
            )
        except subprocess.TimeoutExpired:
            # Breed не успел ответить - вероятно, уже уходит в ребут
            print(c("    Breed не ответил, возможно уходит в ребут...", C_CYAN))
            if wait_gone(ip, timeout):
                return True
            continue
        except Exception as e:
            print(f"    ошибка запроса: {e}")
            continue

        # Даём Breed секунду на старт ребута и смотрим, не пропал ли роутер
        if wait_gone(ip, timeout):
            return True
    return False


def wait_gone(ip, timeout=15):
    """Ждёт, пока роутер пропадёт из сети. True - пропал."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _stop:
            return False
        if not ping_router(ip):
            print(c("  [+] Роутер ушёл в перезагрузку", C_CYAN))
            return True
        time.sleep(0.5)
    return False


def wait_back_only(ip, back_timeout=150):
    """Ждёт возвращения роутера в сеть (ребут уже отправлен)."""
    for _ in range(int(back_timeout / 0.5)):
        if _stop:
            return False
        if ping_router(ip):
            print(c("  [+] Роутер вернулся в сеть!", C_GREEN))
            time.sleep(2.0)   # веб-сервер Breed поднимается не мгновенно
            return True
        time.sleep(0.5)
    return False


def wait_router_back(ip, gone_timeout=60, back_timeout=120):
    """Ручной ребут: ждёт пропадания роутера, затем возвращения.

    Используется как запасной вариант, если Breed не смог перезагрузить
    устройство сам. Breed после правки MAC сам не перезагружается, в этом
    режиме перезагрузку делает оператор кнопкой питания.
    """
    print()
    print(c("=" * 62, C_BOLD))
    print(c(" НУЖНА ПЕРЕЗАГРУЗКА РОУТЕРА", C_YELLOW))
    print(c("=" * 62, C_YELLOW))
    print("  Breed не смог перезагрузить роутер сам.")
    print("  Нажмите питание на роутере (выкл/вкл) и дождитесь,")
    print(f"  пока он снова ответит на {ip}.")
    print("-" * 62)

    gone = False
    for _ in range(int(gone_timeout / 0.5)):
        if _stop:
            return False
        if not ping_router(ip):
            gone = True
            print(c("  [*] Роутер ушёл в перезагрузку", C_CYAN))
            break
        time.sleep(0.5)
    if _stop:
        return False
    if not gone:
        print(c("  [!] Роутер не пропал из сети, жду появления", C_YELLOW))

    for _ in range(int(back_timeout / 0.5)):
        if _stop:
            return False
        if ping_router(ip):
            print(c("  [+] Роутер вернулся в сеть!", C_GREEN))
            time.sleep(2.0)
            return True
        time.sleep(0.5)
    return False


def prompt_mac():
    """Запрашивает MAC с корпуса, пока не введён корректный."""
    print()
    print(c("=" * 62, C_BOLD))
    print(c(" ВВЕДИТЕ MAC С КОРПУСА РОУТЕРА", C_BOLD))
    print(c("=" * 62, C_BOLD))
    print("  Примеры: 14:2e:5e:7b:8c:72   или   142E5E7B8C72")
    print("  Остальные адреса скрипт посчитает сам.")
    print("-" * 62)
    for _ in range(5):
        try:
            raw = input("  MAC: ").strip()
        except EOFError:
            return None
        if raw.lower() in ("q", "quit", "exit"):
            return None
        mac = normalize_mac(raw)
        if mac is None:
            print(c("  [-] Некорректный формат: нужно 12 hex-символов (0-9, A-F).", C_RED))
            continue
        if mac_is_invalid(mac):
            print(c("  [-] Такой MAC не подходит (нули, broadcast или multicast).", C_RED))
            continue
        return mac
    return None


def run_step(title, script, router_ip, env_extra=None):
    """Запускает шаг пайплайна, печатая вывод по мере поступления."""
    path = os.path.join(HERE, script)
    env = dict(os.environ)
    env["S1010_ROUTER_IP"] = router_ip
    if env_extra:
        env.update(env_extra)

    print()
    print(c("=" * 62, C_BOLD))
    print(f" {title}")
    print(c("=" * 62, C_BOLD))

    proc = subprocess.Popen(
        [sys.executable, path],
        cwd=HERE,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    tail = []
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            if len(tail) > 10:
                tail = tail[-10:]
            print(f"  {line}")
    except KeyboardInterrupt:
        proc.kill()
        raise
    finally:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    if proc.returncode != 0 and tail:
        print(c(f"  [итог] ошибка: {tail[-1]}", C_RED))
    return proc.returncode == 0

def main():
    p = argparse.ArgumentParser(
        description="Стенд прошивки одного Sercomm S1010 (Breed -> U-Boot -> Wive-NG)"
    )
    p.add_argument("--router-ip", default="192.168.1.1",
                   help="адрес роутера в Breed (по умолчанию 192.168.1.1)")
    p.add_argument("--mac", help="задать MAC сразу, без интерактивного ввода")
    p.add_argument("--no-wait", action="store_true",
                   help="не ждать пинг, сразу спросить MAC")
    p.add_argument("--skip-reboot-wait", action="store_true",
                   help="не ждать ребута после правки MAC")
    args = p.parse_args()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(c("=" * 62, C_BOLD))
    print(c(" СТЕНД ПРОШИВКИ SERCOMM S1010", C_BOLD))
    print(c("=" * 62, C_BOLD))
    print("  0 (вручную)  Breed уже залит через веб-интерфейс стоковой")
    print("               прошивки Ростелекома (http://192.168.0.1)")
    print(f"  1            Ждём роутер на {args.router_ip}, вводим MAC с корпуса")
    print("  2            Ребут кнопкой питания -> U-Boot -> Wive-NG")
    print("  3            Приёмка: логин в Wive-NG и сверка MAC")
    print()

    for f in REQUIRED_FILES:
        if not os.path.exists(os.path.join(HERE, f)):
            print(f"[-] Не найден файл {f} в каталоге {HERE}")
            return 1

    if args.no_wait:
        print(f"[*] --no-wait: пропускаю ожидание {args.router_ip}.")
    elif not wait_for_router(args.router_ip):
        print(c("\n[-] Роутер не появился. Выход.", C_RED))
        return 1

    if args.mac:
        mac = normalize_mac(args.mac)
        if not mac or mac_is_invalid(mac):
            print(f"[-] Некорректный MAC в --mac: {args.mac}")
            return 1
        print(f"[*] MAC задан аргументом: {mac}")
    else:
        mac = prompt_mac()
        if mac is None:
            print(c("\n[-] MAC не введён. Выход.", C_RED))
            return 1

    base = int(mac, 16)
    print()
    print(c("  Будет записано в Breed:", C_BOLD))
    print(f"    RF1 MAC1 (оригинал, +0): {fmt(mac)}")
    print(f"    RF1 WLAN      (MAC + 1): {fmt(f'{base + 1:012X}')}")
    print(f"    RF2 WLAN      (MAC + 2): {fmt(f'{base + 2:012X}')}")
    print("    RF1 MAC2, RF2 MAC1/MAC2, LAN, WAN: обнуляются (00/FF)")

    if not run_step("ШАГ 1/3: ПРАВКА MAC-АДРЕСОВ В BREED",
                    "change_mac_breed.py", args.router_ip,
                    env_extra={"S1010_BASE_MAC": mac, "S1010_ASSUME_YES": "1"}):
        print(c("\n❌ Не удалось записать MAC в Breed. Конвейер остановлен.", C_RED))
        return 1

    # --- Ребут роутера через Breed, иначе просим нажать питание ---
    if args.skip_reboot_wait:
        print()
        print(c("[!] --skip-reboot-wait: ожидание ребута пропущено.", C_YELLOW))
    else:
        print()
        print(c("=" * 62, C_BOLD))
        print(c(" ПЕРЕЗАГРУЗКА РОУТЕРА ЧЕРЕЗ BREED", C_BOLD))
        print(c("=" * 62, C_BOLD))
        print(f"  Отправляю команду ребута на {args.router_ip} через веб-интерфейс Breed.")
        print("  Если Breed не умеет - скрипт попросит нажать питание вручную.")
        print("-" * 62)

        if try_reboot_via_breed(args.router_ip):
            print()
            print("[*] Ребут отправлен, жду возвращения роутера в сеть...")
            if not wait_back_only(args.router_ip):
                print(c("\n❌ Роутер не вернулся после ребута. Конвейер остановлен.", C_RED))
                return 1
        else:
            print()
            print(c("  [!] Breed не смог перезагрузить роутер, перехожу на ручной режим.", C_YELLOW))
            if not wait_router_back(args.router_ip):
                print(c("\n❌ Роутер не вернулся после ребута. Конвейер остановлен.", C_RED))
                return 1

    if _stop:
        return 1
    if not run_step("ШАГ 2/3: ЗАЛИВКА U-BOOT И WIVE-NG",
                    "autoflash_wiveng.py", args.router_ip):
        print(c("\n❌ Прошивка не удалась. Конвейер остановлен.", C_RED))
        return 1

    print()
    print("[*] Роутер перезагружается в Wive-NG, ждём 10 секунд...")
    for _ in range(10):
        if _stop:
            return 1
        time.sleep(1.0)

    if _stop:
        return 1
    if not run_step("ШАГ 3/3: ПРИЁМКА РОУТЕРА WIVE-NG",
                    "check_wive.py", args.router_ip):
        print(c("\n❌ Приёмка не пройдена. Проверьте роутер вручную.", C_RED))
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


