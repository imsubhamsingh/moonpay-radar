"""Search X for a term and return tweets in the shape radar/fraud expect.

twikit is used only as transport -- it handles the session, the CSRF header
and the X-Client-Transaction-Id maths, which are genuinely fiddly. Its object
model is not used: X reshaped the user payload (screen_name moved to
core.screen_name, verified to verification.verified, and so on) and twikit
still expects the old flat `legacy` user, so it parses live responses as zero
results. Parsing the raw GraphQL here keeps that breakage out of the way.

Requires cookies.json. X has no unauthenticated search.
"""

import html

import x_endpoints


def _verified_kind(user):
    """Normalize X's several verification flags into one label.

    Same trap as the syndication endpoint: brand accounts come back with
    verified=False and is_blue_verified=False but a verified_type of
    Business, so checking only the booleans marks real brands unverified --
    exactly backwards for telling a brand from an impersonator.
    """
    kind = user.get("verified_type")
    if not kind:
        verification = user.get("verification") or {}
        kind = verification.get("verified_type")
        if not kind and verification.get("verified"):
            kind = "Legacy"
    if not kind and user.get("is_blue_verified"):
        kind = "Blue"
    return kind


def _normalize(tweet):
    """One GraphQL tweet result -> the dict radar.classify/fraud.assess want."""
    legacy = tweet.get("legacy") or {}
    user = (tweet.get("core", {}).get("user_results", {}).get("result", {})) or {}
    user_core = user.get("core") or {}

    text = legacy.get("full_text") or ""
    # X appends the media permalink to full_text; display_text_range marks the
    # part meant to be shown, in UTF-16 code units (an emoji counts as 2).
    rng = legacy.get("display_text_range")
    if isinstance(rng, list) and len(rng) == 2:
        units = text.encode("utf-16-le")
        text = units[rng[0] * 2 : rng[1] * 2].decode("utf-16-le", "ignore")

    kind = _verified_kind(user)
    screen_name = user_core.get("screen_name") or user.get("screen_name")

    return {
        "id": legacy.get("id_str") or tweet.get("rest_id"),
        "text": html.unescape(text),
        "created_at": legacy.get("created_at"),
        "author": screen_name,
        "author_name": html.unescape(user_core.get("name") or ""),
        "author_avatar": (user.get("avatar") or {}).get("image_url"),
        "author_created_at": user_core.get("created_at"),
        "author_verified": bool(kind),
        "author_verified_type": kind,
        "favorite_count": legacy.get("favorite_count"),
        "reply_count": legacy.get("reply_count"),
        "entities": legacy.get("entities") or {},
    }


def _collect(node, out, seen):
    """Pull every Tweet result out of the response, whatever the nesting."""
    if isinstance(node, dict):
        if node.get("__typename") == "Tweet" and "legacy" in node:
            tweet = _normalize(node)
            if tweet["id"] and tweet["id"] not in seen:
                seen.add(tweet["id"])
                out.append(tweet)
        for value in node.values():
            _collect(value, out, seen)
    elif isinstance(node, list):
        for value in node:
            _collect(value, out, seen)


def _cursor(node):
    if isinstance(node, dict):
        if node.get("cursorType") == "Bottom" and node.get("value"):
            return node["value"]
        for value in node.values():
            found = _cursor(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _cursor(value)
            if found:
                return found
    return None


async def search(query, limit=40, cookies_path="cookies.json", product="Latest",
                 log=print):
    """Return up to `limit` tweets matching `query`, newest first."""
    import asyncio

    from twikit import Client

    x_endpoints.patch_transaction(cookies_path)
    changed = x_endpoints.apply()
    if changed:
        log(f"[endpoints] refreshed {len(changed)} stale query id(s)")

    client = Client("en-US")
    client.load_cookies(cookies_path)

    tweets, seen, cursor = [], set(), None
    while len(tweets) < limit:
        raw, _ = await client.gql.search_timeline(query, product, 20, cursor)
        if raw.get("errors"):
            raise RuntimeError(f"search error: {raw['errors'][:1]}")

        before = len(tweets)
        _collect(raw, tweets, seen)
        if len(tweets) == before:
            break  # nothing new -> end of results

        cursor = _cursor(raw)
        if not cursor:
            break
        await asyncio.sleep(2)

    return tweets[:limit]
