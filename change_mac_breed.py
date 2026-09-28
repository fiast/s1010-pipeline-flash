import os
import re
import sys
import time
import subprocess
import yaml

YAML_FILE = "routers.yml"

# Базовый MAC можно ввести вручную (с корпуса) — тогда YAML не нужен
MODE_MANUAL = "manual"
MODE_BASE = "base"

# Неинтерактивный режим для автоматизации (см. vlan_station.py).
#   S1010_BASE_MAC  - базовый MAC с корпуса, задаётся заранее
#   S1010_ASSUME_YES - "1", чтобы не спрашивать подтверждение записи
#   S1010_ROUTER_IP - адрес роутера (в netns это всё равно 192.168.1.1)
ENV_BASE_MAC = "S1010_BASE_MAC"
ENV_ASSUME_YES = "S1010_ASSUME_YES"
ENV_ROUTER_IP = "S1010_ROUTER_IP"
ROUTER_IP = os.environ.get(ENV_ROUTER_IP) or "192.168.1.1"

def normalize_mac(raw):
    """Принимает 12 hex-символов с разделителями или без -> 12 заглавных hex."""
    clean = re.sub(r"[\s:\-.]", "", str(raw or "")).upper()
    if not clean:
        return None
    if not re.fullmatch(r"[0-9A-F]{12}", clean):
        return None
    return clean

def is_multicast_or_invalid(mac):
    """Отсекаем нули, broadcast и multicast-невалидные адреса."""
    first = int(mac[0:2], 16)
    return mac == "000000000000" or mac == "FFFFFFFFFFFF" or (first & 1) == 1

def prompt_base_mac():
    """Ручной ввод базового MAC с корпуса."""
    print("--- РУЧНОЙ ВВОД MAC С КОРПУСА ---")
    print("Введите LAN MAC с наклейки (12 hex-символов, например 142E5E7B8C72).")
    print("Двоеточия/пробелы/дефисы можно оставить - они отбросятся.")
    for _ in range(5):
        raw = input("MAC: ").strip()
        if raw.lower() in ("q", "quit", "exit"):
            return None
        mac = normalize_mac(raw)
        if mac is None:
            print("[-] Некорректный формат. Нужно 12 hex-символов (0-9, A-F).")
            continue
        if is_multicast_or_invalid(mac):
            print("[-] Такой MAC не подходит (нули, broadcast или multicast).")
            continue
        return mac
    print("[-] Превышено число попыток ввода.")
    return None

def prompt_selection(routers):
    """Выбор роутера из базы (использует готовый lan_mac из YAML)."""
    while True:
        try:
            user_input = input(f"Введите номер роутера от 1 до {len(routers)} (или 'q' для выхода): ")
        except EOFError:
            return None
        if user_input.strip().lower() in ('q', 'quit', 'exit'):
            return None
        try:
            choice = int(user_input)
        except ValueError:
            print("[-] Введите корректное число.")
            continue
        if 1 <= choice <= len(routers):
            return routers[choice - 1]
        print(f"[-] Число должно быть от 1 до {len(routers)}.")

def load_routers_data(file_path):
    if not os.path.exists(file_path):
        print(f"[-] Файл {file_path} не найден в текущей директории!")
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("routers", [])

def calculate_mac_octets(base_mac_str, offset=0):
    clean_mac = base_mac_str.replace(":", "").strip()
    mac_int = int(clean_mac, 16)
    target_int = mac_int + offset
    target_hex = f"{target_int:012x}"
    # Окнеты СТРОГО заглавными буквами, как в вашем рабочем curl
    return [target_hex[i:i+2].upper() for i in range(0, 12, 2)]

def wait_for_breed_via_ping():
    print(f"[*] Ожидание появления Breed (пингуем {ROUTER_IP})...")
    devnull = open(os.devnull, 'w')
    while True:
        res = subprocess.call(['ping', '-c', '1', '-W', '1', ROUTER_IP], stdout=devnull, stderr=devnull)
        if res == 0:
            print("[+] Роутер ответил на ping! Breed в сети.")
            time.sleep(1)
            return True
        time.sleep(0.5)

