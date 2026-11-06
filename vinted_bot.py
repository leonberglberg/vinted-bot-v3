#!/usr/bin/env python3
"""
Bot de veille Vinted -> Google Sheets.
 
Usage :
  python vinted_bot.py            # un seul passage (GitHub Actions)
  python vinted_bot.py --loop     # tourne en continu (VPS / Raspberry Pi)
 
Variables d'environnement requises :
  GOOGLE_CREDS : contenu JSON du compte de service Google
  SHEET_ID     : identifiant du Google Sheets (dans l'URL)
"""
import json
import os
import random
import sys
import time
from datetime import datetime
 
import gspread
from vinted_scraper import VintedWrapper
from google.oauth2.service_account import Credentials
 
# ----------------------------------------------------------------------------
# CONFIGURATION : à modifier selon tes goûts
# ----------------------------------------------------------------------------
BASE = "https://www.vinted.fr"
MAX_PRICE = 20  # prix maximum en euros
 
# 6 = neuf avec étiquette, 1 = neuf sans étiquette, 2 = très bon état, 3 = bon état
STATUS_IDS = [6, 1, 2, 3]
 
BRANDS = [
    "Nike", "Adidas", "Carhartt", "Ralph Lauren", "Lacoste", "Levi's",
    "The North Face", "Tommy Hilfiger", "Champion", "Stüssy", "Dickies",
    "Patagonia", "Fila", "Puma", "Jordan", "Calvin Klein", "Gertrude", "Under armour"
]
SIZES = {"S", "M", "L", "XL"}  # filtre de taille pour les vêtements uniquement
 
ELECTRONICS = [
    "écouteurs", "airpods", "enceinte bluetooth", "manette", "casque audio",
    "console", "nintendo", "montre connectée", "powerbank",
]
ACCESSORIES = ["casquette", "bonnet", "bob", "écharpe"]
 
LOOP_INTERVAL_SECONDS = 600  # mode --loop : 10 minutes entre deux passages
PAUSE_BETWEEN_SEARCHES = (2.0, 5.0)  # pause aléatoire (secondes) entre requêtes
# ----------------------------------------------------------------------------
 
SEARCHES = (
    [{"cat": "Vêtements", "text": b, "brand": b, "sizes": SIZES} for b in BRANDS]
    + [{"cat": "Électronique", "text": k} for k in ELECTRONICS]
    + [{"cat": "Accessoires", "text": k} for k in ACCESSORIES]
)
 
HEADERS = ["ID", "Détecté le", "Titre", "Marque", "Prix (€)", "Taille", "État",
           "Recherche", "Lien", "Photo"]
 
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
 
 
def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)
 
 
# ----------------------------- Vinted ---------------------------------------
def make_session():
    # Vinted a supprimé l'ancien endpoint /api/v2/catalog/items (404).
    # La bibliothèque vinted-scraper gère les cookies et le nouvel endpoint.
    return VintedWrapper(BASE)
 
 
_first_call_logged = False
 
 
def fetch_items(session, text):
    global _first_call_logged
    params = {
        "search_text": text,
        "price_to": MAX_PRICE,
        "order": "newest_first",
        "per_page": 30,
        "page": 1,
        "status_ids[]": STATUS_IDS,
    }
    data = session.search(params)
    items = data.get("items", []) if isinstance(data, dict) else (data or [])
    if not _first_call_logged:
        _first_call_logged = True
        top = list(data.keys()) if isinstance(data, dict) else type(data).__name__
        log(f"Diagnostic : réponse = {top}, {len(items)} annonce(s)")
        if items and isinstance(items[0], dict):
            log(f"Diagnostic : champs d'une annonce = {sorted(items[0].keys())}")
    return items
 
 
def price_of(item):
    p = item.get("price")
    if isinstance(p, dict):
        p = p.get("amount")
    try:
        return float(p)
    except (TypeError, ValueError):
        return None
 
 
