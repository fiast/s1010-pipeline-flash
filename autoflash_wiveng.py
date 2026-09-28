import os
import sys
import time
import subprocess
import requests
import urllib3

# ОТКЛЮЧАЕМ SSL ВОРНИНГИ ДЛЯ СТАБИЛЬНОСТИ ВЫВОДА
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# НАСТРОЙКА ИМЕН ФАЙЛОВ (ДОЛЖНЫ ЛЕЖАТЬ РЯДОМ СО СКРИПТОМ)
UBOOT_FILE = "uboot-s1010-wive.bin"
FIRMWARE_FILE = "wive-ng-s1010.bin"  # <--- ПОМЕНЯЙТЕ НА ВАШЕ ИМЯ ФАЙЛА ПРОШИВКИ

# Адрес роутера. В VLAN-станции передаётся через окружение,
# но внутри каждого netns это всё равно 192.168.1.1.
ROUTER_IP = os.environ.get("S1010_ROUTER_IP") or "192.168.1.1"

# URL-АДРЕСА ЗАГРУЗЧИКА
UPLOAD_URL = f"http://{ROUTER_IP}/upload.html"
START_URL = f"http://{ROUTER_IP}/upgrading.html"
FIRMWARE_URL = f"http://{ROUTER_IP}/"

def check_files():
    """Проверяет физическое наличие файлов прошивок"""
    errors = False
    for filename in [UBOOT_FILE, FIRMWARE_FILE]:
        if not os.path.exists(filename):
            print(f"[-] Ошибка: Файл '{filename}' не найден в текущей директории!")
            errors = True
    if errors:
        print("[-] Пожалуйста, скопируйте недостающие файлы к скрипту.")
        sys.exit(1)
    print(f"[+] Все необходимые файлы найдены.")

def wait_for_ping(stage_name):
    """Ожидание доступности IP адреса роутера через системный пинг"""
    print(f"[*] [{stage_name}] Ожидание появления роутера в сети (пингуем {ROUTER_IP})...")
    devnull = open(os.devnull, 'w')
    while True:
        res = subprocess.call(['ping', '-c', '1', '-W', '1', ROUTER_IP], stdout=devnull, stderr=devnull)
        if res == 0:
            print(f"[+] [{stage_name}] Роутер ответил на ping!")
            time.sleep(2.0)  # Даем 2 секунды веб-серверу окончательно подняться
            return True
        time.sleep(0.5)

def wait_for_disconnect():
    """Ожидание, пока роутер уйдет в ребут (пропадет из сети)"""
    # 1. Даем процессору роутера спокойно завершить прошивку uboot в чип памяти
    print("[*] Железка шьет uboot в память чипа. Ожидаем завершения операции (25 сек)...")
    time.sleep(25)
    
    # 2. Начинаем ловить момент физического ребута устройства
    print("[*] Ожидание перезагрузки роутера (пропадание пинга)...")
    devnull = open(os.devnull, 'w')
    for _ in range(40):  # Проверяем в течение 20 секунд (40 итераций по 0.5 сек)
        res = subprocess.call(['ping', '-c', '1', '-W', '1', ROUTER_IP], stdout=devnull, stderr=devnull)
        if res != 0:
            print("[+] Роутер успешно ушел в перезагрузку.")
            return True
        time.sleep(0.5)
        
    print("[!] Предупреждение: Роутер не пропал из сети по таймауту, продолжаем конвейер.")
    return True


