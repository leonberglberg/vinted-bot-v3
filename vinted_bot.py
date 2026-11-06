
#!/usr/bin/env python3
"""
Bot de veille Vinted -> Google Sheets.

Usage :
  python vinted_bot.py
  python vinted_bot.py --loop

Variables d'environnement :
  GOOGLE_CREDS : JSON du compte de service Google
  SHEET_ID     : identifiant Google Sheets
"""

import json
import os
import random
import sys
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import gspread
from vinted_scraper import VintedWrapper
from google.oauth2.service_account import Credentials


# ----------------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------------

BASE = "https://www.vinted.fr"

MAX_PRICE = 20

STATUS_IDS = [6, 1, 2, 3]

BRANDS = [
    "Nike", "Adidas", "Carhartt", "Ralph Lauren",
    "Lacoste", "Levi's", "The North Face",
    "Tommy Hilfiger", "Champion", "Stüssy",
    "Dickies", "Patagonia", "Fila", "Puma",
    "Jordan", "Calvin Klein",
]

SIZES = {"S", "M", "L"}

ELECTRONICS = [
    "écouteurs", "airpods", "enceinte bluetooth",
    "manette", "casque audio", "console",
    "nintendo", "montre connectée", "powerbank",
]

ACCESSORIES = [
    "casquette", "bonnet", "bob", "écharpe"
]

LOOP_INTERVAL_SECONDS = 600

PAUSE_BETWEEN_SEARCHES = (2.0, 5.0)

# ----------------------------------------------------------------------------

SEARCHES = (
    [
        {
            "cat": "Vêtements",
            "text": b,
            "brand": b,
            "sizes": SIZES
        }
        for b in BRANDS
    ]
    +
    [
        {"cat": "Électronique", "text": k}
        for k in ELECTRONICS
    ]
    +
    [
        {"cat": "Accessoires", "text": k}
        for k in ACCESSORIES
    ]
)

HEADERS = [
    "ID",
    "Détecté le",
    "Titre",
    "Marque",
    "Prix (€)",
    "Taille",
    "État",
    "Recherche",
    "Lien",
    "Photo"
]


def log(msg):
    print(
        f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}",
        flush=True
    )


# ----------------------------------------------------------------------------
# VINTED
# ----------------------------------------------------------------------------

def make_session():
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

    items = (
        data.get("items", [])
        if isinstance(data, dict)
        else (data or [])
    )

    # Diagnostic complet de la première annonce
    if not _first_call_logged:

        _first_call_logged = True

        log("========== DIAGNOSTIC VINTED ==========")

        log(
            f"Type de réponse : {type(data).__name__}"
        )

        log(
            f"Nombre d'annonces : {len(items)}"
        )

        if items:

            log(
                "DONNEES COMPLETES DE LA PREMIERE ANNONCE :"
            )

            try:
                print(
                    json.dumps(
                        items[0],
                        indent=4,
                        ensure_ascii=False,
                        default=str
                    ),
                    flush=True
                )

            except Exception as e:
                log(f"Impossible d'afficher les données : {e}")

        log("=======================================")

    return items


# ----------------------------------------------------------------------------
# RECUPERATION DU LIEN DIRECT
# ----------------------------------------------------------------------------

def get_item_url(item):

    item_id = item.get("id")

    if not item_id:
        return ""

    # Champs pouvant contenir le véritable lien
    possible_urls = [
        item.get("url"),
        item.get("item_url"),
        item.get("share_url"),
        item.get("canonical_url"),
        item.get("path"),
    ]

    # Certains objets peuvent contenir un lien dans un objet imbriqué
    for key in ("item", "share", "links"):

        nested = item.get(key)

        if isinstance(nested, dict):

            possible_urls.extend([
                nested.get("url"),
                nested.get("item_url"),
                nested.get("share_url"),
                nested.get("canonical_url"),
                nested.get("path"),
            ])

    for candidate in possible_urls:

        if not isinstance(candidate, str):
            continue

        candidate = candidate.strip()

        if not candidate:
            continue

        if candidate.startswith("//"):
            candidate = "https:" + candidate

        elif candidate.startswith("/"):
            candidate = urljoin(BASE, candidate)

        parsed = urlparse(candidate)

        if (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and (
                parsed.hostname == "vinted.fr"
                or parsed.hostname.endswith(".vinted.fr")
            )
            and "/items/" in parsed.path
        ):

            # Vérification que le lien correspond à l'annonce
            if str(item_id) in parsed.path.split("/")[-1]:
                return candidate

    log(
        f"Aucun lien direct exploitable pour l'annonce {item_id}"
    )

    return ""


# ----------------------------------------------------------------------------
# DONNEES DES ANNONCES
# ----------------------------------------------------------------------------

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

        value = (
            value.get("title")
            or value.get("name")
            or ""
        )

    return value if isinstance(value, str) else ""


def status_text(item):

    return text_of(item.get("status"))


def status_ok(item):

    s = status_text(item).lower()

    if not s:
        return True

    return any(
        k in s
        for k in (
            "neuf",
            "très bon",
            "tres bon",
            "bon état",
            "bon etat"
        )
    )


