# OneMint - OpenSea Public Mint Alert Bot

An automated Python bot that monitors OpenSea for qualifying public NFT mints across 15 blockchain networks. When a mint meets strict liquidity and resale criteria, it sends a scheduled alert to your Telegram group topic 15 minutes before the mint begins, then automatically deletes the message 10 minutes after launch to keep your chat clutter-free.

---

## How It Works

1. **Continuous Scanning**: Polls OpenSea v2 API across supported chains (Base, Ethereum, Polygon, Arbitrum, Ink, Robinhood, etc.).
2. **Strict Filtering**: Checks every drop against strict criteria (no pass-through on missing data):
   - **Public Only**: Strictly public stages. Skips whitelist, allowlist, presale, or token-gated tiers.
   - **Price Cap**: Mint price must be $\le$ $13 USD.
   - **Resale Spread**: Active collection offer or floor must be at least **1.40×** the mint price.
   - **Mint Progress**: At least **70%** of total supply must already be minted.
3. **Timed Alerts**:
   - Sends an alert immediately when discovered as qualified, plus a reminder **15 minutes before mint starts** (after re-verifying fresh market data).
   - **Auto-Deletes at mint start + 10 minutes** so outdated callouts don't linger in your Telegram group.
4. **Crash-Proof State**: Uses SQLite/PostgreSQL with APScheduler to persist alert timers across server restarts.

---

## Supported Networks

- **Layer 2s & EVM Chains**: Base, Arbitrum, Optimism, Polygon, Zora, Ink, Shape, Abstract, ApeChain, HyperEVM, Arc, Robinhood Chain, Avalanche.
- **L1**: Ethereum Mainnet.
- *(Note: BSC is in the chain registry but currently unsupported by OpenSea's drop endpoints).*

---

## Quick Setup

### 1. Requirements
- Python 3.11+
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Telegram Group with Topics enabled
- OpenSea API Key

### 2. Install & Configure

```bash
git clone https://github.com/Raashiyeeee/OneMint-Public.git
cd OneMint-Public

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Setup Environment Variables

```bash
cp .env.example .env
```

Open `.env` and fill in your keys:

```env
# Telegram
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
TELEGRAM_GROUP_ID=-1001234567890
PUBLIC_MINT_TOPIC_ID=12345
ADMIN_USER_IDS=123456789

# OpenSea API
OPENSEA_API_KEY=your_opensea_key_here
OPENSEA_API_ENDPOINT=https://api.opensea.io/api/v2

# Polling Interval (in seconds)
POLL_INTERVAL_SECONDS=45

# Rules (defaults match the strategy floor)
MAX_MINT_PRICE_USD=13
MIN_OFFER_MULTIPLIER=1.40
MIN_MINTED_PERCENTAGE=70
```

---

## Running the Bot

### Local Development
```bash
python -m app.main
```

### Docker Deployment
```bash
# Build and run in background
docker compose up -d --build

# View logs
docker compose logs -f public-mint-bot

# Stop
docker compose down
```

### Cloud Deployment (Render Blueprint)
This repo includes a pre-configured `render.yaml`:
1. Connect this repo to [Render](https://render.com).
2. Create a new **Blueprint Instance**.
3. Under Environment Variables in the Render dashboard, add your `TELEGRAM_BOT_TOKEN`, `OPENSEA_API_KEY`, and `TELEGRAM_GROUP_ID`.

---

## Telegram Bot Commands

| Command | Who Can Use | Description |
| :--- | :---: | :--- |
| `/start` | Everyone | Basic greeting and bot information |
| `/status` | Admin | Shows API health, database status, and active scheduled jobs |
| `/monitor on` | Admin | Resumes OpenSea polling loop |
| `/monitor off` | Admin | Pauses polling without taking the bot offline |
| `/test` | Admin | Sends a sample test alert to verify Telegram permissions |
| `/sendtoday` | Admin | Forces an immediate scan and broadcasts any eligible mints today |

---

## Testing

Run the full automated test suite (mocked data, no live API credits consumed):

```bash
# Run tests
pytest

# Run with verbose output
pytest -v

# Run specific filter tests
pytest tests/test_filters.py -v
```

---

## Project Structure

```text
app/
├── main.py              # Application lifecycle and webhook/polling init
├── config.py            # Pydantic environment configuration
├── bot/                 # Telegram bot handlers and message formatting
├── monitor/             # OpenSea polling loop, pipelines, and APScheduler
├── filters/             # Strict public eligibility and multiplier checks
├── providers/           # OpenSea v2 API integration
├── chains/              # Multi-chain registry and metadata
├── database/            # SQLAlchemy models, sessions, and recovery queries
├── services/            # Alert delivery, cleanup, and health metrics
└── utils/               # Retries, backoff, and UTC time calculations
```

---

## License

MIT License. Provided as-is for educational and personal automation purposes. Always verify contract addresses before minting.
