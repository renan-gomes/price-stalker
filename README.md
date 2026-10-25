# price-stalker

Tracks product prices across a curated allowlist of trusted US retailers.

```
price_stalker/
├── discovery/    Google Programmable Search -> candidate product URLs
├── retailers/    Trusted-domain allowlist (+ per-retailer hooks)
├── extraction/   Fetch page, parse JSON-LD / meta tags into a price
├── database/     SQLite: products, listings, price history
├── scheduler/    Periodic re-check of every tracked listing
└── dashboard/    Streamlit UI
```

## Setup

    pip install -e ".[dev]"
    export GOOGLE_API_KEY=...   # Custom Search JSON API key
    export GOOGLE_CSE_ID=...    # Programmable Search Engine id (search the entire web)

## Usage

    price-stalker search "sony wh-1000xm5"        # find listings on trusted retailers
    price-stalker add "Sony WH-1000XM5" <url>...  # track specific URLs
    price-stalker check                           # fetch current prices once
    price-stalker schedule --every 6              # check every 6 hours
    streamlit run price_stalker/dashboard/app.py

DB path: `PRICE_STALKER_DB` (default `price_stalker.db`).

## Notes / known limits

- Many large retailers block simple HTTP scraping and forbid it in their ToS. Amazon is not in the built-in
  list for that reason (it answers with a robot-check page); `retailer add Amazon amazon.com` re-enables it.
  The extractor is retailer-agnostic (JSON-LD, then meta tags); for those sites prefer an official
  API/affiliate feed, implemented as a `Retailer.fetch` hook.
- Prices are stored as integer cents plus currency.

## Alerts

`check` (and `schedule`) print an alert when an item **drops** since the previous check, **reaches its
target price**, or **fails 3 checks in a row**. Set a target with `add ... --target 120` or
`price-stalker target <#> 120` (`none` clears it).

To also get alerts on Telegram:

1. In Telegram, talk to @BotFather: `/newbot`, pick a name, copy the **token**.
2. Open your new bot and press **Start** (or send any text).
3. `export TELEGRAM_BOT_TOKEN=<token>` then run `price-stalker alerts chat-id` to find your chat id.
4. `export TELEGRAM_CHAT_ID=<id>` then run `price-stalker alerts test`: a message should arrive.

Put both `export` lines in `~/.bashrc` so `schedule` (also under `nohup`/`tmux`) always sees them.

`list` shows the change since the previous check (▼/▲), the lowest price seen, the target (✔ when
reached) and a `⚠ failing since ...` line for items that keep failing.

## Retailers

    price-stalker retailers                          # everything that can be tracked
    price-stalker retailer add Sephora sephora.com   # register a shop (a full URL also works)
    price-stalker retailer remove sephora.com        # only the ones you registered

Registered shops are stored in the database next to your products; subdomains are accepted and
look-alike domains (`evilsephora.com`) are not.

## Shops that block scripts (HTTP 403)

Some shops (e.g. Sephora) refuse plain requests. When a shop answers 403/429/503 or serves a
"robot or human" page, the fetch automatically falls back to a headless Chromium:

    pip install -e ".[browser]" && playwright install chromium
    price-stalker debug '<product url>' --browser     # force browser mode to test a shop

Variables: `PRICE_STALKER_CHROMIUM` (path to an existing Chrome/Chromium) and
`PRICE_STALKER_HEADED=1` (show the window; some shops only accept a visible browser).
Browser checks take a few seconds each and are not guaranteed to pass every anti-bot system.

## Which shops work?

    price-stalker probe <product url> <product url> ...   # one product page per shop
    price-stalker probe -f urls.txt --register            # file with one URL per line; register the ones that work

Each shop is reported as `OK` (price read), `NEEDS_VARIANT` (several options; use `--variant`),
`NO_PRICE` (page loads but the price is added by JavaScript), `BLOCKED` (403 / robot check) or `ERROR`.
`probe` works on shops that are not registered yet, and uses plain requests only.
