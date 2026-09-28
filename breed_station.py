#!/usr/bin/env python3
"""
Пайплайн прошивки Sercomm S1010, стартующий сразу с загрузчика Breed.

Используется, когда Breed уже залит и роутер поднялся на 192.168.1.1.
Шаги со стоковой прошивкой Ростелекома (вход, чтение MAC, заливка Breed)
здесь пропускаются.

Последовательность:
  1. Ждём Breed на 192.168.1.1 и его веб-интерфейс
  2. Определяем базовый MAC (аргумент --mac или читаем из самого Breed)
  3. Записываем MAC-адреса в Breed (+0 LAN, +1 WLAN 2.4, +2 WLAN 5)
  4. Перезагружаем роутер средствами Breed
  5. Заливаем прошивку Wive-NG
  6. Заливаем U-Boot ПОСЛЕДНИМ шагом
  7. Приёмка Wive-NG

Почему U-Boot последним: после его записи загрузчик Breed исчезает,
и прошивку выше уже некуда положить. Обратный порядок приводит к
состоянию "U-Boot есть, прошивки нет" - так роутер мигает оранжевым
и не грузится.

Запуск:
    python3 breed_station.py --mac 14:2E:5E:8B:51:BA
    python3 breed_station.py                    # MAC читается из Breed
    python3 breed_station.py --mac ... --dry-run
"""

import argparse
import os
import re
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Переиспользуем функции основного пайплайна, чтобы логика не расходилась
import station  # noqa: E402
from station import (  # noqa: E402
    BREED_IP, C_BOLD, C_GREEN, C_RED, C_YELLOW, STOCK_IP, c, fmt,
    handle_signal, is_uboot, reboot_via_breed, run_step, step_banner,
    upload_to_breed, wait_back, wait_for_host, wait_for_stage, wait_gone,
    write_mac_to_breed, REQUIRED_FILES,
)
from stock_stage import normalize_mac, mac_is_invalid  # noqa: E402


def read_mac_from_breed(ip):
    """Читает текущий MAC из формы Breed /mac.html.

    Breed показывает и пустые поля (00) - их пропускаем и берём
    первый непустой unicast-адрес. Если все адреса нулевые, значит
    MAC в Breed не восстановлен, и его нужно задать ключом --mac.
    """
    proc = station.breed_curl([f"http://{ip}/mac.html"])
    text = (proc.stdout or b"").decode("utf-8", "ignore")
    pairs = []
    for m in re.finditer(
            r'generate_mac_input\(\s*"[^"]*"\s*,\s*"([a-z0-9_]+)"\s*,\s*"([0-9A-Fa-f:-]{17})"',
            text):
        pairs.append((m.group(1), normalize_mac(m.group(2))))
    if not pairs:
        for m in re.finditer(r'name="([a-z0-9_]+)"[^>]*value="([0-9A-Fa-f:-]{17})"',
                             text):
            pairs.append((m.group(1), normalize_mac(m.group(2))))

    print("  MAC-адреса, записанные в Breed сейчас:")
    for name, mac in pairs:
        if mac is None:
            continue
        flag = "  <- нулевой" if mac == "000000000000" else ""
        print(f"    {name:12} = {fmt(mac)}{flag}")

    for want in ("mac1_1", "lan_mac", "wlan_mac1", "wlan_mac2"):
        for name, mac in pairs:
            if name == want and mac and not mac_is_invalid(mac):
                return mac
    return None


