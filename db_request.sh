#!/bin/bash
# Скрипт для вывода всех данных из таблицы user_request

DB_PATH="/home/glugoff/guardpvdbot/guardpvdbot.sqlite"

sqlite3 -header -column "$DB_PATH" "SELECT * FROM users_requests;"
