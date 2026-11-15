# Smart Canteen server image. Works on any host that runs Docker (Fly.io, Railway, Render, a VPS...).
# The database is ONE file: mount a persistent volume at /data or every restart forgets all accounts and orders.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CANTEEN_DB=/data/canteen.db BEHIND_HTTPS_PROXY=1 PORT=8080
COPY requirements.txt requirements-prod.txt ./
RUN pip install --no-cache-dir -r requirements-prod.txt
COPY . .
# Runs as root on purpose: a freshly mounted volume (Fly, Render disks) is owned by root, and a non-root user could not write the database to it.
RUN mkdir -p /data
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/healthz',timeout=4)"
CMD ["sh", "-c", "exec gunicorn -w 1 --threads 8 -b 0.0.0.0:${PORT} --access-logfile - wsgi:app"]
