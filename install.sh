#!/bin/bash
set -e

# ========= НАСТРОЙКИ ========= #
REPO_URL="https://github.com/glugoff/guardpvdbot.git"
APP_DIR="/home/glugoff/guardpvdbot"
SERVICE_NAME="guardpvdbot"
PYTHON_BIN="/usr/bin/python3"
BOT_TOKEN="PASTE_YOUR_TOKEN_HERE"  # ← потом можно удалить
# ============================= #

echo "=== Обновление системы ==="
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip

echo "=== Удаление старой версии ==="
sudo systemctl stop $SERVICE_NAME 2>/dev/null || true
sudo systemctl disable $SERVICE_NAME 2>/dev/null || true
sudo rm -f /etc/systemd/system/$SERVICE_NAME.service
sudo rm -rf $APP_DIR

echo "=== Клонирование репозитория ==="
git clone "$REPO_URL" "$APP_DIR"

echo "=== Создание виртуального окружения ==="
python3 -m venv "$APP_DIR/venv"
source "$APP_DIR/venv/bin/activate"

echo "=== Установка зависимостей ==="
if [[ -f "$APP_DIR/requirements.txt" ]]; then
    pip install -r "$APP_DIR/requirements.txt"
else
    pip install aiogram aiosqlite
fi

echo "=== Создание systemd сервиса ==="

sudo bash -c "cat > /etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Telegram Guard Bot
After=network.target

[Service]
WorkingDirectory=$APP_DIR
Environment=BOT_TOKEN=$BOT_TOKEN
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/guardpvdbot.py
Restart=always
User=glugoff

[Install]
WantedBy=multi-user.target
EOF

echo "=== Перезагрузка systemd ==="
sudo systemctl daemon-reload

echo "=== Включение автозапуска ==="
sudo systemctl enable $SERVICE_NAME

echo "=== Запуск бота ==="
sudo systemctl start $SERVICE_NAME

echo "=== Готово ==="
echo "Статус:"
sudo systemctl status $SERVICE_NAME --no-pager
