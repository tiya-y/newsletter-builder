"""
Newsletter Builder — Flask Backend
Serves the frontend and handles article scraping, email generation, and Brevo API calls.

Config comes from environment variables (set in Vercel dashboard or local .env file).
"""

from flask import Flask, jsonify, request, send_file, abort, Response, redirect
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import os, json, re, html, functools
from urllib.parse import urljoin

try:
    import anthropic as anthropic_sdk
except ImportError:
    anthropic_sdk = None

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor, Json as PgJson
except ImportError:
    psycopg2 = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
CORS(app)

BREVO_BASE = 'https://api.brevo.com/v3'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Database (Neon Postgres) — optional; everything falls back gracefully ─────
# if DATABASE_URL isn't set (e.g. local dev without a DB configured).

_schema_ready = False

def get_db_connection():
    """Returns a psycopg2 connection with dict-row cursors, or None if no DB is configured/reachable."""
    if not psycopg2 or not os.environ.get('DATABASE_URL'):
        return None
    try:
        return psycopg2.connect(os.environ['DATABASE_URL'], cursor_factory=RealDictCursor)
    except Exception as e:
        print(f'DB connection failed: {e}')
        return None

def ensure_schema(conn):
    """Idempotently create all tables. Cached per-process so warm invocations skip the round trip."""
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute('''
            CREATE TABLE IF NOT EXISTS articles (
                id SERIAL PRIMARY KEY,
                url TEXT UNIQUE NOT NULL,
                title TEXT,
                category TEXT,
                description TEXT,
                image TEXT,
                traffic INTEGER,
                keywords INTEGER
            );
            CREATE TABLE IF NOT EXISTS rei_assets (
                id SERIAL PRIMARY KEY,
                title TEXT,
                category TEXT,
                description TEXT,
                url TEXT,
                image TEXT
            );
            CREATE TABLE IF NOT EXISTS rei_subscription_promos (
                id SERIAL PRIMARY KEY,
                title TEXT,
                description TEXT,
                cta_text TEXT,
                cta_url TEXT,
                image TEXT
            );
            CREATE TABLE IF NOT EXISTS draft (
                id INTEGER PRIMARY KEY DEFAULT 1,
                series TEXT,
                month TEXT,
                year TEXT,
                blocks JSONB,
                subject TEXT,
                preview_text TEXT,
                headline TEXT,
                subheadline TEXT,
                intro TEXT,
                examples_used TEXT,
                updated_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS push_history (
                id SERIAL PRIMARY KEY,
                series TEXT,
                month TEXT,
                year TEXT,
                subject TEXT,
                campaign_id INTEGER,
                campaign_name TEXT,
                list_ids JSONB,
                pushed_at TIMESTAMPTZ DEFAULT now()
            );
        ''')
    conn.commit()
    _schema_ready = True

# ── Config — env vars only (no filesystem writes on Vercel) ───────────────────

def get_config():
    """Read config from environment variables."""
    return {
        'brevo_api_key':        os.environ.get('BREVO_API_KEY', ''),
        'innago_blog_url':      os.environ.get('INNAGO_BLOG_URL', 'https://innago.com/blog'),
        'rei_grove_blog_url':   os.environ.get('REI_GROVE_BLOG_URL', ''),
        'rei_grove_webinar_url':os.environ.get('REI_GROVE_WEBINAR_URL', ''),
        'po_sender_name':       os.environ.get('PO_SENDER_NAME', 'Innago'),
        'po_sender_email':      os.environ.get('PO_SENDER_EMAIL', 'newsletter@innago.com'),
        'rei_sender_name':      os.environ.get('REI_SENDER_NAME', 'REI Grove'),
        'rei_sender_email':     os.environ.get('REI_SENDER_EMAIL', 'newsletter@reigrove.com'),
    }

# ── Optional password protection ──────────────────────────────────────────────

