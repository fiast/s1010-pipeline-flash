import os
import sys
import time
import subprocess
import re
import requests
import urllib3

# Отключаем ворнинги безопасности
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Адрес роутера. В VLAN-станции передаётся через окружение,
# но внутри каждого netns это всё равно 192.168.1.1.
ROUTER_IP = os.environ.get("S1010_ROUTER_IP") or "192.168.1.1"

AUTH_URL = f"http://{ROUTER_IP}/goform/auth"
SYSINFO_URL = f"http://{ROUTER_IP}/adm/sysinfo.js"

def wait_for_wive_ping():
    """Ожидание доступности роутера Wive-NG в сети через системный пинг"""
    print(f"[*] Ожидание загрузки новой прошивки Wive-NG (пингуем {ROUTER_IP})...")
    devnull = open(os.devnull, 'w')
    while True:
        res = subprocess.call(['ping', '-c', '1', '-W', '1', ROUTER_IP], stdout=devnull, stderr=devnull)
        if res == 0:
            print("[+] Роутер появился в сети на сетевом уровне!")
            # Даем встроенному веб-серверу Wive-NG 3 секунды, чтобы полностью инициализироваться
            time.sleep(3)
            return True
        time.sleep(1)

def check_router_status():
    session = requests.Session()
    # 1. Заголовки для авторизации (один в один из вашего дампа)
    auth_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "ru,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": f"http://{ROUTER_IP}",
        "Pragma": "no-cache",
        "Referer": f"http://{ROUTER_IP}",
        "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36"
    }

    # Данные формы авторизации Wive-NG (Admin / Admin)
    auth_data = {
        "username": "Admin",
        "password": "Admin",
        "submit-url": "/"
    }

    print("[*] Попытка авторизации в панели Wive-NG...")
    try:
        # requests автоматически сохранит полученный cookie 'sessionid' внутри объекта session
        response_auth = session.post(AUTH_URL, headers=auth_headers, data=auth_data, timeout=10, verify=False)
        # ИСПРАВЛЕНО: проверяем, что статус ответа успешный (200 OK или 302 Found)
        if response_auth.status_code not in [200,302]:
            print(f"[-] Ошибка авторизации. Сервер вернул код: {response_auth.status_code}")
            return False
        print("[+] Авторизация успешно пройдена. Сессия получена.")
    except requests.exceptions.RequestException as e:
        print(f"[-] Не удалось достучаться до формы авторизации: {e}")
        return False

    # 2. Заголовки для получения JS-файла состояния
    js_headers = {
        "Accept": "*/*",
        "Accept-Language": "ru,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Pragma": "no-cache",
        "Referer": f"http://{ROUTER_IP}/overview.asp",
        "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36"
    }

    print("[*] Запрашиваем системную информацию со страницы статуса...")
    try:
        response_js = session.get(SYSINFO_URL, headers=js_headers, timeout=10, verify=False)
        if response_js.status_code != 200:
            print(f"[-] Не удалось загрузить sysinfo.js. Код: {response_js.status_code}")
            return False
        js_content = response_js.text
        # 3. Парсим данные регулярными выражениями прямо из текста JS-файла
        sdk_version_match = re.search(r"statusSDKversion_value'\)\.innerHTML\s*=\s*'([^']+)'", js_content)
        lan_mac_match = re.search(r"statusLANMAC_value'\)\.innerHTML\s*=\s*'([^']+)'", js_content)
        sdk_version = sdk_version_match.group(1) if sdk_version_match else "Не найдено"
        lan_mac = lan_mac_match.group(1) if lan_mac_match else "Не найдено"
        # Если в версии SDK летит длинная строка '9.2.0.RU.31122024 / ...', забираем только первую часть
        if ' / ' in sdk_version:
            sdk_version = sdk_version.split(' / ')[0]

        # Выводим финальный красивый отчет на экран пользователя
        print("\n" + "="*50)
        print("🎉 [ПРИЕМКА РЕЗУЛЬТАТОВ: ВСЕ ОТЛИЧНО!]")
        print(f"🔹 Версия прошивки: {sdk_version}")
        print(f"🔹 Текущий LAN MAC:  {lan_mac.upper()}")
        print("="*50 + "\n")
        return True

    except requests.exceptions.RequestException as e:
        print(f"[-] Ошибка при чтении данных sysinfo.js: {e}")
        return False

def main():
    print("[*] ===============================================")
    print("[*]  ШАГ 3/3: ПРИЁМКА РОУТЕРА WIVE-NG")
    print("[*] ===============================================\n")
    wait_for_wive_ping()

    # Делаем 10 попыток опроса веб-интерфейса на случай, если веб-сервер
    # роутера поднялся чуть позже, чем сам пинг.
    for attempt in range(1, 11):
        if check_router_status():
            return 0
        print(f"[!] Попытка опроса {attempt} не удалась, ожидаем 3 секунды...")
        time.sleep(5)

    print("[-] Не удалось получить финальный статус роутера за несколько попыток.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