def text_of(value):
    if isinstance(value, dict):
        value = value.get("title") or value.get("name") or ""
    return value if isinstance(value, str) else ""
 
 
def status_text(item):
    return text_of(item.get("status"))
 
 
def status_ok(item):
    s = status_text(item).lower()
    if not s:
        return True  # champ absent : on fait confiance au filtre de la requête
    return any(k in s for k in ("neuf", "très bon", "tres bon", "bon état", "bon etat"))
 
 
def size_ok(item, allowed):
    if "size_title" not in item and "size" not in item:
        return True  # taille inconnue : on garde l'annonce
    raw = (text_of(item.get("size_title")) or text_of(item.get("size"))).upper()
    first = raw.split("/")[0].strip()
    return first in allowed
 
 
def brand_ok(item, brand):
    b = text_of(item.get("brand_title")) or text_of(item.get("brand")) or item.get("title", "")
    return brand.lower().replace(" ", "") in b.lower().replace(" ", "")
 
 
def keep(item, search):
    price = price_of(item)
    if price is None or price > MAX_PRICE:
        return False
    if not status_ok(item):
        return False
    if "brand" in search and not brand_ok(item, search["brand"]):
        return False
    if "sizes" in search and not size_ok(item, search["sizes"]):
        return False
    return True
 
 
# ----------------------------- Google Sheets --------------------------------
def open_spreadsheet():
    info = json.loads(os.environ["GOOGLE_CREDS"])
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(os.environ["SHEET_ID"])
 
 
def get_worksheet(sh, name):
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=1000, cols=len(HEADERS))
    if not ws.row_values(1):
        ws.update("A1", [HEADERS])
        ws.freeze(rows=1)
    return ws
 
 
# ----------------------------- Passage principal ----------------------------
def run_once():
    sh = open_spreadsheet()
    sheets = {c: get_worksheet(sh, c) for c in {s["cat"] for s in SEARCHES}}
    known = set()
    for ws in sheets.values():
        known.update(ws.col_values(1)[1:])
 
    session = make_session()
    new_rows = {c: [] for c in sheets}
    seen_now = set()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    errors = 0
 
    for search in SEARCHES:
        try:
            items = fetch_items(session, search["text"])
            errors = 0
        except Exception as e:
            errors += 1
            log(f"Erreur pour '{search['text']}' : {type(e).__name__}: {e}")
            if errors == 2:
                try:
                    session = make_session()  # nouvelle session
                except Exception as e2:
                    log(f"Impossible de recréer la session : {e2}")
            if errors >= 5:
                log("Trop d'erreurs consécutives : arrêt de ce passage.")
                break
            continue
 
        for it in items:
            iid = str(it.get("id"))
            if iid in known or iid in seen_now or not keep(it, search):
                continue
            seen_now.add(iid)
            photo = (it.get("photo") or {}).get("url", "")
            new_rows[search["cat"]].append([
                iid, now, it.get("title", ""), text_of(it.get("brand_title")) or text_of(it.get("brand")),
                price_of(it), text_of(it.get("size_title")) or text_of(it.get("size")), status_text(it),
                search["text"], it.get("url") or (BASE + it.get("path", "") if it.get("path") else ""),
                f'=IMAGE("{photo}")' if photo else "",
            ])
        time.sleep(random.uniform(*PAUSE_BETWEEN_SEARCHES))
 
    total = 0
    for cat, rows in new_rows.items():
        if rows:
            rows.sort(key=lambda r: r[4] if r[4] is not None else 0)
            sheets[cat].append_rows(rows, value_input_option="USER_ENTERED")
            total += len(rows)
    log(f"{total} nouvelle(s) annonce(s) ajoutée(s)")
 
 
if __name__ == "__main__":
    if "--loop" in sys.argv:
        while True:
            try:
                run_once()
            except Exception as e:
                log(f"Erreur : {e}")
            time.sleep(LOOP_INTERVAL_SECONDS + random.uniform(0, 60))
    else:
        run_once()