def require_auth(f):
    """Simple password gate — set APP_PASSWORD env var to enable."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        password = os.environ.get('APP_PASSWORD', '')
        if not password:
            return f(*args, **kwargs)
        # Check Authorization header (Basic auth)
        auth = request.authorization
        if auth and auth.password == password:
            return f(*args, **kwargs)
        # Also accept ?pw= query param (for easy browser access)
        if request.args.get('pw') == password:
            return f(*args, **kwargs)
        return Response(
            'Newsletter Builder — Authentication required.',
            401,
            {'WWW-Authenticate': 'Basic realm="Newsletter Builder"'}
        )
    return decorated

@app.route('/api/config', methods=['GET'])
@require_auth
def api_config():
    cfg = get_config()
    key = cfg.get('brevo_api_key', '')
    return jsonify({
        'po_sender_name':        cfg['po_sender_name'],
        'po_sender_email':       cfg['po_sender_email'],
        'rei_sender_name':       cfg['rei_sender_name'],
        'rei_sender_email':      cfg['rei_sender_email'],
        'innago_blog_url':       cfg['innago_blog_url'],
        'rei_grove_blog_url':    cfg['rei_grove_blog_url'],
        'rei_grove_webinar_url': cfg['rei_grove_webinar_url'],
        'brevo_configured':      bool(key),
        'brevo_api_key_masked':  ('••••••' + key[-6:]) if len(key) > 6 else ('set' if key else 'NOT SET'),
    })

# ── Brevo helpers ──────────────────────────────────────────────────────────────

def brevo_headers():
    key = get_config().get('brevo_api_key', '')
    return {'api-key': key, 'Content-Type': 'application/json', 'Accept': 'application/json'}

@app.route('/api/brevo/lists')
@require_auth
def brevo_lists():
    try:
        r = requests.get(f'{BREVO_BASE}/contacts/lists?limit=50', headers=brevo_headers(), timeout=10)
        r.raise_for_status()
        lists = r.json().get('lists', [])
        return jsonify([{'id': l['id'], 'name': l['name'], 'subscribers': l.get('uniqueSubscribers', 0)} for l in lists])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/brevo/push', methods=['POST'])
@require_auth
def brevo_push():
    data = request.json or {}
    series = data.get('series', 'po')
    cfg = get_config()

    if series == 'po':
        sender_name = cfg['po_sender_name']
        sender_email = cfg['po_sender_email']
        list_ids = [int(i) for i in data.get('list_ids', []) if i]
    else:
        sender_name = cfg['rei_sender_name']
        sender_email = cfg['rei_sender_email']
        list_ids = [int(i) for i in data.get('list_ids', []) if i]

    month = data.get('month', '')
    year = data.get('year', '')
    prefix = 'PO - Newsletter' if series == 'po' else 'Innago Insight - Newsletter'
    campaign_name = f"{prefix} - {month} {year}"

    email_html = generate_email_html(data)

    payload = {
        'name': campaign_name,
        'subject': data.get('subject', campaign_name),
        'previewText': data.get('preview_text', ''),
        'sender': {'name': sender_name, 'email': sender_email},
        'htmlContent': email_html,
        'recipients': {'listIds': list_ids} if list_ids else {'listIds': []},
    }

    try:
        r = requests.post(f'{BREVO_BASE}/emailCampaigns', headers=brevo_headers(),
                          json=payload, timeout=15)
        r.raise_for_status()
        resp = r.json()
        record_push_history(series, month, year, data.get('subject', campaign_name), resp.get('id'), campaign_name, list_ids)
        return jsonify({'ok': True, 'campaign_id': resp.get('id'), 'name': campaign_name})
    except requests.HTTPError as e:
        return jsonify({'error': e.response.text}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def record_push_history(series, month, year, subject, campaign_id, campaign_name, list_ids):
    """Best-effort local log of every push — used as a History fallback if the live Brevo query ever fails."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute('''
                INSERT INTO push_history (series, month, year, subject, campaign_id, campaign_name, list_ids)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            ''', (series, month, year, subject, campaign_id, campaign_name, PgJson(list_ids)))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'Failed to record push history (non-fatal): {e}')

# ── Article scraping ───────────────────────────────────────────────────────────

def fetch_og(url):
    """Extract OG/meta tags from a URL."""
    try:
        resp = requests.get(url, timeout=12, headers={'User-Agent': 'Mozilla/5.0 (compatible; NewsletterBuilder/1.0)'})
        soup = BeautifulSoup(resp.text, 'html.parser')

        def og(prop):
            t = soup.find('meta', property=f'og:{prop}') or soup.find('meta', attrs={'name': f'og:{prop}'})
            return (t.get('content') or '').strip() if t else ''

        def meta(name):
            t = soup.find('meta', attrs={'name': name})
            return (t.get('content') or '').strip() if t else ''

        title = og('title') or (soup.find('title') and soup.find('title').get_text().strip()) or ''
        description = og('description') or meta('description') or ''
        image = og('image') or ''
        if image and not image.startswith('http'):
            image = urljoin(url, image)

        return {
            'title': title[:120],
            'description': description[:250],
            'image': image,
            'url': url,
            'category': og('type') or '',
        }
    except Exception as e:
        return {'error': str(e), 'url': url, 'title': url, 'description': '', 'image': '', 'category': ''}

def scrape_blog(blog_url):
    """Scrape blog listing page for article cards."""
    if not blog_url:
        return []
    try:
        resp = requests.get(blog_url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        soup = BeautifulSoup(resp.text, 'html.parser')
        found = []

        # Try different selectors
        candidates = (
            soup.find_all('article') or
            soup.find_all(class_=re.compile(r'\b(post|blog-card|article|entry)\b', re.I)) or
            soup.find_all('div', class_=re.compile(r'\b(post|blog-item|resource)\b', re.I))
        )

        for card in candidates[:25]:
            link = card.find('a', href=True)
            if not link:
                continue
            href = urljoin(blog_url, link['href'])
            if not href.startswith('http') or href == blog_url:
                continue

            heading = card.find(['h1', 'h2', 'h3', 'h4'])
            title = heading.get_text().strip() if heading else link.get_text().strip()
            if len(title) < 5:
                continue

            img_tag = card.find('img')
            img_src = ''
            if img_tag:
                img_src = img_tag.get('src') or img_tag.get('data-src') or img_tag.get('data-lazy-src') or ''
                if img_src and not img_src.startswith('http'):
                    img_src = urljoin(blog_url, img_src)

            paras = card.find_all('p')
            desc = next((p.get_text().strip() for p in paras if len(p.get_text().strip()) > 30), '')

            cat_el = card.find(class_=re.compile(r'\b(categ|tag|label|topic)\b', re.I))
            cat = cat_el.get_text().strip() if cat_el else ''

            found.append({'title': title[:120], 'url': href, 'image': img_src,
                          'description': desc[:250], 'category': cat})

        # Deduplicate
        seen, result = set(), []
        for a in found:
            if a['url'] not in seen:
                seen.add(a['url'])
                result.append(a)
        return result

    except Exception as e:
        return [{'error': str(e)}]

ARTICLES_FILE = os.path.join(BASE_DIR, 'articles.json')

def load_articles_library():
    conn = get_db_connection()
    if conn:
        try:
            ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute('SELECT url, title, category, description, image, traffic, keywords FROM articles ORDER BY traffic DESC NULLS LAST')
                rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            if rows:
                return rows
        except Exception as e:
            print(f'DB read failed for articles, falling back to JSON: {e}')
            conn.close()
    if os.path.exists(ARTICLES_FILE):
        with open(ARTICLES_FILE) as f:
            return json.load(f)
    return []

@app.route('/api/articles')
@require_auth
def get_articles():
    source = request.args.get('source', 'po')
    category = request.args.get('category', '')
    q = request.args.get('q', '').lower().strip()

    # PO newsletter: use local article library from Ahrefs export
    if source == 'po':
        articles = load_articles_library()
        if category:
            articles = [a for a in articles if a.get('category', '').lower() == category.lower()]
        if q:
            articles = [a for a in articles
                        if q in a.get('title', '').lower()
                        or q in a.get('description', '').lower()
                        or q in a.get('category', '').lower()]
        return jsonify(articles)

    # REI Grove: scrape blog
    cfg = get_config()
    url = cfg.get('rei_grove_blog_url', '')
    articles = scrape_blog(url)
    if q:
        articles = [a for a in articles if q in a.get('title','').lower() or q in a.get('description','').lower()]
    return jsonify(articles)

@app.route('/api/articles/categories')
@require_auth
def get_categories():
    articles = load_articles_library()
    cats = sorted(set(a.get('category','') for a in articles if a.get('category','')))
    return jsonify(cats)

ASSETS_FILE = os.path.join(BASE_DIR, 'rei_assets.json')

def load_assets_library():
    conn = get_db_connection()
    if conn:
        try:
            ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute('SELECT title, category, description, url, image FROM rei_assets ORDER BY id')
                rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            if rows:
                return rows
        except Exception as e:
            print(f'DB read failed for rei_assets, falling back to JSON: {e}')
            conn.close()
    if os.path.exists(ASSETS_FILE):
        with open(ASSETS_FILE) as f:
            return json.load(f)
    return []

@app.route('/api/rei-assets')
@require_auth
def get_rei_assets():
    category = request.args.get('category', '')
    q = request.args.get('q', '').lower().strip()

    assets = load_assets_library()
    if category:
        assets = [a for a in assets if a.get('category', '').lower() == category.lower()]
    if q:
        assets = [a for a in assets
                  if q in a.get('title', '').lower()
                  or q in a.get('description', '').lower()
                  or q in a.get('category', '').lower()]
    return jsonify(assets)

@app.route('/api/rei-assets/categories')
@require_auth
def get_rei_asset_categories():
    assets = load_assets_library()
    cats = sorted(set(a.get('category', '') for a in assets if a.get('category', '')))
    return jsonify(cats)

SUBSCRIPTION_PROMOS_FILE = os.path.join(BASE_DIR, 'rei_subscription_promos.json')

def load_subscription_promos():
    conn = get_db_connection()
    if conn:
        try:
            ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute('SELECT title, description, cta_text, cta_url, image FROM rei_subscription_promos ORDER BY id')
                rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            if rows:
                return rows
        except Exception as e:
            print(f'DB read failed for rei_subscription_promos, falling back to JSON: {e}')
            conn.close()
    if os.path.exists(SUBSCRIPTION_PROMOS_FILE):
        with open(SUBSCRIPTION_PROMOS_FILE) as f:
            return json.load(f)
    return []

@app.route('/api/rei-subscription-promos')
@require_auth
def get_rei_subscription_promos():
    return jsonify(load_subscription_promos())

# ── Database status + one-time seed ────────────────────────────────────────────

DB_TABLES = ['articles', 'rei_assets', 'rei_subscription_promos', 'draft', 'push_history']

@app.route('/api/db/status')
@require_auth
def db_status():
    conn = get_db_connection()
    if not conn:
        return jsonify({'connected': False})
    try:
        ensure_schema(conn)
        counts = {}
        with conn.cursor() as cur:
            for table in DB_TABLES:
                cur.execute(f'SELECT COUNT(*) AS c FROM {table}')
                counts[table] = cur.fetchone()['c']
        conn.close()
        return jsonify({'connected': True, 'counts': counts})
    except Exception as e:
        conn.close()
        return jsonify({'connected': False, 'error': str(e)})

@app.route('/api/db/seed', methods=['POST'])
@require_auth
def db_seed():
    """Loads the bundled JSON content libraries into the DB — skips any table that already has rows."""
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Database not configured — set DATABASE_URL first.'}), 500
    try:
        ensure_schema(conn)
        seeded = {}
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) AS c FROM articles')
            if cur.fetchone()['c'] == 0 and os.path.exists(ARTICLES_FILE):
                with open(ARTICLES_FILE) as f:
                    rows = json.load(f)
                for a in rows:
                    cur.execute('''
                        INSERT INTO articles (url, title, category, description, image, traffic, keywords)
                        VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (url) DO NOTHING
                    ''', (a.get('url'), a.get('title'), a.get('category'), a.get('description'),
                          a.get('image'), a.get('traffic'), a.get('keywords')))
                seeded['articles'] = len(rows)
            else:
                seeded['articles'] = 0

            cur.execute('SELECT COUNT(*) AS c FROM rei_assets')
            if cur.fetchone()['c'] == 0 and os.path.exists(ASSETS_FILE):
                with open(ASSETS_FILE) as f:
                    rows = json.load(f)
                for a in rows:
                    cur.execute('''
                        INSERT INTO rei_assets (title, category, description, url, image)
                        VALUES (%s,%s,%s,%s,%s)
                    ''', (a.get('title'), a.get('category'), a.get('description'), a.get('url'), a.get('image')))
                seeded['rei_assets'] = len(rows)
            else:
                seeded['rei_assets'] = 0

            cur.execute('SELECT COUNT(*) AS c FROM rei_subscription_promos')
            if cur.fetchone()['c'] == 0 and os.path.exists(SUBSCRIPTION_PROMOS_FILE):
                with open(SUBSCRIPTION_PROMOS_FILE) as f:
                    rows = json.load(f)
                for p in rows:
                    cur.execute('''
                        INSERT INTO rei_subscription_promos (title, description, cta_text, cta_url, image)
                        VALUES (%s,%s,%s,%s,%s)
                    ''', (p.get('title'), p.get('description'), p.get('cta_text'), p.get('cta_url'), p.get('image')))
                seeded['rei_subscription_promos'] = len(rows)
            else:
                seeded['rei_subscription_promos'] = 0
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'seeded': seeded})
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'error': str(e)}), 500

# ── Draft persistence — a single in-progress newsletter, DB-backed ─────────────
# Mirrors the old localStorage singleton exactly: one active draft at a time,
# regardless of series. Requires DATABASE_URL — with no DB configured, Input/
# Approve just won't have anything to hand off between page loads.

@app.route('/api/draft', methods=['GET'])
@require_auth
def get_draft():
    conn = get_db_connection()
    if not conn:
        return jsonify({})
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM draft WHERE id = 1')
            row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({})
        result = dict(row)
        result.pop('id', None)
        if result.get('updated_at'):
            result['updated_at'] = result['updated_at'].isoformat()
        return jsonify(result)
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/draft', methods=['POST'])
@require_auth
def save_draft_route():
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Database not configured'}), 500
    data = request.json or {}
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute('''
                INSERT INTO draft (id, series, month, year, blocks, subject, preview_text, headline, subheadline, intro, examples_used, updated_at)
                VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET
                    series = EXCLUDED.series, month = EXCLUDED.month, year = EXCLUDED.year,
                    blocks = EXCLUDED.blocks, subject = EXCLUDED.subject, preview_text = EXCLUDED.preview_text,
                    headline = EXCLUDED.headline, subheadline = EXCLUDED.subheadline, intro = EXCLUDED.intro,
                    examples_used = EXCLUDED.examples_used, updated_at = now()
            ''', (
                data.get('series'), data.get('month'), data.get('year'), PgJson(data.get('blocks', [])),
                data.get('subject'), data.get('preview_text'), data.get('headline'),
                data.get('subheadline'), data.get('intro'), data.get('_examples_used'),
            ))
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/draft', methods=['DELETE'])
@require_auth
def clear_draft_route():
    conn = get_db_connection()
    if not conn:
        return jsonify({'ok': True})
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute('DELETE FROM draft WHERE id = 1')
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/fetch-url', methods=['POST'])
@require_auth
def fetch_url():
    url = (request.json or {}).get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    if not url.startswith('http'):
        url = 'https://' + url
    return jsonify(fetch_og(url))

@app.route('/api/webinars')
@require_auth
def get_webinars():
    cfg = get_config()
    url = cfg.get('rei_grove_webinar_url', '')
    if not url:
        return jsonify([])
    articles = scrape_blog(url)
    return jsonify(articles)

# ── Email HTML generation ──────────────────────────────────────────────────────

def esc(s):
    return html.escape(str(s or ''))

def generate_email_html(data):
    series = data.get('series', 'po')
    if series == 'po':
        return build_po_email(data)
    else:
        return build_rei_email(data)

PO_FONT = "'Poppins',Arial,Helvetica,sans-serif"

def po_utm(url, month, year):
    """Match the real Brevo template's tracking convention on every outbound link."""
    if not url or url == '#':
        return url or '#'
    campaign = f"{month.lower()}+{year}+newsletter" if month and year else 'newsletter'
    sep = '&' if '?' in url else '?'
    return f"{url}{sep}utm_source=brevo&utm_medium=email&utm_campaign={campaign}"

def po_divider():
    return f'''
        <tr><td style="padding:0 15px;">
          <table width="100%" cellpadding="0" cellspacing="0" role="presentation" height="1" style="border-top:1px solid #4A4A4A;font-size:1px;line-height:1px;"><tr><td>&nbsp;</td></tr></table>
        </td></tr>'''

def build_article_card_po(block, index, month, year):
    title = esc(block.get('title', ''))
    desc = esc(block.get('description', ''))
    url = esc(po_utm(block.get('url', '#'), month, year))
    image = block.get('image', '')

    cat_tag = ''
    if block.get('category'):
        cat_tag = f'<p style="margin:0 0 8px;color:#2675ff;font-family:{PO_FONT};font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.8px;">{esc(block["category"])}</p>'

    text_cell = f'''
              <td width="{"50%" if image else "100%"}" valign="top" style="padding:0 15px;">
                {cat_tag}
                <h2 style="margin:0 0 10px;color:#1f2d3d;font-family:{PO_FONT};font-size:22px;font-weight:700;line-height:1.3;">{title}</h2>
                <p style="margin:0 0 15px;color:#3b3f44;font-family:{PO_FONT};font-size:15px;line-height:1.5;">{desc}</p>
                <a href="{url}" target="_blank" style="display:inline-block;background-color:#f6b42a;color:#3b3f44;font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:bold;text-decoration:none;padding:12px 24px;border-radius:8px;">Read Now</a>
              </td>'''

    if not image:
        row = text_cell
    else:
        image_cell = f'''
              <td width="50%" valign="top" style="padding:0 15px;">
                <img src="{esc(image)}" width="100%" alt="{title}" style="display:block;width:100%;border-radius:8px;">
              </td>'''
        # Alternate image-left/image-right like the real template's blog section
        row = image_cell + text_cell if index % 2 == 1 else text_cell + image_cell

    return f'''
        <tr>
          <td style="padding:20px 15px;">
            <table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>{row}</tr></table>
          </td>
        </tr>''' + po_divider()

def build_promo_card_po(block, month, year):
    title = esc(block.get('title', ''))
    desc = esc(block.get('description', ''))
    image = block.get('image', '')
    cta_text = block.get('cta_text', '')
    cta_url = block.get('cta_url', '')

    cta = ''
    if cta_url and cta_text:
        url = esc(po_utm(cta_url, month, year))
        cta = f'<a href="{url}" target="_blank" style="display:inline-block;background-color:#2e3b47;color:#ffffff;font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:bold;text-decoration:none;padding:12px 24px;border-radius:8px;">{esc(cta_text)}</a>'

    text_block = f'''
                <h3 style="margin:0 0 10px;color:#1f2d3d;font-family:{PO_FONT};font-size:20px;font-weight:700;">{title}</h3>
                <p style="margin:0 0 15px;color:#3b3f44;font-family:{PO_FONT};font-size:15px;line-height:1.5;">{desc}</p>
                {cta}'''

    if image:
        row = f'''
              <td width="25%" valign="top" style="padding:0 15px;">
                <img src="{esc(image)}" width="100%" alt="{title}" style="display:block;width:100%;border-radius:8px;">
              </td>
              <td width="75%" valign="top" style="padding:0 15px;">{text_block}
              </td>'''
    else:
        row = f'<td width="100%" valign="top" style="padding:0 15px;">{text_block}\n              </td>'

    return f'''
        <tr>
          <td style="padding:20px 15px;">
            <table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>{row}</tr></table>
          </td>
        </tr>''' + po_divider()

def build_po_email(data):
    month = data.get('month', '')
    year = data.get('year', '')
    headline = data.get('headline') or f'{month} {year} edition'
    subheadline = data.get('subheadline', '')
    intro = data.get('intro', "Here's your monthly update from Innago.")
    login_url = po_utm('https://my.innago.com/login', month, year)

    blocks_html = ''
    for i, block in enumerate(data.get('blocks', [])):
        btype = block.get('type', 'article')
        if btype in ('article', 'webinar'):
            blocks_html += build_article_card_po(block, i, month, year)
        elif btype == 'promo':
            blocks_html += build_promo_card_po(block, month, year)

    subheadline_html = ''
    if subheadline:
        subheadline_html = f'<p style="margin:0 0 12px;color:#3b3f44;font-family:{PO_FONT};font-size:16px;line-height:1.5;">{esc(subheadline)}</p>'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="supported-color-schemes" content="light">
  <style>:root{{color-scheme:light only;}}body,table,td,a{{-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;}}img{{border:0;height:auto;line-height:100%;outline:none;text-decoration:none;}}table{{border-collapse:collapse!important;}}body{{height:100%!important;margin:0!important;padding:0!important;width:100%!important;background-color:#ffffff;}}</style>
</head>
<body style="margin:0;padding:0;background-color:#ffffff;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation">
  <tr>
    <td align="center">
      <table width="600" cellpadding="0" cellspacing="0" border="0" role="presentation" style="max-width:600px;background:#ffffff;">

        <!-- HEADER: logo only, matching the real template -->
        <tr>
          <td style="background-color:#2675ff;padding:20px 15px;text-align:center;">
            <a href="https://innago.com" target="_blank">
              <img src="https://img.mailinblue.com/7977391/images/content_library/original/679a52465b47d6d757542268.jpg"
                   width="193" alt="Innago" style="display:block;width:193px;margin:0 auto;">
            </a>
          </td>
        </tr>

        <!-- GREETING + INTRO -->
        <tr>
          <td style="padding:20px 15px;">
            <p style="margin:0 0 12px;color:#3b3f44;font-family:{PO_FONT};font-size:16px;line-height:1.5;">Hi {{{{ contact.FIRSTNAME }}}},</p>
            <p style="margin:0 0 12px;color:#3b3f44;font-family:{PO_FONT};font-size:16px;line-height:1.5;">Welcome back to <strong><span style="color:#2675ff;">Innago's monthly newsletter — {esc(headline)}!</span></strong></p>
            {subheadline_html}
            <p style="margin:0 0 12px;color:#3b3f44;font-family:{PO_FONT};font-size:16px;line-height:1.5;">{esc(intro)}</p>
            <p style="margin:0;color:#3b3f44;font-family:{PO_FONT};font-size:16px;line-height:1.5;">Let's dive in! 👇</p>
          </td>
        </tr>

        <!-- LOGIN CTA -->
        <tr>
          <td align="center" style="padding:0 15px 20px;">
            <a href="{esc(login_url)}" target="_blank" style="display:inline-block;background-color:#f6b42a;color:#3b3f44;font-family:Arial,Helvetica,sans-serif;font-size:16px;font-weight:bold;text-decoration:none;padding:12px 24px;border-radius:12px;">Login Now</a>
          </td>
        </tr>
        {po_divider()}

        <!-- CONTENT BLOCKS -->
        {blocks_html if blocks_html else '<tr><td style="padding:20px 15px;color:#999;font-family:Poppins,Arial,sans-serif;font-size:14px;text-align:center;">[No content blocks added yet]</td></tr>'}

        <!-- FOOTER -->
        <tr>
          <td style="background-color:#2675ff;padding:20px 15px;text-align:center;">
            <p style="margin:0 0 6px;color:#ffffff;font-family:{PO_FONT};font-size:18px;font-weight:700;">Innago LLC</p>
            <p style="margin:0 0 10px;color:#ffffff;font-family:{PO_FONT};font-size:14px;">1308 Race St Suite 100, 45202, Cincinnati</p>
            <p style="margin:0;font-family:{PO_FONT};font-size:14px;">
              <a href="{{{{ mirror }}}}" style="color:#ffffff;text-decoration:underline;">View in browser</a>
              <span style="color:#ffffff;"> | </span>
              <a href="{{{{ unsubscribe }}}}" style="color:#ffffff;text-decoration:underline;">Unsubscribe</a>
            </p>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body>
</html>'''

