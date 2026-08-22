"""
X/Twitter promo + airdrop radar for vendor accounts. No official API key,
no paid third-party service, no database.

Two fetch backends, same output shape:

  syndication  X's public embed/syndication endpoint. Zero auth, zero
               install (stdlib only). Rate-limited per IP -- fine for a
               handful of accounts on a slow poll.

  twikit       Authenticated session against X's internal GraphQL, via
               the open-source `twikit` library. Needs your own session
               cookies. Higher limits, full tweet text, replies/quotes.

Usage:
    python radar.py                      # one-shot: fetch, classify, print JSON
    python radar.py --all                # include non-matching tweets too
    python radar.py --backend twikit
    python radar.py --watch              # poll loop, in-memory dedup
    python radar.py --watch --state seen.json   # dedup survives restarts
    python radar.py --webhook https://...       # POST each match

State is a plain JSON file (or nothing at all). No SQLite.
"""

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import fraud as fraud_mod
import tweet_api

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
COOKIES_PATH = HERE / "cookies.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def log(msg):
    """Human-readable progress goes to stderr so stdout stays clean JSON."""
    print(msg, file=sys.stderr)


def load_config():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    return normalize_config(cfg)


def normalize_config(cfg):
    """Expand the clients model into the flat handle list the fetchers want.

    A client is not a handle -- Coinbase runs several accounts -- so events
    carry both, and the dashboard can group by client. A plain `watchlist`
    still works: each handle becomes its own single-handle client.
    """
    clients = cfg.get("clients")
    if not clients:
        clients = [{"name": h, "handles": [h]} for h in cfg.get("watchlist", [])]

    handles, owner = [], {}
    for client in clients:
        name = client.get("name") or "?"
        for handle in client.get("handles") or []:
            handle = handle.strip().lstrip("@")
            if not handle:
                continue
            handles.append(handle)
            owner[handle.lower()] = name

    cfg["clients"] = clients
    cfg["watchlist"] = handles          # what the fetchers iterate
    cfg["_client_of"] = owner           # handle -> client name
    return cfg


# --------------------------------------------------------------------------
# Backend 1: syndication endpoint (no auth, stdlib only)
# --------------------------------------------------------------------------

SYNDICATION_URL = (
    "https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"
    "?dnt=true&maxResults={count}"
)
# X's per-IP budget on this endpoint, from its own x-rate-limit-* headers.
SYNDICATION_BUDGET = 30
SYNDICATION_WINDOW = 900  # seconds
WINDOW_WAIT = SYNDICATION_WINDOW + 60  # most a --watch loop will sleep for a reset

NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


def verification(user):
    """Normalize X's three separate verification flags.

    Big brand accounts come back with verified=False and
    is_blue_verified=False but verified_type="Business" -- checking only
    the booleans marks Binance unverified, which is exactly backwards for
    telling a real vendor account from an impersonator.
    """
    kind = user.get("verified_type")  # "Business" | "Government" | None
    if not kind and user.get("is_blue_verified"):
        kind = "Blue"
    elif not kind and user.get("verified"):
        kind = "Legacy"
    return {"author_verified": bool(kind), "author_verified_type": kind}


def _walk_tweets(node, out, seen_ids):
    """Recursively pull tweet-shaped dicts out of an arbitrary JSON blob.

    The syndication payload's exact nesting changes without notice, so we
    look for the shape (an id_str plus some text field) rather than a fixed
    path. Costs a full tree walk, buys us not breaking on every reshuffle.
    """
    if isinstance(node, dict):
        tid = node.get("id_str") or node.get("conversation_id_str")
        text = node.get("full_text") or node.get("text")
        if tid and isinstance(text, str) and tid not in seen_ids:
            seen_ids.add(tid)
            user = node.get("user") or {}
            out.append(
                {
                    "id": str(tid),
                    # X serves tweet text HTML-escaped here; "&amp;" would stop
                    # the "RT & follow" signal from ever matching.
                    "text": html.unescape(text),
                    "created_at": node.get("created_at"),
                    "author": user.get("screen_name"),
                    "author_name": user.get("name"),
                    **verification(user),
                    "entities": node.get("entities") or {},
                }
            )
        for v in node.values():
            _walk_tweets(v, out, seen_ids)
    elif isinstance(node, list):
        for v in node:
            _walk_tweets(v, out, seen_ids)


