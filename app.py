"""
Scribe — ton ombudsman personnel (version Flask).

Application Flask qui analyse tes factures et abonnements, suit tes depenses
bancaires, et te donne le controle total sur tes finances personnelles.

Tout reste en local : les documents ne sont envoyes qu'a l'API Gemini pour
etre analyses, et l'historique est stocke dans de simples fichiers CSV/JSON
sur ton ordinateur. Rien n'est envoye ailleurs.
"""

import base64
import io
import json
import mimetypes
import os
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import requests as http_requests

import pandas as pd
from flask import (
    Flask, render_template, request, jsonify, redirect, url_for,
    send_file, flash, session,
)

try:
    from google import genai
    from google.genai import types as genai_types
    _HAS_GENAI = True
except ImportError:
    _HAS_GENAI = False

try:
    from woob.core import Woob
    from woob.capabilities.bill import CapDocument
    from woob.capabilities.bank import CapBank
    _HAS_WOOB = True
except ImportError:
    _HAS_WOOB = False

try:
    import jwt as pyjwt
    _HAS_PYJWT = True
except ImportError:
    _HAS_PYJWT = False

# --------------------------------------------------------------------------
# Flask app
# --------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.urandom(24)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
HISTORY_PATH = DATA_DIR / "history.csv"

MODEL = "gemini-2.5-flash"
HISTORY_COLUMNS = [
    "date_ajout", "fournisseur", "type_contrat", "montant", "devise",
    "date_facture", "numero_contrat", "periode", "notes", "fichier",
]

ANOMALY_PCT_THRESHOLD = 5.0
ANOMALY_ABS_THRESHOLD = 2.0

PAYSLIP_PATH = DATA_DIR / "payslips.csv"
PAYSLIP_COLUMNS = [
    "date_ajout", "employeur", "mois", "salaire_brut", "salaire_net",
    "net_imposable", "devise", "date_fiche", "notes", "fichier",
]

BILLS_DIR = DATA_DIR / "bills"
BILLS_DIR.mkdir(exist_ok=True)

BANK_TX_PATH = DATA_DIR / "bank_transactions.csv"
BANK_TX_COLUMNS = [
    "date", "label", "amount", "category", "bank_name", "account_label",
    "date_import",
]

SAVINGS_PATH = DATA_DIR / "savings_goals.json"
CATEGORY_BUDGETS_PATH = DATA_DIR / "category_budgets.json"
USER_PREFS_PATH = DATA_DIR / "user_prefs.json"
EB_CONFIG_PATH = DATA_DIR / "enable_banking.json"

# --------------------------------------------------------------------------
# Enable Banking (Open Banking)
# --------------------------------------------------------------------------

EB_BASE = "https://api.enablebanking.com"


def _eb_load():
    """Charge la config Enable Banking depuis le fichier JSON."""
    if EB_CONFIG_PATH.exists():
        try:
            return json.loads(EB_CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}


def _eb_save(data):
    """Sauvegarde la config Enable Banking."""
    EB_CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _eb_configured():
    """Verifie si Enable Banking est configure (Application ID + cle privee)."""
    return bool(os.environ.get("EB_APPLICATION_ID", "")) and bool(os.environ.get("EB_PRIVATE_KEY", ""))


def _eb_create_jwt():
    """Cree un JWT pour l'API Enable Banking (RS256)."""
    if not _HAS_PYJWT:
        return None, "PyJWT non installe (pip install PyJWT[crypto])"

    app_id = os.environ.get("EB_APPLICATION_ID", "")
    key_data = os.environ.get("EB_PRIVATE_KEY", "")
    if not app_id or not key_data:
        return None, "Enable Banking non configure (EB_APPLICATION_ID / EB_PRIVATE_KEY manquants)"

    # Decoder la cle privee (base64 ou PEM brut avec \n)
    try:
        if key_data.startswith("-----"):
            key_bytes = key_data.replace("\\n", "\n").encode()
        else:
            key_bytes = base64.b64decode(key_data)
    except Exception as e:
        return None, f"Erreur cle privee: {e}"

    now = int(datetime.now().timestamp())
    payload = {
        "iss": "enablebanking.com",
        "aud": "api.enablebanking.com",
        "iat": now,
        "exp": now + 3600,
    }
    try:
        token = pyjwt.encode(payload, key_bytes, algorithm="RS256",
                              headers={"kid": app_id})
        return token, None
    except Exception as e:
        return None, f"Erreur creation JWT: {e}"


def _eb_headers():
    """Cree les headers d'authentification Enable Banking."""
    token, err = _eb_create_jwt()
    if err:
        return None, err
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, None


def eb_list_banks(country="FR"):
    """Liste les banques disponibles pour un pays via Enable Banking."""
    headers, err = _eb_headers()
    if err:
        return [], err
    resp = http_requests.get(f"{EB_BASE}/aspsps?country={country}",
                             headers=headers, timeout=15)
    if resp.status_code != 200:
        return [], f"Erreur: {resp.status_code} - {resp.text}"
    return resp.json().get("aspsps", []), None


def eb_start_auth(bank_name, bank_country, redirect_url):
    """Initie l'authentification bancaire via Enable Banking."""
    headers, err = _eb_headers()
    if err:
        return None, err
    state = str(uuid4())
    valid_until = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    payload = {
        "access": {"valid_until": valid_until},
        "aspsp": {"name": bank_name, "country": bank_country},
        "state": state,
        "redirect_url": redirect_url,
        "psu_type": "personal",
    }
    resp = http_requests.post(f"{EB_BASE}/auth", headers=headers,
                              json=payload, timeout=15)
    if resp.status_code not in (200, 201):
        return None, f"Erreur authentification: {resp.status_code} - {resp.text}"
    result = resp.json()
    result["state"] = state
    return result, None


def eb_create_session(auth_code):
    """Cree une session Enable Banking avec le code d'autorisation."""
    headers, err = _eb_headers()
    if err:
        return None, err
    resp = http_requests.post(f"{EB_BASE}/sessions", headers=headers,
                              json={"code": auth_code}, timeout=15)
    if resp.status_code not in (200, 201):
        return None, f"Erreur session: {resp.status_code} - {resp.text}"
    return resp.json(), None


def eb_fetch_transactions(account_uid, date_from=None):
    """Recupere les transactions d'un compte via Enable Banking."""
    headers, err = _eb_headers()
    if err:
        return [], err
    url = f"{EB_BASE}/accounts/{account_uid}/transactions"
    params = {}
    if date_from:
        params["date_from"] = date_from
    resp = http_requests.get(url, headers=headers, params=params, timeout=30)
    if resp.status_code != 200:
        return [], f"Erreur: {resp.status_code} - {resp.text}"
    data = resp.json()
    return data.get("transactions", []), None


def eb_fetch_balances(account_uid):
    """Recupere les soldes d'un compte via Enable Banking."""
    headers, err = _eb_headers()
    if err:
        return None, err
    resp = http_requests.get(f"{EB_BASE}/accounts/{account_uid}/balances",
                             headers=headers, timeout=15)
    if resp.status_code != 200:
        return None, f"Erreur: {resp.status_code}"
    return resp.json().get("balances", []), None


def eb_sync_all_accounts():
    """Synchronise toutes les transactions des comptes connectes."""
    eb_data = _eb_load()
    accounts = eb_data.get("accounts", [])
    if not accounts:
        return 0, "Aucun compte connecte"

    all_transactions = []
    for acc in accounts:
        account_uid = acc.get("uid")
        if not account_uid:
            continue
        txs, err = eb_fetch_transactions(account_uid)
        if err:
            continue
        if not isinstance(txs, list):
            continue
        for tx in txs:
            try:
                # Enable Banking suit le format PSD2 Berlin Group
                # Le montant peut etre dans transactionAmount.amount ou amount directement
                if isinstance(tx.get("transactionAmount"), dict):
                    amount_raw = float(tx["transactionAmount"].get("amount", 0))
                else:
                    amount_raw = float(tx.get("amount", 0))

                if amount_raw >= 0:
                    continue  # Ignorer les credits (revenus)

                tx_date = tx.get("bookingDate") or tx.get("valueDate") or tx.get("date", "")
                label = (tx.get("remittanceInformationUnstructured")
                         or (tx.get("remittanceInformationUnstructuredArray", [None]) or [None])[0]
                         or tx.get("creditorName")
                         or tx.get("debtorName")
                         or "Transaction")
                all_transactions.append({
                    "date": tx_date,
                    "label": label.strip() if isinstance(label, str) else str(label),
                    "amount": round(abs(amount_raw), 2),
                    "category": "",
                    "bank_name": acc.get("bank_name", "Banque"),
                    "account_label": acc.get("iban", ""),
                    "date_import": datetime.now().strftime("%Y-%m-%d %H:%M"),
                })
            except Exception:
                continue

    if not all_transactions:
        return 0, None

    count, err = import_transactions_from_list(all_transactions)
    return count, err

# --------------------------------------------------------------------------
# Categorisation automatique des transactions
# --------------------------------------------------------------------------