# ── REI Grove email template ───────────────────────────────────────────────────

def build_article_card_rei(block):
    img_row = ''
    if block.get('image'):
        img_row = f'''
        <tr>
          <td style="padding:0;line-height:0;">
            <img src="{esc(block['image'])}" alt="{esc(block.get('title',''))}"
                 width="100%" style="display:block;width:100%;max-height:220px;object-fit:cover;border-radius:10px 10px 0 0;">
          </td>
        </tr>'''

    cat_tag = ''
    if block.get('category'):
        cat_tag = f'<p style="color:#57823C;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.8px;margin:0 0 8px 0;">{esc(block["category"])}</p>'

    return f'''
        <tr>
          <td style="padding:0 32px 20px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E2E8E0;border-radius:10px;overflow:hidden;background:#ffffff;">
              {img_row}
              <tr>
                <td style="padding:20px 24px 22px;">
                  {cat_tag}
                  <p style="color:#26463D;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:17px;font-weight:600;margin:0 0 8px;line-height:1.35;">{esc(block.get('title',''))}</p>
                  <p style="color:#4A5568;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:14px;line-height:1.65;margin:0 0 16px;">{esc(block.get('description',''))}</p>
                  <a href="{esc(block.get('url','#'))}" style="color:#57823C;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:14px;font-weight:600;text-decoration:none;">Read more &rarr;</a>
                </td>
              </tr>
            </table>
          </td>
        </tr>'''

