"""Fetch a single tweet by ID from X's syndication endpoint. No API key.

This is the same endpoint the `react-tweet` library wraps, called directly so
nothing depends on a third-party deployment staying up.

    from tweet_api import get_tweet
    t = get_tweet("2090952173177676237")

Hydration only: it resolves an ID you already have and cannot list an
account's tweets. Discovery still comes from radar.py's timeline poll.
"""

import html
import json
import math
import urllib.error
import urllib.parse
import urllib.request

DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
ENDPOINT = "https://cdn.syndication.twimg.com/tweet-result"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _js_number_to_string(value, radix=36):
    """Port of V8's DoubleToRadixCString — JS `Number.prototype.toString(36)`.

    Python has no stdlib equivalent for a float in an arbitrary base, and the
    token below is whatever JS would have printed, so this has to match V8
    digit for digit. The delta term is how V8 decides when to stop emitting
    fractional digits: once the remainder is below half an ULP, further digits
    carry no information.
    """
    if value != value or value in (math.inf, -math.inf):  # NaN / Infinity
        return "NaN" if value != value else "Infinity"

    negative = value < 0
    value = abs(value)
    integer, fraction = math.floor(value), value - math.floor(value)

    delta = max(0.5 * (math.nextafter(value, math.inf) - value), math.ulp(0.0))

    out = ""
    if fraction >= delta:
        out += "."
        while True:
            fraction *= radix
            delta *= radix
            digit = int(fraction)
            out += DIGITS[digit]
            fraction -= digit
            # Round half up, ties to even -- and propagate the carry back
            # through the digits already emitted.
            if fraction > 0.5 or (fraction == 0.5 and (digit & 1)):
                if fraction + delta > 1:
                    carried = ""
                    for ch in reversed(out):
                        if ch == ".":
                            carried = "." + carried
                            continue
                        pos = DIGITS.index(ch) + 1
                        if pos < radix:
                            carried = DIGITS[pos] + carried
                            break
                        carried = "0" + carried
                    else:  # carry ran off the front: bump the integer part
                        integer += 1
                    out = out[: len(out) - len(carried)] + carried
                    break
            if fraction < delta:
                break

    head = ""
    if integer == 0:
        head = "0"
    while integer > 0:
        head = DIGITS[integer % radix] + head
        integer //= radix

    return ("-" if negative else "") + head + out


def token_for(tweet_id):
    """The `token` query param, derived from the ID itself -- not a secret."""
    n = (int(tweet_id) / 1e15) * math.pi
    return _js_number_to_string(n, 36).replace(".", "").replace("0", "")


def normalize(raw, tweet_id):
    """Flatten the syndication payload into the shape the radar already uses.

    A deleted or withheld tweet comes back as a TweetTombstone with a 200, not
    a 404. That is worth surfacing rather than dropping: a promo that vanishes
    shortly after posting is itself a fraud signal.
    """
    if raw is None:
        return {"available": False, "id": str(tweet_id), "reason": "not found"}

    if raw.get("__typename") == "TweetTombstone":
        reason = raw.get("tombstone", {}).get("text", {}).get("text", "unavailable")
        return {"available": False, "id": str(tweet_id), "reason": reason}

    # X appends the media/quote permalink to `text`; display_text_range marks
    # the part meant to be shown. Its indices are UTF-16 code units (JS string
    # offsets), so an emoji counts as 2 -- slice in UTF-16 space, not code points.
    text = raw.get("text", "")
    rng = raw.get("display_text_range")
    if isinstance(rng, list) and len(rng) == 2:
        units = text.encode("utf-16-le")
        text = units[rng[0] * 2 : rng[1] * 2].decode("utf-16-le", "ignore")

    user = raw.get("user") or {}
    kind = user.get("verified_type")
    if not kind and user.get("is_blue_verified"):
        kind = "Blue"
    elif not kind and user.get("verified"):
        kind = "Legacy"

    return {
        "available": True,
        "id": raw.get("id_str", str(tweet_id)),
        # Same escaping trap as the timeline endpoint: "&amp;" would break
        # both the classifier and the rendered page.
        "text": html.unescape(text),
        "created_at": raw.get("created_at"),
        "author": user.get("screen_name"),
        "author_name": html.unescape(user.get("name") or ""),
        "author_avatar": user.get("profile_image_url_https"),
        "author_verified": bool(kind),
        "author_verified_type": kind,
        "favorite_count": raw.get("favorite_count"),
        "reply_count": raw.get("conversation_count"),
        "entities": raw.get("entities") or {},
        "media": [
            {"url": m.get("media_url_https"), "type": m.get("type")}
            for m in (raw.get("mediaDetails") or [])
        ],
        "url": f"https://x.com/{user.get('screen_name')}/status/{raw.get('id_str', tweet_id)}",
    }


def get_tweet(tweet_id, lang="en"):
    """Returns a normalized dict; check `available` before using tweet fields."""
    query = urllib.parse.urlencode(
        {"id": str(tweet_id), "token": token_for(tweet_id), "lang": lang}
    )
    req = urllib.request.Request(
        f"{ENDPOINT}?{query}", headers={"User-Agent": UA, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raw = None
        else:
            raise RuntimeError(f"tweet {tweet_id}: HTTP {e.code}") from e
    return normalize(raw, tweet_id)
