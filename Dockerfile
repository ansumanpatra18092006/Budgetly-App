FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN python - <<'PY'
from pathlib import Path
p=Path('requirements.txt'); b=p.read_bytes()
try: t=b.decode('utf-8-sig')
except UnicodeDecodeError: t=b.decode('utf-16')
Path('/tmp/requirements.txt').write_text(t)
PY
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY . .
EXPOSE 5000
CMD ["gunicorn","-w","2","-b","0.0.0.0:5000","--timeout","120","app:app"]