def build_promo_card_rei(block):
    img_row = ''
    if block.get('image'):
        img_row = f'''
        <tr>
          <td style="padding:0;line-height:0;">
            <img src="{esc(block['image'])}" alt="{esc(block.get('title',''))}"
                 width="100%" style="display:block;width:100%;max-height:200px;object-fit:cover;border-radius:8px 8px 0 0;">
          </td>
        </tr>'''

    cta = ''
    if block.get('cta_url') and block.get('cta_text'):
        cta = f'<a href="{esc(block["cta_url"])}" style="display:inline-block;background-color:#57823C;color:#ffffff;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:14px;font-weight:600;text-decoration:none;padding:12px 28px;border-radius:8px;">{esc(block["cta_text"])}</a>'

    return f'''
        <tr>
          <td style="padding:0 32px 20px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#EAF0E8;border:1px solid #C0DD97;border-left:4px solid #57823C;border-radius:8px;overflow:hidden;">
              {img_row}
              <tr>
                <td style="padding:20px 24px 22px;">
                  <p style="color:#26463D;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:16px;font-weight:600;margin:0 0 8px;">{esc(block.get('title',''))}</p>
                  <p style="color:#4A5568;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:14px;line-height:1.65;margin:0 0 16px;">{esc(block.get('description',''))}</p>
                  {cta}
                </td>
              </tr>
            </table>
          </td>
        </tr>'''