def do_diagnose(breed_ip, stock_ip="192.168.0.1"):
    """Показывает состояние роутера, ничего не записывая.

    Полезно перед запуском пайплайна: видно, на какой стадии находится
    роутер, жив ли загрузчик и что в нём записано.
    """
    step_banner("ДИАГНОСТИКА РОУТЕРА (только чтение)")
    print(f"  Сток:  {stock_ip}")
    print(f"  Breed: {breed_ip}\n")

    ok = 0
    fail = 0

    def mark(good, text):
        nonlocal ok, fail
        if good:
            ok += 1
            print(c(f"  [+] {text}", C_GREEN))
        else:
            fail += 1
            print(c(f"  [-] {text}", C_RED))

    # --- стоковая прошивка ---
    stock_ping = station.ping_host(stock_ip)
    mark(stock_ping, f"пинг {stock_ip}: " +
         ("отвечает" if stock_ping else "тишина"))
    if stock_ping:
        proc = station.breed_curl([f"http://{stock_ip}/login.html"], timeout=10)
        mark(b"csrf_token" in (proc.stdout or b""),
             "сток отдаёт страницу логина (веб-интерфейс живой)")

    # --- Breed ---
    breed_ping = station.ping_host(breed_ip)
    mark(breed_ping, f"пинг {breed_ip}: " +
         ("отвечает" if breed_ping else "тишина"))

    if breed_ping:
        proc = station.breed_curl([f"http://{breed_ip}/"], timeout=10)
        web_ok = b"Breed" in (proc.stdout or b"")
        mark(web_ok, "веб-интерфейс Breed отвечает")
        if not web_ok:
            print(c("      роутер пингуется, но httpd не поднят.", C_YELLOW))
            print(c("      Нужен ребут питанием; если не помог - вход в Breed", C_YELLOW))
            print(c("      зажатием Reset при включении.", C_YELLOW))

    # --- MAC в Breed ---
    if breed_ping and station.breed_curl([f"http://{breed_ip}/mac.html"],
                                         timeout=10).stdout:
        print()
        mac = read_mac_from_breed(breed_ip)
        if mac:
            print(c(f"  [+] базовый MAC определён: {fmt(mac)}", C_GREEN))
        else:
            print(c("  [-] все MAC нулевые - нужен ключ --mac", C_YELLOW))

    print()
    print(c("-" * 62, C_BOLD))
    print(f"  Итог: успешно {ok}, проблем {fail}")
    if fail and not breed_ping:
        print(c("  Роутер не в сети. Проверьте питание и подключение.", C_YELLOW))
    print(c("-" * 62, C_BOLD))
    return 0 if fail == 0 else 1


