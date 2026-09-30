#!/bin/sh
set -e

echo "等待 PostgreSQL..."
python - <<'PY'
import os, time
import psycopg2

host = os.environ.get("POSTGRES_HOST", "db")
port = int(os.environ.get("POSTGRES_PORT", "5432"))
name = os.environ.get("POSTGRES_DB", "indigovat")
user = os.environ.get("POSTGRES_USER", "indigovat")
password = os.environ.get("POSTGRES_PASSWORD", "indigovat")

for i in range(60):
    try:
        conn = psycopg2.connect(
            host=host, port=port, dbname=name, user=user, password=password
        )
        conn.close()
        print("数据库已就绪")
        break
    except Exception as exc:
        print(f"等待中 ({i+1}/60): {exc}")
        time.sleep(1)
else:
    raise SystemExit("数据库连接超时")
PY

exec uvicorn app.main:app --host 0.0.0.0 --port 4720
