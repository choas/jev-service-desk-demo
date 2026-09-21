FROM python:3.13-alpine

# Standard library only, so there is nothing to install.
WORKDIR /app
COPY jev_service_bot.py server.py shop_data.json ./
COPY static ./static

RUN adduser -D -u 10001 app
USER app

ENV PORT=8000 PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD wget -qO- http://127.0.0.1:8000/healthz || exit 1

CMD ["python", "server.py"]
