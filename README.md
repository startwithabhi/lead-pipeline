# Lead Pipeline — Web App

A local web app version of the Lead -> Audit -> Rank -> Build -> Outreach
pipeline. You run one command, then do everything else by clicking around
in your browser — no terminal commands after that, no copy-pasting prompts.

## 1. Install Python packages (one-time)

Open a terminal in this folder and run:

```
pip install -r requirements.txt
```

This installs Flask (the web server), requests, and BeautifulSoup.

## 2. Get two API keys (one-time)

**Google Maps API key** — powers the business search and website speed check
1. Go to https://console.cloud.google.com/
2. Create a project (or use an existing one)
3. Go to "APIs & Services" -> "Library", enable **Places API**
   (optionally also enable "PageSpeed Insights API" for load-speed scoring)
4. Go to "APIs & Services" -> "Credentials" -> "Create Credentials" -> "API key"
5. Copy the key. Google gives $200/month free credit — a run of 20-30 leads
   costs a few cents, so this stays free for normal use.

**Anthropic API key** — powers the audit summaries, ranking, build prompts, and outreach copy
1. Go to https://console.anthropic.com/
2. Settings -> API Keys -> Create Key
3. Copy the key.

## 3. Start the app

In this folder, run:

```
python app.py
```

You'll see:
```
Lead Pipeline running at http://127.0.0.1:5050
```

Open that address in your browser (Chrome, Firefox, whatever you use).
Leave the terminal window open in the background — closing it stops the app.

## 4. Use it

1. **Paste your two API keys** into the settings box at the top and click
   "Save keys in this browser". They're stored only in your browser
   (localStorage) and sent straight from your browser to Google/Anthropic
   with each request — this app's server never writes them to disk.
2. **Type a niche and a location**, e.g. "clothing boutique" and
   "Siliguri, West Bengal".
3. Click **Preview leads** first — it's a quick, cheap check that shows you
   how many businesses match and which ones already have websites, before
   you spend any AI credits.
4. Click **Run full pipeline** — you'll see a 5-stage progress bar
   (Scrape -> Audit -> Rank -> Build -> Outreach) with a live log underneath.
   This takes roughly 1-3 minutes depending on how many leads you scan.
5. When it finishes, you get a card per top lead with: the audit findings,
   an estimated monthly revenue loss, a ready-to-paste website-build prompt
   (Copy button), and outreach messages (Copy button) — plus, if a phone
   number was found, a direct "Open in WhatsApp" link with the first
   message pre-filled.
6. Click **Download full report (.md)** to save everything to a file.

## What it's actually checking (not just AI guessing)

- **Scrape**: real Google Places data — name, phone, rating, review count,
  and whether a website is listed at all.
- **Audit**: for any business with a website, the app actually fetches the
  page and checks: mobile viewport tag, WhatsApp click-to-chat link, which
  builder/platform it's on (Wix/Shopify/WooCommerce/Squarespace/WordPress/
  custom), title and meta description, footer copyright year (staleness
  signal), and a real PageSpeed Insights load-time + performance score.
  Claude then turns those raw signals into the 3-4 bullet summary.
- **Rank, Build, Outreach**: these are genuinely Claude's reasoning, same as
  before — ranking by opportunity size, writing the build brief, drafting
  the WhatsApp messages.

## Notes

- This app runs only on your own machine (`127.0.0.1`) — nobody else can
  reach it unless you deliberately expose it.
- Nothing is sent automatically. Outreach messages are drafts for you to
  read and send yourself.
- Google's Places API caps at ~60 results per search — for a niche bigger
  than that in one city, narrow by neighborhood and run it again.
- If a run errors out partway, the error message is shown in the log — the
  most common cause is a typo'd API key or an API (Places / PageSpeed) that
  hasn't been enabled yet in your Google Cloud project.