TRANSACTION_CATEGORIES = {
    "Alimentation": {
        "icon": "cart",
        "emoji": "\U0001f6d2",
        "color": "#22c55e",
        "keywords": [
            "CARREFOUR", "LECLERC", "AUCHAN", "LIDL", "INTERMARCHE", "SUPER U",
            "MONOPRIX", "FRANPRIX", "PICARD", "CASINO", "ALDI", "NETTO",
            "SPAR", "CORA", "MATCH", "BOULANGERIE", "BOUCHERIE", "PRIMEUR",
            "EPICERIE", "BIOCOOP", "NATURALIA", "GRAND FRAIS", "MARCHE",
            "DELIVEROO", "UBER EATS", "JUST EAT", "MC DONALD", "MCDO",
            "BURGER KING", "KFC", "SUBWAY", "DOMINOS", "PIZZA", "SUSHI",
            "RESTAURANT", "RESTO", "BRASSERIE", "CAFE", "STARBUCKS",
        ],
    },
    "Transport": {
        "icon": "car",
        "emoji": "\U0001f697",
        "color": "#3b82f6",
        "keywords": [
            "SNCF", "RATP", "NAVIGO", "UBER", "BOLT", "TAXI", "BLABLACAR",
            "TOTAL ENERGIES", "TOTALENERGIES", "SHELL", "BP ", "ESSO",
            "STATION SERVICE", "CARBURANT", "ESSENCE", "GASOIL", "PEAGE",
            "AUTOROUTE", "PARKING", "STATIONNEMENT", "VINCI", "SANEF",
            "VELIB", "LIME", "TIER", "BIRD", "CITROEN", "RENAULT",
            "PEUGEOT", "CONTROLE TECHNIQUE", "ASSURANCE AUTO",
        ],
    },
    "Logement": {
        "icon": "home",
        "emoji": "\U0001f3e0",
        "color": "#f59e0b",
        "keywords": [
            "LOYER", "CHARGES", "SYNDIC", "FONCIA", "NEXITY", "ORPI",
            "EDF", "ENGIE", "GDF", "VEOLIA", "SUEZ", "ELECTRICITE",
            "GAZ", "CHAUFFAGE", "TAXE HABITATION", "TAXE FONCIERE",
            "ASSURANCE HABITATION", "MRH",
        ],
    },
    "Sante": {
        "icon": "heart-pulse",
        "emoji": "\U0001f3e5",
        "color": "#ef4444",
        "keywords": [
            "PHARMACIE", "MEDECIN", "DOCTEUR", "HOPITAL", "CLINIQUE",
            "DENTISTE", "OPTICIEN", "KINE", "OSTEO", "CPAM", "AMELI",
            "MUTUELLE", "SANTE", "LABORATOIRE", "LABO ", "OPTIQUE",
            "LUNETTES", "DENTAL", "ORTHODONT",
        ],
    },
    "Loisirs": {
        "icon": "gamepad-2",
        "emoji": "\U0001f3ae",
        "color": "#8b5cf6",
        "keywords": [
            "NETFLIX", "SPOTIFY", "DEEZER", "DISNEY", "AMAZON PRIME",
            "CANAL+", "CANAL PLUS", "OCS", "APPLE MUSIC", "YOUTUBE",
            "GAMING", "STEAM", "PLAYSTATION", "XBOX", "NINTENDO",
            "CINEMA", "UGC", "PATHE", "GAUMONT", "FNAC", "CULTURA",
            "CONCERT", "SPECTACLE", "THEATRE", "MUSEE", "SPORT",
            "FITNESS", "SALLE DE SPORT", "BASIC FIT", "KEEP COOL",
        ],
    },
    "Shopping": {
        "icon": "shopping-bag",
        "emoji": "\U0001f6cd️",
        "color": "#ec4899",
        "keywords": [
            "AMAZON", "CDISCOUNT", "ALIEXPRESS", "SHEIN", "ZALANDO",
            "ZARA", "H&M", "KIABI", "DECATHLON", "IKEA", "LEROY MERLIN",
            "CASTORAMA", "DARTY", "BOULANGER", "ELECTRO", "VINTED",
            "LEBONCOIN", "ACTION", "GIFI", "HEMA", "PRIMARK", "UNIQLO",
        ],
    },
    "Assurance": {
        "icon": "shield",
        "emoji": "\U0001f6e1️",
        "color": "#14b8a6",
        "keywords": [
            "ASSURANCE", "AXA", "MAIF", "MACIF", "MATMUT", "ALLIANZ",
            "GROUPAMA", "MMA", "GMF", "ACM", "GENERALI", "DIRECT ASSUR",
            "OLIVIER ASSURANCE", "PRLV ASSUR",
        ],
    },
    "Telecom": {
        "icon": "smartphone",
        "emoji": "\U0001f4f1",
        "color": "#06b6d4",
        "keywords": [
            "ORANGE", "FREE", "SFR", "BOUYGUES", "SOSH", "RED BY SFR",
            "B&YOU", "PRIXTEL", "OVH", "ICLOUD", "GOOGLE STORAGE",
        ],
    },
    "Impots & Taxes": {
        "icon": "landmark",
        "emoji": "\U0001f3db️",
        "color": "#78716c",
        "keywords": [
            "IMPOT", "TRESOR PUBLIC", "DGFIP", "TAXE", "AMENDE",
            "CONTRIB", "PRELEVEMENT SOURCE", "URSSAF", "CAF",
        ],
    },
    "Autre": {
        "icon": "package",
        "emoji": "\U0001f4e6",
        "color": "#6b7280",
        "keywords": [],
    },
}


def categorize_transaction(label: str) -> str:
    up = label.upper().strip()
    for cat_name, cat_info in TRANSACTION_CATEGORIES.items():
        if cat_name == "Autre":
            continue
        for kw in cat_info["keywords"]:
            if kw in up:
                return cat_name
    return "Autre"


def categorize_all_transactions(tx_df: pd.DataFrame) -> pd.DataFrame:
    df = tx_df.copy()
    df["auto_category"] = df["label"].apply(categorize_transaction)
    return df


# --------------------------------------------------------------------------
# Banques et fournisseurs connus
# --------------------------------------------------------------------------

KNOWN_BANKS = {
    "BNP Paribas": {"module": "bnporc", "icon": "bank"},
    "Credit Agricole": {"module": "cragr", "icon": "bank"},
    "Societe Generale": {"module": "societegenerale", "icon": "bank"},
    "La Banque Postale": {"module": "bp", "icon": "bank"},
    "Credit Mutuel": {"module": "creditmutuel", "icon": "bank"},
    "CIC": {"module": "cic", "icon": "bank"},
    "Caisse d'Epargne": {"module": "caissedepargne", "icon": "bank"},
    "Boursorama": {"module": "boursorama", "icon": "bank"},
    "LCL": {"module": "lcl", "icon": "bank"},
    "Fortuneo": {"module": "fortuneo", "icon": "bank"},
    "ING": {"module": "ing", "icon": "bank"},
    "Banque Populaire": {"module": "banquepopulaire", "icon": "bank"},
    "HSBC": {"module": "hsbc", "icon": "bank"},
    "Monabanq": {"module": "monabanq", "icon": "bank"},
    "Hello Bank": {"module": "hellobank", "icon": "bank"},
    "Revolut": {"module": None, "icon": "bank"},
    "N26": {"module": None, "icon": "bank"},
}

KNOWN_PROVIDERS = {
    "EDF": {"module": "edfparticulier", "icon": "zap", "cat": "Energie"},
    "Engie": {"module": "engie", "icon": "zap", "cat": "Energie"},
    "TotalEnergies": {"module": None, "icon": "zap", "cat": "Energie"},
    "Orange": {"module": "orange", "icon": "smartphone", "cat": "Telecom"},
    "Bouygues Telecom": {"module": "bouyguestelecom", "icon": "smartphone", "cat": "Telecom"},
    "Free Mobile": {"module": "freemobile", "icon": "smartphone", "cat": "Telecom"},
    "Free (Internet)": {"module": "free", "icon": "globe", "cat": "Telecom"},
    "SFR": {"module": "sfr", "icon": "smartphone", "cat": "Telecom"},
    "Ameli": {"module": "ameli", "icon": "heart-pulse", "cat": "Sante"},
    "Foncia": {"module": "foncia", "icon": "home", "cat": "Logement"},
}

PROVIDERS = KNOWN_PROVIDERS

# --------------------------------------------------------------------------
# Palettes d'accent
# --------------------------------------------------------------------------

ACCENT_THEMES = {
    "Bleu nuit": {
        "accent_dark": "#1c5cab", "accent_mid": "#2a78d6",
        "accent_light": "#5598e7", "accent_text": "#86b6ef",
        "accent_pale": "#b7d3f6", "accent_bg": "#16233a",
        "gradient1": "#0f1a2e", "gradient2": "#111827", "gradient3": "#0e1c30",
        "swatch": "#2a78d6",
    },
    "Emeraude": {
        "accent_dark": "#0f7b56", "accent_mid": "#10b981",
        "accent_light": "#34d399", "accent_text": "#6ee7b7",
        "accent_pale": "#a7f3d0", "accent_bg": "#132a20",
        "gradient1": "#0a1f17", "gradient2": "#0f2922", "gradient3": "#0b2119",
        "swatch": "#10b981",
    },
    "Violet": {
        "accent_dark": "#7c3aed", "accent_mid": "#8b5cf6",
        "accent_light": "#a78bfa", "accent_text": "#c4b5fd",
        "accent_pale": "#ddd6fe", "accent_bg": "#1e1636",
        "gradient1": "#170f2e", "gradient2": "#1c1333", "gradient3": "#191030",
        "swatch": "#8b5cf6",
    },
    "Corail": {
        "accent_dark": "#c2410c", "accent_mid": "#ea580c",
        "accent_light": "#f97316", "accent_text": "#fdba74",
        "accent_pale": "#fed7aa", "accent_bg": "#2a1810",
        "gradient1": "#1f150d", "gradient2": "#261912", "gradient3": "#22160e",
        "swatch": "#ea580c",
    },
    "Rose": {
        "accent_dark": "#be185d", "accent_mid": "#ec4899",
        "accent_light": "#f472b6", "accent_text": "#f9a8d4",
        "accent_pale": "#fbcfe8", "accent_bg": "#2a1226",
        "gradient1": "#1f0e1c", "gradient2": "#261324", "gradient3": "#220f1f",
        "swatch": "#ec4899",
    },
    "Or": {
        "accent_dark": "#b45309", "accent_mid": "#d97706",
        "accent_light": "#f59e0b", "accent_text": "#fbbf24",
        "accent_pale": "#fde68a", "accent_bg": "#271e0a",
        "gradient1": "#1c1608", "gradient2": "#231b0c", "gradient3": "#1f180a",
        "swatch": "#d97706",
    },
}

LIGHT_GRADS = {
    "Bleu nuit":  ("#dde6f4", "#e4ecf8", "#d8e2f2"),
    "Emeraude":   ("#ddf0e8", "#e4f4ed", "#d8ece4"),
    "Violet":     ("#e8e0f4", "#e2dbf2", "#ede5f8"),
    "Corail":     ("#f4e4d8", "#f6e6dc", "#f2e0d4"),
    "Rose":       ("#f4dde8", "#f2d8e4", "#f6e0ea"),
    "Or":         ("#f4ead8", "#f6eddc", "#f2e7d4"),
}

# --------------------------------------------------------------------------
# Fonctions de stockage
# --------------------------------------------------------------------------

def load_user_prefs():
    if USER_PREFS_PATH.exists():
        try:
            return json.loads(USER_PREFS_PATH.read_text())
        except Exception:
            return {}
    return {}

def save_user_prefs(prefs):
    USER_PREFS_PATH.write_text(json.dumps(prefs, ensure_ascii=False, indent=2))

def load_history() -> pd.DataFrame:
    if HISTORY_PATH.exists():
        df = pd.read_csv(HISTORY_PATH)
        for col in HISTORY_COLUMNS:
            if col not in df.columns:
                df[col] = None
        return df[HISTORY_COLUMNS]
    return pd.DataFrame(columns=HISTORY_COLUMNS)

def save_entry(entry: dict) -> None:
    df = load_history()
    df = pd.concat([df, pd.DataFrame([entry])], ignore_index=True)
    df.to_csv(HISTORY_PATH, index=False)

