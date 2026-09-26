# Public Mint Link Bot

A production-ready Telegram automation bot that continuously monitors the OpenSea API
for qualifying public NFT mints across 15 blockchain networks and delivers scheduled,
auto-deleting alerts to a configured Telegram group topic.

---

## Features

- **Monitors 15 chains** via OpenSea v2 API with cursor-based pagination
- **Filters** by public mint type, price ($0–$20), offer ratio, and minted supply (≥70%)
- **Scheduled alerts** sent exactly 10 minutes before mint start
- **Auto-deletion** exactly 15 minutes after mint start
- **Persistent scheduler** (APScheduler + SQLite) — survives server restarts
- **Restart recovery** — reschedules missed jobs on startup
- **Duplicate prevention** via database unique constraints
- **Exponential-backoff retry** for API errors (429, 5xx, timeouts)
- **Telegram forum topic** targeting via `message_thread_id`
- **Admin commands**: `/status`, `/monitor on|off`, `/test`
- **Graceful shutdown** (SIGTERM/SIGINT)
- **Docker + systemd** deployment

---

## Supported Chains

| Chain | API Identifier | Supported |
|-------|---------------|-----------|
| Base | `base` | ✅ |
| Ethereum | `ethereum` | ✅ |
| Polygon | `polygon` | ✅ |
| Arbitrum | `arbitrum` | ✅ |
| Optimism | `optimism` | ✅ |
| Zora | `zora` | ✅ |
| Robinhood | `robinhood` | ✅ |
| Avalanche | `avalanche` | ✅ |
| BSC | *(none)* | ⚠️ Not in OpenSea API |
| Shape | `shape` | ✅ |
| Abstract | `abstract` | ✅ |
| ApeChain | `ape_chain` | ✅ |
| Arc | `arc` | ✅ |
| HyperEVM | `hyperevm` | ✅ |
| Ink | `ink` | ✅ |

> **Note:** BSC is not supported by OpenSea. It is monitored as an enabled chain but skipped during API polling with a `[CHAIN_UNSUPPORTED]` log entry.

---

## Quick Start

### Prerequisites

- Python 3.11+
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- A Telegram group with a forum topic enabled
- An OpenSea API key

### 1. Clone and configure

```bash
git clone <repo-url>
cd public-mint-bot

cp .env.example .env
# Edit .env with your actual values
nano .env
```

### 2. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Run

```bash
python -m app.main
```

---

## Docker Deployment

### Build and start

```bash
docker-compose up -d --build
```

### View logs

```bash
docker-compose logs -f public-mint-bot
```

### Stop

```bash
docker-compose down
```

### Restart

```bash
docker-compose restart public-mint-bot
```

### Update

```bash
git pull
docker-compose up -d --build
```

---

## AWS Linux + systemd Deployment

### 1. Create the bot user

```bash
sudo useradd -r -s /bin/false botuser
sudo mkdir -p /opt/public-mint-bot/data
```

### 2. Deploy application

```bash
sudo cp -r . /opt/public-mint-bot/
sudo chown -R botuser:botuser /opt/public-mint-bot
```

### 3. Set up Python environment

```bash
sudo -u botuser python3.11 -m venv /opt/public-mint-bot/.venv
sudo -u botuser /opt/public-mint-bot/.venv/bin/pip install -r /opt/public-mint-bot/requirements.txt
```

### 4. Configure environment

```bash
sudo cp /opt/public-mint-bot/.env.example /opt/public-mint-bot/.env
sudo nano /opt/public-mint-bot/.env
# Fill in TELEGRAM_BOT_TOKEN, OPENSEA_API_KEY, etc.
sudo chmod 600 /opt/public-mint-bot/.env
```

### 5. Install systemd service

```bash
sudo cp systemd/public-mint-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable public-mint-bot
sudo systemctl start public-mint-bot
```

### Service management

```bash
# Start
sudo systemctl start public-mint-bot

# Stop
sudo systemctl stop public-mint-bot

# Restart
sudo systemctl restart public-mint-bot

# Status
sudo systemctl status public-mint-bot

# View logs (live)
sudo journalctl -u public-mint-bot -f

# View last 100 lines
sudo journalctl -u public-mint-bot -n 100

# Update and restart
git -C /opt/public-mint-bot pull
sudo systemctl restart public-mint-bot
```

---

## Configuration Reference

