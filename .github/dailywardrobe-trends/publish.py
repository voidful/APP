#!/usr/bin/env python3
"""Checks and signs the trend seasons that the app downloads (Core/TrendFeed.swift).

usage:
  publish.py check [FILE] [--online]   validate a feed source (default: feed.json next to this script);
                                       --online also opens every source URL
  publish.py sign OUT                  sign feed.json into OUT, unless OUT already carries the same seasons
  publish.py verify [FILE|URL]         check an envelope's signature with the app's public key (default: the live file)

Runs in voidful/APP (.github/dailywardrobe-trends/, driven by .github/workflows/dailywardrobe-trends.yml) and locally.
Next to it: feed.json (the unsigned seasons, without "generated"), languages.json (every language the app ships) and
public-key.pem (the key built into the app); scripts/trends/sync_site.py generates the last two from the app.
The private key comes from $TREND_SIGNING_KEY_FILE, else ~/.config/dailywardrobe/trend-signing-key.pem.
"""
import base64, datetime, json, os, re, subprocess, sys, tempfile, unicodedata, urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
FEED = HERE / 'feed.json'
URL = 'https://eric-lam.com/APP/dailywardrobe_trends.json'
KEY = Path(os.environ.get('TREND_SIGNING_KEY_FILE') or Path.home() / '.config/dailywardrobe/trend-signing-key.pem')
OPENSSL = next((p for p in ['/opt/homebrew/bin/openssl', '/usr/bin/openssl'] if Path(p).exists()), 'openssl')
# eric-lam.com sits behind Cloudflare, which turns away Python's default User-Agent.
AGENT = 'DailyWardrobe-publisher'

KINDS = {'mainColor', 'upperColor', 'tonal', 'layered', 'earthWithAccent', 'tailored'}
COLOR_KINDS = {'mainColor', 'upperColor'}
COLORS = {'black', 'white', 'gray', 'beige', 'brown', 'blue', 'green', 'red', 'pink', 'purple', 'yellow', 'orange'}
ID = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}$')
LANG = re.compile(r'^[a-z]{2,3}(-[A-Za-z0-9]{2,4})?$')
DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')
MAX_BYTES = 512 * 1024  # TrendFeed.maxBytes


def app_languages(): return json.loads((HERE / 'languages.json').read_text())
def public_key(): return (HERE / 'public-key.pem').read_text()
def today(): return datetime.datetime.now(datetime.timezone.utc).date()


def day(text):
    if not isinstance(text, str) or not DAY.match(text): return None
    try: return datetime.date.fromisoformat(text)
    except ValueError: return None


def line_ok(text, limit):
    """Mirrors TrendFeed.isLine; Python counts code points, which is never fewer than Swift's characters."""
    return (isinstance(text, str) and text.strip() == text and text and len(text) <= limit
            and not any(unicodedata.category(c) == 'Cc' or 0x202A <= ord(c) <= 0x202E or 0x2066 <= ord(c) <= 0x2069 for c in text))


def fetch_status(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Macintosh) DailyWardrobe-trend-check'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response: return response.status
    except urllib.error.HTTPError as error: return error.code
    except Exception: return None


