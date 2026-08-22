"""Fraud scoring for tweets that mention you but don't come from you.

Discovery (finding the mentions) needs an authenticated search -- see
radar.py --search. This module is the analysis half: given a tweet, decide
how much it looks like someone impersonating a brand rather than the brand
posting.

Signals, roughly in order of how much they matter:

  handle look-alike   @m00npay / @moonpay_official / @moonpayHQ
  unverified author   real brands carry verified_type Business or Government
  wallet action       "connect wallet", "seed phrase" -- the actual payload
  off-domain link     claims your campaign, links somewhere that isn't yours
  airdrop / urgency   the wrapper the payload arrives in
"""

import re
from urllib.parse import urlparse

# Characters swapped to make a handle look right at a glance.
CONFUSABLES = str.maketrans({
    "0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t",
    "$": "s", "|": "l", "!": "i",
})


def normalize_handle(handle):
    """Fold a handle to its visually-equivalent skeleton.

    @m00npay, @M0onPay_ and @moonpay all reduce to "moonpay", which is what
    makes look-alikes detectable without a fuzzy-match library.
    """
    h = (handle or "").lower().strip().lstrip("@")
    h = h.translate(CONFUSABLES)
    return re.sub(r"[^a-z]", "", h)


# Words impersonators append to a real brand name to look official.
OFFICIAL_SUFFIXES = (
    "official", "support", "help", "team", "hq", "app", "global", "announcements",
    "news", "airdrop", "rewards", "claim", "giveaway", "eth", "sol", "bot",
)


def handle_verdict(author, official_handles):
    """Compare an author handle against the brand's real handles."""
    author_norm = normalize_handle(author)
    official_norm = {normalize_handle(h): h for h in official_handles}

    if author_norm in official_norm and (author or "").lower().lstrip("@") in {
        h.lower() for h in official_handles
    }:
        return {"kind": "official", "weight": 0, "detail": None}

    # Same skeleton, different actual string -> confusable substitution.
    if author_norm in official_norm:
        return {"kind": "lookalike", "weight": 6,
                "detail": f"@{author} reduces to the same text as @{official_norm[author_norm]}"}

    # Brand name plus an official-sounding suffix or prefix.
    for norm, real in official_norm.items():
        if norm and norm in author_norm and author_norm != norm:
            extra = author_norm.replace(norm, "")
            if any(s in extra for s in OFFICIAL_SUFFIXES):
                return {"kind": "lookalike", "weight": 6,
                        "detail": f"@{author} wraps @{real} in official-sounding wording"}
            return {"kind": "contains_brand", "weight": 3,
                    "detail": f"@{author} contains @{real}"}

    return {"kind": "unrelated", "weight": 0, "detail": None}


def domain_of(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def link_verdict(urls, official_domains):
    """Links that aren't yours, on a tweet claiming your campaign."""
    official = {d.lower().lstrip(".") for d in official_domains}
    offsite = []
    for url in urls:
        host = domain_of(url)
        if not host or host in ("t.co", "x.com", "twitter.com"):
            continue
        if any(host == d or host.endswith("." + d) for d in official):
            continue
        offsite.append(host)
    return sorted(set(offsite))


def assess(tweet, verdict, cfg):
    """Combine author, verification, language and link signals into a score.

    `verdict` is the output of radar.classify for the same tweet -- the promo
    classifier already found the airdrop/wallet/urgency language, so this
    reuses it rather than re-matching.
    """
    fraud_cfg = cfg.get("fraud") or {}
    official_handles = set(fraud_cfg.get("official_handles") or [])
    # Every client handle is legitimate too -- a partner posting about you is
    # not an impersonator.
    official_handles |= set(cfg.get("_client_of", {}))
    official_domains = fraud_cfg.get("official_domains") or []

    author = tweet.get("author") or ""
    signals, score = [], 0

    hv = handle_verdict(author, official_handles)
    if hv["kind"] == "official":
        return {"is_impersonation": False, "fraud_score": 0, "fraud_risk": "none",
                "fraud_signals": [{"kind": "official_account",
                                   "detail": f"@{author} is a known account"}]}
    if hv["weight"]:
        score += hv["weight"]
        signals.append({"kind": hv["kind"], "detail": hv["detail"], "weight": hv["weight"]})

    kind = tweet.get("author_verified_type")
    if not kind:
        score += 3
        signals.append({"kind": "unverified", "weight": 3,
                        "detail": "author carries no verification"})
    elif kind == "Blue":
        score += 1
        signals.append({"kind": "blue_only", "weight": 1,
                        "detail": "Blue check only -- purchasable, not a brand badge"})

    categories = set(verdict.get("categories") or [])
    if "wallet_action" in categories and "scam_warning" not in categories:
        score += 5
        signals.append({"kind": "wallet_action", "weight": 5,
                        "detail": "asks for a wallet connection or key"})
    if categories & {"airdrop", "giveaway", "presale"}:
        score += 3
        signals.append({"kind": "incentive", "weight": 3,
                        "detail": "offers an airdrop, giveaway or presale"})
    if "urgency" in categories:
        score += 2
        signals.append({"kind": "urgency", "weight": 2, "detail": "time pressure"})

    urls = [u.get("expanded_url") or u.get("url")
            for u in (tweet.get("entities") or {}).get("urls", [])]
    offsite = link_verdict([u for u in urls if u], official_domains)
    if offsite and (categories & {"airdrop", "giveaway", "presale", "promo"}):
        score += 5
        signals.append({"kind": "offsite_link", "weight": 5,
                        "detail": "campaign links to " + ", ".join(offsite)})

    # Being unverified and mentioning a presale is ordinary promotion, not
    # impersonation -- plenty of real projects use a payment provider and say
    # so. Impersonation needs a signal that someone is pretending to BE the
    # brand (a look-alike handle) or is harvesting credentials (a wallet
    # action). Everything else only amplifies those.
    core_kinds = {"lookalike", "contains_brand", "wallet_action"}
    has_core = any(sig["kind"] in core_kinds for sig in signals)

    risk = "high" if score >= 10 else "medium" if score >= 6 else "low"
    if not has_core:
        risk = "low"
    return {
        "is_impersonation": has_core and score >= 6,
        "promotional_mention": bool(not has_core and score >= 6),
        "fraud_score": score,
        "fraud_risk": risk,
        "fraud_signals": signals,
    }