def load_payslips() -> pd.DataFrame:
    if PAYSLIP_PATH.exists():
        df = pd.read_csv(PAYSLIP_PATH)
        for col in PAYSLIP_COLUMNS:
            if col not in df.columns:
                df[col] = None
        return df[PAYSLIP_COLUMNS]
    return pd.DataFrame(columns=PAYSLIP_COLUMNS)

def save_payslip(entry: dict) -> None:
    df = load_payslips()
    df = pd.concat([df, pd.DataFrame([entry])], ignore_index=True)
    df.to_csv(PAYSLIP_PATH, index=False)

def load_bank_transactions() -> pd.DataFrame:
    if BANK_TX_PATH.exists():
        df = pd.read_csv(BANK_TX_PATH)
        for col in BANK_TX_COLUMNS:
            if col not in df.columns:
                df[col] = None
        return df[BANK_TX_COLUMNS]
    return pd.DataFrame(columns=BANK_TX_COLUMNS)

def save_bank_transactions(df: pd.DataFrame) -> None:
    df.to_csv(BANK_TX_PATH, index=False)


def import_transactions_from_list(transactions: list):
    """Importe une liste de dicts de transactions dans le CSV bancaire."""
    if not transactions:
        return 0, None
    new_df = pd.DataFrame(transactions)
    for col in BANK_TX_COLUMNS:
        if col not in new_df.columns:
            new_df[col] = None
    new_df = new_df[BANK_TX_COLUMNS]
    existing = load_bank_transactions()
    if not existing.empty:
        # Deduplication par date + label + amount
        existing["_key"] = existing["date"].astype(str) + "|" + existing["label"].astype(str) + "|" + existing["amount"].astype(str)
        new_df["_key"] = new_df["date"].astype(str) + "|" + new_df["label"].astype(str) + "|" + new_df["amount"].astype(str)
        new_df = new_df[~new_df["_key"].isin(existing["_key"])]
        new_df = new_df.drop(columns=["_key"])
        existing = existing.drop(columns=["_key"])
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df
    save_bank_transactions(combined)
    return len(new_df), None

def load_savings_goals() -> list:
    if SAVINGS_PATH.exists():
        try:
            return json.loads(SAVINGS_PATH.read_text())
        except Exception:
            return []
    return []

def save_savings_goals(goals: list) -> None:
    SAVINGS_PATH.write_text(json.dumps(goals, ensure_ascii=False, indent=2))

def load_category_budgets() -> dict:
    if CATEGORY_BUDGETS_PATH.exists():
        try:
            return json.loads(CATEGORY_BUDGETS_PATH.read_text())
        except Exception:
            return {}
    return {}