def size_ok(item, allowed):

    if (
        "size_title" not in item
        and "size" not in item
    ):
        return True

    raw = (
        text_of(item.get("size_title"))
        or text_of(item.get("size"))
    ).upper()

    first = raw.split("/")[0].strip()

    return first in allowed


def brand_ok(item, brand):

    b = (
        text_of(item.get("brand_title"))
        or text_of(item.get("brand"))
        or item.get("title", "")
    )

    return (
        brand.lower().replace(" ", "")
        in b.lower().replace(" ", "")
    )


def keep(item, search):

    price = price_of(item)

    if price is None or price > MAX_PRICE:
        return False

    if not status_ok(item):
        return False

    if "brand" in search:

        if not brand_ok(item, search["brand"]):
            return False

    if "sizes" in search:

        if not size_ok(item, search["sizes"]):
            return False

    return True


# ----------------------------------------------------------------------------
# PHOTOS
# ----------------------------------------------------------------------------

def get_photo_url(item):

    photo = item.get("photo") or {}

    if isinstance(photo, dict):
        return photo.get("url", "")

    if isinstance(photo, str):
        return photo

    return ""


# ----------------------------------------------------------------------------
# GOOGLE SHEETS
# ----------------------------------------------------------------------------

def open_spreadsheet():

    info = json.loads(
        os.environ["GOOGLE_CREDS"]
    )

    creds = Credentials.from_service_account_info(
        info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets"
        ]
    )

    return gspread.authorize(
        creds
    ).open_by_key(
        os.environ["SHEET_ID"]
    )


def get_worksheet(sh, name):

    try:
        ws = sh.worksheet(name)

    except gspread.WorksheetNotFound:

        ws = sh.add_worksheet(
            title=name,
            rows=1000,
            cols=len(HEADERS)
        )

    if not ws.row_values(1):

        ws.update("A1", [HEADERS])
        ws.freeze(rows=1)

    return ws


# ----------------------------------------------------------------------------
# PASSAGE PRINCIPAL
# ----------------------------------------------------------------------------

def run_once():

    log("Démarrage du bot Vinted")

    sh = open_spreadsheet()

    sheets = {
        c: get_worksheet(sh, c)
        for c in {s["cat"] for s in SEARCHES}
    }

    known = set()

    for ws in sheets.values():
        known.update(ws.col_values(1)[1:])

    session = make_session()

    new_rows = {
        c: []
        for c in sheets
    }

    seen_now = set()

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M"
    )

    errors = 0

    for search in SEARCHES:

        try:

            items = fetch_items(
                session,
                search["text"]
            )

            errors = 0

        except Exception as e:

            errors += 1

            log(
                f"Erreur pour '{search['text']}' : "
                f"{type(e).__name__}: {e}"
            )

            if errors == 2:

                try:
                    session = make_session()
                    log("Nouvelle session Vinted créée")

                except Exception as e2:
                    log(f"Erreur de session : {e2}")

            if errors >= 5:

                log(
                    "Trop d'erreurs consécutives : "
                    "arrêt de ce passage."
                )

                break

            continue

        for it in items:

            iid = str(it.get("id", ""))

            if (
                not iid
                or iid in known
                or iid in seen_now
            ):
                continue

            if not keep(it, search):
                continue

            # Récupération obligatoire du lien direct
            link = get_item_url(it)

            # Pas d'ajout sans lien direct
            if not link:
                continue

            seen_now.add(iid)

            log(f"Lien direct récupéré : {link}")

            photo = get_photo_url(it)

            photo_formula = (
                f'=IMAGE("{photo}")'
                if photo
                else ""
            )

            new_rows[search["cat"]].append([

                iid,

                now,

                it.get("title", ""),

                (
                    text_of(it.get("brand_title"))
                    or text_of(it.get("brand"))
                ),

                price_of(it),

                (
                    text_of(it.get("size_title"))
                    or text_of(it.get("size"))
                ),

                status_text(it),

                search["text"],

                link,

                photo_formula,

            ])

        time.sleep(
            random.uniform(*PAUSE_BETWEEN_SEARCHES)
        )

    # Enregistrement Google Sheets
    total = 0

    for cat, rows in new_rows.items():

        if rows:

            rows.sort(
                key=lambda r: (
                    r[4]
                    if r[4] is not None
                    else 0
                )
            )

            sheets[cat].append_rows(
                rows,
                value_input_option="USER_ENTERED"
            )

            total += len(rows)

            log(
                f"{len(rows)} annonce(s) ajoutée(s) "
                f"dans {cat}"
            )

    log(
        f"Terminé : {total} nouvelle(s) annonce(s) ajoutée(s)"
    )


# ----------------------------------------------------------------------------
# LANCEMENT
# ----------------------------------------------------------------------------

if __name__ == "__main__":

    if "--loop" in sys.argv:

        log("Mode continu activé")

        while True:

            try:
                run_once()

            except Exception as e:

                log(
                    f"Erreur générale : "
                    f"{type(e).__name__}: {e}"
                )

            time.sleep(
                LOOP_INTERVAL_SECONDS
                + random.uniform(0, 60)
            )

    else:

        run_once()
