# Playwright's official image ships Chromium/Firefox/WebKit + all system
# deps already installed for THIS exact playwright version — that's why
# requirements.txt below pins the same version instead of leaving it loose.
# Check playwright.dev/python/docs/docker for the current tag before
# building; v1.50.0 is what this file was tested against.
FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sih_telegram_bot.py .

# Render sets $PORT itself; this is just documentation for local `docker run`.
EXPOSE 10000

CMD ["python", "sih_telegram_bot.py"]
