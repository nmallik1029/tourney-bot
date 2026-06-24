# Playwright's image ships Chromium + all the system libraries it needs, so the bot can
# drive a headless browser (used to fetch Krunker clan tags). Pin the tag to the same
# Playwright version as requirements.txt so the preinstalled browsers match.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
