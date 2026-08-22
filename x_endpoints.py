"""Keep twikit's GraphQL query ids in step with X's live web client.

X embeds an operationName -> queryId table in its logged-in `main.*.js`
bundle and rotates the ids periodically. twikit ships them hardcoded, so a
rotation turns every affected call into a 404 with no useful error.

This reads the live table using your session, caches it, and patches twikit's
Endpoint constants at import time. When X rotates again, re-run the refresh
instead of waiting for a twikit release.

    python radar.py --refresh-endpoints    # re-read from X, update the cache
"""

import json
import re
from pathlib import Path

HERE = Path(__file__).parent
CACHE_PATH = HERE / "x_endpoints.json"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# twikit Endpoint attribute -> X operationName
ENDPOINT_MAP = {
    "SEARCH_TIMELINE": "SearchTimeline",
    "USER_BY_SCREEN_NAME": "UserByScreenName",
    "USER_BY_REST_ID": "UserByRestId",
    "USER_TWEETS": "UserTweets",
    "USER_TWEETS_AND_REPLIES": "UserTweetsAndReplies",
    "USER_MEDIA": "UserMedia",
    "TWEET_DETAIL": "TweetDetail",
    "TWEET_RESULT_BY_REST_ID": "TweetResultByRestId",
    "RETWEETERS": "Retweeters",
    "FAVORITERS": "Favoriters",
}

_OPS_PATTERNS = (
    re.compile(r'queryId:"([\w-]{15,})",operationName:"(\w+)"'),
    re.compile(r'operationName:"(\w+)",queryId:"([\w-]{15,})"'),
)


def fetch_live_operations(cookies_path):
    """Read the operationName -> queryId table out of X's logged-in bundle.

    Needs a session: the logged-out page serves a different, much smaller
    bundle that carries no GraphQL operations at all.
    """
    import httpx

    cookies = json.loads(Path(cookies_path).read_text())
    jar = {k: v for k, v in cookies.items() if k in ("auth_token", "ct0")}
    if not jar.get("auth_token"):
        raise RuntimeError("cookies.json has no auth_token")

    headers = {"User-Agent": UA}
    home = httpx.get("https://x.com/home", headers=headers, cookies=jar,
                     follow_redirects=True, timeout=30)
    home.raise_for_status()

    scripts = re.findall(r'https://abs\.twimg\.com/[^"\']+?main\.[\w]+\.js', home.text)
    if not scripts:
        raise RuntimeError(
            "no main.js in the page -- the session may be logged out "
            "(a logged-out page has no GraphQL table)"
        )

    bundle = httpx.get(scripts[0], headers=headers, timeout=60)
    bundle.raise_for_status()

    # The two orderings are separate patterns, so group positions are known.
    ops = {}
    for qid, name in _OPS_PATTERNS[0].findall(bundle.text):
        ops[name] = qid
    for name, qid in _OPS_PATTERNS[1].findall(bundle.text):
        ops[name] = qid
    if not ops:
        raise RuntimeError("found main.js but no GraphQL operations inside it")
    return ops


def refresh(cookies_path="cookies.json"):
    ops = fetch_live_operations(cookies_path)
    CACHE_PATH.write_text(json.dumps(ops, indent=1, sort_keys=True))
    return ops


def load_cached():
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except (ValueError, OSError):
            return {}
    return {}


def apply(ops=None, log=print):
    """Point twikit's Endpoint constants at the live query ids.

    Returns the list of endpoints actually changed, so callers can say
    whether anything was stale rather than claiming success blindly.
    """
    ops = ops or load_cached()
    if not ops:
        return []

    from twikit.client.gql import Endpoint

    changed = []
    for attr, operation in ENDPOINT_MAP.items():
        qid = ops.get(operation)
        if not qid or not hasattr(Endpoint, attr):
            continue
        current = getattr(Endpoint, attr)
        updated = Endpoint.url(f"{qid}/{operation}")
        if current != updated:
            setattr(Endpoint, attr, updated)
            changed.append((attr, current.rsplit("/", 2)[-2], qid))
    return changed


# X moved the ondemand.s hash out of a plain "ondemand.s":"<hash>" pair and
# into webpack's chunk tables: one maps chunk id -> name, another chunk id ->
# hash. twikit still looks for the old pair, so it never finds the file that
# holds the key-byte indices. The file itself is unchanged -- twikit's index
# regex matches it fine once you can locate it.
CHUNK_NAME_RE = re.compile(r'(\d{3,7}):"ondemand\.s"')


def _ondemand_url(page_html):
    named = CHUNK_NAME_RE.search(page_html)
    if not named:
        return None
    chunk_id = named.group(1)
    hashed = re.search(rf'{chunk_id}:"([\w-]+)"', page_html.replace(named.group(0), ""))
    if not hashed:
        return None
    return ("https://abs.twimg.com/responsive-web/client-web/"
            f"ondemand.s.{hashed.group(1)}a.js")


def patch_transaction(cookies_path="cookies.json"):
    """Repair twikit's transaction-id derivation instead of stubbing it.

    X enforces X-Client-Transaction-Id on some endpoints (search among them)
    while ignoring it on others, so a stub gets you a bare 404 with no clue
    why. This locates the ondemand chunk the new way and lets twikit's own
    maths run unchanged.
    """
    import httpx
    from twikit.x_client_transaction.transaction import (
        ClientTransaction, INDICES_REGEX,
    )
    import bs4

    cookies = json.loads(Path(cookies_path).read_text())
    jar = {k: v for k, v in cookies.items() if k in ("auth_token", "ct0")}

    async def _init(self, session, headers):
        page = httpx.get("https://x.com/home", headers={"User-Agent": UA},
                         cookies=jar, follow_redirects=True, timeout=30)
        html = page.text
        self.home_page_response = bs4.BeautifulSoup(html, "lxml")

        url = _ondemand_url(html)
        if not url:
            raise RuntimeError(
                "could not locate the ondemand.s chunk -- X changed its bundle "
                "layout again, or the session is logged out"
            )
        js = httpx.get(url, headers={"User-Agent": UA}, timeout=30).text
        indices = [int(m.group(2)) for m in INDICES_REGEX.finditer(js)]
        if not indices:
            raise RuntimeError(f"no key-byte indices in {url}")

        self.DEFAULT_ROW_INDEX, self.DEFAULT_KEY_BYTES_INDICES = indices[0], indices[1:]
        self.key = self.get_key(response=self.home_page_response)
        self.key_bytes = self.get_key_bytes(key=self.key)
        self.animation_key = self.get_animation_key(
            key_bytes=self.key_bytes, response=self.home_page_response)

    ClientTransaction.init = _init
