"""Один UDP-пакет модулю EyBond: «подключись к мониторингу на этом ПК».

Запускать из домашней сети. Адрес облака SmartESS в модуле не меняется.
Не слать, пока модуль уже подключён: каждый пакет рвёт текущее соединение.

  python redirect.py            - отправить пакет
"""
import json
import os
import socket

CFG = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"), encoding="utf-8"))
MODULE = (CFG["module_ip"], 58899)
COMMAND = f"set>server={CFG['redirect_to']};".encode()

with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
    u.settimeout(3)
    u.sendto(COMMAND, MODULE)
    print("отправлено:", COMMAND.decode(), "->", MODULE[0])
    try:
        print("ответ модуля:", u.recvfrom(1024)[0])
    except socket.timeout:
        print("ответа нет (так бывает, смотреть monitor.log)")
