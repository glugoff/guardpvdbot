#!/bin/bash
# Скрипт для вывода всех данных из таблицы messages

DB_PATH="/home/glugoff/guardpvdbot/guardpvdbot.sqlite"

sqlite3 -header -column "$DB_PATH" "SELECT * FROM messages;"
