FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && useradd --system --uid 10001 --no-create-home signalgrid

COPY signalgrid_frontend ./signalgrid_frontend
USER 10001
EXPOSE 8004
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8004/health', timeout=2)"
# One worker: dashboard state is in-process (see signalgrid_frontend/wsgi.py).
CMD ["gunicorn", "--bind", "0.0.0.0:8004", "--workers", "1", "--threads", "8", \
     "--access-logfile", "-", "signalgrid_frontend.wsgi:server"]
