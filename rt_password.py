#!/usr/bin/env python3
"""
Генератор паролей роутеров Ростелеком / МГТС / Huawei / Iskratel и т.п.
по серийному номеру корпуса.

Алгоритм (CWE-330 / CWE-521, предсказуемая генерация секрета):

    password = base64( SHA256( Brand + NormalizedSN )[0:12] )

Нормализация SN:
    * убрать пробелы/табуляции/переводы строк, дефисы и двоеточия
    * верхний регистр
    * префикс HWTC  -> 48575443
    * 8 hex-символов -> спереди 48575443
    * результат должен матчить ^[0-9A-F]{16}$

Примеры:
    python3 rt_password.py 48575443B5210BA6
    python3 rt_password.py SERCOMM ST1906001965
    python3 rt_password.py --all routers.yml
    python3 rt_password.py --check routers.yml
"""

import base64
import hashlib
import re
import sys

import yaml

# Теги брендов, которые поддерживает публичный генератор паролей
# (https://www.tav.perm.ru/passgen) и провайдерские прошивки.
BRANDS = [
    "SERCOMM",
    "Huawei",
    "Iskratel",
    "MGTS",
    "Transservice",
    "Rotek",
    "Electra",
]

# Бренд по умолчанию для проекта Sercomm S1010
DEFAULT_BRAND = "SERCOMM"

HEX8 = re.compile(r"^[0-9A-F]{8}$")
SN16 = re.compile(r"^[0-9A-F]{16}$")


def normalize_sn(value: str) -> str:
    """Приводит серийный номер к 16 шестнадцатеричным символам."""
    sn = re.sub(r"[\s\t\r\n:-]", "", str(value or "")).upper()
    if sn.startswith("HWTC"):
        sn = "48575443" + sn[4:]
    if HEX8.match(sn):
        sn = "48575443" + sn
    return sn


def is_valid_sn(sn: str) -> bool:
    return bool(SN16.match(sn))


def generate_password(brand: str, sn: str) -> str:
    """Возвращает 16-символьный base64-пароль для бренда и серийника."""
    digest = hashlib.sha256((brand + sn).encode("utf-8")).digest()
    return base64.b64encode(digest[:12]).decode("ascii")


def generate_all_brands(sn: str) -> dict:
    return {brand: generate_password(brand, sn) for brand in BRANDS}


def load_routers(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("routers", [])


def cmd_single(brand: str, raw_sn: str) -> int:
    sn = normalize_sn(raw_sn)
    if not is_valid_sn(sn):
        print(f"[-] Некорректный SN: {raw_sn!r} -> {sn!r}")
        print("    Ожидается 16 hex-символов (например 48575443B5210BA6).")
        return 1
    print(f"[+] SN (нормализованный): {sn}")
    print(f"{'':->60}")
    print(f"{'Brand':<16}{'Пароль'}")
    for name, pwd in generate_all_brands(sn).items():
        print(f"{name:<16}{pwd}")
    print(f"\n[*] Для {brand}: {generate_password(brand, sn)}")
    return 0


def cmd_all(path: str, brand: str) -> int:
    routers = load_routers(path)
    if not routers:
        print(f"[-] В {path} нет ни одного роутера.")
        return 1
    print(f"[*] Обработано роутеров: {len(routers)}")
    print(f"{'':->78}")
    print(f"{'SN':<22}{'Brand':<14}{'Пароль'}")
    for r in routers:
        sn = normalize_sn(r.get("sn", ""))
        if not is_valid_sn(sn):
            print(f"{r.get('sn', '?'):<22}{'-':<14}пропущен (некорректный SN)")
            continue
        print(f"{sn:<22}{brand:<14}{generate_password(brand, sn)}")
    return 0


def cmd_check(path: str, brand: str) -> int:
    routers = load_routers(path)
    ok = bad = skipped = 0
    for r in routers:
        sn = normalize_sn(r.get("sn", ""))
        expected = (r.get("web_interface") or {}).get("password", "")
        if not is_valid_sn(sn):
            print(f"[-] {r.get('sn', '?'):<22} SN не нормализуется, пропуск")
            skipped += 1
            continue
        computed = generate_password(brand, sn)
        mark = "OK  " if computed == expected else "DIFF"
        if computed == expected:
            ok += 1
        else:
            bad += 1
        print(f"[{mark}] {sn:<22} в yaml: {expected:<18} вычислено: {computed}")
    print(f"\n[*] Совпало: {ok}, расходится: {bad}, пропущено: {skipped}")
    if bad:
        print("[!] Расхождения могут означать другую схему паролей (например,")
        print("    старую 8-символьную выгрузку с наклейки) или другой бренд.")
        print(f"    Проверить другой бренд: python3 rt_password.py --check {path} --brand <BRAND>")
    if skipped:
        print("[!] Часть SN не в формате 16 hex-символов — для них схема не применима,")
        print("    пароль снимается с наклейки и алгоритмом не восстанавливается.")
    return 0


def main(argv):
    args = list(argv[1:])
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    path = None
    brand = DEFAULT_BRAND
    mode = "single"
    rest = []

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--all":
            mode, i = "all", i + 1
            path = args[i] if i < len(args) else "routers.yml"
            i += 1
        elif a == "--check":
            mode, i = "check", i + 1
            path = args[i] if i < len(args) else "routers.yml"
            i += 1
        elif a == "--brand":
            i += 1
            brand = args[i] if i < len(args) else DEFAULT_BRAND
            i += 1
        else:
            rest.append(a)
            i += 1

    if mode == "single":
        if not rest:
            print("[-] Укажите серийный номер.")
            return 1
        brand_arg = rest[0]
        if brand_arg in BRANDS or brand_arg.isalpha():
            brand = brand_arg
            rest = rest[1:]
        if not rest:
            print("[-] Укажите серийный номер.")
            return 1
        return cmd_single(brand, rest[0])

    if mode == "all":
        return cmd_all(path, brand)
    return cmd_check(path, brand)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
