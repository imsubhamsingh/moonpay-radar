"""Shareable event pages for radar hits -- the ScoutQuest share-message pattern.

    python share.py                  # serves on http://localhost:8800

    /                                index of stored events
    /share?id=<event_id>             one event: verdict + the tweet

Events are JSON files in events/ (no database). radar.py --emit-dir events/
writes them; this only reads.

The tweet is hydrated server-side through tweet_api and rendered as plain
HTML, so the page carries no third-party JavaScript and doesn't depend on
anyone else's deployment staying online.
"""

import html
import json
import re
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import tweet_api

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


CFG = load_config()
EVENTS_DIR = HERE / CFG.get("events_dir", "events")
PORT = CFG.get("share_port", 8800)
BRAND = CFG.get("brand", "MoonPay Radar")

RISK_COLOR = {"high": "#ff4757", "medium": "#ffa502", "low": "#4a5568"}

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0f1115;color:#e6e9ef;
 font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
a{color:#5aa9ff;text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:640px;margin:0 auto;padding:28px 18px 60px}
.brand{display:flex;align-items:center;gap:8px;font-weight:700;letter-spacing:-.2px;
 margin-bottom:22px;color:#9aa4b2;font-size:14px}
.dot{width:9px;height:9px;border-radius:50%;background:#5aa9ff}
.card{background:#171a21;border:1px solid #242833;border-radius:14px;padding:18px;margin-bottom:16px}
.badge{display:inline-block;padding:3px 11px;border-radius:999px;font-size:12px;
 font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#fff}
.meta{color:#7c8698;font-size:13px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
.chip{background:#222735;border:1px solid #2f3545;border-radius:6px;
 padding:3px 9px;font-size:12px;color:#b8c1d1}
.chip b{color:#fff;font-weight:600}
h1{font-size:19px;margin:12px 0 6px;letter-spacing:-.3px}
.tweet{background:#fff;color:#0f1419;border-radius:14px;padding:16px;margin-top:4px}
.tweet .hdr{display:flex;align-items:center;gap:10px;margin-bottom:11px}
.tweet img.pfp{width:44px;height:44px;border-radius:50%}
.tweet .nm{font-weight:700;display:flex;align-items:center;gap:4px;line-height:1.2}
.tweet .hd{color:#536471;font-size:14px}
.tweet .body{font-size:16px;white-space:pre-wrap;word-wrap:break-word}
.tweet .foot{color:#536471;font-size:13px;margin-top:12px;
 border-top:1px solid #eff3f4;padding-top:10px;display:flex;gap:16px}
.tweet img.media{width:100%;border-radius:12px;margin-top:12px;border:1px solid #eff3f4}
.vt{width:16px;height:16px;flex:none}
.urls{margin-top:14px;padding-top:12px;border-top:1px solid #242833}
.urls div{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
 color:#8fd19e;word-break:break-all;margin-top:4px}
.gone{background:#241a1a;border:1px solid #4a2a2a;color:#ff8f8f;
 border-radius:10px;padding:14px;font-size:14px}
.row{display:flex;justify-content:space-between;align-items:center;gap:12px}
.idx{display:block;padding:13px 15px;border-bottom:1px solid #242833;color:#e6e9ef}
.idx:last-child{border-bottom:0}.idx:hover{background:#1c202a;text-decoration:none}
.empty{color:#7c8698;padding:26px;text-align:center}
.stats{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap}
.stat{background:#171a21;border:1px solid #242833;border-radius:10px;
 padding:10px 14px;flex:1;min-width:96px}
a.stat{text-decoration:none;color:inherit}
.stat b{display:block;font-size:21px;letter-spacing:-.5px}
.stat span{color:#7c8698;font-size:11px;text-transform:uppercase;letter-spacing:.6px}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px}
.f{background:#171a21;border:1px solid #242833;border-radius:999px;
 padding:5px 12px;font-size:12px;color:#b8c1d1}
.f:hover{border-color:#3a4459}
.f.on{background:#5aa9ff;border-color:#5aa9ff;color:#08101c;font-weight:700}
.client{margin-bottom:18px}
.chead{display:flex;align-items:baseline;gap:9px;margin:0 2px 8px}
.chead h2{font-size:15px;margin:0;letter-spacing:-.2px}
.chead span{color:#7c8698;font-size:12px}
.cat{display:inline-block;background:#1e2a3d;color:#7fb5ff;border-radius:5px;
 padding:2px 7px;font-size:11px;font-weight:600;margin-right:5px}
"""

VERIFIED_SVG = (
    '<svg class="vt" viewBox="0 0 22 22" fill="%s" aria-label="Verified">'
    '<path d="M20.396 11c-.018-.646-.215-1.275-.57-1.816-.354-.54-.852-.972-1.438-1.246.223-.607.27-1.264.14-1.897'
    "-.131-.634-.437-1.218-.882-1.687-.47-.445-1.053-.75-1.687-.882-.633-.13-1.29-.083-1.897.14-.273-.587-.704-1.086"
    "-1.245-1.44S11.647 1.62 11 1.604c-.646.017-1.273.213-1.813.568s-.969.854-1.24 1.44c-.608-.223-1.267-.272-1.902-.14"
    "-.635.13-1.22.436-1.69.882-.445.47-.749 1.055-.878 1.688-.13.633-.08 1.29.144 1.896-.587.274-1.087.705-1.443 1.245"
    'C1.822 9.725 1.624 10.354 1.604 11c.02.646.218 1.276.573 1.817.356.54.856.972 1.443 1.245-.224.606-.274 1.263-.144 1.896'
    ".13.634.433 1.218.877 1.688.47.443 1.054.747 1.687.878.633.132 1.29.084 1.897-.136.274.586.705 1.084 1.246 1.439"
    ".54.354 1.17.551 1.816.569.647-.016 1.276-.213 1.817-.567s.972-.854 1.245-1.44c.604.239 1.266.296 1.903.164.636-.132"
    '1.22-.447 1.68-.907.46-.46.776-1.044.908-1.681s.075-1.299-.165-1.903c.586-.274 1.084-.705 1.439-1.246.354-.54.551-1.17.569-1.816zM9.662 '
    '14.85l-3.429-3.428 1.293-1.302 2.072 2.072 4.4-4.794 1.347 1.246z"/></svg>'
)


def esc(t):
    return html.escape(t or "", quote=True)


def linkify(text, entities):
    """Escape first, then swap t.co links for their expanded targets."""
    out = esc(text)
    for u in (entities or {}).get("urls", []):
        short, full = u.get("url"), u.get("expanded_url") or u.get("url")
        if short:
            out = out.replace(
                esc(short),
                f'<a href="{esc(full)}" rel="nofollow noopener noreferrer" '
                f'target="_blank">{esc(u.get("display_url") or full)}</a>',
            )
    out = re.sub(r"(^|\s)@(\w{1,15})",
                 r'\1<a href="https://x.com/\2" target="_blank">@\2</a>', out)
    out = re.sub(r"(^|\s)#(\w+)",
                 r'\1<a href="https://x.com/hashtag/\2" target="_blank">#\2</a>', out)
    return out


def render_tweet(t):
    if not t.get("available"):
        return (f'<div class="gone"><b>Tweet unavailable</b><br>{esc(t.get("reason"))}'
                "<br><br>A promo that disappears shortly after posting is itself "
                "worth flagging.</div>")

    badge = ""
    if t.get("author_verified"):
        color = {"Business": "#e2b719", "Government": "#829aab"}.get(
            t.get("author_verified_type"), "#1d9bf0")
        badge = VERIFIED_SVG % color

    when = ""
    if t.get("created_at"):
        try:
            when = datetime.fromisoformat(
                t["created_at"].replace("Z", "+00:00")).strftime("%-I:%M %p · %b %-d, %Y")
        except ValueError:
            when = esc(t["created_at"])

    media = "".join(
        f'<img class="media" src="{esc(m["url"])}" alt="">'
        for m in t.get("media", []) if m.get("url") and m.get("type") == "photo")

    pfp = (f'<img class="pfp" src="{esc(t["author_avatar"])}" alt="">'
           if t.get("author_avatar") else "")

    return f"""<div class="tweet">
  <div class="hdr">{pfp}
    <div><div class="nm">{esc(t.get('author_name'))}{badge}</div>
    <div class="hd">@{esc(t.get('author'))}</div></div>
  </div>
  <div class="body">{linkify(t.get('text'), t.get('entities'))}</div>
  {media}
  <div class="foot"><span>{when}</span>
    <span>&hearts; {t.get('favorite_count') or 0}</span>
    <span><a href="{esc(t.get('url'))}" target="_blank" rel="noopener">Read on X</a></span>
  </div>
</div>"""


def render_event(ev):
    tweet = tweet_api.get_tweet(ev["tweet_id"])
    risk = ev.get("risk", "low")
    chips = "".join(
        f'<span class="chip"><b>{esc(s["category"])}</b> {esc(s["match"])}</span>'
        for s in ev.get("signals", []))
    urls = "".join(f"<div>{esc(u)}</div>"
                   for u in (ev.get("entities") or {}).get("urls", []))
    urls_block = (f'<div class="urls"><span class="meta">Official links in this post '
                  f"&mdash; anything claiming this campaign on another domain is "
                  f"suspect</span>{urls}</div>" if urls else "")

    return f"""<div class="card">
  <div class="row">
    <span class="badge" style="background:{RISK_COLOR.get(risk,'#4a5568')}">{esc(risk)} risk</span>
    <span class="meta">score {ev.get('score')}</span>
  </div>
  <h1>@{esc(ev.get('account'))} &middot; {esc(', '.join(ev.get('categories') or []) or 'no category')}</h1>
  <div class="meta">Detected {esc((ev.get('detected_at') or '')[:19].replace('T',' '))} UTC</div>
  <div class="chips">{chips}</div>
  {urls_block}
</div>
{render_tweet(tweet)}"""


def dashboard(query):
    """Promo-first: grouped by client, filterable by category or risk.

    Impersonation candidates get their own view. They come from searching all
    of X rather than from polling the watchlist, so there are many more of
    them and they would bury the client timeline if mixed in.
    """
    all_events = load_events()
    fraud_events = [e for e in all_events if e.get("is_impersonation")]
    events = [e for e in all_events if not e.get("is_impersonation")]
    if (query.get("view") or [""])[0] == "fraud":
        return fraud_view(fraud_events, len(events))

    want_cat = (query.get("category") or [""])[0]
    want_risk = (query.get("risk") or [""])[0]

    shown = [
        e for e in events
        if (not want_cat or want_cat in (e.get("categories") or []))
        and (not want_risk or e.get("risk") == want_risk)
    ]

    if not events:
        return ('<div class="card"><div class="empty">No events yet.<br><br>'
                "Run <code>uv run radar.py --emit-dir</code> to populate."
                "</div></div>")

    clients = sorted({e.get("client") or e.get("account") for e in events})
    high = sum(1 for e in events if e.get("risk") == "high")
    fraud_tile = (
        f'<a class="stat" href="/?view=fraud" style="border-color:#4a2a2a">'
        f'<b style="color:#ff6b6b">{len(fraud_events)}</b>'
        f'<span>impersonation</span></a>'
        if fraud_events else
        '<div class="stat"><b>0</b><span>impersonation</span></div>'
    )
    stats = f"""<div class="stats">
      <div class="stat"><b>{len(events)}</b><span>client events</span></div>
      <div class="stat"><b>{len(clients)}</b><span>clients</span></div>
      <div class="stat"><b>{high}</b><span>high risk</span></div>
      {fraud_tile}
    </div>"""

    # Filter chips: every category actually present, plus a risk cut.
    cats = sorted({c for e in events for c in (e.get("categories") or [])})
    chips = [f'<a class="f{" on" if not want_cat and not want_risk else ""}" href="/">All</a>']
    chips += [
        f'<a class="f{" on" if want_cat == c else ""}'
        f'" href="/?category={urllib.parse.quote(c)}">{esc(c)}'
        f' {sum(1 for e in events if c in (e.get("categories") or []))}</a>'
        for c in cats
    ]
    if high:
        chips.append(f'<a class="f{" on" if want_risk == "high" else ""}'
                     f'" href="/?risk=high">high risk {high}</a>')
    filters = f'<div class="filters">{"".join(chips)}</div>'

    if not shown:
        return stats + filters + '<div class="card"><div class="empty">Nothing matches that filter.</div></div>'

    blocks = []
    for client in clients:
        mine = [e for e in shown if (e.get("client") or e.get("account")) == client]
        if not mine:
            continue
        rows = "".join(
            f'<a class="idx" href="/share?id={esc(e["tweet_id"])}">'
            f'<div class="row"><span>'
            + "".join(f'<span class="cat">{esc(c)}</span>' for c in (e.get("categories") or []))
            + f'<span class="meta">@{esc(e.get("account"))}</span></span>'
            f'<span class="badge" style="background:{RISK_COLOR.get(e.get("risk"),"#4a5568")}">'
            f'{esc(e.get("risk"))}</span></div>'
            f'<div class="meta" style="margin-top:5px">{esc((e.get("text") or "")[:120])}</div></a>'
            for e in mine)
        blocks.append(
            f'<div class="client"><div class="chead"><h2>{esc(client)}</h2>'
            f'<span>{len(mine)} event{"s" if len(mine) != 1 else ""}</span></div>'
            f'<div class="card" style="padding:0">{rows}</div></div>')

    return stats + filters + "".join(blocks)


def fraud_view(fraud_events, promo_count):
    """Impersonation candidates, grouped by the account posting them.

    Grouped by author rather than listed flat because these arrive in
    clusters: one operator posting near-identical copy from several handles.
    """
    back = (f'<div class="filters"><a class="f" href="/">&larr; back to '
            f'{promo_count} client event(s)</a></div>')
    if not fraud_events:
        return back + ('<div class="card"><div class="empty">No impersonation '
                       'candidates.<br><br>Run <code>uv run radar.py --search</code>'
                       '</div></div>')

    by_author = {}
    for e in fraud_events:
        by_author.setdefault(e.get("account") or "?", []).append(e)

    high = sum(1 for e in fraud_events if e.get("fraud_risk") == "high")
    stats = f"""<div class="stats">
      <div class="stat"><b style="color:#ff6b6b">{len(fraud_events)}</b><span>candidates</span></div>
      <div class="stat"><b>{len(by_author)}</b><span>accounts</span></div>
      <div class="stat"><b>{high}</b><span>high risk</span></div>
    </div>"""

    warn = ('<div class="card" style="border-color:#4a3a2a;background:#241f1a">'
            '<span class="meta">Leads, not verdicts &mdash; scored by pattern '
            'matching and not confirmed. Review before acting on any of these.'
            '</span></div>')

    blocks = []
    for author, items in sorted(by_author.items(), key=lambda kv: -len(kv[1])):
        rows = "".join(
            f'<a class="idx" href="/share?id={esc(e["tweet_id"])}">'
            f'<div class="row"><span>'
            + "".join(f'<span class="cat">{esc(sig["kind"])}</span>'
                      for sig in (e.get("fraud_signals") or []))
            + f'</span><span class="badge" style="background:'
            f'{RISK_COLOR.get(e.get("fraud_risk"), "#4a5568")}">'
            f'{esc(e.get("fraud_risk"))} {e.get("fraud_score")}</span></div>'
            f'<div class="meta" style="margin-top:5px">'
            f'{esc((e.get("text") or "")[:120])}</div></a>'
            for e in items)
        blocks.append(
            f'<div class="client"><div class="chead"><h2>@{esc(author)}</h2>'
            f'<span>{len(items)} post{"s" if len(items) != 1 else ""}</span></div>'
            f'<div class="card" style="padding:0">{rows}</div></div>')

    return back + stats + warn + "".join(blocks)


def page(title, body):
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>{CSS}</style></head><body><div class="wrap">
<div class="brand"><span class="dot"></span> {esc(BRAND)}</div>
{body}</div></body></html>"""


def load_events():
    if not EVENTS_DIR.exists():
        return []
    events = []
    for f in EVENTS_DIR.glob("*.json"):
        try:
            events.append(json.loads(f.read_text()))
        except (ValueError, OSError):
            continue
    return sorted(events, key=lambda e: e.get("detected_at") or "", reverse=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep the console for our own output

    def _send(self, code, body):
        raw = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path in ("/", "/index.html"):
            return self._send(200, page(BRAND, dashboard(query)))

        if parsed.path == "/share":
            event_id = (query.get("id") or [""])[0]
            match = next((e for e in load_events() if e["tweet_id"] == event_id), None)
            if not match:
                return self._send(404, page("Not found",
                    '<div class="card">No event with that id. <a href="/">All events</a></div>'))
            try:
                body = render_event(match)
            except Exception as e:
                body = f'<div class="card">Could not load tweet: {esc(str(e))}</div>'
            return self._send(200, page(f"@{match.get('account')} — {BRAND}", body))

        self._send(404, page("Not found", '<div class="card">Not found</div>'))


if __name__ == "__main__":
    EVENTS_DIR.mkdir(exist_ok=True)
    print(f"{BRAND}  ->  http://localhost:{PORT}")
    print(f"{len(load_events())} event(s) in {EVENTS_DIR}/")
    try:
        HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
