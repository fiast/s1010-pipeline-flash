# вбиваем в файл yml роутеры
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install requests pyyaml
#python change_mac_breed.py
./run_pipeline.sh

# если нужно проверить прошившку python check_wive.ng

# описание уязвимости провайдерской прошивки (предсказуемый пароль по SN)
# описание: VULNERABILITY.md
# генератор паролей по серийному номеру:
python3 rt_password.py SERCOMM 48575443B5210BA6
python3 rt_password.py --all routers.yml
python3 rt_password.py --check routers.yml

# ============================================================
# СТЕНД ПРОШИВКИ: ждёт роутер, спрашивает MAC, гонит пайплайн
# ============================================================
# Основной запуск (роутер уже в Breed на 192.168.1.1):
./run_pipeline.sh
# или то же самое напрямую:
python3 station.py

# Ключи:
python3 station.py --mac 142E5E7B8C72   # MAC сразу, без ввода с клавиатуры
python3 station.py --no-wait            # не ждать пинг, сразу спросить MAC
python3 station.py --skip-reboot-wait   # не ждать ребута после правки MAC

# Как проходит работа:
#   1. ждём появления 192.168.1.1 (роутер в Breed)
#   2. просим MAC с корпуса, сами считаем +1 и +2
#   3. пишем MAC в Breed
#   4. перезагружаем роутер командой в веб-интерфейс Breed
#      (если Breed не умеет - скрипт попросит нажать питание)
#   5. шьём U-Boot, затем из него Wive-NG
#   6. приёмка: логин в Wive-NG и сверка MAC