def step_1_flash_uboot():
    """Этап 1: Загрузка uboot и отправка команды на запись"""
    print("\n=== ЭТАП 1: ЗАЛИВКА И СТАРТ U-BOOT ===")
    session = requests.Session()
    
    upload_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Origin": f"http://{ROUTER_IP}",
        "Referer": "http://192.168.1",
        "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5)"
    }

    upload_data = {
        "boot_check": "1",
        "flash_layout": "reference",
        "fw_type": "generic",
        "autoreboot": "1",
        "submit": "Upload"
    }

    try:
        with open(UBOOT_FILE, "rb") as f:
            upload_files = {
                "boot_file": (UBOOT_FILE, f, "application/octet-stream"),
                "fw_file": ("", b"", "application/octet-stream"),
                "eeprom_file": ("", b"", "application/octet-stream")
            }
            print(f"[*] Отправка файла '{UBOOT_FILE}'...")
            resp = session.post(UPLOAD_URL, headers=upload_headers, data=upload_data, files=upload_files, timeout=30)
            if resp.status_code != 200:
                print(f"[-] Ошибка загрузки U-Boot. Код: {resp.status_code}")
                return False
            print("[+] Файл U-Boot успешно загружен в память.")

        time.sleep(0.5)

        # Подтверждение старта
        print("[*] Отправка команды подтверждения прошивки...")
        start_headers = {
            "Accept": "*/*",
            "Content-type": "application/x-www-form-urlencoded",
            "Origin": f"http://{ROUTER_IP}",
            "Referer": "http://192.168.1",
            "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5)"
        }
        resp_start = session.post(START_URL, headers=start_headers, data={}, timeout=10)
        if resp_start.status_code == 200:
            print("[+] Команда на запись U-Boot успешно принята.")
            return True
        print(f"[-] Ошибка подтверждения. Код: {resp_start.status_code}")
        return False
    except Exception as e:
        print(f"[-] Исключение на Этапе 1: {e}")
        return False

def step_2_flash_firmware():
    """Этап 2: Отправка финальной прошивки в корень загрузчика"""
    print("\n=== ЭТАП 2: ЗАЛИВКА ФИНАЛЬНОЙ ПРОШИВКИ ===")
    
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Origin": f"http://{ROUTER_IP}",
        "Referer": f"http://{ROUTER_IP}/",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N)"
    }

    try:
        with open(FIRMWARE_FILE, "rb") as f:
            # Имя инпута строго по вашему дампу: "firmware"
            files = {
                "firmware": (FIRMWARE_FILE, f, "application/octet-stream")
            }
            print(f"[*] Отправка бинарника прошивки '{FIRMWARE_FILE}' в корень загрузчика...")
            
            # Отправляем multipart форму
            response = requests.post(FIRMWARE_URL, headers=headers, files=files, timeout=60)
            
            if response.status_code == 200:
                print("\n[==================================================]")
                print("[+] ФИНАЛЬНАЯ ПРОШИВКА УСПЕШНО ЗАЛИТА НА РОУТЕР!")
                print("[!] Устройство применит прошивку Wive-NG и перезагрузится.")
                print("[!] Стенд готов к подключению следующего роутера.")
                print("[==================================================]\n")
                return True
            else:
                print(f"[-] Ошибка: Загрузчик вернул код ответа {response.status_code}")
                return False
    except Exception as e:
        print(f"[-] Исключение на Этапе 2: {e}")
        return False

def main():
    print("[*] --- АВТОМАТИЧЕСКИЙ КОНВЕЙЕР ПОЛНОЙ ПРОШИВКИ SERCOMM S1010 ---")
    print("=== ЭТАП 2/3: ЗАЛИВКА U-BOOT + WIVE-NG ===\n")
    check_files()

    # 1. Ждем устройство на старте (оно должно быть в режиме Breed)
    wait_for_ping("Инициализация")

    # 2. Шьем U-Boot
    if not step_1_flash_uboot():
        print("[-] Процесс прерван из-за ошибки на Этапе 1.")
        return 1

    # 3. Ждем пока железка перезагрузится после U-Boot
    wait_for_disconnect()

    # 4. Ждем когда она снова поднимется в сети в режиме обновления
    wait_for_ping("Ожидание после U-Boot")

    # 5. Шьем саму финальную прошивку
    if not step_2_flash_firmware():
        print("[-] Процесс прерван из-за ошибки на Этапе 2.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