def build_rei_email(data):
    month = data.get('month', '')
    year = data.get('year', '')
    headline = data.get('headline') or f'{month} {year} Newsletter'
    subheadline = data.get('subheadline', 'Monthly insights for real estate investors')
    intro = data.get('intro', "Hello! Here's your monthly REI Grove update.")

    blocks_html = ''
    for block in data.get('blocks', []):
        btype = block.get('type', 'article')
        if btype in ('article', 'webinar'):
            blocks_html += build_article_card_rei(block)
        elif btype == 'promo':
            blocks_html += build_promo_card_rei(block)

    # REI Grove logo as styled text (email-safe)
    rei_logo = '''<table cellpadding="0" cellspacing="0" border="0" style="margin:0 auto 22px;">
      <tr>
        <td style="font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:22px;font-weight:700;color:#9FE1CB;letter-spacing:-0.3px;">REI<span style="color:#C0DD97;"> Grove</span></td>
        <td style="padding-left:8px;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:10px;color:rgba(255,255,255,0.5);vertical-align:middle;padding-top:4px;">by Innago</td>
      </tr>
    </table>'''

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="supported-color-schemes" content="light">
  <style>:root{{color-scheme:light only;}}body,table,td,a{{-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;}}img{{border:0;height:auto;line-height:100%;outline:none;text-decoration:none;}}table{{border-collapse:collapse!important;}}body{{height:100%!important;margin:0!important;padding:0!important;width:100%!important;background-color:#F5F7F5;}}</style>
</head>
<body style="margin:0;padding:0;background-color:#F5F7F5;">
<table width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr>
    <td align="center" style="padding:32px 16px;">
      <table width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.09);">

        <!-- HEADER -->
        <tr>
          <td style="background-color:#26463D;border-radius:12px 12px 0 0;padding:36px 40px;text-align:center;">
            {rei_logo}
            <h1 style="color:#ffffff;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:26px;font-weight:600;margin:0;line-height:1.3;">{esc(headline)}</h1>
            <p style="color:rgba(255,255,255,0.75);font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:15px;font-weight:300;margin:10px 0 0;line-height:1.5;">{esc(subheadline)}</p>
          </td>
        </tr>

        <!-- INTRO -->
        <tr>
          <td style="background-color:#ffffff;padding:32px 32px 16px;">
            <p style="color:#4A5568;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:15px;line-height:1.7;margin:0;">{esc(intro)}</p>
          </td>
        </tr>

        <!-- SPACER -->
        <tr><td style="background:#ffffff;height:16px;"></td></tr>

        <!-- CONTENT BLOCKS -->
        {blocks_html if blocks_html else '<tr><td style="padding:0 32px 20px;color:#999;font-family:Poppins,Arial,sans-serif;font-size:14px;text-align:center;">[No content blocks added yet]</td></tr>'}

        <!-- SPACER -->
        <tr><td style="background:#ffffff;height:12px;"></td></tr>

        <!-- FOOTER -->
        <tr>
          <td style="background-color:#EAF0E8;border-radius:0 0 12px 12px;padding:28px 40px;text-align:center;">
            <p style="color:#4A5568;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:13px;margin:0 0 10px;">Best, <strong>The REI Grove Team</strong></p>
            <p style="color:#718096;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:11px;margin:0;line-height:1.9;">
              <a href="{{{{ unsubscribe }}}}" style="color:#718096;text-decoration:underline;">Unsubscribe</a> &nbsp;&middot;&nbsp;
              <a href="https://innago.com/privacy-policy" style="color:#718096;text-decoration:underline;">Privacy Policy</a>
            </p>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body>
</html>'''

# ── Auto-generate copy ─────────────────────────────────────────────────────────

def fetch_campaign_content(campaign_id):
    """Fetch a single Brevo campaign and extract its copy."""
    try:
        r = requests.get(f'{BREVO_BASE}/emailCampaigns/{campaign_id}',
                         headers=brevo_headers(), timeout=10)
        r.raise_for_status()
        data = r.json()
        subject = data.get('subject', '')
        html_content = data.get('htmlContent', '')
        sent_date = (data.get('sentDate') or '')[:10]

        # Extract headline, subheadline, intro from HTML
        soup = BeautifulSoup(html_content, 'html.parser')
        h1 = soup.find('h1')
        headline = h1.get_text().strip() if h1 else ''

        subheadline = ''
        if h1:
            p = h1.find_next('p')
            if p:
                subheadline = p.get_text().strip()

        intro = ''
        for p in soup.find_all('p'):
            text = p.get_text().strip()
            if len(text) > 60 and text != subheadline and 'unsubscribe' not in text.lower():
                intro = text[:400]
                break

        return {
            'subject': subject,
            'headline': headline,
            'subheadline': subheadline,
            'intro': intro,
            'sent_date': sent_date,
        }
    except Exception:
        return None

def get_past_newsletters(series, limit=3):
    """Fetch the last N sent newsletters of a given series from Brevo."""
    name_prefix = 'PO - Newsletter' if series == 'po' else 'Innago Insight - Newsletter'
    try:
        r = requests.get(
            f'{BREVO_BASE}/emailCampaigns?limit=100&status=sent&sort=desc&excludeHtmlContent=true',
            headers=brevo_headers(), timeout=10
        )
        r.raise_for_status()
        campaigns = r.json().get('campaigns', [])
        matching = [c for c in campaigns if c.get('name', '').startswith(name_prefix)][:limit]
        results = []
        for c in matching:
            content = fetch_campaign_content(c['id'])
            if content:
                results.append(content)
        return results
    except Exception:
        return []

def get_history_campaigns():
    """Fetch every PO + REI Grove newsletter campaign (any status) for the History page."""
    try:
        r = requests.get(
            f'{BREVO_BASE}/emailCampaigns?limit=200&sort=desc&excludeHtmlContent=true',
            headers=brevo_headers(), timeout=15
        )
        r.raise_for_status()
        campaigns = r.json().get('campaigns', [])
        results = []
        for c in campaigns:
            name = c.get('name', '')
            if name.startswith('PO - Newsletter'):
                series = 'po'
            elif name.startswith('Innago Insight - Newsletter'):
                series = 'rei'
            else:
                continue
            results.append({
                'id': c.get('id'),
                'name': name,
                'subject': c.get('subject', ''),
                'status': c.get('status', ''),
                'series': series,
                'createdAt': (c.get('createdAt') or '')[:10],
                'sentDate': (c.get('sentDate') or '')[:10] if c.get('sentDate') else None,
            })
        return results
    except Exception as e:
        return {'error': str(e)}

def get_local_push_history():
    """Fallback source for History when the live Brevo query fails — reflects
    only what this tool pushed (won't see campaigns sent later from within Brevo)."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM push_history ORDER BY pushed_at DESC LIMIT 200')
            rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return [{
            'id': r['campaign_id'] or r['id'],
            'name': r['campaign_name'],
            'subject': r['subject'],
            'status': 'draft',  # a push only ever creates a Brevo draft
            'series': r['series'],
            'createdAt': r['pushed_at'].strftime('%Y-%m-%d') if r['pushed_at'] else None,
            'sentDate': None,
        } for r in rows]
    except Exception as e:
        print(f'Local push history fallback also failed: {e}')
        conn.close()
        return None

@app.route('/api/history')
@require_auth
def api_history():
    data = get_history_campaigns()
    if isinstance(data, dict) and data.get('error'):
        fallback = get_local_push_history()
        if fallback is not None:
            return jsonify(fallback)
        return jsonify(data), 500
    return jsonify(data)

@app.route('/api/generate', methods=['POST'])
@require_auth
def generate_copy():
    if not anthropic_sdk:
        return jsonify({'error': 'anthropic package not installed'}), 500

    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'ANTHROPIC_API_KEY not set'}), 500

    data = request.json or {}
    series = data.get('series', 'po')
    month = data.get('month', '')
    year = data.get('year', '')
    blocks = data.get('blocks', [])

    # Fetch past newsletter examples from Brevo
    examples = get_past_newsletters(series, limit=3)

    # Build content list from selected blocks
    content_lines = []
    for b in blocks:
        btype = b.get('type', 'article')
        title = b.get('title', '')
        cat = b.get('category', '')
        if btype == 'promo':
            content_lines.append(f'- [PROMO] {title}')
        elif btype == 'webinar':
            content_lines.append(f'- [WEBINAR] {title}')
        else:
            content_lines.append(f'- {title}' + (f' ({cat})' if cat else ''))

    content_summary = '\n'.join(content_lines) if content_lines else '(No articles selected yet — write generically for the month)'

    # Series context
    if series == 'po':
        series_context = (
            "This newsletter goes to Innago's property owner users — independent landlords "
            "and small property managers. Tone: friendly, practical, helpful. Opens with 'Hi there!'."
        )
    else:
        series_context = (
            "This newsletter goes to the REI Grove community — real estate investors, "
            "landlords, and property enthusiasts. Tone: informed, professional, community-driven. "
            "Opens conversationally."
        )

    # Build examples block
    if examples:
        ex_block = '\n\n'.join([
            f"[{e['sent_date']}]\n"
            f"Subject: {e['subject']}\n"
            f"Headline: {e['headline']}\n"
            f"Subheadline: {e['subheadline']}\n"
            f"Intro: {e['intro']}"
            for e in examples
        ])
        examples_section = f"Here are the last {len(examples)} newsletters to match in tone and style:\n\n{ex_block}"
    else:
        examples_section = "No previous newsletters available — write fresh copy."

    prompt = f"""You are writing copy for a monthly email newsletter.

{series_context}

{examples_section}

---

For {month} {year}, the newsletter will feature these articles and content:
{content_summary}

---

Generate copy for this month's newsletter. Return ONLY valid JSON, no markdown, no explanation:

{{
  "subject": "< catchy subject line, under 60 chars, 1 emoji OK >",
  "preview_text": "< preview/preheader text, under 90 chars >",
  "headline": "< email header headline, 4-7 words >",
  "subheadline": "< 1 short sentence describing the month's theme >",
  "intro": "< 2-3 sentence intro paragraph, warm and conversational, 'Hi there!' opening for PO series >"
}}"""

    try:
        client = anthropic_sdk.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=600,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = msg.content[0].text.strip()
        # Strip markdown fences if present
        raw = re.sub(r'^```[a-z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)
        result = json.loads(raw)
        result['_examples_used'] = f'{len(examples)} past newsletters' if examples else 'no history found'
        return jsonify(result)
    except json.JSONDecodeError as e:
        return jsonify({'error': f'Failed to parse AI response: {e}', 'raw': raw}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Preview endpoint ───────────────────────────────────────────────────────────

@app.route('/api/preview', methods=['POST'])
@require_auth
def preview():
    data = request.json or {}
    email_html = generate_email_html(data)
    return jsonify({'html': email_html})

# ── Serve frontend — standard four-page structure ─────────────────────────────

@app.route('/')
@require_auth
def index():
    return redirect('/input')

@app.route('/input')
@require_auth
def input_page():
    return send_file(os.path.join(BASE_DIR, 'input.html'))

@app.route('/approve')
@require_auth
def approve_page():
    return send_file(os.path.join(BASE_DIR, 'approve.html'))

@app.route('/history')
@require_auth
def history_page():
    return send_file(os.path.join(BASE_DIR, 'history.html'))

@app.route('/settings')
@require_auth
def settings_page():
    return send_file(os.path.join(BASE_DIR, 'settings.html'))

if __name__ == '__main__':
    print('\n🗞  Newsletter Builder running at http://localhost:5050\n')
    app.run(debug=True, port=5050, host='0.0.0.0')
