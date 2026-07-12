#!/bin/sh
set -e
cd /app
gunicorn --bind 127.0.0.1:8090 --workers 2 app:app &
nginx -g 'daemon off;'
