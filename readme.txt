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
# СТЕНД ПРОШИВКИ: сток Ростелекома -> Breed -> Wive-NG
# ============================================================
# Полный цикл от роутера со стоковой прошивкой до принятого Wive-NG:
#   1. ждём стоковую прошивку на 192.168.0.1
#   2. входим (admin + пароль), читаем MAC роутера, заливаем Breed
#   3. ждём Breed на 192.168.1.1, пишем MAC, перезагружаем
#   4. шьём Wive-NG, затем U-Boot последним
#   5. приёмка Wive-NG
#
# Запуск:
python3 station.py --password 'пароль'

# Ключи:
python3 station.py --password 'п' --mac 749D7987B86E   # MAC вручную
python3 station.py --password 'п' --breed-file /путь/к/breed-s1010.img
python3 station.py --password 'п' --stock-ip 192.168.0.1 --breed-ip 192.168.1.1

# Нужен файл образа Breed (breed_s1010.img), положить в корень проекта
# или указать через --breed-file.

# ПАРОЛЬ: передаётся ключом --password, переменной S1010_STOCK_PASSWORD
# или запрашивается скрыто. В коде и репозитории паролей нет.

# ВАЖНО про порядок прошивки: сначала Wive-NG, затем U-Boot последним.
# После записи U-Boot загрузчик Breed исчезает, и прошивку уже некуда писать.
# Обратный порядок приводит к состоянию "U-Boot есть, прошивки нет".

# Особенности стокового интерфейса Ростелекома (выяснено при разведке,
# учтены в stock_stage.py):
#   * без заголовка Accept-Language сервер отвечает "(null) 400 Bad Request"
#   * пароль не передаётся открытым текстом, а хешируется HMAC-SHA256
#   * ключ шифрования выдаётся сервером и меняется при каждом входе
