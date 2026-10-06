FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && useradd --system --uid 10001 --no-create-home signalgrid

COPY signalgrid_forecast ./signalgrid_forecast
USER 10001
CMD ["python", "-m", "signalgrid_forecast"]
