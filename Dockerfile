# Playwright's image ships Chromium + all the system libraries it needs, so the bot can
# drive a headless browser (used to fetch Krunker clan tags). Pin the tag to the same
# Playwright version as requirements.txt so the preinstalled browsers match.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# Unbuffered stdout/stderr so print() logs show up in Railway in real time (otherwise
# Python block-buffers in a container and the deploy logs look empty).
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Belt-and-suspenders: make sure the Chromium build matching our playwright is installed.
RUN python -m playwright install chromium

COPY . .

CMD ["python", "-u", "bot.py"]