def main():
    p = argparse.ArgumentParser(
        description="Прошивка Sercomm S1010 начиная с загрузчика Breed")
    p.add_argument("--mac", help="базовый MAC роутера (иначе читается из Breed)")
    p.add_argument("--breed-ip", default=BREED_IP, help="адрес Breed")
    p.add_argument("--stock-ip", default=STOCK_IP, help="адрес стоковой прошивки")
    p.add_argument("--no-wait", action="store_true", help="не ждать пинг Breed")
    p.add_argument("--dry-run", action="store_true",
                   help="показать план и выйти, ничего не записывая")
    p.add_argument("--doctor", action="store_true",
                   help="диагностика роутера без записи чего-либо")
    args = p.parse_args()

    if args.doctor:
        return do_diagnose(args.breed_ip, args.stock_ip)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(c("=" * 62, C_BOLD))
    print(c(" ПРОШИВКА S1010 НАЧИНАЯ С BREED", C_BOLD))
    print(c("=" * 62, C_BOLD))
    print(f"  Breed ожидается на {args.breed_ip}")
    print("  Затем: MAC -> ребут -> Wive-NG -> U-Boot -> приёмка")

    for f in REQUIRED_FILES:
        if not os.path.exists(os.path.join(HERE, f)):
            print(f"[-] Не найден файл {f} в {HERE}")
            return 1

    # ---------- 1. Ждём Breed ----------
    step_banner("ШАГ 1/6: ОЖИДАНИЕ ЗАГРУЗЧИКА BREED")
    if args.no_wait:
        print("[*] --no-wait: пропускаю ожидание")
    elif not wait_for_host(args.breed_ip, "загрузчик Breed", timeout=300):
        return 1

    # Веб-интерфейс Breed поднимается чуть позже самого пинга
    for _ in range(30):
        if station._stop:
            return 1
        proc = station.breed_curl([f"http://{args.breed_ip}/"], timeout=8)
        if b"Breed" in (proc.stdout or b""):
            print(c("[+] Веб-интерфейс Breed отвечает", C_GREEN))
            break
        time.sleep(1.0)
    else:
        print(c("[-] Веб-интерфейс Breed не отвечает", C_RED))
        return 1

    # ---------- 2. MAC ----------
    step_banner("ШАГ 2/6: ОПРЕДЕЛЕНИЕ БАЗОВОГО MAC")
    if args.mac:
        mac = normalize_mac(args.mac)
        if not mac or mac_is_invalid(mac):
            print(f"[-] Некорректный MAC в --mac: {args.mac}")
            return 1
        print(f"[*] MAC задан аргументом: {fmt(mac)}")
    else:
        mac = read_mac_from_breed(args.breed_ip)
        if not mac:
            print(c("[-] В Breed все MAC нулевые - укажите --mac вручную", C_RED))
            return 1
        print(f"[+] Базовый MAC прочитан из Breed: {fmt(mac)}")

    base = int(mac, 16)
    print("\n  Будет записано в Breed:")
    print(f"    RF1 MAC1 (оригинал, +0): {fmt(mac)}")
    print(f"    RF1 WLAN       (MAC + 1): {fmt(f'{base + 1:012X}')}")
    print(f"    RF2 WLAN       (MAC + 2): {fmt(f'{base + 2:012X}')}")
    print("    RF1 MAC2, RF2 MAC1/MAC2, LAN, WAN: обнуляются (00/FF)")

    if args.dry_run:
        print()
        print(c("--dry-run: ничего не записываю, выхожу.", C_YELLOW))
        return 0

    confirm = input("\n  Записать эти адреса в Breed? (y/n): ").strip().lower()
    if confirm not in ("y", "yes", "д", "да"):
        print(c("[-] Отменено оператором.", C_YELLOW))
        return 1

    # ---------- 3. Запись MAC ----------
    step_banner("ШАГ 3/6: ЗАПИСЬ MAC-АДРЕСОВ В BREED")
    if not write_mac_to_breed(args.breed_ip, mac):
        return 1

    # ---------- 4. Ребут ----------
    step_banner("ШАГ 4/6: ПЕРЕЗАГРУЗКА СРЕДСТВАМИ BREED")
    print("[*] Отправляю команду ребута...")
    if not reboot_via_breed(args.breed_ip):
        print(c("  [!] Breed не смог перезагрузить роутер.", C_YELLOW))
        print("      Нажмите питание на роутере и дождитесь возврата в сеть.")
    if not wait_back(args.breed_ip, timeout=240, label="роутер в Breed"):
        print(c("[-] Роутер не вернулся после ребута. Останов.", C_RED))
        return 1

    # ВАЖНО: порядок именно такой.
    #   Breed -> пишем U-Boot -> перезагрузка -> на том же адресе
    #   приходит U-Boot -> в него кладём прошивку Wive-NG.
    # Обратный порядок невозможен: после записи U-Boot Breed исчезает.
    step_banner("ШАГ 5/6: ЗАЛИВКА U-BOOT, ПРОШИВКА WIVE-NG И ПРИЁМКА")
    wive = os.path.join(HERE, "wive-ng-s1010.bin")
    uboot = os.path.join(HERE, "uboot-s1010-wive.bin")

    print("[*] Заливаю U-Boot из Breed...")
    res = upload_to_breed(args.breed_ip, uboot, field="boot_file")
    if res is False:
        print(c("[-] U-Boot не залит", C_RED))
        return 1
    print("[*] Жду перезагрузки после записи U-Boot...")
    if not wait_gone(args.breed_ip, timeout=120):
        print(c("  [!] роутер не пропал из сети, жду возврата", C_YELLOW))
    if not wait_back(args.breed_ip, timeout=300, label="роутер"):
        print(c("[-] Роутер не вернулся после записи U-Boot", C_RED))
        return 1

    # На 192.168.1.1 теперь должен быть U-Boot, а не Breed
    if not wait_for_stage(args.breed_ip, is_uboot, "загрузчик U-Boot", timeout=240):
        print(c("[-] U-Boot не поднялся.", C_RED))
        print(c("    Если вместо U-Boot пришёл Breed - загрузчик не записался.", C_YELLOW))
        print(c("    Если страницы нет - U-Boot записан, но не запускается.", C_YELLOW))
        return 1

    print("[*] Заливаю прошивку Wive-NG в U-Boot...")
    res = upload_to_breed(args.breed_ip, wive, field="firmware")
    if res is False:
        print(c("[-] Прошивка Wive-NG не залита", C_RED))
        return 1
    print("[*] Прошивка идёт, жду загрузки Wive-NG (до 4 минут)...")
    if not wait_gone(args.breed_ip, timeout=120):
        print(c("  [!] роутер пока не пропал из сети", C_YELLOW))
    if not wait_for_stage(args.breed_ip,
                          lambda b: b"wive" in b.lower() or b"nginx" in b.lower(),
                          "систему Wive-NG", timeout=300):
        print(c("[-] Wive-NG не загрузился. Останов.", C_RED))
        return 1

    # ---------- 6. Приёмка ----------
    step_banner("ШАГ 6/6: ОЖИДАНИЕ WIVE-NG И ПРИЁМКА")
    print("[*] Роутер загружает Wive-NG, ждём 15 секунд...")
    for _ in range(15):
        if station._stop:
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