All settings are loaded from environment variables. Copy `.env.example` to `.env`.

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(required)* | Bot API token from BotFather |
| `TELEGRAM_GROUP_ID` | *(required)* | Target group/channel ID |
| `PUBLIC_MINT_TOPIC_ID` | *(required)* | Forum topic ID for alerts |
| `ADMIN_USER_IDS` | `""` | Comma-separated admin Telegram user IDs |
| `OPENSEA_API_KEY` | *(required)* | OpenSea API key |
| `OPENSEA_API_ENDPOINT` | `https://api.opensea.io/api/v2` | API base URL |
| `MONITOR_ALL_CHAINS` | `true` | Monitor all enabled chains |
| `ENABLED_CHAINS` | *(all 15)* | Comma-separated chain slugs |
| `POLL_INTERVAL_SECONDS` | `45` | API polling interval (30–60) |
| `MIN_MINT_PRICE_USD` | `0` | Minimum mint price |
| `MAX_MINT_PRICE_USD` | `20` | Maximum mint price |
| `MIN_OFFER_MULTIPLIER` | `1.5` | Offer/price ratio for paid mints |
| `FREE_MINT_MIN_OFFER_USD` | `5` | Minimum offer for free mints |
| `MIN_MINTED_PERCENTAGE` | `70` | Minimum minted % required |
| `ALLOW_SOLD_OUT` | `false` | Allow 100% minted drops |
| `NOTIFICATION_BEFORE_MINUTES` | `10` | Minutes before mint to notify |
| `DELETE_AFTER_MINUTES` | `15` | Minutes after mint to delete alert |
| `DATABASE_URL` | SQLite | SQLAlchemy async database URL |
| `TEST_MODE` | `false` | Enable test mode with mock data |
| `LOG_LEVEL` | `INFO` | Python logging level |

---

## Filtering Logic

```
Public? (stage_type = public_sale)
  ├── NO → SKIP
  └── YES
      ↓
Price $0–$20?
  ├── NO → SKIP
  └── YES
      ↓
Price > $0 → offer >= price × 1.5?   (Rule 3)
Price = $0 → offer >= $5?            (Rule 4)
  ├── NO → SKIP
  └── YES
      ↓
Minted >= 70%?
  ├── NO → SKIP
  └── YES
      ↓
Sold out & ALLOW_SOLD_OUT=false?
  ├── YES → SKIP
  └── NO
      ↓
Duplicate?
  ├── YES → SKIP
  └── NO
      ↓
SCHEDULE → NOTIFY → DELETE
```

---

## Running Tests

```bash
# Install test dependencies
pip install -r requirements.txt

# Run all tests
pytest

# Run with coverage
pytest --cov=app --cov-report=term-missing

# Run specific test file
pytest tests/test_filters.py -v
```

---

## Admin Commands

| Command | Description | Admin Only |
|---|---|---|
| `/start` | Greeting and bot info | No |
| `/status` | Bot and monitor health metrics | Yes |
| `/monitor on` | Enable polling | Yes |
| `/monitor off` | Pause polling (bot stays active) | Yes |
| `/test` | Send a test alert to the topic | Yes |

---

## Architecture

```
app/
├── main.py              # Entry point, startup/shutdown orchestration
├── config.py            # Environment-based configuration (pydantic-settings)
│
├── bot/                 # Telegram bot layer
│   ├── bot.py           # Application factory
│   └── handlers.py      # Command handlers
│
├── providers/           # API abstraction
│   ├── base.py          # Abstract provider interface + raw models
│   └── opensea.py       # OpenSea v2 implementation
│
├── chains/              # Chain registry
│   ├── registry.py      # Single source of truth for 15 chains
│   └── models.py        # Pydantic chain model
│
├── monitor/             # Core monitoring engine
│   ├── monitor.py       # Main polling loop + recovery
│   ├── processor.py     # Single-mint pipeline
│   └── scheduler.py     # APScheduler wrapper
│
├── filters/             # Filtering engine
│   └── mint_filter.py   # All 5 filtering rules
│
├── models/              # Normalised internal models
│   ├── mint.py          # MintOpportunity + MintStatus
│   └── notification.py  # NotificationRecord
│
├── database/            # Data layer
│   ├── database.py      # Engine + session factory
│   ├── models.py        # SQLAlchemy ORM models
│   └── repository.py    # All DB read/write operations
│
├── services/            # Business services
│   ├── notification_service.py
│   ├── mint_service.py
│   └── health_service.py
│
└── utils/               # Shared utilities
    ├── retry.py         # Exponential backoff
    ├── time.py          # UTC time helpers
    └── logging.py       # Logging setup
```

---

## API Limitations

1. **BSC (Binance Smart Chain):** Not in the OpenSea v2 `ChainIdentifier` enum. Monitored in config but skipped during API polling.
2. **Offer Price:** OpenSea drops API does not expose offer prices. The collection floor price (cheapest listing) from `/api/v2/collections/{slug}/stats` is used as a proxy. See `docs/API_FIELD_MAPPING.md`.
3. **Supply data:** `total_supply` and `max_supply` are only available in the `/drops/{slug}` detail endpoint, requiring an additional API call per discovered drop.
4. **Minted quantity:** OpenSea uses inverted naming — their `total_supply` field = minted count, `max_supply` = total cap.
5. **Arc chain:** Present in the ChainIdentifier enum but may return no drops at present.

---

## Security Notes

- API keys and bot tokens are never logged or hard-coded
- Admin commands restricted to `ADMIN_USER_IDS`
- All dynamic Telegram content is HTML-escaped
- Database credential patterns compatible with least-privilege access
- Non-root Docker user (`botuser`)
- systemd `NoNewPrivileges=yes`, `ProtectSystem=strict`
