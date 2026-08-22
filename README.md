# moonpayradar

Tracks marketing activity across a list of client X/Twitter accounts, and
separately searches X for people impersonating you.

Two jobs that look similar and are not:

| | promo tracking | fraud detection |
|---|---|---|
| question | what are my clients running? | who is pretending to be me? |
| discovery | poll each client's timeline | search all of X for your brand |
| auth | none needed | requires a session |
| output | client dashboard | impersonation candidates |

The second cannot be done by polling: an impersonator is by definition not on
your watchlist, so you only find them by searching.

**No official X API key, no paid third-party service, no database.**

---

## Quick start

```bash
uv sync
uv run radar.py --check-handles      # confirm every configured handle resolves
uv run radar.py --emit-dir           # fetch, classify, store
uv run share.py                      # dashboard at http://localhost:8800
```

For impersonation search, add `cookies.json` (see [Authentication](#authentication)):

```bash
uv run radar.py --search             # uses fraud.watch_terms from config
```

---

## How it works

```
                    config.json
                  (clients, rules)
                          |
        +-----------------+------------------+
        |                                    |
   DISCOVERY                             DISCOVERY
   timeline poll                         search all of X
   (no auth)                             (needs cookies.json)
   radar.py                              x_search.py
        |                                    |
        |  syndication endpoint              |  GraphQL SearchTimeline
        |  30 req / 15 min / IP              |  via twikit transport
        |                                    |
        +----------------+-------------------+
                         |
                    CLASSIFIER
                 radar.classify()
          weighted regex -> categories, score
                         |
          +--------------+--------------+
          |                             |
    is_event >= threshold          fraud.assess()
    promo event                    impersonation scoring
          |                             |
          +--------------+--------------+
                         |
                  events/<id>.json
                  (one file each, no DB)
                         |
        +----------------+----------------+
        |                                 |
   share.py                          webhook POST
   client view + fraud view          your platform
```

### Modules

| file | role |
|---|---|
| `radar.py` | CLI, timeline discovery, classifier, event store, webhook |
| `tweet_api.py` | hydrate one tweet by ID, no auth, effectively unthrottled |
| `x_search.py` | search all of X, parse the raw GraphQL response |
| `x_endpoints.py` | keep twikit's GraphQL query ids and transaction maths current |
| `fraud.py` | impersonation scoring: handle look-alikes, verification, links |
| `share.py` | dashboard and per-event share pages |

---

## Data access: three different surfaces

This is the part that governs everything else. X exposes three surfaces with
completely different characteristics, and the design follows from that.

### 1. Syndication timeline — discovery, no auth

```
https://syndication.twitter.com/srv/timeline-profile/screen-name/<handle>
```

What X publishes for embedded timeline widgets. Returns ~20 recent tweets as
JSON inside a `__NEXT_DATA__` script tag.

- **No authentication.**
- **30 requests per 15-minute window, per IP**, reported on every response via
  `x-rate-limit-remaining` / `x-rate-limit-reset`.
- One request per handle per poll, so the arithmetic is
  `handles x (900 / interval) <= 30`:

  | handles | minimum interval | at 300s poll |
  |---|---|---|
  | 4  | 120s | 12 / 30 |
  | 6  | 181s | 18 / 30 |
  | 10 | 300s | 30 / 30 (at the limit) |
  | 20 | 600s | over budget |

- **Do not poll while locked out.** Observed during development: a reset
  reported 722s away was, after 700s of waiting *plus six requests made during
  the lockout*, reported 873s away — roughly 850s further out than when the
  wait started. Requests made while limited are not free. `--check-handles`
  waits for a known reset before spending anything and stops at the first 429
  rather than burning one request per remaining handle; `fetch_all` does the
  same. If you are locked out, wait a full 900s window **without touching the
  endpoint** rather than probing to see whether it cleared.

The parser (`_walk_tweets`) hunts for tweet-shaped objects anywhere in the
payload rather than following a fixed path, so most reshuffles pass through.

### 2. Tweet hydration — one tweet by ID, no auth, no rate limit

```
https://cdn.syndication.twimg.com/tweet-result?id=<id>&token=<token>&lang=en
```

The endpoint the `react-tweet` library wraps, called directly so nothing
depends on a third party's deployment staying up.

- **No authentication.** The `token` is derived from the tweet ID, not a secret.
- **No rate limiting observed** — 13 back-to-back requests, no throttling, no
  `x-rate-limit-*` headers at all, CDN-cached at `max-age=60`. A completely
  separate bucket from the timeline endpoint.
- **Hydration, not discovery.** It resolves an ID you already have and cannot
  list an account's tweets.

`tweet_api._js_number_to_string` is a port of V8's `DoubleToRadixCString`,
because the token is `Number.toString(36)` of a float and Python has no stdlib
equivalent. Verified digit-for-digit against node across 32 ids including
edge cases.

Deleted tweets return **HTTP 200 with a `TweetTombstone`**, not a 404.
`tweet_api` surfaces that as `available: false` plus X's own reason, and the
share page renders it. The stored classification outlives the tweet — a promo
that vanishes shortly after posting is itself a fraud signal.

### 3. GraphQL search — discovery across all of X, needs auth

```
https://x.com/i/api/graphql/<queryId>/SearchTimeline
```

No unauthenticated search exists. `cdn.syndication.twimg.com` returns 200 with
zero bytes for every search-shaped path; it serves only `tweet-result`.

---

## Keeping twikit working

`twikit` is used **only as transport** — it handles the session, the CSRF
header, and the `X-Client-Transaction-Id` maths, which are genuinely fiddly.
Its object model is not used.

Four things were broken against current X, all repaired in `x_endpoints.py`
and `x_search.py`:

1. **Stale GraphQL query ids.** X rotates them; twikit ships them hardcoded.
   8 of 10 were out of date, and a rotated id returns a bare 404 with no
   useful error. `x_endpoints` reads the live `operationName -> queryId` table
   out of X's logged-in `main.*.js` bundle and patches twikit's `Endpoint`
   constants at runtime.

2. **Transaction-id derivation.** X enforces `X-Client-Transaction-Id` on
   search but ignores it on user lookup — so stubbing it produces a 404 that
   looks identical to a stale query id. twikit derives it correctly, but could
   no longer *locate* the file holding the key-byte indices: X moved the
   `ondemand.s` hash out of a plain `"ondemand.s":"<hash>"` pair and into
   webpack chunk tables (one maps chunk id -> name, another chunk id -> hash).
   `patch_transaction` resolves it the new way and lets twikit's own maths run
   unchanged.

3. **Feature flags.** Each operation declares required `featureSwitches` in
   the bundle; they are extracted per-operation.

4. **Reshaped user payload.** `screen_name` moved to `core.screen_name`,
   `verified` to `verification.verified`, avatars to `avatar.image_url`.
   twikit still expects the old flat `legacy` user, so it parses live
   responses as **zero results** and raises `KeyError: 'urls'`. `x_search`
   parses the raw GraphQL itself.

When X rotates ids again:

```bash
uv run radar.py --refresh-endpoints
```

Search breaking and your session expiring look identical from outside, and
only one is worth re-exporting cookies for:

```bash
uv run radar.py --check-session
```

---

## The classifier

Weighted regex, not keyword matching. Each rule is
`{category, weight, pattern}`; weights sum into a score; an event fires at
`alert_threshold` (default 4). Weighting is what stops a single generic
marketing word from tripping an alert.

Categories: `airdrop`, `giveaway`, `referral`, `sponsorship`, `launch`,
`presale`, `listing`, `promo`, `campaign`, `wallet_action`, `urgency`,
`prize`.

Two more are applied in code rather than by a config rule, because both need
logic a single pattern cannot express: `partner_mention` (generated from
`watch_mentions`) and `scam_warning` (a negation check that damps the score
instead of raising it).

Rules live in `config.json` and are editable without touching Python. A rule
with a broken regex is reported and skipped rather than taking the sweep down;
if `rules` is missing entirely, the built-in set in `radar.py` is used. A bad
edit degrades rather than breaks.

### Two behaviours worth knowing

**`partner_mention`** — any client tweet mentioning a handle in
`fraud.watch_mentions` scores 4, enough to fire alone. A client tagging you is
co-marketing that involves you directly, whether or not it uses promo
vocabulary. Be aware this can mask gaps: a tweet can fire on the mention alone
while every promo signal in it scores zero.

**`scam_warning`** — when an account warns *about* a scam it trips the same
words the scam does. It still raises an event (an impersonation wave is
usually live) but is not escalated to high risk.

### Calibration is not optional

Measured against 43 real tweets from two clients, the shipped rules fire on
**7%**. That number is close to meaningless as a coverage estimate, because
the corpus contains only phrasings already tuned for. During development every
single real tweet supplied by hand exposed a distinct miss:

| tweet | gap |
|---|---|
| Binance referral programme | pattern required the word "code" |
| TrustWallet launch | required exact "now live"; ignored the @mention entirely |
| Ledger BuyDay | "0 **processing** fees"; "for the next 24 hours" |

Regex rules only cover phrasings someone thought of. Run with `--all` for a
day, then review what lands at score 3 versus 5 before trusting the threshold.

---

## Fraud scoring

`fraud.py` answers a different question from the classifier: not "is this
marketing?" but "is this someone pretending to be you?"

| signal | weight | notes |
|---|---|---|
| look-alike handle | 6 | see below |
| contains brand name | 3 | `@moonpaynews` |
| wallet action | 5 | connect wallet, seed phrase — the actual payload |
| unverified author | 3 | no verification of any kind |
| Blue only | 1 | purchasable, not a brand badge |
| offsite link | 5 | claims your campaign, links somewhere that isn't yours |
| incentive | 3 | airdrop, giveaway, presale |
| urgency | 2 | time pressure |

**Handle look-alikes** fold visually-confusable characters, so `@m00npay`,
`@M0onPay_` and `@moonpay` all reduce to the same skeleton. Brand name plus an
official-sounding suffix (`_official`, `support`, `airdrop`, `hq`, …) is
treated the same way.

**A core signal is required.** Being unverified and mentioning a presale is
ordinary promotion — plenty of real projects use a payment provider and say
so. Flagging requires either a look-alike handle or a wallet action;
everything else only amplifies. Without this the first live sweep flagged
23 of 40, mostly a legitimate third-party presale that uses MoonPay as a
payment rail. Events that clear the score but lack a core signal are marked
`promotional_mention` instead.

**Verification is the trap.** Brand accounts come back with `verified: false`
**and** `is_blue_verified: false` but `verified_type: "Business"`. Checking
only the booleans marks Binance and Ledger unverified — exactly backwards for
telling a real brand from an impersonator. All three flags are normalized into
`author_verified` / `author_verified_type` (`Business` | `Government` |
`Blue` | `Legacy` | `null`).

Output is **leads, not verdicts**. The dashboard says so on the page.

---

## Configuration

Everything tunable lives in `config.json`.

```json
{
  "brand": "MoonPay Radar",
  "clients": [
    { "name": "Trust Wallet", "handles": ["TrustWallet"] },
    { "name": "Coinbase", "handles": ["coinbase", "CoinbaseAssets"] }
  ],
  "tweets_per_check": 10,
  "poll_interval_seconds": 300,
  "webhook_url": "",
  "events_dir": "events",
  "share_port": 8800,
  "alert_threshold": 4,
  "watch_mentions": ["moonpay"],
  "fraud": {
    "watch_terms": ["moonpay airdrop", "moonpay giveaway", "moonpay claim"],
    "official_handles": ["moonpay", "MoonPayHelp"],
    "official_domains": ["moonpay.com", "moonpay.io"],
    "search_limit": 40
  },
  "rules": [
    { "category": "airdrop", "weight": 5, "pattern": "\\bair\\s?drops?\\b" }
  ]
}
```

| key | meaning |
|---|---|
| `clients` | each has a `name` and one or more `handles`; events group by client |
| `alert_threshold` | summed weight at which a promo event fires |
| `watch_mentions` | handles that, when mentioned by a client, raise `partner_mention` |
| `fraud.watch_terms` | search queries; pair the brand with scam vocabulary |
| `fraud.official_handles` | your real accounts — never flagged |
| `fraud.official_domains` | your real domains — links elsewhere are suspicious |
| `rules` | the classifier |

**A client is not a handle.** Coinbase runs several accounts; listing them
under one client makes them one dashboard row instead of three unrelated
sources. A flat `watchlist` of handles also works — each becomes its own
single-handle client.

**Search terms matter.** Bare `"moonpay"` returns mostly "gm" replies from the
community. Pairing the brand with the words scams actually use spends the
search budget on plausible candidates.

---

## Authentication

Only impersonation search needs this. Promo tracking runs without credentials.

**Create `cookies.json` yourself. Never paste these values into a chat or a
commit.**

1. Log into x.com as a **dedicated throwaway account** — not your personal or
   company account. Scripted access can get an account rate-limited or flagged.
2. DevTools -> Application -> Cookies -> `https://x.com`. Copy `auth_token`
   and `ct0`.
3. Save alongside `radar.py`:

```json
{ "auth_token": "...", "ct0": "..." }
```

`cookies.json` is gitignored. That file *is* a live login — anyone holding it
is signed in as that account. Rotate by logging that account out, which
invalidates it.

---

## Command reference

```bash
# setup and health
uv run radar.py --check-handles            # every handle resolves? (1 req each)
uv run radar.py --check-session            # cookies still authenticate?
uv run radar.py --refresh-endpoints        # re-read X's live GraphQL query ids

# promo tracking
uv run radar.py                            # one-shot, print JSON to stdout
uv run radar.py --all                      # include below-threshold tweets
uv run radar.py --emit-dir                 # store matches for the dashboard
uv run radar.py --accounts binance,Ledger  # ad-hoc, ignores config clients
uv run radar.py --watch --state seen.json  # poll loop, dedup across restarts
uv run radar.py --backend twikit           # authenticated timelines

# single tweet
uv run radar.py --add-tweet "https://x.com/Ledger/status/2090080621443862531"

# impersonation search
uv run radar.py --search                   # all fraud.watch_terms
uv run radar.py --search "moonpay airdrop" --limit 40

# dashboard
uv run share.py                            # http://localhost:8800
```

`--add-tweet` hydrates, classifies and stores in one step. It works even when
the timeline budget is spent, because hydration is a separate bucket.
Below-threshold tweets are stored anyway when added by hand — a deliberate add
is its own signal of intent.

### A wrong handle is the worst failure mode

X returns HTTP 200 with an empty timeline for a handle that does not exist, so
the account looks *quiet* rather than *broken*, forever. The display name and
the @handle frequently differ — pump.fun posts as `@Pumpfun`, not
`@pumpdotfun`. Run `--check-handles` before starting a long-running watch. If
a handle cannot be reached, it is reported as **unchecked** rather than
passing; an unverified handle is never reported as verified.

---

## The dashboard

```bash
uv run share.py
```

- `/` — client view, grouped by client, filterable by category or risk
- `/?view=fraud` — impersonation candidates, grouped by posting account
- `/share?id=<tweet_id>` — one event: verdict, matched signals, official
  links, and the tweet itself

The two views are separate because they have very different volumes: search
across all of X produces far more candidates than polling a handful of
clients, and mixing them buries the client timeline. The fraud view groups by
account because candidates arrive in clusters — one operator posting
near-identical copy from several handles.

Tweets are hydrated server-side, so pages carry no third-party JavaScript and
do not depend on anyone else's deployment. Events are one JSON file each in
`events/`; `share.py` only reads them.

`entities.urls` is the useful field for fraud work: the domains the real
account linked. Anything claiming the same campaign on a different domain is
your signal.

---

## Limits worth knowing before this is load-bearing

- **This is scraping, not an API.** It uses endpoints X publishes for embeds
  and for its own web client, outside the official API terms. Sessions get
  invalidated, shapes change, IPs get rate-limited. Get compliance sign-off
  before it is a contractual guarantee to a client.
- **twikit needs periodic repair.** X rotates GraphQL query ids without
  notice; `--refresh-endpoints` is the fix, but it is a maintenance task, not
  a one-off.
- **Polling is not push.** Detection lag is up to one interval.
- **Keyword scoring has no nuance.** It will not catch a paraphrased promo
  with none of the trigger words. Keep it as a cheap pre-filter and send only
  candidates to an LLM for a second pass if that matters.
- **`events/` grows without bound**, and `share.py` re-reads every file per
  request. Fine at dozens, not at tens of thousands.
- **No supervision.** The watcher is a foreground process; if it dies it stays
  dead silently — the same failure mode as a wrong handle.
- **Fraud output is unreviewed.** Candidates are scored by pattern matching
  against rules tuned on a small sample. Treat them as leads to verify.
- **Same pattern works beyond X.** Airdrop scams propagate hardest on Telegram
  and Discord; `telethon` and `discord.py` give the same poll -> classify ->
  webhook loop, both free.