class Unavailable(RuntimeError):
    """X served a 200 but will not hand over this account's timeline."""


class RateLimited(RuntimeError):
    """X refused us for budget reasons. `reset` is the epoch it frees up."""

    def __init__(self, reset):
        self.reset = reset
        wait = max(0, int(reset - time.time())) if reset else None
        super().__init__(
            f"rate limited by X (429), resets in {wait}s" if wait is not None
            else "rate limited by X (429)"
        )


# X hands back its own budget accounting on every response. Track it so we
# throttle to the documented limit instead of discovering it via 429s.
_budget = {"remaining": None, "reset": None}


def _note_budget(headers):
    try:
        remaining = headers.get("x-rate-limit-remaining")
        reset = headers.get("x-rate-limit-reset")
        if remaining is not None:
            _budget["remaining"] = int(remaining)
        if reset is not None:
            _budget["reset"] = int(reset)
    except (TypeError, ValueError):
        pass


def fetch_syndication(handle, count):
    url = SYNDICATION_URL.format(handle=handle, count=count)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            _note_budget(resp.headers)
            html = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        _note_budget(e.headers)
        if e.code == 429:
            raise RateLimited(_budget["reset"]) from e
        raise RuntimeError(f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {e.reason}") from e

    m = NEXT_DATA_RE.search(html)
    if not m:
        raise RuntimeError(
            "no __NEXT_DATA__ in response -- account may be private, suspended, "
            "or X changed the page shape"
        )
    data = json.loads(m.group(1))

    # A 200 with an empty timeline is the trap: X serves this for accounts it
    # won't embed (sensitive-content flagged, embeds disabled, suspended), and
    # it is indistinguishable from "posted nothing new" unless we check the
    # flag. A radar that silently goes blind on an account is worse than one
    # that errors, so make it loud.
    page = data.get("props", {}).get("pageProps", {})
    has_results = page.get("contextProvider", {}).get("hasResults")
    if has_results is False:
        raise Unavailable(
            f"@{handle}: X returned an empty timeline. Most often the handle is "
            "wrong -- check it resolves at https://x.com/" + handle + " (the "
            "display name and the @handle frequently differ). Otherwise the "
            "account may be suspended, protected, or embed-restricted, in which "
            "case --backend twikit can still reach it."
        )

    tweets = []
    _walk_tweets(data, tweets, set())
    # Own tweets only: the walk also picks up quoted/retweeted authors.
    own = [t for t in tweets if (t["author"] or "").lower() == handle.lower()]
    return (own or tweets)[:count]


# --------------------------------------------------------------------------
# Backend 2: twikit (authenticated session cookies)
# --------------------------------------------------------------------------


def fetch_twikit(handles, count):
    """Fetch every handle in one event loop. Returns {handle: [tweets] | Exception}."""
    import asyncio

    try:
        from twikit import Client
    except ImportError:
        raise RuntimeError("twikit not installed -- run: pip install twikit") from None

    if not COOKIES_PATH.exists():
        raise RuntimeError(
            f"{COOKIES_PATH.name} not found. Create it from a logged-in browser "
            "session (see README: 'Authenticating twikit'). Never paste credentials "
            "into a chat or commit them."
        )

    def normalize(t, handle, user):
        """Flatten retweets/quotes so promo text inside them still classifies."""
        source = t.retweeted_tweet or t
        text = html.unescape(source.full_text)
        if t.retweeted_tweet:
            text = f"RT @{source.user.screen_name}: {text}"
        if t.quote:
            text = f"{text}\n[quoted @{t.quote.user.screen_name}] {html.unescape(t.quote.full_text)}"
        urls = list(source.urls or [])
        if t.quote:
            urls += list(t.quote.urls or [])
        return {
            "id": str(t.id),
            "text": text,
            "created_at": t.created_at,
            "author": handle,
            "author_name": user.name,
            **verification(
                {
                    "verified": user.verified,
                    "is_blue_verified": user.is_blue_verified,
                    "verified_type": getattr(user, "verified_type", None),
                }
            ),
            "entities": {"urls": urls},
        }

    async def run():
        client = Client("en-US")
        client.load_cookies(str(COOKIES_PATH))
        results = {}
        for i, handle in enumerate(handles):
            try:
                user = await client.get_user_by_screen_name(handle)
                raw = await user.get_tweets("Tweets", count=count)
                results[handle] = [normalize(t, handle, user) for t in raw]
            except Exception as e:  # one bad handle shouldn't kill the sweep
                results[handle] = e
            if i < len(handles) - 1:
                await asyncio.sleep(2)  # stagger, stay under the radar
        return results

    return asyncio.run(run())


# --------------------------------------------------------------------------
# Classifier
# --------------------------------------------------------------------------

# (category, weight, pattern). Weights are tuned so a single generic
# marketing word can't trip an alert on its own, but wallet-action or
# claim language -- the stuff scams imitate -- lands hard.
SIGNALS = [
    ("airdrop",       5, r"\bair\s?drops?\b|\btoken\s+drop\b|\bretro(?:active)?\s+drop\b"),
    ("airdrop",       4, r"\bclaim\s+(?:your|now|here|tokens?|rewards?|allocation)\b"),
    ("giveaway",      4, r"\bgive\s?aways?\b|\braffle\b|\bsweepstakes?\b|\blucky\s+draw\b"),
    ("giveaway",      3, r"\b(?:rt|retweet)\s*(?:\+|&|and)\s*follow\b|\btag\s+\d+\s+friends?\b"),
    ("wallet_action", 6, r"\bconnect\s+(?:your\s+)?wallet\b|\bverify\s+(?:your\s+)?wallet\b"),
    ("wallet_action", 6, r"\bseed\s+phrase\b|\bprivate\s+key\b|\bimport\s+wallet\b"),
    ("wallet_action", 4, r"\bsign\s+(?:the\s+)?(?:message|transaction)\b|\bapprove\s+(?:the\s+)?contract\b"),
    ("presale",       4, r"\bpre-?sale\b|\bwhitelist\b|\bwl\s+spots?\b|\bfree\s+mint\b|\bmint\s+is\s+live\b"),
    ("listing",       4, r"\bwill\s+list\b|\bnow\s+listed\b|\bnew\s+listing\b|\btrading\s+(?:opens?|is\s+live)\b"),
    ("promo",         3, r"\bbonus\s+code\b|\breferral\s+code\b|\bpromo\s+code\b|\buse\s+code\b"),
    ("promo",         2, r"\b\d{1,3}\s*%\s*(?:bonus|off|apy|back|discount)\b|\bzero\s+fees?\b|\bfee-?free\b"),
    ("promo",         2, r"\brewards?\s+program\b|\bcashback\b|\bearn\s+up\s+to\b"),
    # Referral programs are the most common vendor promo and rarely use the
    # word "code" -- requiring it missed real campaigns outright.
    ("referral",      3, r"\brefer(?:ral|rals|ring)?\b|\brefer\s+(?:a\s+friend|them|your)\b"),
    ("referral",      2, r"\bearn\s+(?:points|rewards?|commission|cashback|\$)\b"),
    ("referral",      2, r"\binvite\s+(?:them|friends?|your\s+friends?)\b"),
    ("launch",        3, r"\b(?:introducing|announcing|now\s+live|launching|just\s+dropped|new\s+feature)\b"),
    ("promo",         2, r"\bget\s+paid\b|\bpaid\s+(?:daily|weekly|out)\b|\bno\s+kyc\b"),
    ("campaign",      2, r"\bcampaign\b|\bpromotion\b|\bearly\s+access\b|\bwaitlist\b"),
    ("sponsorship",   4, r"\bsponsor(?:ed|ship|ing|s)?\b|\bofficial\s+partner\w*\b|\bproud\s+partner\w*\b"),
    ("sponsorship",   4, r"\bpartner(?:ed|ship|ships)\s+with\b|\bin\s+partnership\s+with\b|\bteam(?:ed|ing)?\s+up\s+with\b|\bcollab\w*\s+with\b"),
    ("urgency",       2, r"\blimited\s+time\b|\bends?\s+(?:today|tonight|soon|in\s+\d+)\b|\bfirst\s+[\d,]+\s+users?\b|\bhurry\b|\blast\s+chance\b"),
    ("prize",         2, r"\bprize\s+pool\b|\bworth\s+\$[\d,]+|\b\$[\d,]+(?:k|m)?\s+(?:in\s+)?(?:prizes?|rewards?)\b"),
]
def compile_rules(cfg=None):
    """Rules come from config.json when present, else the defaults above.

    Keeping the defaults in code means a broken or missing config still runs;
    keeping them mirrored in config.json means they're editable without
    touching Python. A bad regex is reported and skipped rather than taking
    the whole sweep down.
    """
    cfg = cfg or {}
    raw = cfg.get("rules")
    if not raw:
        raw = [{"category": c, "weight": w, "pattern": p} for c, w, p in SIGNALS]

    compiled = []
    for rule in raw:
        try:
            compiled.append(
                (rule["category"], int(rule["weight"]),
                 re.compile(rule["pattern"], re.I))
            )
        except (KeyError, ValueError, re.error) as e:
            log(f"[rules] skipping bad rule {rule.get('pattern','?')!r}: {e}")

    # A client tagging one of your own handles is co-marketing involving you.
    # Weighted to fire alone: it matters even without promo wording.
    for handle in cfg.get("watch_mentions") or []:
        compiled.append(
            ("partner_mention", 4, re.compile(rf"@{re.escape(handle)}\b", re.I))
        )
    return compiled


COMPILED = compile_rules()

# The vendor warning people about a scam trips the same words the scam does.
# Still worth an event -- it usually means an impersonation wave is live --
# but it shouldn't be scored as if the official account is the one phishing.
WARNING_RE = re.compile(
    r"\bnever\s+(?:ask|request|dm)\b|\bbewares?\b|\bphishing\b|\bimpersonat\w+\b"
    r"|\bfake\s+(?:account|site|airdrop|giveaway)s?\b|\bscam(?:mers?|s)?\b"
    r"|\bdo\s+not\s+(?:click|connect|share)\b",
    re.I,
)

# Score at or above this fires an alert.
ALERT_THRESHOLD = 4


def classify(text, compiled=None, threshold=None):
    hits, categories, score = [], set(), 0
    for cat, weight, rx in (compiled if compiled is not None else COMPILED):
        m = rx.search(text or "")
        if m:
            hits.append({"category": cat, "match": m.group(0).strip(), "weight": weight})
            categories.add(cat)
            score += weight

    is_warning = bool(WARNING_RE.search(text or ""))
    if is_warning:
        categories.add("scam_warning")
        score += 3

    # A promo that also demands a wallet action is the shape scams take.
    risk = "low"
    if "wallet_action" in categories and not is_warning:
        risk = "high"
    elif score >= 8 or ("urgency" in categories and score >= 6):
        risk = "medium"

    return {
        "score": score,
        "categories": sorted(categories),
        "signals": hits,
        "risk": risk,
        "is_event": score >= (ALERT_THRESHOLD if threshold is None else threshold),
    }


URL_RE = re.compile(r"https?://\S+")
CASHTAG_RE = re.compile(r"\$[A-Za-z][A-Za-z0-9]{1,9}\b")
HASHTAG_RE = re.compile(r"#\w+")


def extract_entities(tweet):
    """Links/cashtags/hashtags the official account used -- the allowlist you
    diff scam copies against."""
    text = tweet.get("text") or ""
    urls = [
        u.get("expanded_url") or u.get("url")
        for u in (tweet.get("entities") or {}).get("urls", [])
        if isinstance(u, dict)
    ]
    urls = [u for u in urls if u] or URL_RE.findall(text)
    return {
        "urls": sorted(set(urls)),
        "cashtags": sorted(set(CASHTAG_RE.findall(text))),
        "hashtags": sorted(set(HASHTAG_RE.findall(text))),
    }


def build_event(handle, tweet, verdict, client=None):
    return {
        "client": client or handle,
        "account": handle,
        "tweet_id": tweet["id"],
        "url": f"https://x.com/{handle}/status/{tweet['id']}",
        "text": tweet["text"],
        "posted_at": tweet.get("created_at"),
        "author_name": tweet.get("author_name"),
        "author_verified": tweet.get("author_verified"),
        "author_verified_type": tweet.get("author_verified_type"),
        "entities": extract_entities(tweet),
        "detected_at": datetime.now(timezone.utc).isoformat(),
        **verdict,
    }


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


def write_event(directory, event):
    """One JSON file per event -- the store share.py reads. Still no database."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{event['tweet_id']}.json").write_text(json.dumps(event, indent=2))


def post_webhook(url, event):
    body = json.dumps(event).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", "User-Agent": UA}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log(f"  -> webhook {resp.status} for {event['tweet_id']}")
    except Exception as e:
        log(f"  -> webhook FAILED for {event['tweet_id']}: {e}")


# --------------------------------------------------------------------------
# Sweep
# --------------------------------------------------------------------------


def fetch_all(cfg, backend, count, max_wait=60):
    """Returns {handle: [tweet] | Exception} for the whole watchlist.

    `max_wait` caps how long we'll sleep for a rate-limit window. A --watch
    loop can afford to wait the window out; a one-shot run should report the
    429 and exit rather than blocking the terminal for a quarter of an hour.
    """
    handles = cfg["watchlist"]
    if backend == "twikit":
        return fetch_twikit(handles, count)

    results = {}
    for i, handle in enumerate(handles):
        # If the last response said we're out, wait for the window rather than
        # spending the next N calls collecting 429s.
        if _budget["remaining"] == 0 and _budget["reset"]:
            wait = int(_budget["reset"] - time.time()) + 2
            if 0 < wait <= max_wait:
                log(f"[budget] exhausted, waiting {wait}s for the window to reset")
                time.sleep(wait)
                _budget["remaining"] = None

        try:
            results[handle] = fetch_syndication(handle, count)
        except RateLimited as e:
            wait = int(e.reset - time.time()) + 2 if e.reset else 0
            if 0 < wait <= max_wait:
                log(f"[budget] {handle}: 429, waiting {wait}s then retrying once")
                time.sleep(wait)
                _budget["remaining"] = None
                try:
                    results[handle] = fetch_syndication(handle, count)
                except Exception as retry_error:
                    results[handle] = retry_error
            else:
                results[handle] = e
        except Exception as e:
            results[handle] = e

        if i < len(handles) - 1:
            time.sleep(1.5)
    return results


def sweep(cfg, backend, count, seen, include_all, webhook, max_wait=60, emit_dir=None):
    """One pass over the watchlist. Returns the events found this pass."""
    compiled = compile_rules(cfg)
    threshold = cfg.get("alert_threshold", ALERT_THRESHOLD)
    events = []
    for handle, result in fetch_all(cfg, backend, count, max_wait).items():
        if isinstance(result, Exception):
            log(f"[{handle}] fetch failed: {result}")
            continue
        log(f"[{handle}] {len(result)} tweets")

        for tweet in reversed(result):  # oldest first, chronological alerts
            if tweet["id"] in seen:
                continue
            seen.add(tweet["id"])
            verdict = classify(tweet["text"], compiled, threshold)
            if not (verdict["is_event"] or include_all):
                continue
            event = build_event(handle, tweet, verdict,
                                cfg.get("_client_of", {}).get(handle.lower()))
            events.append(event)
            log(
                f"  [{event['risk'].upper():6}] {event['client']} | score={event['score']:<3} "
                f"{','.join(event['categories']) or '-'} :: "
                f"{event['text'][:90].replace(chr(10), ' ')}"
            )
            if emit_dir and verdict["is_event"]:
                write_event(emit_dir, event)
            if webhook and verdict["is_event"]:
                post_webhook(webhook, event)
    return events


def load_seen(path):
    if path and Path(path).exists():
        try:
            return set(json.loads(Path(path).read_text()))
        except (ValueError, OSError) as e:
            log(f"[state] ignoring unreadable {path}: {e}")
    return set()


def save_seen(path, seen):
    if not path:
        return
    # Keep the file from growing forever; ordering is arbitrary but any
    # 5000 recent IDs are enough to stop re-alerting on a poll window.
    Path(path).write_text(json.dumps(sorted(seen)[-5000:]))


def check_session():
    """Confirm cookies.json still authenticates, independent of search.

    Search breaking and the session expiring look identical from the outside,
    and only one of them is worth re-exporting cookies for.
    """
    import asyncio

    from twikit import Client
    from twikit.x_client_transaction.transaction import ClientTransaction

    if not COOKIES_PATH.exists():
        log(f"{COOKIES_PATH.name} not found.")
        return False

    # X accepts requests without a valid transaction id; twikit's derivation of
    # it targets a page layout X no longer serves, so stub it out.
    async def _noop(self, session, headers):
        self.home_page_response = "stub"

    ClientTransaction.init = _noop
    ClientTransaction.generate_transaction_id = lambda self, *a, **k: ""

    async def run():
        client = Client("en-US")
        client.load_cookies(str(COOKIES_PATH))
        return await client.get_user_by_screen_name("x")

    try:
        asyncio.run(run())
        log("Session OK -- X accepted the cookies.")
        return True
    except KeyError as e:
        # Reached X and got data back; twikit just could not parse it.
        log(f"Session OK -- X returned data (twikit parse gap: missing {e}).")
        return True
    except Exception as e:
        log(f"Session FAILED: {type(e).__name__}: {str(e)[:200]}")
        log("Re-export auth_token and ct0 from a logged-in browser.")
        return False


def search_mentions(cfg, query, limit, emit_dir):
    """Search all of X for a term, then score each hit for impersonation.

    The half a timeline poll cannot do: an impersonator is by definition not
    on your watchlist, so searching is the only way to find them.
    """
    import asyncio

    import x_search

    compiled = compile_rules(cfg)
    threshold = cfg.get("alert_threshold", ALERT_THRESHOLD)

    log(f"Searching X for {query!r} (limit {limit})...")
    try:
        tweets = asyncio.run(x_search.search(query, limit, str(COOKIES_PATH), log=log))
    except FileNotFoundError:
        raise RuntimeError(
            f"{COOKIES_PATH.name} not found -- search needs an authenticated "
            "session. See README: 'Authenticating twikit'."
        ) from None
    log(f"{len(tweets)} result(s)")

    flagged = []
    for tweet in tweets:
        verdict = classify(tweet["text"], compiled, threshold)
        assessment = fraud_mod.assess(tweet, verdict, cfg)
        if not assessment["is_impersonation"]:
            continue

        event = build_event(tweet["author"], tweet, verdict,
                            client=f"[unauthorized] @{tweet['author']}")
        event.update(assessment)
        event["source"] = "search"
        event["query"] = query
        flagged.append(event)
        log(f"  [{assessment['fraud_risk'].upper():6}] fraud={assessment['fraud_score']:<3} "
            f"@{tweet['author']}: {tweet['text'][:56]!r}")
        for sig in assessment["fraud_signals"]:
            log(f"           +{sig.get('weight', 0)} {sig['kind']}: {sig.get('detail') or ''}")
        write_event(emit_dir or cfg.get("events_dir", "events"), event)

    log(f"  -> {len(flagged)} of {len(tweets)} flagged\n")
    return flagged


TWEET_URL_RE = re.compile(r"(?:x|twitter)\.com/([^/]+)/status/(\d+)")


def add_tweet(cfg, ref, emit_dir):
    """Classify one tweet by URL or ID and store it, bypassing the timeline poll.

    Useful when someone forwards you a campaign directly, and for backfilling
    an account whose timeline budget is spent -- hydration is a separate,
    unthrottled bucket.
    """
    match = TWEET_URL_RE.search(ref)
    handle, tweet_id = (match.group(1), match.group(2)) if match else (None, ref.strip())
    if not tweet_id.isdigit():
        log(f"Not a tweet URL or id: {ref!r}")
        return None

    tweet = tweet_api.get_tweet(tweet_id)
    if not tweet["available"]:
        log(f"Tweet {tweet_id} unavailable: {tweet['reason']}")
        return None

    handle = tweet.get("author") or handle
    client = cfg.get("_client_of", {}).get((handle or "").lower())
    if not client:
        log(f"note: @{handle} is not in config.json -- filing under @{handle}")

    verdict = classify(tweet["text"], compile_rules(cfg),
                       cfg.get("alert_threshold", ALERT_THRESHOLD))
    event = build_event(handle, tweet, verdict, client)
    log(f"[{event['risk'].upper()}] {event['client']} | score={event['score']} "
        f"{','.join(event['categories']) or '(no category)'}")
    if not verdict["is_event"]:
        log("Below threshold -- storing anyway since it was added by hand.")
    write_event(emit_dir or cfg.get("events_dir", "events"), event)
    log(f"Stored -> /share?id={event['tweet_id']}")
    return event


def check_handles(cfg, count=1):
    """Confirm every configured handle actually resolves.

    A wrong handle returns an empty timeline forever without erroring, so the
    dashboard just looks quiet. Worth one request each before committing to a
    long-running watch.
    """
    log(f"Checking {len(cfg['watchlist'])} handle(s)...\n")

    # Requesting while locked out appears to push the reset further away, so
    # wait for the window before spending anything -- and stop at the first
    # 429 rather than burning one request per remaining handle to learn the
    # same fact six times.
    if _budget["remaining"] == 0 and _budget["reset"]:
        wait = int(_budget["reset"] - time.time()) + 5
        if wait > 0:
            log(f"[budget] exhausted; waiting {wait}s before checking\n")
            time.sleep(wait)
            _budget["remaining"] = None

    bad, errored = [], []
    for i, handle in enumerate(cfg["watchlist"]):
        client = cfg.get("_client_of", {}).get(handle.lower(), handle)
        try:
            tweets = fetch_syndication(handle, count)
            log(f"  OK    @{handle:20} {client:12} {len(tweets)} tweet(s)")
        except Unavailable:
            bad.append(handle)
            log(f"  BAD   @{handle:20} {client:12} empty -- check https://x.com/{handle}")
        except RateLimited as e:
            remaining = cfg["watchlist"][i:]
            errored.extend(remaining)
            log(f"  ERR   @{handle:20} {client:12} {e}")
            log(f"\n[budget] stopping -- {len(remaining) - 1} handle(s) unchecked. "
                "Retry after the window resets; requesting while limited only "
                "pushes it further out.")
            break
        except Exception as e:
            errored.append(handle)
            log(f"  ERR   @{handle:20} {client:12} {e}")
        if i < len(cfg["watchlist"]) - 1:
            time.sleep(1.5)
    if bad:
        log(f"\n{len(bad)} handle(s) resolve to nothing: {', '.join(bad)}")
        log("Fix these in config.json before starting a watch -- they will stay "
            "silently empty otherwise.")
    if errored:
        # Never report success on a handle we could not actually reach; an
        # unchecked handle is not a verified one.
        log(f"\n{len(errored)} handle(s) could not be checked: {', '.join(errored)}")
        log("Not a verdict on the handles -- retry once the errors clear.")
    if not bad and not errored:
        log("\nAll handles resolve.")
    return not bad and not errored


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--backend", choices=["syndication", "twikit"], default="syndication")
    ap.add_argument("--count", type=int, help="tweets to pull per account")
    ap.add_argument("--watch", action="store_true", help="poll on a loop")
    ap.add_argument("--interval", type=int, help="seconds between polls in --watch")
    ap.add_argument("--state", help="JSON file for dedup across restarts (optional)")
    ap.add_argument("--webhook", help="POST each match here (overrides config)")
    ap.add_argument("--emit-dir", dest="emit_dir", nargs="?", const="",
                    help="write each match as JSON for share.py to serve "
                         "(no value = use events_dir from config.json)")
    ap.add_argument("--all", action="store_true", help="emit every tweet, not just matches")
    ap.add_argument(
        "--max-wait", type=int, default=60, dest="max_wait",
        help="one-shot only: seconds to wait out a rate-limit window before "
             "giving up (default 60; --watch always waits the full window)",
    )
    ap.add_argument("--accounts", help="comma-separated handles, overrides config watchlist")
    ap.add_argument("--check-handles", action="store_true", dest="check_handles",
                    help="verify every configured handle resolves, then exit")
    ap.add_argument("--add-tweet", dest="add_tweet", metavar="URL_OR_ID",
                    help="classify and store one tweet by URL or id, then exit")
    ap.add_argument("--search", metavar="TERM",
                    help="search X for a term and flag impersonation (needs cookies.json); "
                         "omit the value to use fraud.watch_terms from config",
                    nargs="?", const="")
    ap.add_argument("--limit", type=int, help="max search results (default from config)")
    ap.add_argument("--check-session", action="store_true", dest="check_session",
                    help="verify cookies.json still authenticates, then exit")
    ap.add_argument("--refresh-endpoints", action="store_true", dest="refresh_endpoints",
                    help="re-read X's live GraphQL query ids, then exit")
    args = ap.parse_args()

    cfg = load_config()
    if args.accounts:
        handles = [h.strip().lstrip("@") for h in args.accounts.split(",") if h.strip()]
        cfg = normalize_config(
            {**cfg, "clients": [{"name": h, "handles": [h]} for h in handles]}
        )
    count = args.count or cfg.get("tweets_per_check", 10)
    interval = args.interval or cfg.get("poll_interval_seconds", 240)
    webhook = args.webhook or cfg.get("webhook_url") or None
    if webhook and "example.com" in webhook:
        webhook = None  # placeholder in config, not a real endpoint

    # Bare --emit-dir means "wherever config.json says", so radar and share
    # stay pointed at the same directory from one setting.
    if args.emit_dir == "":
        args.emit_dir = cfg.get("events_dir", "events")

    if args.refresh_endpoints:
        import x_endpoints
        ops = x_endpoints.refresh(str(COOKIES_PATH))
        changed = x_endpoints.apply(ops)
        log(f"{len(ops)} operations cached; {len(changed)} endpoint(s) were stale.")
        for attr, old, new_id in changed:
            log(f"  {attr:24} {old} -> {new_id}")
        sys.exit(0)

    if args.check_session:
        sys.exit(0 if check_session() else 1)

    if args.search is not None:
        fraud_cfg = cfg.get("fraud") or {}
        terms = ([args.search] if args.search
                 else fraud_cfg.get("watch_terms") or [])
        if not terms:
            log("No search term given and fraud.watch_terms is empty in config.json.")
            sys.exit(1)
        limit = args.limit or fraud_cfg.get("search_limit", 40)
        total = 0
        for term in terms:
            total += len(search_mentions(cfg, term, limit, args.emit_dir))
        log(f"\n{total} flagged across {len(terms)} term(s).")
        sys.exit(0)

    if args.add_tweet:
        sys.exit(0 if add_tweet(cfg, args.add_tweet, args.emit_dir) else 1)

    if args.check_handles:
        sys.exit(0 if check_handles(cfg) else 1)

    seen = load_seen(args.state)

    if not args.watch:
        events = sweep(cfg, args.backend, count, seen, args.all, webhook,
                       args.max_wait, args.emit_dir)
        save_seen(args.state, seen)
        json.dump(events, sys.stdout, indent=2)
        print()
        log(f"\n{len(events)} event(s).")
        return

    log(
        f"Watching {len(cfg['watchlist'])} account(s) via {args.backend}, "
        f"every {interval}s. Ctrl-C to stop."
    )
    if args.backend == "syndication":
        # X allows ~30 requests per 15-minute window per IP, one per account
        # per poll. Say so up front instead of letting it surface as 429s.
        per_window = len(cfg["watchlist"]) * (SYNDICATION_WINDOW / interval)
        if per_window > SYNDICATION_BUDGET:
            floor = int(len(cfg["watchlist"]) * SYNDICATION_WINDOW / SYNDICATION_BUDGET) + 1
            log(
                f"WARNING: {len(cfg['watchlist'])} accounts every {interval}s is "
                f"~{per_window:.0f} requests per 15min, over X's ~{SYNDICATION_BUDGET} "
                f"budget. Use --interval {floor} or higher, or --backend twikit."
            )
    if not args.state:
        log("(no --state: dedup is in-memory, a restart may re-alert recent tweets)")
    # First pass primes `seen` so we don't alert on the existing backlog.
    log("\n-- priming (backlog will not alert) --")
    sweep(cfg, args.backend, count, seen, False, None, WINDOW_WAIT)
    save_seen(args.state, seen)

    while True:
        try:
            time.sleep(interval)
            log(f"\n-- {datetime.now().strftime('%H:%M:%S')} --")
            for event in sweep(cfg, args.backend, count, seen, args.all, webhook,
                               WINDOW_WAIT, args.emit_dir):
                json.dump(event, sys.stdout)
                print(flush=True)
            save_seen(args.state, seen)
        except KeyboardInterrupt:
            log("\nstopped.")
            save_seen(args.state, seen)
            return


if __name__ == "__main__":
    main()
