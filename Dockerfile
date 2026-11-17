FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip==26.3 \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home opspilot && mkdir data && chown opspilot:opspilot data
COPY opspilot/ opspilot/
COPY LICENSE THIRD_PARTY_NOTICES.md ./
USER opspilot
EXPOSE 8000
HEALTHCHECK CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/api/health', timeout=3)"]
CMD ["python", "-m", "uvicorn", "opspilot.app:from_env", "--factory", "--host", "0.0.0.0"]
