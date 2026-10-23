# PriceWatch

Self-hosted marketplace price monitor. Create monitors in a web UI, and PriceWatch checks eBay, Craigslist and any custom site on a schedule, learns the going price for each search, and notifies you when something is a real deal.

## Run it

**Docker (recommended)**

```
cp .env.example .env      # set PRICEWATCH_PASSWORD
docker compose up -d
```
Open `http://your-server:8000` (user `admin`, password from `.env`). Data lives in the `pricewatch-data` volume.

**Without Docker**

```
pip install -r requirements.txt
PRICEWATCH_PASSWORD=yourpassword uvicorn app.asgi:app --host 0.0.0.0 --port 8000
```
Data is stored in `data/pricewatch.db` (override with `PRICEWATCH_DB`).

Put it behind HTTPS (Caddy, nginx, Cloudflare Tunnel) before exposing it to the internet; the password uses HTTP Basic auth.

**Try the UI first (synthetic data, no keys, no network):** `python -m tests.demo_server` then open http://127.0.0.1:8765

## First-time setup (Settings page)

1. **eBay**: free keyset at https://developer.ebay.com. Copy the *Production* App ID and Cert ID into Settings.
2. **Notifications**: turn on at least one and press "Send test". ntfy is the quickest (install the ntfy app, pick a long random topic). Discord, Slack, Telegram, Pushover, email and a generic webhook are also supported.
3. Set "Public address" so alerts link back to the dashboard.

## What you get

- **Monitors**: search text (`"quotes"` for phrases, `-word` to exclude), sources, filters, check interval, alert rules.
- **Alert rules** (any can fire): in the cheapest N% of the market (auto-learned), at or under a price you set, or X% below the going price.
- **Analytics per monitor**: going price (median), good/great deal lines (cheapest 25% / 10%), lowest real price, price distribution, trend over time, per-source comparison, and an estimate of what items actually sell for (from listings that disappear).
- **Price check**: one-off market analysis for anything, nothing saved; one click turns it into a monitor.
- Smarts: outliers (accessories, $1 bait, absurd asks) are excluded from stats and never alerted; suspiciously cheap deals are flagged "verify"; price drops re-alert; shipping is included in prices where the source provides it; failed sources back off automatically.

## Custom sites

Use "Add a custom site" in a monitor: give the search URL with `{query}` and CSS selectors for the result item, title and price (right-click a result in your browser, Inspect). Works for sites whose results are in the page HTML. For JavaScript-built pages choose "Needs JavaScript" and install `pip install playwright && playwright install chromium`.

## Honest limits

- **Facebook Marketplace is not supported.** It requires login, forbids automated access, and actively blocks it. Use its built-in saved-search notifications alongside PriceWatch.
- **Craigslist and custom sites are scraped**, which their terms may restrict, and layouts change. PriceWatch is deliberately gentle (spaced requests, backoff on blocks) and reports a clear error in the UI when a page can't be read instead of silently showing nothing. Keep intervals reasonable; personal use only.
- **eBay "sold" prices need restricted API access.** The going price comes from current listings; the "what things sell for" panel is an estimate from listings that vanished (sold or removed) and needs time to learn.
- Prices are shown with `$`; other eBay marketplaces work but are not currency-converted.
- The Craigslist and custom-site parsers were tested against fixtures modeled on their page structure, not live sites (my build environment had no internet access). If Craigslist returns an error saying it can't read results, that is the signal the selectors need adjusting in `app/scrapers/craigslist.py`.

## Tests

`python -m unittest discover -s tests -t .` (28 tests; no network needed).