def send_mac_via_system_curl(mac_data):
    print("[*] Формируем пакет данных для отправки через системный curl...")
    
    data_parts = []
    
    # 1. Вычисленные MAC-адреса (в строгом порядке вашего рабочего curl)
    for i in range(6):
        data_parts.append(f"wlan_mac1_mac{i}={mac_data['wlan_mac1'][i]}")
    for i in range(6):
        data_parts.append(f"mac1_1_mac{i}={mac_data['mac1_1'][i]}")
    for i in range(6):
        data_parts.append(f"mac1_2_mac{i}=FF")
    for i in range(6):
        data_parts.append(f"wlan_mac2_mac{i}={mac_data['wlan_mac2'][i]}")
    for i in range(6):
        data_parts.append(f"mac2_1_mac{i}=00")
    for i in range(6):
        data_parts.append(f"mac2_2_mac{i}=00")
    for i in range(6):
        data_parts.append(f"lan_mac_mac{i}=00")
    for i in range(6):
        data_parts.append(f"wan_mac_mac{i}=00")
        
    # СТРОГО Modify
    data_parts.append("submit=Modify")
    raw_data_string = "&".join(data_parts)

    curl_cmd = [
        "curl",
        "--output", "-",
        "-i",
        f"http://{ROUTER_IP}/mac.html",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "-H", "Accept-Language: ru,en-US;q=0.9,en;q=0.8,bg;q=0.7,zh-CN;q=0.6,zh;q=0.5",
        "-H", "Cache-Control: no-cache",
        "-H", "Connection: keep-alive",
        "-H", "Content-Type: application/x-www-form-urlencoded",
        "-H", f"Origin: http://{ROUTER_IP}",
        "-H", "Pragma: no-cache",
        "-H", f"Referer: http://{ROUTER_IP}/mac.html",
        "-H", "Upgrade-Insecure-Requests: 1",
        "-H", "User-Agent: Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36",
        "--data-raw", raw_data_string,
        "--insecure"
    ]

    try:
        result = subprocess.run(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        output_text = result.stdout.decode('utf-8', errors='ignore')
        
        if "successfully" in output_text or "200 OK" in output_text:
            print("\n[==================================================]")
            print("[+] МАК-адреса успешно применены через curl!")
            print("[!] Теперь перезагрузите роутер для проверки.")
            print("[==================================================]\n")
            return True
        else:
            print("[-] Ошибка: Breed отклонил пакет.")
            return False
            
    except Exception as e:
        print(f"[-] Ошибка вызова curl: {e}")
        return False

def build_mac_data(base_mac):
    """Расчитывает адреса, которые пишем в Breed: +0 LAN/RF1, +1 WLAN 2.4, +2 WLAN 5."""
    return {
        "mac1_1": calculate_mac_octets(base_mac, offset=0),
        "wlan_mac1": calculate_mac_octets(base_mac, offset=1),
        "wlan_mac2": calculate_mac_octets(base_mac, offset=2),
    }


def show_plan_and_confirm(base_mac, mac_data):
    """Печатает, что будет записано, и спрашивает подтверждение.

    При S1010_ASSUME_YES=1 подтверждение запрашивается не у пользователя,
    а считается автоматически (режим автопрошивки).
    """
    print("\n--- БУДУТ ЗАПИСАНЫ В BREED ---")
    print(f"    Базовый MAC с корпуса:   {base_mac}")
    print(f"    RF1 MAC1 (оригинал, +0): {':'.join(mac_data['mac1_1'])}")
    print(f"    RF1 WLAN      (MAC + 1): {':'.join(mac_data['wlan_mac1'])}")
    print(f"    RF2 WLAN      (MAC + 2): {':'.join(mac_data['wlan_mac2'])}")
    print("    RF1 MAC2, RF2 MAC1/MAC2, LAN, WAN: обнуляются (00/FF)")

    if os.environ.get(ENV_ASSUME_YES) == "1":
        print("\n[*] Авторежим: подтверждение пропущено (S1010_ASSUME_YES=1).")
        return True

    confirm = input("\nПодтверждаете запись этих адресов? (y/n): ").strip().lower()
    if confirm not in ("y", "yes", "д", "да"):
        print("[-] Запись отменена оператором.")
        return False
    return True


def main():
    routers = load_routers_data(YAML_FILE) or []

    print("==============================================")
    print(" ШАГ 1/3: ПРАВКА MAC-АДРЕСОВ В BREED")
    print("==============================================\n")

    # Неинтерактивный режим: MAC уже задан извне (автоматизация VLAN-станции)
    env_mac = normalize_mac(os.environ.get(ENV_BASE_MAC, ""))
    if env_mac:
        if is_multicast_or_invalid(env_mac):
            print(f"[-] {ENV_BASE_MAC} содержит недопустимый MAC: {env_mac}")
            return 1
        mac_data = build_mac_data(env_mac)
        if not show_plan_and_confirm(env_mac, mac_data):
            return 1
        wait_for_breed_via_ping()
        if not send_mac_via_system_curl(mac_data):
            return 1
        print("[*] MAC записаны. Даю 5 секунд на применение настроек Breed...")
        time.sleep(5)
        print("[*] Роутер перезагрузите кнопкой питания перед прошивкой U-Boot.")
        return 0

    # Ручной (интерактивный) режим
    print("Как задать базовый MAC?")
    print("  1) Ввести вручную с корпуса роутера (рекомендуется для стенда)")
    print("  2) Выбрать роутер из базы routers.yml")

    mode = None
    while mode not in (MODE_MANUAL, MODE_BASE):
        try:
            raw = input("Выберите режим (1/2): ").strip().lower()
        except EOFError:
            print("\n[-] Нет интерактивного ввода (EOF). Завершаемся.")
            return 1
        if raw in ("1", MODE_MANUAL):
            mode = MODE_MANUAL
        elif raw in ("2", MODE_BASE):
            if not routers:
                print(f"[-] Файл {YAML_FILE} пуст или отсутствует, режим 2 недоступен.")
                continue
            mode = MODE_BASE
        elif raw in ("q", "quit", "exit"):
            print("[-] Выход.")
            return 1
        else:
            print("[-] Введите 1 или 2.")

    if mode == MODE_MANUAL:
        base_mac = prompt_base_mac()
        if base_mac is None:
            print("[-] MAC не задан. Конвейер остановлен.")
            return 1
        print(f"\n[*] Базовый MAC с корпуса: {base_mac}")
    else:
        print(f"\n[+] База загружена. Устройств: {len(routers)}")
        print("--- СПИСОК ДОСТУПНЫХ РОУТЕРОВ ---")
        for index, router in enumerate(routers):
            print(f"[{index + 1}] SN: {router['sn']} | LAN MAC: {router['lan_mac']}")
        print("------------------------------------\n")
        target_router = prompt_selection(routers)
        if target_router is None:
            print("[-] Роутер не выбран. Конвейер остановлен.")
            return 1
        base_mac = normalize_mac(target_router["lan_mac"])
        if base_mac is None:
            print("[-] В базе некорректный lan_mac. Конвейер остановлен.")
            return 1
        print(f"\n[*] Выбран роутер: SN {target_router['sn']} | Базовый MAC: {base_mac}")

    mac_data = build_mac_data(base_mac)
    if not show_plan_and_confirm(base_mac, mac_data):
        return 1

    wait_for_breed_via_ping()

    if not send_mac_via_system_curl(mac_data):
        return 1

    print("[*] MAC записаны. Даю 5 секунд на применение настроек Breed...")
    time.sleep(5)
    print("[*] Роутер перезагрузите кнопкой питания перед прошивкой U-Boot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
