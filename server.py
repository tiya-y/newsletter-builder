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
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
CORS(app)

BREVO_BASE = 'https://api.brevo.com/v3'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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
        return jsonify({'ok': True, 'campaign_id': resp.get('id'), 'name': campaign_name})
    except requests.HTTPError as e:
        return jsonify({'error': e.response.text}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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

@app.route('/api/rei-subscription-promos')
@require_auth
def get_rei_subscription_promos():
    if not os.path.exists(SUBSCRIPTION_PROMOS_FILE):
        return jsonify([])
    with open(SUBSCRIPTION_PROMOS_FILE) as f:
        return jsonify(json.load(f))

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

def build_article_card_po(block):
    img_row = ''
    if block.get('image'):
        img_row = f'''
        <tr>
          <td style="padding:0; line-height:0;">
            <img src="{esc(block['image'])}" alt="{esc(block.get('title',''))}"
                 width="100%" style="display:block; width:100%; max-height:220px; object-fit:cover; border-radius:10px 10px 0 0;">
          </td>
        </tr>'''

    cat_tag = ''
    if block.get('category'):
        cat_tag = f'<p style="color:#2676FF;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.8px;margin:0 0 8px 0;">{esc(block["category"])}</p>'

    return f'''
        <tr>
          <td style="padding:0 32px 20px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E7E7E7;border-radius:10px;overflow:hidden;background:#ffffff;">
              {img_row}
              <tr>
                <td style="padding:20px 24px 22px;">
                  {cat_tag}
                  <p style="color:#2E3B47;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:17px;font-weight:700;margin:0 0 8px;line-height:1.35;">{esc(block.get('title',''))}</p>
                  <p style="color:#69727A;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:14px;line-height:1.65;margin:0 0 16px;">{esc(block.get('description',''))}</p>
                  <a href="{esc(block.get('url','#'))}" style="color:#2676FF;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:14px;font-weight:600;text-decoration:none;">Read more &rarr;</a>
                </td>
              </tr>
            </table>
          </td>
        </tr>'''

def build_promo_card_po(block):
    img_row = ''
    if block.get('image'):
        img_row = f'''
        <tr>
          <td style="padding:0; line-height:0;">
            <img src="{esc(block['image'])}" alt="{esc(block.get('title',''))}"
                 width="100%" style="display:block;width:100%;max-height:200px;object-fit:cover;border-radius:8px 8px 0 0;">
          </td>
        </tr>'''

    cta = ''
    if block.get('cta_url') and block.get('cta_text'):
        cta = f'<a href="{esc(block["cta_url"])}" style="display:inline-block;background-color:#2676FF;color:#ffffff;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:14px;font-weight:600;text-decoration:none;padding:12px 28px;border-radius:8px;letter-spacing:0.2px;">{esc(block["cta_text"])}</a>'

    return f'''
        <tr>
          <td style="padding:0 32px 20px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f5ff;border:1px solid #dce8ff;border-left:4px solid #2676FF;border-radius:8px;overflow:hidden;">
              {img_row}
              <tr>
                <td style="padding:20px 24px 22px;">
                  <p style="color:#2E3B47;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:16px;font-weight:700;margin:0 0 8px;">{esc(block.get('title',''))}</p>
                  <p style="color:#69727A;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:14px;line-height:1.65;margin:0 0 16px;">{esc(block.get('description',''))}</p>
                  {cta}
                </td>
              </tr>
            </table>
          </td>
        </tr>'''

def build_po_email(data):
    month = data.get('month', '')
    year = data.get('year', '')
    headline = data.get('headline') or f'{month} {year} Newsletter'
    subheadline = data.get('subheadline', "What's new at Innago this month")
    intro = data.get('intro', 'Hi there! Here\'s your monthly update from Innago.')

    blocks_html = ''
    for block in data.get('blocks', []):
        btype = block.get('type', 'article')
        if btype in ('article', 'webinar'):
            blocks_html += build_article_card_po(block)
        elif btype == 'promo':
            blocks_html += build_promo_card_po(block)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="supported-color-schemes" content="light">
  <style>:root{{color-scheme:light only;}}body,table,td,a{{-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;}}img{{border:0;height:auto;line-height:100%;outline:none;text-decoration:none;}}table{{border-collapse:collapse!important;}}body{{height:100%!important;margin:0!important;padding:0!important;width:100%!important;background-color:#F4F5F7;}}</style>
</head>
<body style="margin:0;padding:0;background-color:#F4F5F7;">
<table width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr>
    <td align="center" style="padding:32px 16px;">
      <table width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.09);">

        <!-- HEADER -->
        <tr>
          <td style="background-color:#2676FF;border-radius:12px 12px 0 0;padding:36px 40px;text-align:center;">
            <img src="https://res.cloudinary.com/dam3qptkg/image/upload/v1773275475/Innago_White_transparent_2_ww7aro.png"
                 alt="Innago" height="34" style="display:block;margin:0 auto 22px;">
            <h1 style="color:#ffffff;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:26px;font-weight:700;margin:0;line-height:1.3;">{esc(headline)}</h1>
            <p style="color:rgba(255,255,255,0.85);font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:15px;font-weight:300;margin:10px 0 0;line-height:1.5;">{esc(subheadline)}</p>
          </td>
        </tr>

        <!-- INTRO -->
        <tr>
          <td style="background-color:#ffffff;padding:32px 32px 16px;">
            <p style="color:#69727A;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:15px;line-height:1.7;margin:0;">{esc(intro)}</p>
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
          <td style="background-color:#F4F5F7;border-radius:0 0 12px 12px;padding:28px 40px;text-align:center;">
            <p style="color:#69727A;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:13px;margin:0 0 10px;">Best, <strong>The Innago Team</strong></p>
            <p style="color:#9AA0A6;font-family:\'Poppins\',\'Segoe UI\',Arial,sans-serif;font-size:11px;margin:0;line-height:1.9;">
              <a href="{{{{ unsubscribe }}}}" style="color:#9AA0A6;text-decoration:underline;">Unsubscribe</a> &nbsp;&middot;&nbsp;
              <a href="https://innago.com/privacy-policy" style="color:#9AA0A6;text-decoration:underline;">Privacy Policy</a> &nbsp;&middot;&nbsp;
              <a href="https://innago.com" style="color:#9AA0A6;text-decoration:underline;">Visit Innago</a>
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

@app.route('/api/history')
@require_auth
def api_history():
    data = get_history_campaigns()
    if isinstance(data, dict) and data.get('error'):
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