def save_category_budgets(budgets: dict) -> None:
    CATEGORY_BUDGETS_PATH.write_text(json.dumps(budgets, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------
# Analyse IA (Gemini)
# --------------------------------------------------------------------------

EXTRACTION_PROMPT = """Tu es un assistant qui lit des factures et des documents \
d'abonnement (telephonie, energie, assurance, internet, etc.) pour un particulier.

Analyse le document ci-joint et reponds UNIQUEMENT avec un objet JSON valide, \
sans texte autour, sans balises markdown, avec exactement ces cles :

{
  "fournisseur": "nom du fournisseur ou de l'entreprise",
  "type_contrat": "categorie courte, ex: Telephonie mobile, Electricite, Assurance habitation",
  "montant": nombre decimal du montant total TTC, avec un point comme separateur (ex: 42.90), sans symbole monetaire,
  "devise": "code devise a 3 lettres, ex: EUR",
  "date_facture": "date du document au format AAAA-MM-JJ, ou null si introuvable",
  "numero_contrat": "numero de contrat ou de client s'il est visible, sinon null",
  "periode": "periode de facturation si indiquee, ex: 'Juillet 2026', sinon null",
  "notes": "une phrase courte si un element inhabituel apparait explicitement sur le document (mention de hausse tarifaire, fin de promotion, nouveau frais...), sinon une chaine vide"
}
"""

PAYSLIP_EXTRACTION_PROMPT = """Tu es un assistant qui lit des fiches de paie francaises.

Analyse le document ci-joint et reponds UNIQUEMENT avec un objet JSON valide, \
sans texte autour, sans balises markdown, avec exactement ces cles :

{
  "employeur": "nom de l'employeur ou de l'entreprise",
  "mois": "mois et annee de la fiche, ex: 'Juillet 2026'",
  "salaire_brut": nombre decimal du salaire brut, avec un point comme separateur,
  "salaire_net": nombre decimal du salaire net a payer, avec un point comme separateur,
  "net_imposable": nombre decimal du net imposable si visible, sinon null,
  "devise": "EUR",
  "date_fiche": "date au format AAAA-MM-JJ (1er du mois de paie), ou null",
  "notes": "une phrase courte si quelque chose d'inhabituel est visible (prime, augmentation, changement de poste...), sinon une chaine vide"
}
"""

TONE_INSTRUCTIONS = {
    "Poli et factuel": "un ton poli, factuel et professionnel",
    "Ferme": "un ton ferme mais courtois, qui montre que tu ne comptes pas laisser passer ca",
    "Negociation": "un ton oriente negociation, en demandant explicitement un geste commercial ou une renegociation du tarif",
}


def extract_invoice_data(client, content_part) -> dict:
    resp = client.models.generate_content(
        model=MODEL, contents=[content_part, EXTRACTION_PROMPT],
    )
    text = resp.text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def extract_payslip_data(client, content_part) -> dict:
    resp = client.models.generate_content(
        model=MODEL, contents=[content_part, PAYSLIP_EXTRACTION_PROMPT],
    )
    text = resp.text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def check_anomaly(df, fournisseur, montant):
    if not fournisseur or df.empty:
        return None
    prev = df[df["fournisseur"].str.lower() == fournisseur.lower()].copy()
    if prev.empty:
        return None
    prev = prev.sort_values("date_ajout")
    last = prev.iloc[-1]
    try:
        last_montant = float(last["montant"])
    except (TypeError, ValueError):
        return None
    if last_montant <= 0:
        return None
    delta_abs = montant - last_montant
    delta_pct = (delta_abs / last_montant) * 100
    is_anomaly = delta_pct > ANOMALY_PCT_THRESHOLD and delta_abs > ANOMALY_ABS_THRESHOLD
    return {
        "is_anomaly": is_anomaly, "delta_abs": delta_abs, "delta_pct": delta_pct,
        "previous_montant": last_montant,
        "previous_date": last.get("date_facture") or last.get("date_ajout"),
    }


def check_payslip_anomaly(df, salaire_net):
    if df.empty:
        return None
    df = df.copy()
    df["salaire_net"] = pd.to_numeric(df["salaire_net"], errors="coerce")
    df = df.dropna(subset=["salaire_net"])
    if df.empty:
        return None
    df = df.sort_values("date_ajout")
    last = df.iloc[-1]
    try:
        last_net = float(last["salaire_net"])
    except (TypeError, ValueError):
        return None
    if last_net <= 0:
        return None
    delta_abs = salaire_net - last_net
    delta_pct = (delta_abs / last_net) * 100
    is_anomaly = abs(delta_pct) > 3.0 and abs(delta_abs) > 10.0
    return {
        "is_anomaly": is_anomaly, "delta_abs": delta_abs, "delta_pct": delta_pct,
        "previous_net": last_net,
        "previous_mois": last.get("mois") or last.get("date_ajout"),
    }


def draft_email(client, entry, anomaly, tone):
    numero_contrat = entry.get("numero_contrat") or "non communique"
    prompt = f"""Redige en francais un email de reclamation destine au service client de \
"{entry['fournisseur']}", avec {TONE_INSTRUCTIONS[tone]}.

Contexte :
- Type de contrat : {entry.get('type_contrat') or 'non precise'}
- Numero de contrat / client : {numero_contrat}
- Montant precedent : {anomaly['previous_montant']:.2f} {entry.get('devise', 'EUR')} (le {anomaly['previous_date']})
- Nouveau montant : {entry['montant']:.2f} {entry.get('devise', 'EUR')}
- Hausse : {anomaly['delta_abs']:.2f} {entry.get('devise', 'EUR')} soit {anomaly['delta_pct']:.1f}%

Demande une explication claire sur cette hausse. Termine par une formule de politesse \
et signe "[Ton prenom et nom]"."""
    resp = client.models.generate_content(model=MODEL, contents=[prompt])
    return resp.text.strip()


# --------------------------------------------------------------------------
# Detection d'abonnements
# --------------------------------------------------------------------------

def detect_subscriptions(tx_df):
    if tx_df.empty:
        return pd.DataFrame()
    tx_df = tx_df.copy()
    tx_df["amount"] = pd.to_numeric(tx_df["amount"], errors="coerce")
    tx_df["date"] = pd.to_datetime(tx_df["date"], errors="coerce", dayfirst=True)
    tx_df = tx_df.dropna(subset=["amount", "date"])
    tx_df["label_norm"] = tx_df["label"].str.upper().str.strip()

    _monthly_keywords = [
        "PRLV", "PRELEVEMENT", "ABONNEMENT", "ASSURANCE", "MUTUELLE",
        "EDF", "ENGIE", "GDF", "FREE", "ORANGE", "SFR", "BOUYGUES",
        "NETFLIX", "SPOTIFY", "DEEZER", "DISNEY", "AMAZON PRIME",
        "CANAL", "OVH", "ADOBE", "ICLOUD", "GOOGLE STORAGE",
        "ACM", "MAIF", "MACIF", "MATMUT", "AXA", "ALLIANZ", "GROUPAMA",
        "LOYER", "CPAM", "CAF", "IMPOT", "YOUTUBE", "APPLE",
        "MICROSOFT", "PLAYSTATION", "XBOX", "NINTENDO", "HBO",
        "CHATGPT", "OPENAI", "NOTION", "FIGMA", "GITHUB", "DOCTOLIB",
    ]

    today = pd.Timestamp.now()
    per_label = tx_df.sort_values("date").groupby("label_norm")
    results = []
    for lbl, grp in per_label:
        if len(grp) < 2:
            continue
        dates_sorted = grp["date"].sort_values()
        intervals = dates_sorted.diff().dropna().dt.days
        if intervals.empty:
            continue
        avg_interval = intervals.mean()
        mean_amt = grp["amount"].mean()
        std_amt = grp["amount"].std()
        is_stable = std_amt <= mean_amt * 0.3 if mean_amt > 0 else True
        is_keyword = any(kw in lbl for kw in _monthly_keywords)
        is_monthly = (20 <= avg_interval <= 45) and is_stable
        is_quarterly = (80 <= avg_interval <= 100) and is_stable
        is_yearly = (350 <= avg_interval <= 380) and is_stable
        if not (is_monthly or is_keyword or is_quarterly or is_yearly):
            continue

        prev = grp["amount"].iloc[-2]
        last = grp["amount"].iloc[-1]
        variation = round(((last - prev) / prev) * 100, 1) if prev > 0 else 0.0
        last_date = grp["date"].max()
        days_since = (today - last_date).days

        # Determiner la frequence
        if is_yearly:
            frequence = "annuel"
            seuil_oubli = 400  # > 13 mois
            cout_mensuel = round(mean_amt / 12, 2)
        elif is_quarterly:
            frequence = "trimestriel"
            seuil_oubli = 120  # > 4 mois
            cout_mensuel = round(mean_amt / 3, 2)
        else:
            frequence = "mensuel"
            seuil_oubli = 60  # > 2 mois sans prelevement
            cout_mensuel = round(mean_amt, 2)

        # Statut : actif ou potentiellement oublie
        if days_since > seuil_oubli:
            statut = "oublie"
        elif days_since > seuil_oubli * 0.7:
            statut = "a_verifier"
        else:
            statut = "actif"

        results.append({
            "label": grp["label"].iloc[0],
            "occurrences": len(grp),
            "montant_moyen": round(mean_amt, 2),
            "dernier_montant": round(last, 2),
            "derniere_date": str(last_date.date()),
            "premiere_date": str(grp["date"].min().date()),
            "variation_pct": variation,
            "intervalle_moyen": round(avg_interval, 0),
            "frequence": frequence,
            "statut": statut,
            "cout_mensuel": cout_mensuel,
            "jours_depuis": days_since,
        })
    subs = pd.DataFrame(results)
    if not subs.empty:
        subs = subs.sort_values("dernier_montant", ascending=False)
    return subs


# --------------------------------------------------------------------------
# Alertes intelligentes
# --------------------------------------------------------------------------

def compute_smart_alerts(tx_df, subscriptions, month_depenses, revenu_mensuel,
                         cat_budgets, month_cat_totals):
    """Genere des alertes intelligentes basees sur l'analyse des transactions."""
    alerts = []

    # 1) Alertes sur les abonnements : hausse de prix
    if subscriptions:
        for sub in subscriptions:
            if sub.get("variation_pct", 0) > 5:
                alerts.append({
                    "type": "price_increase",
                    "severity": "warning",
                    "icon": "💸",
                    "title": f"Hausse de prix : {sub['label']}",
                    "message": f"+{sub['variation_pct']}% — passe de {sub['montant_moyen']:.2f} EUR a {sub['dernier_montant']:.2f} EUR",
                })
            if sub.get("statut") == "oublie":
                alerts.append({
                    "type": "forgotten_sub",
                    "severity": "info",
                    "icon": "👻",
                    "title": f"Abonnement oublie ? {sub['label']}",
                    "message": f"Pas vu depuis {sub['jours_depuis']} jours. Verifie si tu l'utilises encore.",
                })

    # 2) Depenses inhabituelles (ce mois vs moyenne des mois precedents)
    if not tx_df.empty:
        tx = tx_df.copy()
        tx["amount"] = pd.to_numeric(tx["amount"], errors="coerce")
        tx["date"] = pd.to_datetime(tx["date"], errors="coerce", dayfirst=True)
        tx = tx.dropna(subset=["amount", "date"])
        now = pd.Timestamp.now()

        # Moyenne mensuelle des 3 derniers mois (hors mois en cours)
        prev_months = tx[
            (tx["date"] < now.replace(day=1)) &
            (tx["date"] >= (now - pd.DateOffset(months=3)).replace(day=1))
        ]
        if not prev_months.empty:
            months_count = prev_months["date"].dt.to_period("M").nunique()
            if months_count > 0:
                avg_monthly = prev_months["amount"].sum() / months_count
                if avg_monthly > 0 and month_depenses > avg_monthly * 1.3:
                    pct_over = round(((month_depenses - avg_monthly) / avg_monthly) * 100, 0)
                    alerts.append({
                        "type": "unusual_spending",
                        "severity": "warning",
                        "icon": "⚠️",
                        "title": "Depenses inhabituelles ce mois",
                        "message": f"+{pct_over:.0f}% par rapport a ta moyenne ({avg_monthly:.0f} EUR/mois). Tu es a {month_depenses:.0f} EUR.",
                    })

    # 3) Depassement de budget par categorie
    if cat_budgets and month_cat_totals:
        for cat, budget_val in cat_budgets.items():
            spent = month_cat_totals.get(cat, 0)
            if budget_val > 0 and spent > budget_val:
                pct = round((spent / budget_val - 1) * 100, 0)
                alerts.append({
                    "type": "budget_exceeded",
                    "severity": "serious",
                    "icon": "🚨",
                    "title": f"Budget depasse : {cat}",
                    "message": f"{spent:.0f} EUR depenses sur {budget_val:.0f} EUR prevus (+{pct:.0f}%).",
                })
            elif budget_val > 0 and spent > budget_val * 0.85:
                pct = round((spent / budget_val) * 100, 0)
                alerts.append({
                    "type": "budget_warning",
                    "severity": "info",
                    "icon": "📊",
                    "title": f"Budget presque atteint : {cat}",
                    "message": f"{spent:.0f} EUR / {budget_val:.0f} EUR ({pct:.0f}%). Attention ce mois-ci.",
                })

    # 4) Taux d'epargne faible
    if revenu_mensuel > 0 and month_depenses > 0:
        taux_epargne = ((revenu_mensuel - month_depenses) / revenu_mensuel) * 100
        if taux_epargne < 5:
            alerts.append({
                "type": "low_savings",
                "severity": "warning",
                "icon": "🐷",
                "title": "Taux d'epargne tres faible",
                "message": f"Seulement {taux_epargne:.1f}% d'epargne ce mois. Objectif recommande : 10-20%.",
            })

    # Trier par severite
    severity_order = {"serious": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda a: severity_order.get(a["severity"], 3))
    return alerts


# --------------------------------------------------------------------------
# Score de sante financiere (0 a 100)
# --------------------------------------------------------------------------

def compute_health_score(tx_df, revenu_mensuel, month_depenses, monthly_budget,
                         subscriptions, cat_budgets, month_cat_totals):
    """Calcule un score de sante financiere de 0 a 100."""
    scores = {}
    weights = {}

    # 1) Taux d'epargne (30 points max)
    if revenu_mensuel > 0:
        taux = (revenu_mensuel - month_depenses) / revenu_mensuel
        if taux >= 0.20:
            scores["epargne"] = 30
        elif taux >= 0.10:
            scores["epargne"] = 20 + (taux - 0.10) * 100  # 20-30
        elif taux >= 0.0:
            scores["epargne"] = taux * 200  # 0-20
        else:
            scores["epargne"] = max(-10, taux * 50)  # negatif si dettes
        weights["epargne"] = 30
    else:
        scores["epargne"] = 0
        weights["epargne"] = 0

    # 2) Respect du budget global (20 points max)
    if monthly_budget > 0:
        ratio = month_depenses / monthly_budget
        if ratio <= 0.85:
            scores["budget"] = 20
        elif ratio <= 1.0:
            scores["budget"] = 10 + (1.0 - ratio) * 66.7  # 10-20
        elif ratio <= 1.2:
            scores["budget"] = max(0, 10 - (ratio - 1.0) * 50)  # 0-10
        else:
            scores["budget"] = 0
        weights["budget"] = 20
    else:
        scores["budget"] = 10  # pas de budget = neutre
        weights["budget"] = 20

    # 3) Stabilite des depenses (20 points max) — coefficient de variation
    if not tx_df.empty:
        tx = tx_df.copy()
        tx["amount"] = pd.to_numeric(tx["amount"], errors="coerce")
        tx["date"] = pd.to_datetime(tx["date"], errors="coerce", dayfirst=True)
        tx = tx.dropna(subset=["amount", "date"])
        monthly_totals = tx.groupby(tx["date"].dt.to_period("M"))["amount"].sum()
        if len(monthly_totals) >= 2:
            cv = monthly_totals.std() / monthly_totals.mean() if monthly_totals.mean() > 0 else 1
            if cv <= 0.15:
                scores["stabilite"] = 20
            elif cv <= 0.30:
                scores["stabilite"] = 10 + (0.30 - cv) * 66.7
            elif cv <= 0.50:
                scores["stabilite"] = max(0, 10 - (cv - 0.30) * 50)
            else:
                scores["stabilite"] = 0
        else:
            scores["stabilite"] = 10
        weights["stabilite"] = 20
    else:
        scores["stabilite"] = 0
        weights["stabilite"] = 0

    # 4) Poids des abonnements (15 points max) — pas trop d'abonnements par rapport au revenu
    if revenu_mensuel > 0 and subscriptions:
        total_subs = sum(s.get("cout_mensuel", 0) for s in subscriptions)
        ratio_subs = total_subs / revenu_mensuel
        if ratio_subs <= 0.15:
            scores["abonnements"] = 15
        elif ratio_subs <= 0.25:
            scores["abonnements"] = 8 + (0.25 - ratio_subs) * 70
        elif ratio_subs <= 0.40:
            scores["abonnements"] = max(0, 8 - (ratio_subs - 0.25) * 53)
        else:
            scores["abonnements"] = 0
        weights["abonnements"] = 15
    else:
        scores["abonnements"] = 15 if revenu_mensuel > 0 else 0
        weights["abonnements"] = 15

    # 5) Respect des budgets par categorie (15 points max)
    if cat_budgets and month_cat_totals:
        cats_with_budget = [c for c, v in cat_budgets.items() if v > 0]
        if cats_with_budget:
            respected = sum(1 for c in cats_with_budget if month_cat_totals.get(c, 0) <= cat_budgets[c])
            ratio_ok = respected / len(cats_with_budget)
            scores["budgets_cat"] = round(ratio_ok * 15, 1)
        else:
            scores["budgets_cat"] = 7.5
        weights["budgets_cat"] = 15
    else:
        scores["budgets_cat"] = 7.5
        weights["budgets_cat"] = 15

    # Calcul du score final
    total_weight = sum(weights.values())
    if total_weight == 0:
        return {"score": 0, "details": {}, "grade": "?", "color": "#6b7280"}

    raw_score = sum(max(0, scores[k]) for k in scores)
    # Normaliser sur 100 si le total des poids n'est pas 100
    final_score = round(min(100, (raw_score / total_weight) * 100), 0)

    # Grade et couleur
    if final_score >= 80:
        grade, color = "Excellent", "#10b981"
    elif final_score >= 65:
        grade, color = "Bon", "#3b82f6"
    elif final_score >= 50:
        grade, color = "Correct", "#f59e0b"
    elif final_score >= 30:
        grade, color = "A ameliorer", "#f97316"
    else:
        grade, color = "Critique", "#ef4444"

    # Details pour l'UI
    details = {}
    labels = {
        "epargne": ("Taux d'epargne", 30),
        "budget": ("Budget global", 20),
        "stabilite": ("Stabilite", 20),
        "abonnements": ("Abonnements", 15),
        "budgets_cat": ("Budgets categories", 15),
    }
    for key, (label, max_pts) in labels.items():
        pts = max(0, round(scores.get(key, 0), 1))
        details[key] = {
            "label": label,
            "score": pts,
            "max": max_pts,
            "pct": round((pts / max_pts) * 100, 0) if max_pts > 0 else 0,
        }

    return {
        "score": int(final_score),
        "grade": grade,
        "color": color,
        "details": details,
    }


# --------------------------------------------------------------------------
# Import CSV bancaire
# --------------------------------------------------------------------------

def import_bank_csv(file_storage):
    raw = file_storage.read()
    text = None
    for enc in ["utf-8", "utf-8-sig", "latin-1", "cp1252"]:
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return None, "Impossible de lire le fichier — encodage non reconnu."

    csv_df = None
    for sep in [";", ",", "\t"]:
        try:
            _try = pd.read_csv(io.StringIO(text), sep=sep, dtype=str)
            if len(_try.columns) >= 2 and len(_try) >= 1:
                csv_df = _try
                break
        except Exception:
            continue
    if csv_df is None or csv_df.empty:
        return None, "Fichier CSV vide ou format non reconnu."

    _col_lower = {c: c.lower().strip() for c in csv_df.columns}
    date_col = label_col = amount_col = debit_col = credit_col = None
    for orig, low in _col_lower.items():
        if low in ("date", "dateop", "date_op", "date operation", "date comptable",
                    "date de comptabilisation", "date valeur"):
            date_col = date_col or orig
        elif low in ("libelle", "label", "libellé", "description", "intitulé",
                      "libelle simplifie", "libellé simplifié", "libelle operation"):
            label_col = label_col or orig
        elif low in ("montant", "amount", "valeur", "montant(euros)"):
            amount_col = amount_col or orig
        elif low in ("debit", "débit"):
            debit_col = debit_col or orig
        elif low in ("credit", "crédit"):
            credit_col = credit_col or orig

    if not date_col:
        for orig in csv_df.columns:
            sample = csv_df[orig].dropna().head(5)
            if sample.str.match(r"^\d{2}[/\-\.]\d{2}[/\-\.]\d{2,4}$").any():
                date_col = orig
                break
    if not label_col:
        for orig in csv_df.columns:
            if orig == date_col:
                continue
            sample = csv_df[orig].dropna().head(10)
            try:
                pd.to_numeric(sample.str.replace(",", ".").str.replace(" ", ""))
            except (ValueError, TypeError):
                label_col = orig
                break
    if not date_col or not label_col:
        return None, "Colonnes 'date' et 'libelle' introuvables."

    if amount_col:
        csv_df["_amount"] = csv_df[amount_col].str.replace(",", ".").str.replace(" ", "").str.replace("\xa0", "")
        csv_df["_amount"] = pd.to_numeric(csv_df["_amount"], errors="coerce")
    elif debit_col and credit_col:
        _deb = pd.to_numeric(csv_df[debit_col].str.replace(",", ".").str.replace(" ", "").str.replace("\xa0", ""), errors="coerce").fillna(0)
        _cre = pd.to_numeric(csv_df[credit_col].str.replace(",", ".").str.replace(" ", "").str.replace("\xa0", ""), errors="coerce").fillna(0)
        csv_df["_amount"] = _cre - _deb
    elif debit_col:
        csv_df["_amount"] = -pd.to_numeric(csv_df[debit_col].str.replace(",", ".").str.replace(" ", "").str.replace("\xa0", ""), errors="coerce").abs()
    else:
        for orig in csv_df.columns:
            if orig in (date_col, label_col):
                continue
            try:
                vals = csv_df[orig].str.replace(",", ".").str.replace(" ", "").str.replace("\xa0", "")
                nums = pd.to_numeric(vals, errors="coerce")
                if nums.notna().sum() > len(csv_df) * 0.5:
                    csv_df["_amount"] = nums
                    break
            except Exception:
                continue

    if "_amount" not in csv_df.columns:
        return None, "Colonne 'montant' introuvable dans le CSV."

    csv_df["_amount"] = csv_df["_amount"].fillna(0)
    debits = csv_df[csv_df["_amount"] < 0].copy()
    if debits.empty:
        debits = csv_df[csv_df["_amount"] != 0].copy()
        debits["_amount"] = debits["_amount"].abs()
    else:
        debits["_amount"] = debits["_amount"].abs()

    new_txs = pd.DataFrame({
        "date": debits[date_col].values,
        "label": debits[label_col].str.strip().values,
        "amount": debits["_amount"].round(2).values,
        "category": None,
        "bank_name": file_storage.filename.rsplit(".", 1)[0],
        "account_label": "",
        "date_import": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })

    existing = load_bank_transactions()
    if not existing.empty:
        combined = pd.concat([existing, new_txs], ignore_index=True)
        combined = combined.drop_duplicates(subset=["date", "label", "amount"], keep="last")
    else:
        combined = new_txs
    save_bank_transactions(combined)
    return len(new_txs), None


# --------------------------------------------------------------------------
# Woob (connexion bancaire automatique)
# --------------------------------------------------------------------------

def fetch_bank_transactions_woob(bank_name, login, password, months_back=3):
    if not _HAS_WOOB:
        return [], "woob n'est pas installe. Lance : pip install woob"
    bank = KNOWN_BANKS.get(bank_name)
    if not bank or not bank.get("module"):
        return [], f"Pas de module woob pour {bank_name}"
    try:
        w = Woob()
        backend_name = f"scribe_bank_{bank['module']}"
        w.load_backend(bank["module"], backend_name,
                       params={"login": login, "password": password})
        cutoff = date.today().replace(day=1)
        if months_back > 1:
            month = cutoff.month - (months_back - 1)
            year = cutoff.year
            while month <= 0:
                month += 12
                year -= 1
            cutoff = cutoff.replace(year=year, month=month)
        transactions = []
        for account in w.iter_accounts():
            for tr in w.iter_history(account):
                tr_date = tr.date if hasattr(tr, "date") else None
                if tr_date and hasattr(tr_date, "date"):
                    tr_date = tr_date if isinstance(tr_date, date) else tr_date.date()
                if tr_date and tr_date < cutoff:
                    continue
                amount = float(tr.amount) if hasattr(tr, "amount") and tr.amount else 0
                if amount >= 0:
                    continue
                transactions.append({
                    "date": str(tr_date) if tr_date else None,
                    "label": str(tr.label).strip() if hasattr(tr, "label") else "",
                    "amount": round(abs(amount), 2),
                    "category": getattr(tr, "category", None),
                    "bank_name": bank_name,
                    "account_label": str(account.label) if hasattr(account, "label") else "",
                    "date_import": datetime.now().strftime("%Y-%m-%d %H:%M"),
                })
        return transactions, None
    except Exception as exc:
        return [], str(exc)


# --------------------------------------------------------------------------
# Bareme impot sur le revenu (France — revenus 2024, applicable 2025)
# --------------------------------------------------------------------------

IR_TRANCHES_2024 = [
    (11_294,  0.00),
    (28_797,  0.11),
    (82_341,  0.30),
    (177_106, 0.41),
    (None,    0.45),
]

# Abattement forfaitaire de 10 % sur les salaires (min / max)
ABATTEMENT_10_MIN = 504
ABATTEMENT_10_MAX = 14_426


def simuler_ir(revenu_net_imposable: float, nb_parts: float = 1.0) -> dict:
    """Calcule l'IR avec le systeme du quotient familial."""
    if revenu_net_imposable <= 0 or nb_parts <= 0:
        return {"ir": 0, "taux_moyen": 0, "taux_marginal": 0, "tranches": [],
                "revenu_par_part": 0, "revenu_net_imposable": 0}

    r_par_part = revenu_net_imposable / nb_parts
    impot_par_part = 0
    detail = []
    prev = 0
    taux_marginal = 0

    for plafond, taux in IR_TRANCHES_2024:
        if plafond is None:
            tranche_base = max(r_par_part - prev, 0)
        else:
            tranche_base = max(min(r_par_part, plafond) - prev, 0)
        montant = round(tranche_base * taux, 2)
        if tranche_base > 0:
            detail.append({
                "de": prev, "a": plafond or "∞",
                "taux": round(taux * 100, 1),
                "base": round(tranche_base, 2),
                "impot": montant,
            })
            taux_marginal = taux
        impot_par_part += montant
        if plafond is not None:
            prev = plafond
        if plafond is not None and r_par_part <= plafond:
            break

    ir_total = round(impot_par_part * nb_parts, 2)
    taux_moyen = round((ir_total / revenu_net_imposable) * 100, 1) if revenu_net_imposable > 0 else 0

    return {
        "ir": ir_total,
        "taux_moyen": taux_moyen,
        "taux_marginal": round(taux_marginal * 100, 1),
        "tranches": detail,
        "revenu_par_part": round(r_par_part, 2),
        "revenu_net_imposable": round(revenu_net_imposable, 2),
    }


def _compute_tax_data(payslips_df, tx_df, now):
    """Prepare les donnees fiscales pour le template."""
    current_year = now.year
    prev_year = current_year - 1  # On declare les revenus de l'annee precedente

    # Revenus annuels (fiches de paie)
    annual_net_imposable = 0
    annual_salaire_brut = 0
    annual_salaire_net = 0
    nb_fiches = 0

    if not payslips_df.empty:
        ps = payslips_df.copy()
        ps["salaire_brut"] = pd.to_numeric(ps["salaire_brut"], errors="coerce")
        ps["salaire_net"] = pd.to_numeric(ps["salaire_net"], errors="coerce")
        ps["net_imposable"] = pd.to_numeric(ps.get("net_imposable", pd.Series(dtype=float)), errors="coerce")

        # Estimer le revenu annuel a partir des fiches disponibles
        nb_fiches = len(ps.dropna(subset=["salaire_net"]))
        annual_salaire_brut = float(ps["salaire_brut"].sum()) if not ps["salaire_brut"].isna().all() else 0
        annual_salaire_net = float(ps["salaire_net"].sum()) if not ps["salaire_net"].isna().all() else 0
        if not ps["net_imposable"].isna().all():
            annual_net_imposable = float(ps["net_imposable"].sum())
        else:
            annual_net_imposable = annual_salaire_net

    # Abattement 10 %
    abattement = max(ABATTEMENT_10_MIN, min(annual_net_imposable * 0.10, ABATTEMENT_10_MAX))
    revenu_apres_abattement = max(annual_net_imposable - abattement, 0)

    # Simulation avec 1 part par defaut
    simulation = simuler_ir(revenu_apres_abattement, 1.0)

    # Depenses par categorie (annee en cours) — utile pour reperer les deductibles
    year_expenses_by_cat = {}
    if not tx_df.empty:
        tx = categorize_all_transactions(tx_df)
        tx["amount"] = pd.to_numeric(tx["amount"], errors="coerce")
        tx["date"] = pd.to_datetime(tx["date"], errors="coerce", dayfirst=True)
        tx = tx.dropna(subset=["amount", "date"])
        # On prend l'annee des transactions
        for y in [current_year, prev_year]:
            year_tx = tx[tx["date"].dt.year == y]
            if not year_tx.empty:
                _cat = year_tx.groupby("auto_category")["amount"].sum().sort_values(ascending=False)
                year_expenses_by_cat = {
                    cat: round(float(amt), 2) for cat, amt in _cat.items()
                }
                break  # On prend la premiere annee avec des donnees

    return {
        "annual_salaire_brut": round(annual_salaire_brut, 2),
        "annual_salaire_net": round(annual_salaire_net, 2),
        "annual_net_imposable": round(annual_net_imposable, 2),
        "nb_fiches": nb_fiches,
        "abattement": round(abattement, 2),
        "revenu_apres_abattement": round(revenu_apres_abattement, 2),
        "simulation": simulation,
        "year_expenses_by_cat": year_expenses_by_cat,
    }


# --------------------------------------------------------------------------
# Calcul des donnees pour le template
# --------------------------------------------------------------------------

def compute_dashboard_data():
    """Calcule toutes les donnees necessaires au rendu de la page."""
    prefs = load_user_prefs()
    theme_name = prefs.get("accent_theme", "Bleu nuit")
    dark_mode = prefs.get("dark_mode", True)
    api_key = prefs.get("api_key", "")
    monthly_budget = prefs.get("monthly_budget", 0.0)
    onboarded = prefs.get("onboarded", False)

    theme = ACCENT_THEMES.get(theme_name, ACCENT_THEMES["Bleu nuit"])

    # History
    history_df = load_history()
    history_data = []
    total_factures = 0
    total_fournisseurs = 0
    total_depense = 0

    if not history_df.empty:
        history_df["_date"] = pd.to_datetime(
            history_df["date_facture"], errors="coerce"
        ).fillna(pd.to_datetime(history_df["date_ajout"], errors="coerce"))
        total_factures = len(history_df)
        total_fournisseurs = history_df["fournisseur"].nunique()
        total_depense = pd.to_numeric(history_df["montant"], errors="coerce").sum()
        history_data = history_df.sort_values("date_ajout", ascending=False).fillna("").to_dict("records")

    # Bank transactions
    tx_df = load_bank_transactions()
    tx_data = []
    cat_totals = {}
    monthly_trends = []
    subscriptions = []
    month_depenses = 0
    revenu_mensuel = 0
    now = date.today()

    if not tx_df.empty:
        tx_all = categorize_all_transactions(tx_df)
        tx_all["amount"] = pd.to_numeric(tx_all["amount"], errors="coerce")
        tx_all["date"] = pd.to_datetime(tx_all["date"], errors="coerce", dayfirst=True)
        tx_all = tx_all.dropna(subset=["amount", "date"])

        # Depenses ce mois
        tx_month = tx_all[
            (tx_all["date"].dt.month == now.month) & (tx_all["date"].dt.year == now.year)
        ]
        month_depenses = float(tx_month["amount"].sum()) if not tx_month.empty else 0

        # Totaux par categorie
        _ct = tx_all.groupby("auto_category")["amount"].sum().sort_values(ascending=False)
        _ct = _ct[_ct > 0]
        grand_total = _ct.sum()
        cat_totals = {
            cat: {
                "amount": round(float(amt), 2),
                "pct": round(float(amt / grand_total * 100), 1) if grand_total > 0 else 0,
                "color": TRANSACTION_CATEGORIES.get(cat, {}).get("color", "#6b7280"),
                "emoji": TRANSACTION_CATEGORIES.get(cat, {}).get("emoji", ""),
            }
            for cat, amt in _ct.items()
        }

        # Totaux par categorie CE MOIS
        month_cat_totals = {}
        if not tx_month.empty:
            _mct = tx_month.groupby("auto_category")["amount"].sum()
            month_cat_totals = {cat: round(float(amt), 2) for cat, amt in _mct.items()}

        # Tendances mensuelles
        tx_trend = tx_all.copy()
        tx_trend["mois"] = tx_trend["date"].dt.to_period("M").astype(str)
        _tbc = tx_trend.groupby(["mois", "auto_category"])["amount"].sum().reset_index()
        monthly_trends = _tbc.to_dict("records")

        # Abonnements
        subs_df = detect_subscriptions(tx_df)
        if not subs_df.empty:
            subscriptions = subs_df.to_dict("records")

        # Transactions pour le tableau
        tx_display = tx_all.copy()
        tx_display["date_str"] = tx_display["date"].dt.strftime("%d/%m/%Y")
        tx_data = tx_display[["date_str", "label", "amount", "auto_category"]].to_dict("records")

    # Revenus (fiches de paie)
    payslips_df = load_payslips()
    payslip_data = []
    last_salary = 0
    salary_evolution = None

    if not payslips_df.empty:
        payslips_df["salaire_net"] = pd.to_numeric(payslips_df["salaire_net"], errors="coerce")
        sorted_ps = payslips_df.dropna(subset=["salaire_net"]).sort_values("date_ajout")
        if not sorted_ps.empty:
            last_salary = float(sorted_ps.iloc[-1]["salaire_net"])
            revenu_mensuel = last_salary
            if len(sorted_ps) >= 2:
                prev = float(sorted_ps.iloc[-2]["salaire_net"])
                if prev > 0:
                    salary_evolution = round(((last_salary - prev) / prev) * 100, 1)
        payslip_data = payslips_df.sort_values("date_ajout", ascending=False).fillna("").to_dict("records")

    # Objectifs d'epargne
    savings_goals = load_savings_goals()

    # Budgets par categorie
    cat_budgets = load_category_budgets()

    # Bilan annuel
    bilan = None
    available_years = []
    if not tx_df.empty:
        tx_all_bilan = categorize_all_transactions(tx_df)
        tx_all_bilan["amount"] = pd.to_numeric(tx_all_bilan["amount"], errors="coerce")
        tx_all_bilan["date"] = pd.to_datetime(tx_all_bilan["date"], errors="coerce", dayfirst=True)
        tx_all_bilan = tx_all_bilan.dropna(subset=["amount", "date"])
        available_years = sorted(tx_all_bilan["date"].dt.year.dropna().unique().astype(int).tolist(), reverse=True)

    # Mois disponibles pour export
    export_months = []
    if not tx_df.empty:
        tx_exp = categorize_all_transactions(tx_df)
        tx_exp["date"] = pd.to_datetime(tx_exp["date"], errors="coerce", dayfirst=True)
        tx_exp = tx_exp.dropna(subset=["date"])
        if not tx_exp.empty:
            export_months = sorted(tx_exp["date"].dt.to_period("M").astype(str).unique().tolist(), reverse=True)

    # Month cat totals for budget tracking
    month_cat_totals_data = {}
    if not tx_df.empty:
        tx_budget = categorize_all_transactions(tx_df)
        tx_budget["amount"] = pd.to_numeric(tx_budget["amount"], errors="coerce")
        tx_budget["date"] = pd.to_datetime(tx_budget["date"], errors="coerce", dayfirst=True)
        tx_budget = tx_budget.dropna(subset=["amount", "date"])
        tx_this_month = tx_budget[
            (tx_budget["date"].dt.month == now.month) & (tx_budget["date"].dt.year == now.year)
        ]
        if not tx_this_month.empty:
            _mct2 = tx_this_month.groupby("auto_category")["amount"].sum()
            month_cat_totals_data = {cat: round(float(amt), 2) for cat, amt in _mct2.items()}

    solde = revenu_mensuel - month_depenses if revenu_mensuel > 0 else 0

    # ------------------------------------------------------------------
    # Alertes intelligentes
    # ------------------------------------------------------------------
    smart_alerts = compute_smart_alerts(
        tx_df, subscriptions, month_depenses, revenu_mensuel,
        cat_budgets, month_cat_totals_data,
    )

    # ------------------------------------------------------------------
    # Score de sante financiere
    # ------------------------------------------------------------------
    health_score = compute_health_score(
        tx_df, revenu_mensuel, month_depenses, monthly_budget,
        subscriptions, cat_budgets, month_cat_totals_data,
    )

    # ------------------------------------------------------------------
    # Donnees fiscales (aide aux impots)
    # ------------------------------------------------------------------
    tax_data = _compute_tax_data(payslips_df, tx_df, now)

    # Estimation net mensuel apres impot
    if tax_data.get("simulation") and revenu_mensuel > 0:
        ir_mensuel = tax_data["simulation"]["ir"] / 12
        tax_data["net_mensuel_apres_impot"] = round(revenu_mensuel - ir_mensuel, 2)
        tax_data["ir_mensuel"] = round(ir_mensuel, 2)
    else:
        tax_data["net_mensuel_apres_impot"] = 0
        tax_data["ir_mensuel"] = 0

    return {
        "onboarded": onboarded,
        "theme_name": theme_name,
        "theme": theme,
        "dark_mode": dark_mode,
        "api_key": api_key,
        "monthly_budget": monthly_budget,
        "themes": ACCENT_THEMES,
        "light_grads": LIGHT_GRADS,
        # Dashboard
        "total_factures": total_factures,
        "total_fournisseurs": total_fournisseurs,
        "total_depense": round(total_depense, 2),
        "history_data": history_data,
        # Bank
        "tx_data": tx_data,
        "cat_totals": cat_totals,
        "monthly_trends": monthly_trends,
        "subscriptions": subscriptions,
        "month_depenses": round(month_depenses, 2),
        "revenu_mensuel": round(revenu_mensuel, 2),
        "solde": round(solde, 2),
        "month_cat_totals": month_cat_totals_data,
        # Payslips
        "payslip_data": payslip_data,
        "last_salary": round(last_salary, 2),
        "salary_evolution": salary_evolution,
        # Savings & budgets
        "savings_goals": savings_goals,
        "cat_budgets": cat_budgets,
        "categories": {k: {"color": v["color"], "emoji": v["emoji"]} for k, v in TRANSACTION_CATEGORIES.items()},
        # Export / bilan
        "export_months": export_months,
        "available_years": available_years,
        # Has features
        "has_genai": _HAS_GENAI,
        "has_woob": _HAS_WOOB,
        "known_banks": list(KNOWN_BANKS.keys()),
        # Impots
        "tax_data": tax_data,
        # Alertes & Score
        "smart_alerts": smart_alerts,
        "health_score": health_score,
        # Enable Banking (Open Banking)
        "eb_data": _eb_load(),
        "eb_configured": _eb_configured(),
    }


# --------------------------------------------------------------------------
# Routes Flask
# --------------------------------------------------------------------------

@app.route("/")
def index():
    data = compute_dashboard_data()
    return render_template("index.html", **data)


@app.route("/api/onboard", methods=["POST"])
def onboard():
    prefs = load_user_prefs()
    prefs["onboarded"] = True
    save_user_prefs(prefs)
    return redirect(url_for("index"))


@app.route("/api/reset-onboard", methods=["POST"])
def reset_onboard():
    prefs = load_user_prefs()
    prefs["onboarded"] = False
    save_user_prefs(prefs)
    return redirect(url_for("index"))


@app.route("/api/settings", methods=["POST"])
def update_settings():
    prefs = load_user_prefs()
    if "accent_theme" in request.form:
        prefs["accent_theme"] = request.form["accent_theme"]
    if "dark_mode" in request.form:
        prefs["dark_mode"] = request.form["dark_mode"] == "true"
    if "api_key" in request.form:
        prefs["api_key"] = request.form["api_key"]
    if "monthly_budget" in request.form:
        try:
            prefs["monthly_budget"] = float(request.form["monthly_budget"])
        except ValueError:
            pass
    save_user_prefs(prefs)
    return redirect(url_for("index"))


@app.route("/api/toggle-dark", methods=["POST"])
def toggle_dark():
    prefs = load_user_prefs()
    prefs["dark_mode"] = not prefs.get("dark_mode", True)
    save_user_prefs(prefs)
    return redirect(url_for("index"))


@app.route("/api/upload-invoice", methods=["POST"])
def upload_invoice():
    prefs = load_user_prefs()
    api_key = prefs.get("api_key", "")
    if not api_key:
        flash("Configure ta cle API Gemini dans les parametres.", "error")
        return redirect(url_for("index"))
    if "file" not in request.files:
        flash("Aucun fichier selectionne.", "error")
        return redirect(url_for("index"))
    file = request.files["file"]
    if file.filename == "":
        flash("Aucun fichier selectionne.", "error")
        return redirect(url_for("index"))
    try:
        client = genai.Client(api_key=api_key)
        raw = file.read()
        mime = file.content_type or mimetypes.guess_type(file.filename)[0] or "image/jpeg"
        if "pdf" in mime:
            mime = "application/pdf"
        elif mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            mime = "image/jpeg"
        content_part = genai_types.Part.from_bytes(data=raw, mime_type=mime)
        data = extract_invoice_data(client, content_part)

        montant = float(data.get("montant") or 0)
        fournisseur = (data.get("fournisseur") or "Fournisseur inconnu").strip()
        history_df = load_history()
        anomaly = check_anomaly(history_df, fournisseur, montant)

        entry = {
            "date_ajout": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "fournisseur": fournisseur,
            "type_contrat": data.get("type_contrat"),
            "montant": montant,
            "devise": data.get("devise", "EUR"),
            "date_facture": data.get("date_facture"),
            "numero_contrat": data.get("numero_contrat"),
            "periode": data.get("periode"),
            "notes": data.get("notes"),
            "fichier": file.filename,
        }
        save_entry(entry)

        msg = f"Facture analysee : {fournisseur} — {montant:.2f} EUR"
        if anomaly and anomaly["is_anomaly"]:
            msg += f" | Hausse detectee : +{anomaly['delta_abs']:.2f} EUR ({anomaly['delta_pct']:.1f}%)"
            flash(msg, "warning")
        elif anomaly:
            msg += f" | Pas d'anomalie ({anomaly['delta_pct']:+.1f}%)"
            flash(msg, "success")
        else:
            flash(msg + " | Premiere facture pour ce fournisseur.", "success")
    except json.JSONDecodeError:
        flash("L'IA n'a pas renvoye un JSON exploitable. Reessaie.", "error")
    except Exception as exc:
        flash(f"Erreur : {exc}", "error")
    return redirect(url_for("index"))


@app.route("/api/upload-payslip", methods=["POST"])
def upload_payslip():
    prefs = load_user_prefs()
    api_key = prefs.get("api_key", "")
    if not api_key:
        flash("Configure ta cle API Gemini dans les parametres.", "error")
        return redirect(url_for("index"))
    if "file" not in request.files:
        flash("Aucun fichier selectionne.", "error")
        return redirect(url_for("index"))
    file = request.files["file"]
    if file.filename == "":
        flash("Aucun fichier selectionne.", "error")
        return redirect(url_for("index"))
    try:
        client = genai.Client(api_key=api_key)
        raw = file.read()
        mime = file.content_type or mimetypes.guess_type(file.filename)[0] or "image/jpeg"
        if "pdf" in mime:
            mime = "application/pdf"
        content_part = genai_types.Part.from_bytes(data=raw, mime_type=mime)
        data = extract_payslip_data(client, content_part)

        salaire_net = float(data.get("salaire_net") or 0)
        employeur = (data.get("employeur") or "Employeur inconnu").strip()
        payslips_df = load_payslips()
        anomaly = check_payslip_anomaly(payslips_df, salaire_net)

        entry = {
            "date_ajout": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "employeur": employeur,
            "mois": data.get("mois"),
            "salaire_brut": float(data.get("salaire_brut") or 0),
            "salaire_net": salaire_net,
            "net_imposable": float(data.get("net_imposable")) if data.get("net_imposable") else None,
            "devise": data.get("devise", "EUR"),
            "date_fiche": data.get("date_fiche"),
            "notes": data.get("notes"),
            "fichier": file.filename,
        }
        save_payslip(entry)

        msg = f"Fiche analysee : {employeur} — {salaire_net:.2f} EUR net"
        if anomaly and anomaly["is_anomaly"]:
            direction = "Hausse" if anomaly["delta_abs"] > 0 else "Baisse"
            msg += f" | {direction} : {anomaly['delta_abs']:+.2f} EUR ({anomaly['delta_pct']:+.1f}%)"
            flash(msg, "warning")
        elif anomaly:
            flash(msg + f" | Stable ({anomaly['delta_pct']:+.1f}%)", "success")
        else:
            flash(msg + " | Premiere fiche enregistree.", "success")
    except json.JSONDecodeError:
        flash("L'IA n'a pas renvoye un JSON exploitable. Reessaie.", "error")
    except Exception as exc:
        flash(f"Erreur : {exc}", "error")
    return redirect(url_for("index"))


@app.route("/api/import-csv", methods=["POST"])
def import_csv():
    if "file" not in request.files:
        flash("Aucun fichier selectionne.", "error")
        return redirect(url_for("index"))
    file = request.files["file"]
    if file.filename == "":
        flash("Aucun fichier selectionne.", "error")
        return redirect(url_for("index"))
    count, error = import_bank_csv(file)
    if error:
        flash(error, "error")
    else:
        flash(f"{count} transaction(s) importee(s) depuis {file.filename} !", "success")
    return redirect(url_for("index"))


@app.route("/api/savings-goal", methods=["POST"])
def add_savings_goal():
    name = request.form.get("name", "").strip()
    try:
        target = float(request.form.get("target", 0))
        saved = float(request.form.get("saved", 0))
    except ValueError:
        flash("Montants invalides.", "error")
        return redirect(url_for("index"))
    if not name or target <= 0:
        flash("Nom et montant cible requis.", "error")
        return redirect(url_for("index"))
    goals = load_savings_goals()
    goals.append({
        "name": name, "target": target, "saved": saved,
        "created": datetime.now().strftime("%Y-%m-%d"),
    })
    save_savings_goals(goals)
    flash(f"Objectif '{name}' ajoute !", "success")
    return redirect(url_for("index"))


@app.route("/api/savings-goal/<int:idx>/add", methods=["POST"])
def add_to_savings(idx):
    try:
        amount = float(request.form.get("amount", 0))
    except ValueError:
        flash("Montant invalide.", "error")
        return redirect(url_for("index"))
    goals = load_savings_goals()
    if 0 <= idx < len(goals) and amount > 0:
        goals[idx]["saved"] += amount
        save_savings_goals(goals)
        flash(f"+{amount:.0f} EUR ajoutes a '{goals[idx]['name']}'", "success")
    return redirect(url_for("index"))


@app.route("/api/savings-goal/<int:idx>/delete", methods=["POST"])
def delete_savings_goal(idx):
    goals = load_savings_goals()
    if 0 <= idx < len(goals):
        name = goals[idx]["name"]
        goals.pop(idx)
        save_savings_goals(goals)
        flash(f"Objectif '{name}' supprime.", "success")
    return redirect(url_for("index"))


@app.route("/api/category-budgets", methods=["POST"])
def update_category_budgets():
    budgets = {}
    for cat_name in TRANSACTION_CATEGORIES:
        if cat_name == "Autre":
            continue
        key = f"budget_{cat_name}"
        try:
            val = float(request.form.get(key, 0))
        except ValueError:
            val = 0
        budgets[cat_name] = val
    save_category_budgets(budgets)
    flash("Budgets par categorie sauvegardes !", "success")
    return redirect(url_for("index"))


@app.route("/api/export-csv/<month>")
def export_csv(month):
    tx_df = load_bank_transactions()
    if tx_df.empty:
        flash("Aucune transaction a exporter.", "error")
        return redirect(url_for("index"))

    tx_all = categorize_all_transactions(tx_df)
    tx_all["amount"] = pd.to_numeric(tx_all["amount"], errors="coerce")
    tx_all["date"] = pd.to_datetime(tx_all["date"], errors="coerce", dayfirst=True)
    tx_all = tx_all.dropna(subset=["amount", "date"])

    exp = tx_all[tx_all["date"].dt.to_period("M").astype(str) == month].copy()
    if exp.empty:
        flash(f"Aucune transaction pour {month}.", "error")
        return redirect(url_for("index"))

    exp = exp.sort_values("date")
    summary = exp.groupby("auto_category")["amount"].agg(["sum", "count"]).reset_index()
    summary.columns = ["Categorie", "Total", "Nb transactions"]
    summary = summary.sort_values("Total", ascending=False)

    csv_buffer = io.StringIO()
    csv_buffer.write(f"Resume mensuel - {month}\n\n")
    csv_buffer.write("=== RESUME PAR CATEGORIE ===\n")
    summary.to_csv(csv_buffer, index=False)
    csv_buffer.write(f"\nTotal general: {exp['amount'].sum():.2f} EUR\n")
    csv_buffer.write(f"Nombre de transactions: {len(exp)}\n\n")
    csv_buffer.write("=== DETAIL DES TRANSACTIONS ===\n")
    exp_display = exp[["date", "label", "amount", "auto_category"]].copy()
    exp_display.columns = ["Date", "Libelle", "Montant", "Categorie"]
    exp_display["Date"] = exp_display["Date"].dt.strftime("%d/%m/%Y")
    exp_display.to_csv(csv_buffer, index=False)

    output = io.BytesIO(csv_buffer.getvalue().encode("utf-8-sig"))
    return send_file(
        output, mimetype="text/csv", as_attachment=True,
        download_name=f"scribe_resume_{month}.csv",
    )


@app.route("/api/bilan/<int:year>")
def bilan_annuel(year):
    tx_df = load_bank_transactions()
    if tx_df.empty:
        return jsonify({"error": "Aucune transaction"})

    tx_all = categorize_all_transactions(tx_df)
    tx_all["amount"] = pd.to_numeric(tx_all["amount"], errors="coerce")
    tx_all["date"] = pd.to_datetime(tx_all["date"], errors="coerce", dayfirst=True)
    tx_all = tx_all.dropna(subset=["amount", "date"])
    year_tx = tx_all[tx_all["date"].dt.year == year]

    if year_tx.empty:
        return jsonify({"error": "Aucune transaction pour cette annee"})

    year_total = float(year_tx["amount"].sum())
    year_count = len(year_tx)
    year_avg = year_total / max(year_tx["date"].dt.month.nunique(), 1)

    month_names = {1:"Janvier",2:"Fevrier",3:"Mars",4:"Avril",5:"Mai",6:"Juin",
                   7:"Juillet",8:"Aout",9:"Septembre",10:"Octobre",11:"Novembre",12:"Decembre"}

    month_sums = year_tx.groupby(year_tx["date"].dt.month)["amount"].sum()
    most_expensive_num = int(month_sums.idxmax())
    most_expensive_name = month_names.get(most_expensive_num, "?")
    most_expensive_amount = float(month_sums.max())

    cat_sums = year_tx.groupby("auto_category")["amount"].sum().sort_values(ascending=False)
    top_cat = cat_sums.index[0] if len(cat_sums) > 0 else "?"
    top_cat_amount = float(cat_sums.iloc[0]) if len(cat_sums) > 0 else 0

    # Revenus estimes
    payslips_df = load_payslips()
    year_revenue = 0
    if not payslips_df.empty:
        payslips_df["salaire_net"] = pd.to_numeric(payslips_df["salaire_net"], errors="coerce")
        last_sal = payslips_df.dropna(subset=["salaire_net"]).sort_values("date_ajout")
        if not last_sal.empty:
            nb_months = year_tx["date"].dt.month.nunique()
            year_revenue = float(last_sal.iloc[-1]["salaire_net"]) * nb_months

    # Donnees mensuelles pour graphique
    monthly_data = []
    for m, amt in month_sums.items():
        monthly_data.append({"month": month_names.get(int(m), "?"), "amount": round(float(amt), 2)})

    # Top 5 categories
    top_cats = []
    for cat, amt in cat_sums.head(5).items():
        top_cats.append({
            "name": cat,
            "amount": round(float(amt), 2),
            "color": TRANSACTION_CATEGORIES.get(cat, {}).get("color", "#6b7280"),
            "emoji": TRANSACTION_CATEGORIES.get(cat, {}).get("emoji", ""),
        })

    return jsonify({
        "year": year,
        "total": round(year_total, 2),
        "count": year_count,
        "avg_month": round(year_avg, 2),
        "revenue": round(year_revenue, 2),
        "epargne": round(year_revenue - year_total, 2) if year_revenue > 0 else 0,
        "most_expensive_month": most_expensive_name,
        "most_expensive_amount": round(most_expensive_amount, 2),
        "top_category": top_cat,
        "top_category_amount": round(top_cat_amount, 2),
        "top_category_emoji": TRANSACTION_CATEGORIES.get(top_cat, {}).get("emoji", ""),
        "monthly": monthly_data,
        "top_categories": top_cats,
    })


@app.route("/api/simuler-impot", methods=["POST"])
def api_simuler_impot():
    """Simulateur d'IR interactif (appele en AJAX)."""
    try:
        revenu = float(request.form.get("revenu", 0))
        parts = float(request.form.get("parts", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "Valeurs invalides"}), 400

    if parts < 1:
        parts = 1
    if revenu < 0:
        revenu = 0

    # Appliquer l'abattement 10 %
    abattement = max(ABATTEMENT_10_MIN, min(revenu * 0.10, ABATTEMENT_10_MAX))
    revenu_apres = max(revenu - abattement, 0)

    result = simuler_ir(revenu_apres, parts)
    result["abattement"] = round(abattement, 2)
    result["revenu_declare"] = round(revenu, 2)
    result["nb_parts"] = parts
    return jsonify(result)


# --------------------------------------------------------------------------
# Enable Banking — Routes API
# --------------------------------------------------------------------------

@app.route("/api/eb-banks")
def eb_banks():
    """Liste les banques disponibles via Enable Banking (AJAX)."""
    country = request.args.get("country", "FR")
    banks, err = eb_list_banks(country)
    if err:
        return jsonify({"error": err}), 400
    # Simplifier la reponse
    result = []
    for b in banks:
        result.append({
            "name": b.get("name", ""),
            "country": b.get("country", country),
            "logo": b.get("logo", ""),
        })
    # Trier par nom
    result.sort(key=lambda x: x["name"])
    return jsonify(result)


@app.route("/api/eb-connect", methods=["POST"])
def eb_connect():
    """Initie la connexion avec une banque via Enable Banking."""
    bank_name = request.form.get("bank_name", "").strip()
    bank_country = request.form.get("bank_country", "FR").strip()
    if not bank_name:
        flash("Selectionne une banque.", "error")
        return redirect(url_for("index"))

    # Determiner l'URL de callback
    host = request.host_url.rstrip("/")
    redirect_url = f"{host}/api/eb-callback"

    auth_result, err = eb_start_auth(bank_name, bank_country, redirect_url)
    if err:
        flash(f"Erreur : {err}", "error")
        return redirect(url_for("index"))

    # Sauvegarder l'etat en attente
    eb_data = _eb_load()
    eb_data["pending_auth"] = {
        "state": auth_result.get("state"),
        "bank_name": bank_name,
        "bank_country": bank_country,
    }
    _eb_save(eb_data)

    # Rediriger vers la page d'authentification de la banque
    return redirect(auth_result.get("url", url_for("index")))


@app.route("/api/eb-callback")
def eb_callback():
    """Callback apres authentification bancaire Enable Banking."""
    try:
        code = request.args.get("code", "")
        state = request.args.get("state", "")

        eb_data = _eb_load()
        pending = eb_data.get("pending_auth")
        if not pending:
            flash("Aucune connexion en attente.", "error")
            return redirect(url_for("index"))

        # Verifier le state
        if state and pending.get("state") and state != pending["state"]:
            flash("Erreur de securite : state invalide.", "error")
            return redirect(url_for("index"))

        if not code:
            flash("Erreur : aucun code d'autorisation recu.", "error")
            return redirect(url_for("index"))

        # Creer la session avec le code
        session_data, err = eb_create_session(code)
        if err:
            flash(f"Erreur session : {err}", "error")
            eb_data.pop("pending_auth", None)
            _eb_save(eb_data)
            return redirect(url_for("index"))

        session_id = session_data.get("session_id", "")
        session_accounts = session_data.get("accounts", [])

        if not session_accounts:
            flash("Aucun compte trouve. La connexion a peut-etre echoue.", "warning")
            eb_data.pop("pending_auth", None)
            _eb_save(eb_data)
            return redirect(url_for("index"))

        # Sauvegarder les comptes connectes
        accounts = eb_data.get("accounts", [])
        new_count = 0
        for acc in session_accounts:
            acc_uid = acc.get("uid", "")
            if not acc_uid:
                continue
            # Verifier qu'il n'est pas deja connecte
            if any(a.get("uid") == acc_uid for a in accounts):
                continue
            # Extraire l'IBAN (format peut varier)
            iban = ""
            if isinstance(acc.get("account_id"), dict):
                iban = acc["account_id"].get("iban", "")
            elif isinstance(acc.get("iban"), str):
                iban = acc["iban"]
            accounts.append({
                "uid": acc_uid,
                "session_id": session_id,
                "iban": iban,
                "bank_name": pending.get("bank_name", "Banque"),
                "bank_country": pending.get("bank_country", "FR"),
                "connected_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
            new_count += 1

        eb_data["accounts"] = accounts
        eb_data.pop("pending_auth", None)
        _eb_save(eb_data)

        flash(f"{new_count} compte(s) connecte(s) avec succes !", "success")

        # Synchroniser les transactions automatiquement
        try:
            count, sync_err = eb_sync_all_accounts()
            if count > 0:
                flash(f"{count} transaction(s) importee(s) automatiquement.", "success")
            elif sync_err:
                flash(f"Sync : {sync_err}", "warning")
        except Exception as sync_exc:
            flash(f"Compte connecte, mais erreur de sync : {sync_exc}", "warning")

    except Exception as e:
        flash(f"Erreur callback : {e}", "error")

    return redirect(url_for("index"))


@app.route("/api/eb-sync", methods=["POST"])
def eb_sync():
    """Synchronise les transactions depuis les comptes connectes."""
    count, err = eb_sync_all_accounts()
    if err:
        flash(f"Erreur sync : {err}", "error")
    elif count == 0:
        flash("Aucune nouvelle transaction.", "info")
    else:
        flash(f"{count} nouvelle(s) transaction(s) importee(s) !", "success")
    return redirect(url_for("index"))


@app.route("/api/eb-disconnect", methods=["POST"])
def eb_disconnect():
    """Deconnecte un compte bancaire."""
    account_uid = request.form.get("account_uid", "").strip()
    eb_data = _eb_load()
    accounts = eb_data.get("accounts", [])
    eb_data["accounts"] = [a for a in accounts if a.get("uid") != account_uid]
    _eb_save(eb_data)
    flash("Compte deconnecte.", "success")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------
# Lancement
# --------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
