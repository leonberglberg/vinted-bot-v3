
#!/usr/bin/env python3
"""
Bot de veille Vinted -> Google Sheets.

Usage :
  python vinted_bot.py
  python vinted_bot.py --loop

Variables d'environnement :
  GOOGLE_CREDS : contenu JSON du compte de service Google
  SHEET_ID     : identifiant du Google Sheets
"""

import json
import os
import random
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import gspread
from vinted_scraper import VintedWrapper
from google.oauth2.service_account import Credentials


# ----------------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------------

BASE = "https://www.vinted.fr"

MAX_PRICE = 20

# 6 = neuf avec étiquette
# 1 = neuf sans étiquette
# 2 = très bon état
# 3 = bon état
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

# Nettoyage des annonces après 72 heures
ANNOUNCEMENT_MAX_AGE_HOURS = 72

# Mode continu : 10 minutes entre deux passages
LOOP_INTERVAL_SECONDS = 600

# Pause entre les recherches
PAUSE_BETWEEN_SEARCHES = (2.0, 5.0)


# ----------------------------------------------------------------------------
# RECHERCHES
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
        {
            "cat": "Électronique",
            "text": k
        }
        for k in ELECTRONICS
    ]
    +
    [
        {
            "cat": "Accessoires",
            "text": k
        }
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

    # Diagnostic de la première annonce
    if not _first_call_logged:

        _first_call_logged = True

        log("========== DIAGNOSTIC VINTED ==========")

        log(f"Type de réponse : {type(data).__name__}")
        log(f"Nombre d'annonces : {len(items)}")

        if items:

            log("DONNEES COMPLETES DE LA PREMIERE ANNONCE :")

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

                log(f"Diagnostic impossible : {e}")

        log("=======================================")

    return items


# ----------------------------------------------------------------------------
# RECUPERATION DU LIEN DIRECT VINTED
# ----------------------------------------------------------------------------

def get_item_url(item):

    """
    Récupère uniquement un lien direct fourni par Vinted.
    Aucun lien de recherche ou lien artificiellement construit.
    """

    item_id = item.get("id")

    if not item_id:
        return ""

    possible_urls = [
        item.get("url"),
        item.get("item_url"),
        item.get("share_url"),
        item.get("canonical_url"),
        item.get("path"),
    ]

    # Recherche éventuelle dans les objets imbriqués
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

        if not (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and (
                parsed.hostname == "vinted.fr"
                or parsed.hostname.endswith(".vinted.fr")
            )
            and "/items/" in parsed.path
        ):
            continue

        # Vérification de l'identifiant de l'annonce
        path_parts = parsed.path.rstrip("/").split("/")

        if any(
            part.startswith(str(item_id))
            for part in path_parts
            if part.isdigit() or part.startswith(str(item_id))
        ):

            return candidate

    log(
        f"Aucun lien direct exploitable pour l'annonce {item_id}"
    )

    return ""


# ----------------------------------------------------------------------------
# PRIX / TEXTE / ETAT / TAILLE
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
# SUPPRESSION AUTOMATIQUE DES ANNONCES DE PLUS DE 3 JOURS
# ----------------------------------------------------------------------------

def delete_old_announcements(sheets):

    """
    Supprime les annonces détectées depuis plus de 72 heures.

    Colonne A : ID
    Colonne B : date de détection

    La première ligne contenant les titres est conservée.
    """

    now = datetime.now()

    limit = now - timedelta(
        hours=ANNOUNCEMENT_MAX_AGE_HOURS
    )

    total_deleted = 0

    log("Début du nettoyage des anciennes annonces")

    for category, ws in sheets.items():

        rows = ws.get_all_values()

        rows_to_delete = []

        # On commence à la ligne 2
        for row_number, row in enumerate(
            rows[1:],
            start=2
        ):

            if len(row) < 2 or not row[1].strip():
                continue

            try:

                detected_date = datetime.strptime(
                    row[1].strip(),
                    "%Y-%m-%d %H:%M"
                )

                if detected_date < limit:

                    rows_to_delete.append(row_number)

            except ValueError:

                log(
                    f"Date invalide dans {category}, "
                    f"ligne {row_number} : {row[1]}"
                )

        # Suppression de bas en haut
        for row_number in reversed(rows_to_delete):

            ws.delete_rows(row_number)

            total_deleted += 1

        if rows_to_delete:

            log(
                f"{len(rows_to_delete)} annonce(s) supprimée(s) "
                f"dans {category}"
            )

    log(
        f"Nettoyage terminé : {total_deleted} annonce(s) supprimée(s)"
    )


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

    # 1. Nettoyage automatique des annonces de plus de 72 heures
    delete_old_announcements(sheets)

    # 2. Récupération des identifiants déjà présents
    known = set()

    for ws in sheets.values():

        known.update(
            str(value)
            for value in ws.col_values(1)[1:]
        )

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

    # 3. Recherches Vinted
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

                    log(
                        f"Impossible de recréer la session : {e2}"
                    )

            if errors >= 5:

                log(
                    "Trop d'erreurs consécutives : "
                    "arrêt de ce passage."
                )

                break

            continue

        # 4. Analyse des annonces
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

            # Pas d'ajout si le lien direct est absent
            if not link:

                continue

            seen_now.add(iid)

            log(
                f"Lien direct récupéré : {link}"
            )

            # Récupération de la photo
            photo = get_photo_url(it)

            photo_formula = (
                f'=IMAGE("{photo}")'
                if photo
                else ""
            )

            # Préparation de la ligne
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
            random.uniform(
                *PAUSE_BETWEEN_SEARCHES
            )
        )

    # 5. Enregistrement dans Google Sheets
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
