# Jev Service Desk Demo

A customer service bot that never generates text. Jev (TypeSafe AI, via OpenRouter)
answers fixed questions with probabilities, plain Python picks a prepared reply, asks back or hands over to a human.

- Live: https://jev.larsgregori.de
- LinkedIn article: https://www.linkedin.com/feed/update/urn:li:activity:7508121837277638656/

## Run

```bash
cp .env.example .env        # add your OpenRouter API key
docker compose up --build   # http://localhost:8000
```

Without Docker: `python server.py` (or `python server.py --mock` without an API key).
Command line only: `python jev_service_bot.py --debug`.

## Files

- `jev_service_bot.py`: the bot, questions, thresholds and prepared replies
- `server.py`: web server, sessions, rate limits, replies from the example data
- `shop_data.json`: fictional shop with customers, orders and invoices
- `static/index.html`: chat UI

Standard library only.