def validate(feed, online=False):
    """Every problem found, as text. Stricter than the app: every language, only known kinds, 3–10 rules."""
    if not isinstance(feed, dict): return ['the feed must be a JSON object']
    problems = []
    def need(condition, message):
        if not condition: problems.append(message)
    languages = app_languages()
    def text_ok(where, value, limit):
        if not isinstance(value, dict): return problems.append(f'{where}: expected a language map')
        missing = [code for code in languages if code not in value]
        need(not missing, f'{where}: missing {", ".join(missing)}')
        need(all(LANG.match(code) for code in value), f'{where}: bad language code')
        for code, text in value.items(): need(line_ok(text, limit), f'{where}[{code}]: empty, too long (>{limit}) or has control characters: {text!r}')
    need(feed.get('schema') == 1, 'schema must be 1')
    need(set(feed) <= {'schema', 'seasons'}, f'unexpected top-level keys: {set(feed) - {"schema", "seasons"}}')
    seasons = feed.get('seasons') or []
    need(1 <= len(seasons) <= 6, 'need 1–6 seasons')
    need(len({s.get('id') for s in seasons}) == len(seasons), 'season ids must be unique')
    for season in seasons:
        sid = season.get('id', '?')
        need(set(season) == {'id', 'title', 'checked', 'validFrom', 'validUntil', 'sources', 'rules'}, f'{sid}: keys must be id, title, checked, validFrom, validUntil, sources, rules')
        need(isinstance(sid, str) and ID.match(sid), f'{sid}: bad id')
        text_ok(f'{sid}.title', season.get('title'), 40)
        start, end, checked = day(season.get('validFrom')), day(season.get('validUntil')), day(season.get('checked'))
        need(start and end and checked, f'{sid}: dates must be yyyy-MM-dd')
        if start and end: need(start <= end and (end - start).days <= 400, f'{sid}: validFrom must be before validUntil, at most 400 days apart')
        if checked: need(checked <= today(), f'{sid}: checked is in the future')
        sources = season.get('sources') or []
        need(1 <= len(sources) <= 12, f'{sid}: need 1–12 sources')
        need(len({s.get('id') for s in sources}) == len(sources), f'{sid}: source ids must be unique')
        for source in sources:
            src = f'{sid}.source {source.get("id")}'
            need(set(source) == {'id', 'publisher', 'title', 'url', 'published'}, f'{src}: keys must be id, publisher, title, url, published')
            need(isinstance(source.get('id'), str) and ID.match(source['id']), f'{src}: bad id')
            need(line_ok(source.get('publisher'), 80) and line_ok(source.get('title'), 200), f'{src}: publisher or title empty, too long or has control characters')
            url = source.get('url', '')
            need(isinstance(url, str) and url.startswith('https://') and len(url) <= 500 and ' ' not in url, f'{src}: url must be https')
            published = day(source.get('published'))
            need(published and (not checked or published <= checked), f'{src}: published must be a date on or before checked')
            if online and isinstance(url, str) and url.startswith('https://'):
                # A made-up page answers 404/410 and a made-up site does not answer at all. Sites that turn scripts
                # away (401/403/429/5xx, common from cloud runners) get the benefit of the doubt.
                status = fetch_status(url)
                need(status is not None and status not in (404, 410), f'{src}: {url} answered {status or "nothing"}')
        known = {s.get('id') for s in sources}
        rules = season.get('rules') or []
        need(3 <= len(rules) <= 10, f'{sid}: need 3–10 rules')
        need(len({r.get('id') for r in rules}) == len(rules), f'{sid}: rule ids must be unique')
        for rule in rules:
            rid = f'{sid}.rule {rule.get("id")}'
            kind = rule.get('kind')
            need(isinstance(rule.get('id'), str) and ID.match(rule['id']), f'{rid}: bad id')
            need(kind in KINDS, f'{rid}: kind must be one of {sorted(KINDS)}')
            need(set(rule) <= {'id', 'kind', 'colors', 'title', 'detail', 'sources'}, f'{rid}: unexpected keys')
            colors = rule.get('colors')
            if kind in COLOR_KINDS:
                need(isinstance(colors, list) and 1 <= len(colors) <= 4 and len(set(colors)) == len(colors) and set(colors) <= COLORS, f'{rid}: colors must be 1–4 of {sorted(COLORS)}')
            else:
                need(not colors, f'{rid}: only mainColor and upperColor take colors')
            text_ok(f'{rid}.title', rule.get('title'), 40)
            text_ok(f'{rid}.detail', rule.get('detail'), 160)
            refs = rule.get('sources') or []
            need(refs and set(refs) <= known, f'{rid}: sources must name this season\'s source ids')
    need(any(season_current(s) for s in seasons), 'no season covers today')
    return problems


def season_current(season, on=None):
    start, end = day(season.get('validFrom')), day(season.get('validUntil'))
    return bool(start and end and start <= (on or today()) <= end)


def payload(feed):
    """Seasons that ended more than 60 days ago are left out; the newest season always stays."""
    seasons = sorted(feed['seasons'], key=lambda s: s['validUntil'])
    keep = [s for s in seasons if (today() - day(s['validUntil'])).days <= 60] or seasons[-1:]
    return {'schema': 1, 'generated': today().isoformat(), 'seasons': keep}


def openssl(*args):
    return subprocess.run([OPENSSL, *args], capture_output=True, check=True).stdout


def verify(data):
    """The same check the app makes, done with openssl against the app's public key. Returns the payload."""
    envelope = json.loads(data)
    body, signature = base64.b64decode(envelope['payload']), base64.b64decode(envelope['signature'])
    with tempfile.TemporaryDirectory() as tmp:
        for name, content in [('payload', body), ('signature', signature), ('key.pem', public_key().encode())]: (Path(tmp) / name).write_bytes(content)
        result = subprocess.run([OPENSSL, 'dgst', '-sha256', '-verify', str(Path(tmp) / 'key.pem'), '-signature', str(Path(tmp) / 'signature'), str(Path(tmp) / 'payload')], capture_output=True)
    if result.returncode != 0: sys.exit('signature does not verify with the app\'s public key')
    return json.loads(body)


def same_seasons(a, b): return json.dumps(a.get('seasons'), sort_keys=True, ensure_ascii=False) == json.dumps(b.get('seasons'), sort_keys=True, ensure_ascii=False)


def sign(out):
    feed = json.loads(FEED.read_text())
    problems = validate(feed)
    if problems: sys.exit('feed.json is not valid:\n  ' + '\n  '.join(problems))
    candidate = payload(feed)
    if out.exists() and same_seasons(verify(out.read_bytes()), candidate):
        print(f'{out.name} already carries these seasons; unchanged'); return
    if not KEY.exists(): sys.exit(f'no signing key at {KEY}')
    if openssl('ec', '-in', str(KEY), '-pubout').decode().split() != public_key().split():
        sys.exit('the signing key does not match public-key.pem (the key built into the app)')
    body = json.dumps(candidate, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / 'payload').write_bytes(body)
        signature = openssl('dgst', '-sha256', '-sign', str(KEY), str(Path(tmp) / 'payload'))
    envelope = json.dumps({'payload': base64.b64encode(body).decode(), 'signature': base64.b64encode(signature).decode()}, separators=(',', ':')) + '\n'
    if len(envelope.encode()) > MAX_BYTES: sys.exit('signed file is over the app\'s 512 KB limit (TrendFeed.maxBytes)')
    verify(envelope.encode())
    out.write_text(envelope)
    print(f'signed {out.name}: {", ".join(s["id"] for s in candidate["seasons"])} ({len(envelope)} bytes)')


def read(target):
    if target.startswith('https://'):
        with urllib.request.urlopen(urllib.request.Request(target, headers={'User-Agent': AGENT, 'Cache-Control': 'no-cache'}), timeout=20) as response:
            return response.read()
    return Path(target).read_bytes()


if __name__ == '__main__':
    command, args = (sys.argv[1] if len(sys.argv) > 1 else ''), sys.argv[2:]
    if command == 'check':
        path = Path(next((a for a in args if not a.startswith('--')), FEED))
        try: feed = json.loads(path.read_text())
        except Exception as error: sys.exit(f'{path}: not readable JSON: {error}')
        problems = validate(feed, online='--online' in args)
        print('\n'.join(problems) if problems else f'{path.name}: valid ({len(feed["seasons"])} seasons, {sum(len(s["rules"]) for s in feed["seasons"])} rules, {len(app_languages())} languages)')
        sys.exit(1 if problems else 0)
    elif command == 'sign' and args: sign(Path(args[0]))
    elif command == 'verify':
        body = verify(read(args[0] if args else URL))
        print(f'signature valid; generated {body["generated"]}; seasons {[s["id"] for s in body["seasons"]]}')
    else: sys.exit(__doc__)
