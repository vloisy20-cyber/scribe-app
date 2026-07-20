"""
Scribe — ton ombudsman personnel.

Une petite application Streamlit qui analyse tes factures et abonnements
(PDF ou photo), repère les hausses de prix ou anomalies par rapport à ton
historique, et rédige un brouillon de réclamation quand c'est nécessaire.

Tout reste en local : les documents ne sont envoyés qu'à l'API Gemini pour
être analysés, et l'historique est stocké dans un simple fichier CSV sur
ton ordinateur (data/history.csv). Rien n'est envoyé ailleurs.

En option, Scribe peut se connecter à tes comptes fournisseurs (EDF,
Orange, Bouygues, Free, SFR, Ameli...) via la bibliothèque woob pour
récupérer tes factures automatiquement.
"""

import base64
import io
import json
import mimetypes
import urllib.parse
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import streamlit as st

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

# --------------------------------------------------------------------------
# Catégorisation automatique des transactions
# --------------------------------------------------------------------------

TRANSACTION_CATEGORIES = {
    "Alimentation": {
        "icon": "🛒",
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
        "icon": "🚗",
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
        "icon": "🏠",
        "color": "#f59e0b",
        "keywords": [
            "LOYER", "CHARGES", "SYNDIC", "FONCIA", "NEXITY", "ORPI",
            "EDF", "ENGIE", "GDF", "VEOLIA", "SUEZ", "ELECTRICITE",
            "GAZ", "CHAUFFAGE", "TAXE HABITATION", "TAXE FONCIERE",
            "ASSURANCE HABITATION", "MRH",
        ],
    },
    "Sante": {
        "icon": "🏥",
        "color": "#ef4444",
        "keywords": [
            "PHARMACIE", "MEDECIN", "DOCTEUR", "HOPITAL", "CLINIQUE",
            "DENTISTE", "OPTICIEN", "KINE", "OSTEO", "CPAM", "AMELI",
            "MUTUELLE", "SANTE", "LABORATOIRE", "LABO ", "OPTIQUE",
            "LUNETTES", "DENTAL", "ORTHODONT",
        ],
    },
    "Loisirs": {
        "icon": "🎮",
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
        "icon": "🛍️",
        "color": "#ec4899",
        "keywords": [
            "AMAZON", "CDISCOUNT", "ALIEXPRESS", "SHEIN", "ZALANDO",
            "ZARA", "H&M", "KIABI", "DECATHLON", "IKEA", "LEROY MERLIN",
            "CASTORAMA", "DARTY", "BOULANGER", "ELECTRO", "VINTED",
            "LEBONCOIN", "ACTION", "GIFI", "HEMA", "PRIMARK", "UNIQLO",
        ],
    },
    "Assurance": {
        "icon": "🛡️",
        "color": "#14b8a6",
        "keywords": [
            "ASSURANCE", "AXA", "MAIF", "MACIF", "MATMUT", "ALLIANZ",
            "GROUPAMA", "MMA", "GMF", "ACM", "GENERALI", "DIRECT ASSUR",
            "OLIVIER ASSURANCE", "PRLV ASSUR",
        ],
    },
    "Telecom": {
        "icon": "📱",
        "color": "#06b6d4",
        "keywords": [
            "ORANGE", "FREE", "SFR", "BOUYGUES", "SOSH", "RED BY SFR",
            "B&YOU", "PRIXTEL", "OVH", "ICLOUD", "GOOGLE STORAGE",
        ],
    },
    "Impots & Taxes": {
        "icon": "🏛️",
        "color": "#78716c",
        "keywords": [
            "IMPOT", "TRESOR PUBLIC", "DGFIP", "TAXE", "AMENDE",
            "CONTRIB", "PRELEVEMENT SOURCE", "URSSAF", "CAF",
        ],
    },
    "Autre": {
        "icon": "📦",
        "color": "#6b7280",
        "keywords": [],
    },
}


def categorize_transaction(label: str) -> str:
    """Classe une transaction dans une catégorie selon le libellé."""
    up = label.upper().strip()
    for cat_name, cat_info in TRANSACTION_CATEGORIES.items():
        if cat_name == "Autre":
            continue
        for kw in cat_info["keywords"]:
            if kw in up:
                return cat_name
    return "Autre"


def categorize_all_transactions(tx_df: pd.DataFrame) -> pd.DataFrame:
    """Ajoute une colonne 'auto_category' à toutes les transactions."""
    df = tx_df.copy()
    df["auto_category"] = df["label"].apply(categorize_transaction)
    return df


def load_savings_goals() -> list:
    """Charge les objectifs d'épargne depuis le JSON."""
    if SAVINGS_PATH.exists():
        try:
            return json.loads(SAVINGS_PATH.read_text())
        except Exception:
            return []
    return []


def save_savings_goals(goals: list) -> None:
    """Sauvegarde les objectifs d'épargne."""
    SAVINGS_PATH.write_text(json.dumps(goals, ensure_ascii=False, indent=2))


def load_category_budgets() -> dict:
    """Charge les budgets par catégorie."""
    if CATEGORY_BUDGETS_PATH.exists():
        try:
            return json.loads(CATEGORY_BUDGETS_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_category_budgets(budgets: dict) -> None:
    """Sauvegarde les budgets par catégorie."""
    CATEGORY_BUDGETS_PATH.write_text(json.dumps(budgets, ensure_ascii=False, indent=2))

# Banques françaises connues (modules woob CapBank)
KNOWN_BANKS = {
    "BNP Paribas": {"module": "bnporc", "icon": "🏦"},
    "Credit Agricole": {"module": "cragr", "icon": "🏦"},
    "Societe Generale": {"module": "societegenerale", "icon": "🏦"},
    "La Banque Postale": {"module": "bp", "icon": "🏦"},
    "Credit Mutuel": {"module": "creditmutuel", "icon": "🏦"},
    "CIC": {"module": "cic", "icon": "🏦"},
    "Caisse d'Epargne": {"module": "caissedepargne", "icon": "🏦"},
    "Boursorama": {"module": "boursorama", "icon": "🏦"},
    "LCL": {"module": "lcl", "icon": "🏦"},
    "Fortuneo": {"module": "fortuneo", "icon": "🏦"},
    "ING": {"module": "ing", "icon": "🏦"},
    "Banque Populaire": {"module": "banquepopulaire", "icon": "🏦"},
    "HSBC": {"module": "hsbc", "icon": "🏦"},
    "Monabanq": {"module": "monabanq", "icon": "🏦"},
    "Hello Bank": {"module": "hellobank", "icon": "🏦"},
    "Revolut": {"module": None, "icon": "🏦"},
    "N26": {"module": None, "icon": "🏦"},
}

# Fournisseurs connus — ceux avec un module woob ont la récupération auto.
# Les autres sont "manuels" (upload de facture classique).
KNOWN_PROVIDERS = {
    # Énergie
    "EDF": {"module": "edfparticulier", "icon": "⚡", "cat": "Energie"},
    "Engie": {"module": "engie", "icon": "⚡", "cat": "Energie"},
    "TotalEnergies": {"module": None, "icon": "⚡", "cat": "Energie"},
    "Enercoop": {"module": "enercoop", "icon": "⚡", "cat": "Energie"},
    "Ekwateur": {"module": "ekwateur", "icon": "⚡", "cat": "Energie"},
    # Téléphonie / Internet
    "Orange": {"module": "orange", "icon": "📱", "cat": "Telecom"},
    "Bouygues Telecom": {"module": "bouyguestelecom", "icon": "📱", "cat": "Telecom"},
    "Free Mobile": {"module": "freemobile", "icon": "📱", "cat": "Telecom"},
    "Free (Internet)": {"module": "free", "icon": "🌐", "cat": "Telecom"},
    "SFR": {"module": "sfr", "icon": "📱", "cat": "Telecom"},
    "RED by SFR": {"module": None, "icon": "📱", "cat": "Telecom"},
    "Sosh": {"module": None, "icon": "📱", "cat": "Telecom"},
    "B&You": {"module": None, "icon": "📱", "cat": "Telecom"},
    # Santé
    "Ameli": {"module": "ameli", "icon": "🏥", "cat": "Sante"},
    # Assurance
    "AXA": {"module": None, "icon": "🛡️", "cat": "Assurance"},
    "MAIF": {"module": None, "icon": "🛡️", "cat": "Assurance"},
    "MACIF": {"module": None, "icon": "🛡️", "cat": "Assurance"},
    "Allianz": {"module": None, "icon": "🛡️", "cat": "Assurance"},
    "L'Olivier Assurance": {"module": None, "icon": "🛡️", "cat": "Assurance"},
    "Groupama": {"module": None, "icon": "🛡️", "cat": "Assurance"},
    "MATMUT": {"module": None, "icon": "🛡️", "cat": "Assurance"},
    "MMA": {"module": None, "icon": "🛡️", "cat": "Assurance"},
    "GMF": {"module": None, "icon": "🛡️", "cat": "Assurance"},
    # Eau
    "Veolia": {"module": None, "icon": "💧", "cat": "Eau"},
    "Suez": {"module": None, "icon": "💧", "cat": "Eau"},
    # Logement
    "Foncia": {"module": "foncia", "icon": "🏠", "cat": "Logement"},
    # Abonnements
    "Canal+": {"module": None, "icon": "📺", "cat": "Abonnement"},
    "Netflix": {"module": None, "icon": "📺", "cat": "Abonnement"},
    "OVH": {"module": "ovh", "icon": "☁️", "cat": "Abonnement"},
}

# Chargement dynamique : si woob est installé, on ajoute automatiquement
# tous les modules qui supportent les factures.
def _load_all_woob_providers():
    """Enrichit KNOWN_PROVIDERS avec tous les modules woob CapDocument."""
    if not _HAS_WOOB:
        return
    try:
        w = Woob()
        for name, info in w.repositories.get_all_modules_info().items():
            if "CapDocument" in info.capabilities:
                display = info.name.replace("_", " ").title() if info.name else name
                if display not in KNOWN_PROVIDERS:
                    KNOWN_PROVIDERS[display] = {
                        "module": name, "icon": "📄", "cat": "Autre",
                    }
    except Exception:
        pass  # réseau indisponible ou premier lancement — on garde la liste fixe

_load_all_woob_providers()

# Constante de compatibilité (utilisée dans le code existant)
PROVIDERS = KNOWN_PROVIDERS

st.set_page_config(page_title="Scribe", page_icon="🖋️", layout="wide")

# ── Supprimer badges et boutons Streamlit Cloud ──
st.html("""
<script>
function removeStreamlitBadges() {
    // Supprimer tous les elements fixes en bas de page (badges, boutons)
    document.querySelectorAll('iframe, [class*="Badge"], [class*="badge"], [class*="embeddedApp"]').forEach(el => el.remove());
    // Supprimer les elements positionnes en fixed dans le coin bas-droite
    document.querySelectorAll('*').forEach(el => {
        const s = window.getComputedStyle(el);
        if (s.position === 'fixed' && parseInt(s.bottom) < 80 && parseInt(s.right) < 80) {
            if (!el.closest('[data-testid="stMain"]') && !el.closest('.stApp')) {
                el.remove();
            }
        }
    });
    // Supprimer toolbar et header
    document.querySelectorAll('[data-testid="stToolbar"], [data-testid="stStatusWidget"], [data-testid="stDecoration"]').forEach(el => el.remove());
}
// Executer plusieurs fois car Streamlit injecte ces elements apres le chargement
removeStreamlitBadges();
setTimeout(removeStreamlitBadges, 1000);
setTimeout(removeStreamlitBadges, 3000);
setTimeout(removeStreamlitBadges, 5000);
setTimeout(removeStreamlitBadges, 10000);
setInterval(removeStreamlitBadges, 15000);
</script>
""")

# --------------------------------------------------------------------------
# Palettes de couleur d'accent
# --------------------------------------------------------------------------

ACCENT_THEMES = {
    "Bleu nuit": {
        "accent_dark": "#1c5cab",
        "accent_mid": "#2a78d6",
        "accent_light": "#5598e7",
        "accent_text": "#86b6ef",
        "accent_pale": "#b7d3f6",
        "accent_bg": "#16233a",
        "gradient_hint": "#0f1a2e",
        "gradient_hint2": "#111827",
        "gradient_hint3": "#0e1c30",
        "swatch": "#2a78d6",
    },
    "Emeraude": {
        "accent_dark": "#0f7b56",
        "accent_mid": "#10b981",
        "accent_light": "#34d399",
        "accent_text": "#6ee7b7",
        "accent_pale": "#a7f3d0",
        "accent_bg": "#132a20",
        "gradient_hint": "#0a1f17",
        "gradient_hint2": "#0f2922",
        "gradient_hint3": "#0b2119",
        "swatch": "#10b981",
    },
    "Violet": {
        "accent_dark": "#7c3aed",
        "accent_mid": "#8b5cf6",
        "accent_light": "#a78bfa",
        "accent_text": "#c4b5fd",
        "accent_pale": "#ddd6fe",
        "accent_bg": "#1e1636",
        "gradient_hint": "#170f2e",
        "gradient_hint2": "#1c1333",
        "gradient_hint3": "#191030",
        "swatch": "#8b5cf6",
    },
    "Corail": {
        "accent_dark": "#c2410c",
        "accent_mid": "#ea580c",
        "accent_light": "#f97316",
        "accent_text": "#fdba74",
        "accent_pale": "#fed7aa",
        "accent_bg": "#2a1810",
        "gradient_hint": "#1f150d",
        "gradient_hint2": "#261912",
        "gradient_hint3": "#22160e",
        "swatch": "#ea580c",
    },
    "Rose": {
        "accent_dark": "#be185d",
        "accent_mid": "#ec4899",
        "accent_light": "#f472b6",
        "accent_text": "#f9a8d4",
        "accent_pale": "#fbcfe8",
        "accent_bg": "#2a1226",
        "gradient_hint": "#1f0e1c",
        "gradient_hint2": "#261324",
        "gradient_hint3": "#220f1f",
        "swatch": "#ec4899",
    },
    "Or": {
        "accent_dark": "#b45309",
        "accent_mid": "#d97706",
        "accent_light": "#f59e0b",
        "accent_text": "#fbbf24",
        "accent_pale": "#fde68a",
        "accent_bg": "#271e0a",
        "gradient_hint": "#1c1608",
        "gradient_hint2": "#231b0c",
        "gradient_hint3": "#1f180a",
        "swatch": "#d97706",
    },
}

if "accent_theme" not in st.session_state:
    st.session_state.accent_theme = "Bleu nuit"

_th_raw = ACCENT_THEMES[st.session_state.accent_theme]

# --------------------------------------------------------------------------
# Mode clair / sombre
# --------------------------------------------------------------------------

if "dark_mode" not in st.session_state:
    st.session_state.dark_mode = True
_dark = st.session_state.dark_mode
_th = dict(_th_raw)
if not _dark:
    _th["accent_text"] = _th_raw["accent_mid"]
    _th["accent_bg"] = _th_raw["accent_pale"]
_link_hover = _th_raw["accent_pale"] if _dark else _th_raw["accent_dark"]

# Surface colors
_bg = "#0d0d0d" if _dark else "#f5f5f0"
_card = "#1a1a19" if _dark else "#ffffff"
_input_bg = "#232322" if _dark else "#ffffff"
_text1 = "#ffffff" if _dark else "#1a1a19"
_text2 = "#c3c2b7" if _dark else "#363632"
_text3 = "#898781" if _dark else "#555550"
_border = "rgba(255,255,255,0.08)" if _dark else "rgba(0,0,0,0.13)"
_sidebar_bg = "#141414" if _dark else "#eaeae5"
_grid = "#232322" if _dark else "#e0e0db"
_scrollbar_track = "#0d0d0d" if _dark else "#f5f5f0"
_scrollbar_thumb = "#383835" if _dark else "#c5c5c0"
_grad_base = "#0d0d0d" if _dark else "#f5f5f0"
# Teintes claires par accent (subtiles mais visibles)
_LIGHT_GRADS = {
    "Bleu nuit":  ("#dde6f4", "#e4ecf8", "#d8e2f2"),
    "Emeraude":   ("#ddf0e8", "#e4f4ed", "#d8ece4"),
    "Violet":     ("#e8e0f4", "#e2dbf2", "#ede5f8"),
    "Corail":     ("#f4e4d8", "#f6e6dc", "#f2e0d4"),
    "Rose":       ("#f4dde8", "#f2d8e4", "#f6e0ea"),
    "Or":         ("#f4ead8", "#f6eddc", "#f2e7d4"),
}
_lg = _LIGHT_GRADS.get(st.session_state.accent_theme, ("#eef0f5", "#edf2f0", "#f0eef5"))
_grad1 = _th["gradient_hint"] if _dark else _lg[0]
_grad2 = _th["gradient_hint2"] if _dark else _lg[1]
_grad3 = _th["gradient_hint3"] if _dark else _lg[2]

# --------------------------------------------------------------------------
# Logo SVG inline (couleur dynamique)
# --------------------------------------------------------------------------

LOGO_SVG_RAW = f'<svg width="44" height="44" viewBox="0 0 44 44" fill="none" xmlns="http://www.w3.org/2000/svg"><rect width="44" height="44" rx="12" fill="{_th_raw["accent_dark"]}"/><path d="M13 31L29 11" stroke="{_th_raw["accent_pale"]}" stroke-width="2.5" stroke-linecap="round"/><path d="M11 33l4-2-2-2-2 4z" fill="{_th_raw["accent_text"]}"/><path d="M18 28h12M18 24h10M18 20h8" stroke="#ffffff" stroke-width="1.5" stroke-linecap="round" opacity="0.7"/></svg>'


def svg_img(svg_str: str, width: int = 20, height: int = 20) -> str:
    """Convert raw SVG markup into an <img> tag with a data URI.

    This is necessary because st.html() renders in an isolated iframe
    where inline SVGs often fail to display. Data-URI images work
    reliably everywhere.
    """
    encoded = urllib.parse.quote(svg_str, safe="")
    return f'<img src="data:image/svg+xml,{encoded}" width="{width}" height="{height}" />'


LOGO_IMG = svg_img(LOGO_SVG_RAW, 44, 44)
LOGO_IMG_LG = svg_img(LOGO_SVG_RAW.replace('width="44"', 'width="56"').replace('height="44"', 'height="56"'), 56, 56)

# --------------------------------------------------------------------------
# Onboarding (première visite)
# --------------------------------------------------------------------------

def _load_user_prefs():
    if USER_PREFS_PATH.exists():
        try:
            return json.loads(USER_PREFS_PATH.read_text())
        except Exception:
            return {}
    return {}

def _save_user_prefs(prefs):
    USER_PREFS_PATH.write_text(json.dumps(prefs, ensure_ascii=False))

if "onboarded" not in st.session_state:
    _prefs = _load_user_prefs()
    st.session_state.onboarded = _prefs.get("onboarded", False)
if "onboard_transition" not in st.session_state:
    st.session_state.onboard_transition = False

# ── Animation de transition (stylo qui écrit) ──
if st.session_state.onboard_transition:
    st.markdown(f"""<style>
      .stApp, [data-testid="stAppViewContainer"] {{ background: #0a0a0a !important; }}
      #MainMenu, footer, [data-testid="stToolbar"], header[data-testid="stHeader"] {{ display: none !important; }}
      section[data-testid="stSidebar"] {{ display: none !important; }}
    </style>""", unsafe_allow_html=True)
    _tr_logo = LOGO_SVG_RAW.replace('width="44"', 'width="72"').replace('height="44"', 'height="72"').replace('rx="12"', 'rx="18"')
    _tr_logo_img = svg_img(_tr_logo, 72, 72)
    st.html(f"""
    <div style="position:fixed; inset:0; display:flex; align-items:center; justify-content:center; background:#0a0a0a; z-index:999999; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
      <style>
        @keyframes logoEnter {{
          0% {{ transform: scale(0.3) rotate(-10deg); opacity: 0; }}
          50% {{ transform: scale(1.1) rotate(2deg); opacity: 1; }}
          100% {{ transform: scale(1) rotate(0deg); opacity: 1; }}
        }}
        @keyframes penStroke {{
          0% {{ stroke-dashoffset: 200; }}
          100% {{ stroke-dashoffset: 0; }}
        }}
        @keyframes lineWrite1 {{
          0% {{ width: 0; opacity: 0; }}
          10% {{ opacity: 1; }}
          100% {{ width: 180px; opacity: 1; }}
        }}
        @keyframes lineWrite2 {{
          0% {{ width: 0; opacity: 0; }}
          10% {{ opacity: 1; }}
          100% {{ width: 140px; opacity: 1; }}
        }}
        @keyframes lineWrite3 {{
          0% {{ width: 0; opacity: 0; }}
          10% {{ opacity: 1; }}
          100% {{ width: 100px; opacity: 1; }}
        }}
        @keyframes titleReveal {{
          0% {{ opacity: 0; letter-spacing: 0.3em; filter: blur(8px); }}
          100% {{ opacity: 1; letter-spacing: -0.02em; filter: blur(0); }}
        }}
        @keyframes subReveal {{
          0% {{ opacity: 0; transform: translateY(10px); }}
          100% {{ opacity: 0.6; transform: translateY(0); }}
        }}
        @keyframes wholeExit {{
          0%,75% {{ opacity: 1; transform: scale(1); }}
          100% {{ opacity: 0; transform: scale(1.05); }}
        }}
        @keyframes glowRing {{
          0% {{ box-shadow: 0 0 0 0 {_th["accent_mid"]}66; }}
          50% {{ box-shadow: 0 0 0 20px {_th["accent_mid"]}00; }}
          100% {{ box-shadow: 0 0 0 0 {_th["accent_mid"]}00; }}
        }}
        .tr-wrap {{
          display: flex; flex-direction: column; align-items: center; gap: 24px;
          animation: wholeExit 3.2s ease forwards;
        }}
        .tr-logo {{
          animation: logoEnter 0.7s cubic-bezier(.34,1.56,.64,1) both, glowRing 1.5s ease 0.8s;
          border-radius: 18px;
        }}
        .tr-lines {{
          display: flex; flex-direction: column; align-items: center; gap: 8px;
          margin-top: 8px;
        }}
        .tr-line {{
          height: 3px; border-radius: 3px;
          background: linear-gradient(90deg, {_th["accent_dark"]}, {_th["accent_mid"]}, {_th["accent_light"]});
        }}
        .tr-l1 {{ animation: lineWrite1 0.8s ease 0.6s both; }}
        .tr-l2 {{ animation: lineWrite2 0.7s ease 0.9s both; }}
        .tr-l3 {{ animation: lineWrite3 0.6s ease 1.1s both; }}
        .tr-title {{
          font-size: 48px; font-weight: 900;
          background: linear-gradient(135deg, {_th["accent_light"]}, {_th["accent_pale"]});
          -webkit-background-clip: text; -webkit-text-fill-color: transparent;
          animation: titleReveal 0.8s ease 1.4s both;
        }}
        .tr-sub {{
          font-size: 14px; color: {_text3};
          animation: subReveal 0.6s ease 1.8s both;
        }}
      </style>
      <div class="tr-wrap">
        <div class="tr-logo">{_tr_logo_img}</div>
        <div class="tr-lines">
          <div class="tr-line tr-l1"></div>
          <div class="tr-line tr-l2"></div>
          <div class="tr-line tr-l3"></div>
        </div>
        <div class="tr-title">Scribe</div>
        <div class="tr-sub">Chargement...</div>
      </div>
    </div>
    """)
    import time
    time.sleep(3)
    st.session_state.onboard_transition = False
    st.session_state.onboarded = True
    _prefs = _load_user_prefs()
    _prefs["onboarded"] = True
    _save_user_prefs(_prefs)
    st.rerun()

# ── Page d'onboarding ──
if not st.session_state.onboarded:
    st.markdown(f"""<style>
      .stApp, [data-testid="stAppViewContainer"] {{
        background: linear-gradient(-45deg, #0a0a0a, {_th["gradient_hint"]}, #0a0a0a, {_th["gradient_hint2"]}) !important;
        background-size: 400% 400% !important;
        animation: waveGradient 20s ease infinite !important;
      }}
      @keyframes waveGradient {{
        0% {{ background-position: 0% 50%; }}
        50% {{ background-position: 100% 50%; }}
        100% {{ background-position: 0% 50%; }}
      }}
      #MainMenu, footer, [data-testid="stToolbar"] {{ display: none !important; }}
      section[data-testid="stSidebar"] {{ display: none !important; width: 0 !important; min-width: 0 !important; }}
      header[data-testid="stHeader"] {{ display: none !important; }}
      [data-testid="stAppViewContainer"], [data-testid="stMain"] {{
        margin-left: 0 !important;
        width: 100% !important;
      }}
      .stMainBlockContainer, [data-testid="stMainBlockContainer"], [data-testid="block-container"] {{
        max-width: 640px !important;
        margin-left: auto !important;
        margin-right: auto !important;
        padding-left: 1rem !important;
        padding-right: 1rem !important;
      }}
    </style>""", unsafe_allow_html=True)

    _logo_big = LOGO_SVG_RAW.replace('width="44"', 'width="80"').replace('height="44"', 'height="80"').replace('rx="12"', 'rx="20"')
    _logo_big_img = svg_img(_logo_big, 80, 80)

    st.html(f"""
    <div id="ob-scene" style="position:relative; max-width:640px; margin:0 auto; min-height:70vh; display:flex; flex-direction:column; align-items:center; justify-content:center; font-family:system-ui,-apple-system,'Segoe UI',sans-serif; overflow:hidden; padding-top:0;">

      <!-- Particules flottantes -->
      <style>
        @keyframes float1 {{ 0%,100% {{ transform: translate(0,0) scale(1); opacity:0.4; }} 25% {{ transform: translate(60px,-80px) scale(1.2); opacity:0.7; }} 50% {{ transform: translate(-30px,-140px) scale(0.8); opacity:0.3; }} 75% {{ transform: translate(40px,-60px) scale(1.1); opacity:0.6; }} }}
        @keyframes float2 {{ 0%,100% {{ transform: translate(0,0) scale(1); opacity:0.3; }} 33% {{ transform: translate(-70px,-100px) scale(1.3); opacity:0.6; }} 66% {{ transform: translate(50px,-50px) scale(0.9); opacity:0.4; }} }}
        @keyframes float3 {{ 0%,100% {{ transform: translate(0,0) scale(0.8); opacity:0.2; }} 50% {{ transform: translate(80px,-120px) scale(1.4); opacity:0.5; }} }}
        @keyframes float4 {{ 0%,100% {{ transform: translate(0,0); opacity:0.3; }} 40% {{ transform: translate(-50px,-90px); opacity:0.6; }} 80% {{ transform: translate(30px,-150px); opacity:0.2; }} }}
        @keyframes float5 {{ 0%,100% {{ transform: translate(0,0) rotate(0deg); opacity:0.2; }} 50% {{ transform: translate(-60px,-110px) rotate(180deg); opacity:0.5; }} }}

        @keyframes pulseGlow {{
          0%,100% {{ box-shadow: 0 0 20px {_th["accent_mid"]}33, 0 0 60px {_th["accent_dark"]}22; }}
          50% {{ box-shadow: 0 0 40px {_th["accent_mid"]}55, 0 0 100px {_th["accent_dark"]}44; }}
        }}
        @keyframes titleGlow {{
          0% {{ opacity:0; transform: translateY(30px) scale(0.9); filter: blur(10px); }}
          100% {{ opacity:1; transform: translateY(0) scale(1); filter: blur(0); }}
        }}
        @keyframes subtitleIn {{
          0% {{ opacity:0; transform: translateY(20px); }}
          100% {{ opacity:1; transform: translateY(0); }}
        }}
        @keyframes featureSlide {{
          0% {{ opacity:0; transform: translateY(30px) scale(0.95); }}
          100% {{ opacity:1; transform: translateY(0) scale(1); }}
        }}
        @keyframes orbitRing {{
          0% {{ transform: rotate(0deg); }}
          100% {{ transform: rotate(360deg); }}
        }}
        @keyframes shimmerText {{
          0% {{ background-position: -200% center; }}
          100% {{ background-position: 200% center; }}
        }}
        @keyframes breathe {{
          0%,100% {{ transform: scale(1); }}
          50% {{ transform: scale(1.05); }}
        }}

        .particle {{
          position: absolute;
          border-radius: 50%;
          pointer-events: none;
        }}
        .p1 {{ width:6px; height:6px; background:{_th["accent_light"]}; top:20%; left:10%; animation: float1 8s ease-in-out infinite; }}
        .p2 {{ width:4px; height:4px; background:{_th["accent_mid"]}; top:60%; left:80%; animation: float2 10s ease-in-out infinite; }}
        .p3 {{ width:8px; height:8px; background:{_th["accent_pale"]}; top:70%; left:20%; animation: float3 12s ease-in-out infinite; }}
        .p4 {{ width:3px; height:3px; background:{_th["accent_text"]}; top:30%; left:70%; animation: float4 9s ease-in-out infinite; }}
        .p5 {{ width:5px; height:5px; background:{_th["accent_light"]}; top:80%; left:50%; animation: float5 11s ease-in-out infinite; }}
        .p6 {{ width:4px; height:4px; background:{_th["accent_mid"]}; top:15%; left:60%; animation: float2 7s ease-in-out infinite 1s; }}
        .p7 {{ width:6px; height:6px; background:{_th["accent_pale"]}; top:50%; left:90%; animation: float1 13s ease-in-out infinite 2s; }}
        .p8 {{ width:3px; height:3px; background:{_th["accent_text"]}; top:85%; left:35%; animation: float3 9s ease-in-out infinite 0.5s; }}

        .ob-orbit {{
          position: absolute;
          width: 200px; height: 200px;
          border: 1px solid {_th["accent_mid"]}15;
          border-radius: 50%;
          animation: orbitRing 20s linear infinite;
        }}
        .ob-orbit2 {{
          position: absolute;
          width: 300px; height: 300px;
          border: 1px solid {_th["accent_dark"]}10;
          border-radius: 50%;
          animation: orbitRing 30s linear infinite reverse;
        }}
        .ob-orbit::after {{
          content: '';
          position: absolute;
          width: 8px; height: 8px;
          background: {_th["accent_light"]};
          border-radius: 50%;
          top: -4px; left: 50%;
          box-shadow: 0 0 12px {_th["accent_light"]}88;
        }}
        .ob-orbit2::after {{
          content: '';
          position: absolute;
          width: 5px; height: 5px;
          background: {_th["accent_pale"]};
          border-radius: 50%;
          bottom: -3px; right: 20%;
          box-shadow: 0 0 8px {_th["accent_pale"]}66;
        }}

        .ob-logo-wrap {{
          position: relative;
          animation: breathe 4s ease-in-out infinite, titleGlow 1s ease 0.2s both;
          z-index: 2;
        }}
        .ob-logo-inner {{
          border-radius: 24px;
          animation: pulseGlow 3s ease-in-out infinite;
          padding: 4px;
        }}
        .ob-logo-ring {{
          position: absolute;
          inset: -16px;
          border: 2px solid {_th["accent_mid"]}22;
          border-radius: 32px;
          animation: breathe 4s ease-in-out infinite 0.5s;
        }}

        .ob-title {{
          font-size: 52px;
          font-weight: 900;
          letter-spacing: -0.04em;
          margin: 24px 0 0 0;
          background: linear-gradient(135deg, {_th["accent_light"]}, #ffffff, {_th["accent_pale"]}, {_th["accent_light"]});
          background-size: 300% auto;
          -webkit-background-clip: text;
          -webkit-text-fill-color: transparent;
          animation: titleGlow 0.8s ease 0.4s both, shimmerText 4s linear infinite;
          z-index: 2;
          position: relative;
        }}
        .ob-sub {{
          font-size: 17px;
          color: {_text3};
          margin: 12px 0 24px 0;
          line-height: 1.6;
          animation: subtitleIn 0.8s ease 0.7s both;
          z-index: 2;
          position: relative;
          text-align: center;
        }}

        .ob-features {{
          display: flex;
          gap: 14px;
          width: 100%;
          z-index: 2;
          position: relative;
          overflow-x: auto;
          -webkit-overflow-scrolling: touch;
          scroll-snap-type: x mandatory;
          padding-bottom: 12px;
          scrollbar-width: none;
        }}
        .ob-features::-webkit-scrollbar {{ display: none; }}
        .ob-feat {{
          flex: 0 0 auto;
          width: calc(25% - 11px);
          background: rgba(255,255,255,0.03);
          border: 1px solid rgba(255,255,255,0.07);
          border-radius: 18px;
          padding: 24px 16px;
          text-align: center;
          backdrop-filter: blur(16px);
          transition: all 0.4s cubic-bezier(.4,0,.2,1);
          scroll-snap-align: start;
        }}
        @media (max-width: 600px) {{
          .ob-features {{
            gap: 10px;
            padding-left: 4px;
            padding-right: 4px;
          }}
          .ob-feat {{
            width: 140px;
            min-width: 140px;
            padding: 20px 12px;
          }}
          .ob-title {{
            font-size: 38px !important;
          }}
          .ob-sub {{
            font-size: 15px !important;
          }}
        }}
        .ob-feat:hover {{
          background: rgba(255,255,255,0.08);
          border-color: {_th["accent_mid"]}44;
          transform: translateY(-6px);
          box-shadow: 0 12px 40px {_th["accent_dark"]}33;
        }}
        .ob-feat-icon {{
          font-size: 28px;
          margin-bottom: 10px;
          display: block;
          filter: drop-shadow(0 4px 8px rgba(0,0,0,0.3));
        }}
        .ob-feat-title {{
          font-weight: 700;
          color: #ffffff;
          font-size: 14px;
          margin-bottom: 6px;
        }}
        .ob-feat-desc {{
          color: {_text3};
          font-size: 12px;
          line-height: 1.4;
        }}
        .ob-feat:nth-child(1) {{ animation: featureSlide 0.7s ease 0.9s both; }}
        .ob-feat:nth-child(2) {{ animation: featureSlide 0.7s ease 1.1s both; }}
        .ob-feat:nth-child(3) {{ animation: featureSlide 0.7s ease 1.3s both; }}
        .ob-feat:nth-child(4) {{ animation: featureSlide 0.7s ease 1.5s both; }}
      </style>

      <!-- Particules -->
      <div class="particle p1"></div><div class="particle p2"></div>
      <div class="particle p3"></div><div class="particle p4"></div>
      <div class="particle p5"></div><div class="particle p6"></div>
      <div class="particle p7"></div><div class="particle p8"></div>

      <!-- Orbites -->
      <div class="ob-orbit" style="top:calc(50% - 100px); left:calc(50% - 100px);"></div>
      <div class="ob-orbit2" style="top:calc(50% - 150px); left:calc(50% - 150px);"></div>

      <!-- Logo avec glow -->
      <div class="ob-logo-wrap">
        <div class="ob-logo-ring"></div>
        <div class="ob-logo-inner">{_logo_big_img}</div>
      </div>

      <!-- Titre -->
      <h1 class="ob-title">Scribe</h1>
      <p class="ob-sub">Tes finances perso sous controle.<br>Analyse tes depenses, suis tes abonnements,<br>et garde le controle — tout reste en local.</p>

      <!-- Features -->
      <div class="ob-features">
        <div class="ob-feat">
          <span class="ob-feat-icon">📊</span>
          <div class="ob-feat-title">Dashboard</div>
          <div class="ob-feat-desc">Revenus, depenses et budgets en un coup d'oeil</div>
        </div>
        <div class="ob-feat">
          <span class="ob-feat-icon">🔍</span>
          <div class="ob-feat-title">Analyse IA</div>
          <div class="ob-feat-desc">Factures et fiches de paie lues automatiquement</div>
        </div>
        <div class="ob-feat">
          <span class="ob-feat-icon">🎯</span>
          <div class="ob-feat-title">Objectifs</div>
          <div class="ob-feat-desc">Epargne et budgets par categorie</div>
        </div>
        <div class="ob-feat">
          <span class="ob-feat-icon">🔒</span>
          <div class="ob-feat-title">100% Local</div>
          <div class="ob-feat-desc">Tes donnees restent sur ton appareil</div>
        </div>
      </div>
    </div>
    """)
    # Bouton animé centré
    st.markdown(f"""<style>
      .stMainBlockContainer .stButton {{
        display: flex !important;
        justify-content: center !important;
        margin-top: 8px !important;
      }}
      .stMainBlockContainer .stButton > button {{
        background: linear-gradient(135deg, {_th["accent_dark"]}, {_th["accent_mid"]}, {_th["accent_light"]}, {_th["accent_mid"]}, {_th["accent_dark"]}) !important;
        background-size: 400% 400% !important;
        animation: btnFlow 6s ease infinite !important;
        color: #ffffff !important;
        border: none !important;
        border-radius: 16px !important;
        font-size: 18px !important;
        font-weight: 800 !important;
        padding: 16px 80px !important;
        letter-spacing: 0.04em !important;
        text-transform: uppercase !important;
        box-shadow: 0 8px 32px {_th["accent_dark"]}66, 0 0 60px {_th["accent_mid"]}22 !important;
        transition: all 0.4s cubic-bezier(.4,0,.2,1) !important;
        width: 580px !important;
        max-width: 90vw !important;
        position: relative !important;
        overflow: hidden !important;
      }}
      .stMainBlockContainer .stButton > button:hover {{
        transform: translateY(-3px) scale(1.03) !important;
        box-shadow: 0 14px 48px {_th["accent_dark"]}88, 0 0 80px {_th["accent_mid"]}33 !important;
      }}
      .stMainBlockContainer .stButton > button::after {{
        content: '' !important;
        position: absolute !important;
        top: -50% !important;
        left: -50% !important;
        width: 200% !important;
        height: 200% !important;
        background: linear-gradient(transparent, rgba(255,255,255,0.1), transparent) !important;
        transform: rotate(45deg) !important;
        animation: btnSweep 3s ease-in-out infinite !important;
      }}
      @keyframes btnFlow {{
        0% {{ background-position: 0% 50%; }}
        50% {{ background-position: 100% 50%; }}
        100% {{ background-position: 0% 50%; }}
      }}
      @keyframes btnSweep {{
        0% {{ transform: rotate(45deg) translateY(-200%); }}
        50% {{ transform: rotate(45deg) translateY(200%); }}
        100% {{ transform: rotate(45deg) translateY(-200%); }}
      }}
    </style>""", unsafe_allow_html=True)
    if st.button("Commencer", type="primary"):
        st.session_state.onboard_transition = True
        st.rerun()
    st.stop()

# --------------------------------------------------------------------------
# Thème sombre — CSS complet
# --------------------------------------------------------------------------

_bg_anim_css = f"""
  @keyframes waveGradient {{
    0%   {{ background-position: 0% 50%; }}
    25%  {{ background-position: 50% 100%; }}
    50%  {{ background-position: 100% 50%; }}
    75%  {{ background-position: 50% 0%; }}
    100% {{ background-position: 0% 50%; }}
  }}

  .stApp, [data-testid="stAppViewContainer"] {{
    background: linear-gradient(
      -45deg,
      {_grad_base},
      {_grad1},
      {_grad_base},
      {_grad2},
      {_grad_base},
      {_grad3}
    ) !important;
    background-size: 400% 400% !important;
    animation: waveGradient 25s ease infinite !important;
  }}
""" if _dark else f"""
  @keyframes waveGradient {{
    0%   {{ background-position: 0% 50%; }}
    25%  {{ background-position: 50% 100%; }}
    50%  {{ background-position: 100% 50%; }}
    75%  {{ background-position: 50% 0%; }}
    100% {{ background-position: 0% 50%; }}
  }}

  .stApp, [data-testid="stAppViewContainer"] {{
    background: linear-gradient(
      -45deg,
      {_grad_base},
      {_grad1},
      {_grad_base},
      {_grad2},
      {_grad_base},
      {_grad3}
    ) !important;
    background-size: 400% 400% !important;
    animation: waveGradient 30s ease infinite !important;
  }}
"""

st.markdown(f"""
<style>
  /* ─── Fond animé vagues/gradient ─── */
  {_bg_anim_css}

  [data-testid="stHeader"], [data-testid="stToolbar"] {{
    background-color: transparent !important;
  }}
  /* ─── Masquer menu GitHub, footer, et boutons Streamlit Cloud ─── */
  #MainMenu, footer, header,
  .viewerBadge_container__r5tak,
  .styles_viewerBadge__CvC9N,
  [data-testid="manage-app-button"],
  [data-testid="stStatusWidget"],
  [data-testid="stToolbar"],
  [data-testid="stHeader"],
  [data-testid="stDecoration"],
  ._link_gzau3_10,
  ._profileContainer_gzau3_53,
  .stDeployButton,
  ._container_gzau3_1,
  [data-testid="baseButton-header"],
  div[class*="stApp"] > header,
  iframe[title="streamlit_app_badge"],
  div[data-testid="collapsedControl"],
  .reportview-container .main footer,
  div.embeddedAppMetaInfoBar_container__DxxL1,
  div[class*="embeddedAppMetaInfoBar"],
  div[class*="StatusWidget"],
  button[kind="header"],
  div[class*="stAppDeployButton"],
  section[data-testid="stSidebar"] button[kind="header"] {{
    display: none !important;
    visibility: hidden !important;
    height: 0 !important;
    width: 0 !important;
    overflow: hidden !important;
    position: absolute !important;
    top: -9999px !important;
  }}
  /* ─── Forcer suppression badge coin bas-droite ─── */
  div[class*="viewerBadge"],
  div[class*="Badge"],
  a[href*="streamlit.io"],
  div.stApp > div:last-child > div:last-child > a,
  .stApp iframe {{
    display: none !important;
    visibility: hidden !important;
    height: 0 !important;
    width: 0 !important;
  }}
  [data-testid="stAppViewContainer"],
  [data-testid="stMain"],
  .stMainBlockContainer,
  [data-testid="stMainBlockContainer"],
  [data-testid="block-container"] {{
    background: transparent !important;
    background-color: transparent !important;
  }}

  /* ─── Sidebar masquee ─── */
  section[data-testid="stSidebar"],
  [data-testid="stSidebarCollapsedControl"],
  button[data-testid="stSidebarNavCollapseButton"] {{
    display: none !important;
    width: 0 !important;
    min-width: 0 !important;
  }}

  /* ─── Textes ─── */
  h1, h2, h3, h4, h5, h6 {{ color: {_text1} !important; }}
  p, span, label, li, div {{ color: {_text2}; }}
  a {{ color: {_th["accent_text"]} !important; }}
  a:hover {{ color: {_link_hover} !important; }}

  /* ─── Hero header ─── */
  .scribe-hero {{
    display: flex;
    align-items: center;
    gap: 20px;
    padding: 32px 0 8px 0;
  }}
  .scribe-hero h1 {{
    font-size: 42px !important;
    font-weight: 800 !important;
    letter-spacing: -0.02em;
    margin: 0 !important;
    padding: 0 !important;
    line-height: 1.1;
  }}
  .scribe-hero-sub {{
    font-size: 15px;
    color: {_text3};
    margin-top: 4px;
  }}
  .scribe-badge {{
    display: inline-block;
    padding: 5px 14px;
    border-radius: 999px;
    background: {_th["accent_bg"]};
    color: {_th["accent_text"]} !important;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: .04em;
    text-transform: uppercase;
    margin-top: 10px;
  }}

  /* ─── Cartes ─── */
  .scribe-card {{
    background: {_card};
    border: 1px solid {_border};
    border-radius: 14px;
    padding: 24px 28px;
    margin-bottom: 20px;
  }}
  .scribe-card h3 {{
    font-size: 18px;
    font-weight: 700;
    margin: 0 0 16px 0;
    color: {_text1} !important;
  }}

  /* ─── Stat tiles ─── */
  @keyframes fadeSlideUp {{
    0% {{ opacity: 0; transform: translateY(12px); }}
    100% {{ opacity: 1; transform: translateY(0); }}
  }}
  .scribe-stats {{
    display: flex;
    gap: 16px;
    margin-bottom: 24px;
  }}
  .scribe-stat {{
    flex: 1;
    background: {_card};
    border: 1px solid {_border};
    border-radius: 14px;
    padding: 20px 24px;
    text-align: center;
    animation: fadeSlideUp 0.5s ease both;
  }}
  .scribe-stat:nth-child(2) {{ animation-delay: 0.1s; }}
  .scribe-stat:nth-child(3) {{ animation-delay: 0.2s; }}
  .scribe-stat-value {{
    font-size: 32px;
    font-weight: 800;
    color: {_th["accent_text"]} !important;
    line-height: 1.2;
    animation: fadeSlideUp 0.6s ease both;
    animation-delay: 0.15s;
  }}
  .scribe-stat-label {{
    font-size: 12px;
    font-weight: 600;
    color: {_text3} !important;
    text-transform: uppercase;
    letter-spacing: .04em;
    margin-top: 6px;
  }}

  /* ─── Section headers ─── */
  .scribe-section {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 32px 0 16px 0;
  }}
  .scribe-section-icon {{
    width: 36px; height: 36px;
    border-radius: 10px;
    background: {_th["accent_bg"]};
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .scribe-section-icon svg {{
    width: 20px; height: 20px;
    stroke: {_th["accent_text"]}; fill: none;
    stroke-width: 1.75; stroke-linecap: round; stroke-linejoin: round;
  }}
  .scribe-section h2 {{
    font-size: 22px !important;
    font-weight: 700 !important;
    margin: 0 !important;
  }}

  /* ─── Upload zone ─── */
  [data-testid="stFileUploader"] {{
    background: {_card} !important;
    border: 2px dashed {_border} !important;
    border-radius: 14px !important;
    padding: 16px !important;
  }}
  [data-testid="stFileUploader"]:hover {{
    border-color: {_th["accent_dark"]} !important;
  }}

  /* ─── Boutons ─── */
  .stButton > button[kind="primary"],
  .stButton > button[data-testid="stBaseButton-primary"] {{
    background: linear-gradient(135deg, {_th["accent_dark"]}, {_th["accent_mid"]}) !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 10px !important;
    font-weight: 600 !important;
    padding: 10px 28px !important;
    transition: all 0.2s ease;
  }}
  .stButton > button[kind="primary"]:hover,
  .stButton > button[data-testid="stBaseButton-primary"]:hover {{
    background: linear-gradient(135deg, {_th["accent_mid"]}, {_th["accent_light"]}) !important;
    transform: translateY(-1px);
    box-shadow: 0 4px 16px {_th["accent_dark"]}4d !important;
  }}
  .stButton > button {{
    background: {_input_bg} !important;
    color: {_text2} !important;
    border: 1px solid {_border} !important;
    border-radius: 10px !important;
  }}

  /* ─── Inputs ─── */
  .stTextInput > div > div > input,
  .stTextArea > div > div > textarea,
  .stSelectbox > div > div {{
    background: {_input_bg} !important;
    color: {_text1} !important;
    border: 1px solid {_border} !important;
    border-radius: 10px !important;
  }}
  .stTextInput > div > div > input:focus,
  .stTextArea > div > div > textarea:focus {{
    border-color: {_th["accent_dark"]} !important;
    box-shadow: 0 0 0 2px {_th["accent_dark"]}33 !important;
  }}

  /* ─── Metrics ─── */
  [data-testid="stMetricValue"] {{
    color: {_th["accent_text"]} !important;
    font-weight: 700 !important;
  }}
  [data-testid="stMetricLabel"] {{
    color: {_text3} !important;
  }}
  div[data-testid="stMetric"] {{
    background: {_card};
    border: 1px solid {_border};
    border-radius: 12px;
    padding: 16px;
  }}

  /* ─── Dataframe ─── */
  [data-testid="stDataFrame"] {{
    border-radius: 14px;
    overflow: hidden;
  }}
  .stDataFrame thead th {{
    background: {_input_bg} !important;
    color: {_text3} !important;
    font-size: 11px !important;
    text-transform: uppercase;
    letter-spacing: .04em;
  }}
  .stDataFrame tbody td {{
    background: {_card} !important;
    color: {_text2} !important;
    border-bottom: 1px solid {_grid} !important;
  }}

  /* ─── Alerts ─── */
  [data-testid="stAlert"] {{
    border-radius: 12px !important;
    border: none !important;
  }}

  /* ─── Expander ─── */
  [data-testid="stExpander"] {{
    background: {_card} !important;
    border: 1px solid {_border} !important;
    border-radius: 12px !important;
  }}

  /* ─── Divider ─── */
  hr {{ border-color: {_grid} !important; }}

  /* ─── Empty state ─── */
  .scribe-empty {{
    text-align: center;
    padding: 48px 24px;
    color: #52514e;
  }}
  .scribe-empty-icon {{
    font-size: 48px;
    margin-bottom: 12px;
    opacity: 0.4;
  }}
  .scribe-empty p {{
    font-size: 15px;
    color: {_text3};
  }}

  /* ─── Scrollbar ─── */
  ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
  ::-webkit-scrollbar-track {{ background: {_scrollbar_track}; }}
  ::-webkit-scrollbar-thumb {{ background: {_scrollbar_thumb}; border-radius: 3px; }}
  ::-webkit-scrollbar-thumb:hover {{ background: #52514e; }}

  /* ─── Hide Streamlit defaults ─── */
  #MainMenu, footer, [data-testid="stToolbar"] {{ display: none !important; }}

  /* ─── Theme radio pills (top bar + expanders) ─── */
  .stRadio [role="radiogroup"] {{
    gap: 4px !important;
    flex-wrap: wrap;
  }}
  .stRadio label {{
    font-size: 11px !important;
    padding: 3px 8px !important;
    border-radius: 8px !important;
    background: {_input_bg} !important;
    border: 1px solid {_border} !important;
    min-height: 0 !important;
    transition: all 0.15s ease;
  }}
  .stRadio label:hover {{
    border-color: {_th["accent_text"]} !important;
  }}
  .stRadio label[data-checked="true"] {{
    background: {_th["accent_bg"]} !important;
    border-color: {_th["accent_text"]} !important;
    color: {_th["accent_text"]} !important;
  }}
  .stRadio label span p {{
    font-size: 11px !important;
  }}
</style>
""", unsafe_allow_html=True)

# --------------------------------------------------------------------------
# Historique (stockage local en CSV)
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# Fiches de paie (stockage local en CSV)
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# Appels à l'API Claude
# --------------------------------------------------------------------------

def file_to_content_part(uploaded_file):
    """Convertit un fichier uploadé en Part Gemini (inline bytes)."""
    raw = uploaded_file.getvalue()
    mime = uploaded_file.type or mimetypes.guess_type(uploaded_file.name)[0] or ""
    if "pdf" in mime:
        mime = "application/pdf"
    elif mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        mime = "image/jpeg"
    return genai_types.Part.from_bytes(data=raw, mime_type=mime)


EXTRACTION_PROMPT = """Tu es un assistant qui lit des factures et des documents \
d'abonnement (téléphonie, énergie, assurance, internet, etc.) pour un particulier.

Analyse le document ci-joint et réponds UNIQUEMENT avec un objet JSON valide, \
sans texte autour, sans balises markdown, avec exactement ces clés :

{
  "fournisseur": "nom du fournisseur ou de l'entreprise",
  "type_contrat": "catégorie courte, ex: Téléphonie mobile, Électricité, Assurance habitation",
  "montant": nombre décimal du montant total TTC, avec un point comme séparateur (ex: 42.90), sans symbole monétaire,
  "devise": "code devise à 3 lettres, ex: EUR",
  "date_facture": "date du document au format AAAA-MM-JJ, ou null si introuvable",
  "numero_contrat": "numéro de contrat ou de client s'il est visible, sinon null",
  "periode": "période de facturation si indiquée, ex: 'Juillet 2026', sinon null",
  "notes": "une phrase courte si un élément inhabituel apparaît explicitement sur le document (mention de hausse tarifaire, fin de promotion, nouveau frais...), sinon une chaîne vide"
}

Si une information est réellement introuvable, mets null (ou 0 pour montant si vraiment aucun montant n'apparaît). \
Ne fais aucune supposition non justifiée par le document."""


def extract_invoice_data(client, content_part) -> dict:
    resp = client.models.generate_content(
        model=MODEL,
        contents=[content_part, EXTRACTION_PROMPT],
    )
    text = resp.text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


PAYSLIP_EXTRACTION_PROMPT = """Tu es un assistant qui lit des fiches de paie françaises.

Analyse le document ci-joint et réponds UNIQUEMENT avec un objet JSON valide, \
sans texte autour, sans balises markdown, avec exactement ces clés :

{
  "employeur": "nom de l'employeur ou de l'entreprise",
  "mois": "mois et année de la fiche, ex: 'Juillet 2026'",
  "salaire_brut": nombre décimal du salaire brut, avec un point comme séparateur,
  "salaire_net": nombre décimal du salaire net à payer, avec un point comme séparateur,
  "net_imposable": nombre décimal du net imposable si visible, sinon null,
  "devise": "EUR",
  "date_fiche": "date au format AAAA-MM-JJ (1er du mois de paie), ou null",
  "notes": "une phrase courte si quelque chose d'inhabituel est visible (prime, augmentation, changement de poste...), sinon une chaîne vide"
}
"""


def extract_payslip_data(client, content_part) -> dict:
    resp = client.models.generate_content(
        model=MODEL,
        contents=[content_part, PAYSLIP_EXTRACTION_PROMPT],
    )
    text = resp.text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def check_payslip_anomaly(df: pd.DataFrame, salaire_net: float):
    """Compare le salaire net au mois precedent et signale les ecarts."""
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
        "is_anomaly": is_anomaly,
        "delta_abs": delta_abs,
        "delta_pct": delta_pct,
        "previous_net": last_net,
        "previous_mois": last.get("mois") or last.get("date_ajout"),
    }


def check_anomaly(df: pd.DataFrame, fournisseur: str, montant: float):
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
        "is_anomaly": is_anomaly,
        "delta_abs": delta_abs,
        "delta_pct": delta_pct,
        "previous_montant": last_montant,
        "previous_date": last.get("date_facture") or last.get("date_ajout"),
    }


TONE_INSTRUCTIONS = {
    "Poli et factuel": "un ton poli, factuel et professionnel",
    "Ferme": "un ton ferme mais courtois, qui montre que tu ne comptes pas laisser passer ça",
    "Négociation": "un ton orienté négociation, en demandant explicitement un geste commercial ou une renégociation du tarif",
}


def draft_email(client, entry: dict, anomaly: dict, tone: str) -> str:
    numero_contrat = entry.get("numero_contrat") or "non communiqué (à compléter par l'utilisateur)"
    prompt = f"""Rédige en français un email de réclamation destiné au service client de \
"{entry['fournisseur']}", avec {TONE_INSTRUCTIONS[tone]}.

Contexte :
- Type de contrat : {entry.get('type_contrat') or 'non précisé'}
- Numéro de contrat / client : {numero_contrat}
- Montant précédent constaté : {anomaly['previous_montant']:.2f} {entry.get('devise', 'EUR')} (le {anomaly['previous_date']})
- Nouveau montant constaté : {entry['montant']:.2f} {entry.get('devise', 'EUR')} (le {entry.get('date_facture') or 'ce mois-ci'})
- Hausse : {anomaly['delta_abs']:.2f} {entry.get('devise', 'EUR')} soit {anomaly['delta_pct']:.1f}%

Demande une explication claire sur cette hausse et, selon le ton demandé, une correction, \
un geste commercial ou une renégociation. Termine par une formule de politesse et signe \
"[Ton prénom et nom]". Ne mets aucun texte avant ou après l'email lui-même."""

    resp = client.models.generate_content(
        model=MODEL,
        contents=[prompt],
    )
    return resp.text.strip()


# --------------------------------------------------------------------------
# Récupération automatique via woob
# --------------------------------------------------------------------------

def fetch_bills_from_provider(provider_name: str, login: str, password: str):
    """Connecte-toi à un fournisseur via woob et récupère les factures."""
    if not _HAS_WOOB:
        return [], "woob n'est pas installé. Lance : pip install woob"

    prov = PROVIDERS.get(provider_name)
    if not prov:
        return [], f"Fournisseur inconnu : {provider_name}"

    try:
        w = Woob()
        backend_name = f"scribe_{prov['module']}"
        w.load_backend(prov["module"], backend_name,
                        params={"login": login, "password": password})

        bills = []
        for sub in w.iter_subscription():
            for doc in w.iter_documents(sub):
                bill_info = {
                    "id": doc.id,
                    "label": doc.label or "Facture",
                    "date": str(doc.date) if doc.date else None,
                    "total_price": float(doc.total_price) if hasattr(doc, "total_price") and doc.total_price else None,
                    "currency": str(doc.currency) if hasattr(doc, "currency") and doc.currency else "EUR",
                    "has_file": doc.has_file if hasattr(doc, "has_file") else False,
                    "provider": provider_name,
                    "backend": backend_name,
                }
                bills.append(bill_info)
        return bills, None
    except Exception as exc:
        return [], str(exc)


def download_bill(provider_name: str, login: str, password: str, doc_id: str):
    """Télécharge une facture spécifique et renvoie le contenu (bytes)."""
    if not _HAS_WOOB:
        return None, "woob n'est pas installé"

    prov = PROVIDERS.get(provider_name)
    if not prov:
        return None, f"Fournisseur inconnu : {provider_name}"

    try:
        w = Woob()
        backend_name = f"scribe_{prov['module']}"
        w.load_backend(prov["module"], backend_name,
                        params={"login": login, "password": password})
        data = w.download_document(doc_id)
        if data:
            return data, None
        return None, "Aucun fichier disponible pour ce document"
    except Exception as exc:
        return None, str(exc)


# --------------------------------------------------------------------------
# Banque — stockage et fonctions
# --------------------------------------------------------------------------

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


def fetch_bank_transactions(bank_name: str, login: str, password: str,
                            months_back: int = 3):
    """Récupère les transactions bancaires via woob CapBank."""
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
                    continue  # on ne garde que les débits (négatifs)
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


def detect_subscriptions(tx_df: pd.DataFrame) -> pd.DataFrame:
    """Détecte les abonnements mensuels (prélèvements réguliers ~30j, montant stable)."""
    if tx_df.empty:
        return pd.DataFrame()

    tx_df = tx_df.copy()
    tx_df["amount"] = pd.to_numeric(tx_df["amount"], errors="coerce")
    tx_df["date"] = pd.to_datetime(tx_df["date"], errors="coerce", dayfirst=True)
    tx_df = tx_df.dropna(subset=["amount", "date"])

    # Normaliser les labels pour regrouper
    tx_df["label_norm"] = tx_df["label"].str.upper().str.strip()

    # Mots-clés typiques de prélèvements mensuels
    _monthly_keywords = [
        "PRLV", "PRELEVEMENT", "ABONNEMENT", "ASSURANCE", "MUTUELLE",
        "EDF", "ENGIE", "GDF", "FREE", "ORANGE", "SFR", "BOUYGUES",
        "NETFLIX", "SPOTIFY", "DEEZER", "DISNEY", "AMAZON PRIME",
        "CANAL", "OVH", "ADOBE", "ICLOUD", "GOOGLE STORAGE",
        "ACM", "MAIF", "MACIF", "MATMUT", "AXA", "ALLIANZ", "GROUPAMA",
        "LOYER", "CPAM", "CAF", "IMPOT",
    ]

    per_label = tx_df.sort_values("date").groupby("label_norm")
    results = []

    for lbl, grp in per_label:
        if len(grp) < 2:
            continue

        # Vérifier la régularité (intervalle moyen entre 20 et 45 jours)
        dates_sorted = grp["date"].sort_values()
        intervals = dates_sorted.diff().dropna().dt.days
        if intervals.empty:
            continue
        avg_interval = intervals.mean()

        # Vérifier la constance du montant (écart-type < 30% de la moyenne)
        mean_amt = grp["amount"].mean()
        std_amt = grp["amount"].std()
        is_stable = std_amt <= mean_amt * 0.3 if mean_amt > 0 else True

        # Vérifier si c'est un mot-clé connu de prélèvement
        is_keyword = any(kw in lbl for kw in _monthly_keywords)

        # Critères: soit mot-clé reconnu + au moins 2 occurrences,
        # soit intervalle régulier (~mensuel) + montant stable
        is_monthly = (20 <= avg_interval <= 45) and is_stable
        if not (is_monthly or is_keyword):
            continue

        # Variation du dernier prélèvement
        prev = grp["amount"].iloc[-2]
        last = grp["amount"].iloc[-1]
        variation = round(((last - prev) / prev) * 100, 1) if prev > 0 else 0.0

        results.append({
            "label": grp["label"].iloc[0],
            "occurrences": len(grp),
            "montant_moyen": round(mean_amt, 2),
            "dernier_montant": round(last, 2),
            "derniere_date": grp["date"].max(),
            "premiere_date": grp["date"].min(),
            "variation_pct": variation,
            "intervalle_moyen": round(avg_interval, 0),
        })

    subs = pd.DataFrame(results)
    if not subs.empty:
        subs = subs.sort_values("dernier_montant", ascending=False)
    return subs


# --------------------------------------------------------------------------
# Interface — Top bar & Parametres
# --------------------------------------------------------------------------

_bar_left, _bar_right = st.columns([6, 2])
with _bar_left:
    st.html(f"""
    <div style="display:flex; align-items:center; gap:20px; padding:24px 0 8px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
        {LOGO_IMG_LG}
        <div>
            <h1 style="font-size:42px; font-weight:800; letter-spacing:-0.02em; margin:0; color:{_text1}; line-height:1.1;">Scribe</h1>
            <div style="font-size:15px; color:{_text3}; margin-top:4px;">Tes finances perso sous controle</div>
            <span style="display:inline-block; padding:5px 14px; border-radius:999px; background:{_th['accent_bg']}; color:{_th['accent_text']}; font-size:11px; font-weight:600; letter-spacing:.04em; text-transform:uppercase; margin-top:10px;">Analyse locale &middot; zero compte tiers &middot; tu valides tout</span>
        </div>
    </div>
    """)
with _bar_right:
    st.html("<div style='height:36px;'></div>")


if not _HAS_GENAI:
    st.error("Le paquet `google-genai` n'est pas installé. Lance `pip install -r requirements.txt`.")
    st.stop()

# Variable api_key initialisée ici, définie dans l'onglet Parametres
if "api_key_val" not in st.session_state:
    st.session_state.api_key_val = ""
api_key = st.session_state.api_key_val

# --------------------------------------------------------------------------
# Onglets principaux
# --------------------------------------------------------------------------

st.markdown(f"""<style>
  div[data-testid="stTabs"] [role="tabpanel"] {{
    animation: tabSlideIn 0.35s ease;
  }}
  @keyframes tabSlideIn {{
    0% {{ opacity: 0; transform: translateX(16px); }}
    100% {{ opacity: 1; transform: translateX(0); }}
  }}
  /* ---- masquer barre et highlight natifs ---- */
  div[data-testid="stTabs"] [data-baseweb="tab-border"],
  div[data-testid="stTabs"] [data-baseweb="tab-highlight"] {{
    display: none !important;
  }}
  /* ---- conteneur : transparent, juste un gap ---- */
  div[data-testid="stTabs"] [role="tablist"] {{
    background: transparent !important;
    border: none !important;
    padding: 0 !important;
    gap: 10px !important;
    justify-content: flex-start !important;
  }}
  /* ---- pousser le dernier onglet (Parametres) a droite ---- */
  div[data-testid="stTabs"] [role="tablist"] button[data-baseweb="tab"]:last-of-type {{
    margin-left: auto !important;
  }}
  /* ---- onglet par défaut : capsule outline ---- */
  div[data-testid="stTabs"] button[data-baseweb="tab"] {{
    font-size: 14px !important;
    font-weight: 600 !important;
    color: {_text3} !important;
    padding: 10px 24px !important;
    border-radius: 12px !important;
    background: {"#1a1a19" if _dark else "#ffffff"} !important;
    border: 1.5px solid {_border} !important;
    transition: all 0.25s cubic-bezier(.4,0,.2,1) !important;
    white-space: nowrap !important;
  }}
  div[data-testid="stTabs"] button[data-baseweb="tab"]:hover {{
    color: {_th["accent_text"]} !important;
    border-color: {_th["accent_mid"]}66 !important;
    background: {_th["accent_bg"]}44 !important;
  }}
  /* ---- onglet actif : rempli accent ---- */
  div[data-testid="stTabs"] button[aria-selected="true"] {{
    background: linear-gradient(135deg, {_th["accent_dark"]}, {_th["accent_mid"]}) !important;
    color: #ffffff !important;
    border-color: transparent !important;
    box-shadow: 0 4px 14px {_th["accent_dark"]}40 !important;
  }}
</style>""", unsafe_allow_html=True)

tab_factures, tab_paie, tab_params = st.tabs(["Factures & Abonnements", "Fiches de paie", "⚙ Parametres"])

with tab_factures:
    # --------------------------------------------------------------------------
    # Interface — Dashboard (stat tiles)
    # --------------------------------------------------------------------------

    history_df = load_history()

    if not history_df.empty:
        # ── Filtre temporel ──
        period_filter = st.radio(
            "Periode",
            ["Tout", "Ce mois", "Cette annee"],
            horizontal=True,
            key="stat_period",
            label_visibility="collapsed",
        )

        filtered_df = history_df.copy()
        filtered_df["_date"] = pd.to_datetime(
            filtered_df["date_facture"], errors="coerce"
        ).fillna(pd.to_datetime(filtered_df["date_ajout"], errors="coerce"))

        now = date.today()
        if period_filter == "Ce mois":
            filtered_df = filtered_df[
                (filtered_df["_date"].dt.month == now.month) &
                (filtered_df["_date"].dt.year == now.year)
            ]
        elif period_filter == "Cette annee":
            filtered_df = filtered_df[filtered_df["_date"].dt.year == now.year]

        total_factures = len(filtered_df)
        total_fournisseurs = filtered_df["fournisseur"].nunique()
        total_depense = pd.to_numeric(filtered_df["montant"], errors="coerce").sum()

        _period_label = {"Tout": "Total analyse", "Ce mois": "Ce mois-ci", "Cette annee": "Cette annee"}
        _stat_box = f"flex:1; background:{_card}; border:1px solid {_border}; border-radius:14px; padding:20px 24px; text-align:center;"
        _stat_val = f"font-size:32px; font-weight:800; color:{_th['accent_text']}; line-height:1.2;"
        _stat_lbl = f"font-size:12px; font-weight:600; color:{_text3}; text-transform:uppercase; letter-spacing:.04em; margin-top:6px;"
        st.html(f"""
        <div style="display:flex; gap:16px; margin-bottom:24px; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
            <div style="{_stat_box}">
                <div style="{_stat_val}">{total_factures}</div>
                <div style="{_stat_lbl}">Factures analysees</div>
            </div>
            <div style="{_stat_box}">
                <div style="{_stat_val}">{total_fournisseurs}</div>
                <div style="{_stat_lbl}">Fournisseurs suivis</div>
            </div>
            <div style="{_stat_box}">
                <div style="{_stat_val}">{total_depense:,.2f} EUR</div>
                <div style="{_stat_lbl}">{_period_label[period_filter]}</div>
            </div>
        </div>
        """)

        # ── Jauge budget ──
        if st.session_state.monthly_budget > 0 and period_filter == "Ce mois":
            _budget = st.session_state.monthly_budget
            _spent = total_depense
            _pct = min((_spent / _budget) * 100, 100) if _budget > 0 else 0
            _over = _spent > _budget
            _bar_color = "#ef4444" if _over else _th["accent_mid"]
            _status_text = f"Depassement de {_spent - _budget:.2f} EUR" if _over else f"Reste {_budget - _spent:.2f} EUR"
            _status_icon = "⚠️" if _over else "✓"
            st.html(f"""
            <div style="background:{_card}; border:1px solid {_border}; border-radius:14px; padding:20px 24px; margin-bottom:24px; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                    <span style="font-weight:700; color:{_text1}; font-size:14px;">Budget mensuel</span>
                    <span style="font-size:13px; color:{'#ef4444' if _over else _th['accent_text']}; font-weight:600;">{_status_icon} {_status_text}</span>
                </div>
                <div style="background:{_input_bg}; border-radius:8px; height:12px; overflow:hidden;">
                    <div style="width:{_pct}%; height:100%; background:linear-gradient(90deg, {_th['accent_dark']}, {_bar_color}); border-radius:8px; transition:width 0.5s ease;"></div>
                </div>
                <div style="display:flex; justify-content:space-between; margin-top:6px;">
                    <span style="font-size:11px; color:{_text3};">{_spent:.2f} EUR depenses</span>
                    <span style="font-size:11px; color:{_text3};">{_budget:.0f} EUR objectif</span>
                </div>
            </div>
            """)

        # ── Camembert repartition par fournisseur ──
        if total_fournisseurs >= 2:
            _sec = "display:flex; align-items:center; gap:10px; margin:32px 0 16px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
            _ico = f"width:36px; height:36px; border-radius:10px; background:{_th['accent_bg']}; display:flex; align-items:center; justify-content:center;"
            _pie_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 2a10 10 0 0 1 10 10h-10z"/></svg>'
            st.html(f"""
            <div style="{_sec}">
                <div style="{_ico}">{svg_img(_pie_svg)}</div>
                <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Repartition des depenses</h2>
            </div>
            """)
            _pie_data = filtered_df.groupby("fournisseur")["montant"].apply(
                lambda x: pd.to_numeric(x, errors="coerce").sum()
            ).sort_values(ascending=False)
            _pie_data = _pie_data[_pie_data > 0]
            if len(_pie_data) >= 2:
                _accent_colors = [_th["accent_dark"], _th["accent_mid"], _th["accent_light"], _th["accent_text"], _th["accent_pale"]]
                _pie_colors = (_accent_colors * ((len(_pie_data) // len(_accent_colors)) + 1))[:len(_pie_data)]
                fig, ax = plt.subplots(figsize=(5, 3.5))
                fig.patch.set_alpha(0)
                ax.set_facecolor("none")
                wedges, texts, autotexts = ax.pie(
                    _pie_data.values, labels=_pie_data.index,
                    autopct="%1.0f%%", startangle=90,
                    colors=_pie_colors, pctdistance=0.8,
                    wedgeprops={"linewidth": 2, "edgecolor": _card},
                )
                for t in texts:
                    t.set_color(_text2)
                    t.set_fontsize(10)
                for t in autotexts:
                    t.set_color("#ffffff")
                    t.set_fontsize(9)
                    t.set_fontweight("bold")
                ax.axis("equal")
                st.pyplot(fig, use_container_width=False)
                plt.close(fig)

        # ── Comparaison mois vs mois precedent ──
        _hist_dates = pd.to_datetime(history_df["date_ajout"], errors="coerce")
        _now_cmp = pd.Timestamp.now()
        _this_month = history_df[(_hist_dates.dt.month == _now_cmp.month) & (_hist_dates.dt.year == _now_cmp.year)]
        _prev_month_dt = _now_cmp - pd.DateOffset(months=1)
        _prev_month = history_df[(_hist_dates.dt.month == _prev_month_dt.month) & (_hist_dates.dt.year == _prev_month_dt.year)]
        if not _this_month.empty and not _prev_month.empty:
            _sec = "display:flex; align-items:center; gap:10px; margin:32px 0 16px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
            _ico = f"width:36px; height:36px; border-radius:10px; background:{_th['accent_bg']}; display:flex; align-items:center; justify-content:center;"
            _cmp_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="18" rx="1"/><rect x="14" y="9" width="7" height="12" rx="1"/></svg>'
            st.html(f"""
            <div style="{_sec}">
                <div style="{_ico}">{svg_img(_cmp_svg)}</div>
                <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Comparaison mensuelle</h2>
            </div>
            """)
            _this_total = pd.to_numeric(_this_month["montant"], errors="coerce").sum()
            _prev_total = pd.to_numeric(_prev_month["montant"], errors="coerce").sum()
            _diff = _this_total - _prev_total
            _diff_pct = ((_diff / _prev_total) * 100) if _prev_total > 0 else 0
            _diff_color = "#ef4444" if _diff > 0 else "#22c55e"
            _diff_icon = "↗" if _diff > 0 else "↘"
            _prev_label = _prev_month_dt.strftime("%B %Y").capitalize()
            _this_label = _now_cmp.strftime("%B %Y").capitalize()
            st.html(f"""
            <div style="display:flex; gap:16px; margin-bottom:24px; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
                <div style="flex:1; background:{_card}; border:1px solid {_border}; border-radius:14px; padding:20px 24px; text-align:center;">
                    <div style="font-size:11px; font-weight:600; color:{_text3}; text-transform:uppercase; letter-spacing:.04em;">{_prev_label}</div>
                    <div style="font-size:28px; font-weight:800; color:{_text2}; margin-top:6px;">{_prev_total:,.2f} EUR</div>
                </div>
                <div style="display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                    <div style="background:{_diff_color}22; color:{_diff_color}; font-weight:700; font-size:14px; padding:8px 14px; border-radius:10px;">{_diff_icon} {_diff_pct:+.1f}%</div>
                </div>
                <div style="flex:1; background:{_card}; border:1px solid {_border}; border-radius:14px; padding:20px 24px; text-align:center;">
                    <div style="font-size:11px; font-weight:600; color:{_text3}; text-transform:uppercase; letter-spacing:.04em;">{_this_label}</div>
                    <div style="font-size:28px; font-weight:800; color:{_th['accent_text']}; margin-top:6px;">{_this_total:,.2f} EUR</div>
                </div>
            </div>
            """)

        # Mini graphique d'évolution par fournisseur
        if total_factures >= 2:
            _sec = "display:flex; align-items:center; gap:10px; margin:32px 0 16px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
            _ico = f"width:36px; height:36px; border-radius:10px; background:{_th['accent_bg']}; display:flex; align-items:center; justify-content:center;"
            _chart_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M3 20h18M5 16l4-4 4 4 6-8"/></svg>'
            st.html(f"""
            <div style="{_sec}">
                <div style="{_ico}">
                    {svg_img(_chart_svg)}
                </div>
                <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Evolution</h2>
            </div>
            """)

            chart_df = history_df[["date_ajout", "fournisseur", "montant"]].copy()
            chart_df["montant"] = pd.to_numeric(chart_df["montant"], errors="coerce")
            chart_df = chart_df.dropna(subset=["montant"])

            if len(chart_df) >= 2:
                import altair as alt
                chart = alt.Chart(chart_df).mark_line(
                    strokeWidth=2.5,
                    point=alt.OverlayMarkDef(filled=True, size=60),
                ).encode(
                    x=alt.X("date_ajout:T", title=None, axis=alt.Axis(
                        labelColor=_text3, tickColor=_grid, domainColor=_grid,
                        grid=False, labelFontSize=11,
                    )),
                    y=alt.Y("montant:Q", title="Montant (EUR)", axis=alt.Axis(
                        labelColor=_text3, tickColor=_grid, domainColor=_grid,
                        gridColor=_grid, labelFontSize=11, titleColor=_text3,
                    )),
                    color=alt.Color("fournisseur:N", legend=alt.Legend(
                        title=None, labelColor=_text2, orient="top",
                    ), scale=alt.Scale(range=[
                        "#3987e5", "#008300", "#d55181", "#c98500",
                        "#199e70", "#d95926", "#9085e9", "#e66767",
                    ])),
                    tooltip=["fournisseur", "montant", "date_ajout"],
                ).properties(
                    height=260,
                ).configure(
                    background=_card,
                ).configure_view(
                    stroke=None,
                )
                st.altair_chart(chart, use_container_width=True)

    # --------------------------------------------------------------------------
    # Interface — Suivi bancaire
    # --------------------------------------------------------------------------

    # ── Suivi bancaire (toujours visible) ──
    _sec = "display:flex; align-items:center; gap:10px; margin:32px 0 16px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
    _ico = f"width:36px; height:36px; border-radius:10px; background:{_th['accent_bg']}; display:flex; align-items:center; justify-content:center;"
    _bank_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M3 21h18M3 10h18M5 6l7-3 7 3M4 10v11M20 10v11M8 14v3M12 14v3M16 14v3"/></svg>'
    st.html(f"""
    <div style="{_sec}">
        <div style="{_ico}">
            {svg_img(_bank_svg)}
        </div>
        <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Suivi bancaire</h2>
    </div>
    """)

    if "bank_tx" not in st.session_state:
        st.session_state.bank_tx = load_bank_transactions()

    # ── Import CSV bancaire ──
    st.caption("Importe le CSV de tes transactions depuis l'espace client de ta banque.")
    bank_csv = st.file_uploader(
        "Fichier CSV bancaire",
        type=["csv", "tsv", "txt"],
        label_visibility="collapsed",
        key="bank_csv_uploader",
    )
    if bank_csv is not None:
        if st.button("Importer les transactions", type="primary", key="import_csv_btn"):
            try:
                raw = bank_csv.getvalue()
                # Detecter l'encodage
                text = None
                for enc in ["utf-8", "utf-8-sig", "latin-1", "cp1252"]:
                    try:
                        text = raw.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
                if text is None:
                    st.error("Impossible de lire le fichier — encodage non reconnu.")
                else:
                    # Detecter le separateur
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
                        st.error("Fichier CSV vide ou format non reconnu.")
                    else:
                        # Auto-detection des colonnes
                        _col_lower = {c: c.lower().strip() for c in csv_df.columns}
                        date_col = label_col = amount_col = debit_col = credit_col = None
                        for orig, low in _col_lower.items():
                            if low in ("date", "dateop", "date_op", "date operation", "date comptable", "date de comptabilisation", "date valeur"):
                                date_col = date_col or orig
                            elif low in ("libelle", "label", "libellé", "description", "intitulé", "libelle simplifie", "libellé simplifié", "libelle operation"):
                                label_col = label_col or orig
                            elif low in ("montant", "amount", "valeur", "montant(euros)"):
                                amount_col = amount_col or orig
                            elif low in ("debit", "débit"):
                                debit_col = debit_col or orig
                            elif low in ("credit", "crédit"):
                                credit_col = credit_col or orig

                        if not date_col:
                            # Fallback: premiere colonne qui ressemble a des dates
                            for orig in csv_df.columns:
                                sample = csv_df[orig].dropna().head(5)
                                if sample.str.match(r"^\d{2}[/\-\.]\d{2}[/\-\.]\d{2,4}$").any():
                                    date_col = orig
                                    break

                        if not label_col:
                            # Fallback: premiere colonne texte (non-numerique)
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
                            st.error("Colonnes 'date' et 'libelle' introuvables. Verifie le format du CSV.")
                        else:
                            # Construire le montant
                            if amount_col:
                                csv_df["_amount"] = csv_df[amount_col].str.replace(",", ".").str.replace(" ", "").str.replace("\xa0", "")
                                csv_df["_amount"] = pd.to_numeric(csv_df["_amount"], errors="coerce")
                            elif debit_col and credit_col:
                                _deb = csv_df[debit_col].str.replace(",", ".").str.replace(" ", "").str.replace("\xa0", "")
                                _cre = csv_df[credit_col].str.replace(",", ".").str.replace(" ", "").str.replace("\xa0", "")
                                _deb = pd.to_numeric(_deb, errors="coerce").fillna(0)
                                _cre = pd.to_numeric(_cre, errors="coerce").fillna(0)
                                csv_df["_amount"] = _cre - _deb
                            elif debit_col:
                                csv_df["_amount"] = csv_df[debit_col].str.replace(",", ".").str.replace(" ", "").str.replace("\xa0", "")
                                csv_df["_amount"] = -pd.to_numeric(csv_df["_amount"], errors="coerce").abs()
                            else:
                                # Fallback: premiere colonne numerique
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
                                st.error("Colonne 'montant' introuvable dans le CSV.")
                            else:
                                # Ne garder que les debits (montants negatifs)
                                csv_df["_amount"] = csv_df["_amount"].fillna(0)
                                debits = csv_df[csv_df["_amount"] < 0].copy()

                                if debits.empty:
                                    # Si tous les montants sont positifs, les considerer comme des debits
                                    debits = csv_df[csv_df["_amount"] != 0].copy()
                                    debits["_amount"] = debits["_amount"].abs()
                                else:
                                    debits["_amount"] = debits["_amount"].abs()

                                new_txs = pd.DataFrame({
                                    "date": debits[date_col].values,
                                    "label": debits[label_col].str.strip().values,
                                    "amount": debits["_amount"].round(2).values,
                                    "category": None,
                                    "bank_name": bank_csv.name.replace(".csv", "").replace(".tsv", "").replace(".txt", ""),
                                    "account_label": "",
                                    "date_import": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                })

                                existing = load_bank_transactions()
                                if not existing.empty:
                                    combined = pd.concat([existing, new_txs], ignore_index=True)
                                    combined = combined.drop_duplicates(
                                        subset=["date", "label", "amount"], keep="last"
                                    )
                                else:
                                    combined = new_txs
                                save_bank_transactions(combined)
                                st.session_state.bank_tx = combined
                                st.success(f"{len(new_txs)} transaction(s) importee(s) depuis {bank_csv.name} !")
                                st.rerun()
            except Exception as exc:
                st.error(f"Erreur lors de l'import : {exc}")

    # ── Connexion automatique (woob) ──
    bk = st.session_state.get("connected_bank")
    if bk:
        st.caption(f"🏦 Connecte a : **{bk['name']}**")
        col_fetch, col_months = st.columns([2, 1])
        months_back = col_months.selectbox("Historique", [1, 2, 3, 6, 12],
                                            index=2, format_func=lambda x: f"{x} mois",
                                            key="bank_months")
        if col_fetch.button("Recuperer mes transactions", type="primary", key="fetch_bank_btn"):
            with st.spinner(f"Connexion a {bk['name']}..."):
                txs, error = fetch_bank_transactions(
                    bk["name"], bk["login"], bk["password"], months_back
                )
                if error:
                    st.error(f"Erreur : {error}")
                else:
                    new_df = pd.DataFrame(txs)
                    existing = load_bank_transactions()
                    if not existing.empty and not new_df.empty:
                        combined = pd.concat([existing, new_df], ignore_index=True)
                        combined = combined.drop_duplicates(
                            subset=["date", "label", "amount"], keep="last"
                        )
                    else:
                        combined = new_df if not new_df.empty else existing
                    save_bank_transactions(combined)
                    st.session_state.bank_tx = combined
                    st.success(f"{len(txs)} transaction(s) recuperee(s) !")
                    st.rerun()

    # Afficher les données bancaires
    tx_df = st.session_state.bank_tx
    if not tx_df.empty and len(tx_df) >= 2:
        import altair as alt

        # Préparer les données catégorisées
        _tx_all = categorize_all_transactions(tx_df)
        _tx_all["amount"] = pd.to_numeric(_tx_all["amount"], errors="coerce")
        _tx_all["date"] = pd.to_datetime(_tx_all["date"], errors="coerce", dayfirst=True)
        _tx_all = _tx_all.dropna(subset=["amount", "date"])

        # ════════════════════════════════════════════════════════════════
        # 1. REVENUS vs DÉPENSES
        # ════════════════════════════════════════════════════════════════
        _sec = "display:flex; align-items:center; gap:10px; margin:24px 0 16px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
        _ico = f"width:36px; height:36px; border-radius:10px; background:{_th['accent_bg']}; display:flex; align-items:center; justify-content:center;"
        _bal_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>'
        st.html(f"""
        <div style="{_sec}">
            <div style="{_ico}">{svg_img(_bal_svg)}</div>
            <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Revenus vs Depenses</h2>
        </div>
        """)

        # Calculer revenus (montants positifs dans le CSV original) et dépenses
        _tx_full = tx_df.copy()
        _tx_full["amount"] = pd.to_numeric(_tx_full["amount"], errors="coerce")
        _tx_full["date"] = pd.to_datetime(_tx_full["date"], errors="coerce", dayfirst=True)
        _tx_full = _tx_full.dropna(subset=["amount", "date"])

        _now_bk = pd.Timestamp.now()
        _tx_this_month = _tx_full[
            (_tx_full["date"].dt.month == _now_bk.month) &
            (_tx_full["date"].dt.year == _now_bk.year)
        ]

        _total_depenses = _tx_all["amount"].sum()
        _month_depenses = _tx_this_month["amount"].sum() if not _tx_this_month.empty else 0

        # Revenus = fiches de paie si disponibles
        _payslips_rev = load_payslips()
        _revenu_mensuel = 0.0
        if not _payslips_rev.empty:
            _payslips_rev["salaire_net"] = pd.to_numeric(_payslips_rev["salaire_net"], errors="coerce")
            _last_pay = _payslips_rev.dropna(subset=["salaire_net"]).sort_values("date_ajout")
            if not _last_pay.empty:
                _revenu_mensuel = _last_pay.iloc[-1]["salaire_net"]

        _solde = _revenu_mensuel - _month_depenses if _revenu_mensuel > 0 else 0
        _solde_color = "#22c55e" if _solde >= 0 else "#ef4444"
        _solde_icon = "+" if _solde >= 0 else ""

        _stat_box = f"flex:1; background:{_card}; border:1px solid {_border}; border-radius:14px; padding:20px 24px; text-align:center;"
        _stat_val = f"font-size:28px; font-weight:800; line-height:1.2;"
        _stat_lbl = f"font-size:12px; font-weight:600; color:{_text3}; text-transform:uppercase; letter-spacing:.04em; margin-top:6px;"

        st.html(f"""
        <div style="display:flex; gap:16px; margin-bottom:24px; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
            <div style="{_stat_box}">
                <div style="{_stat_val} color:#22c55e;">{_revenu_mensuel:,.2f} EUR</div>
                <div style="{_stat_lbl}">Revenu mensuel</div>
            </div>
            <div style="{_stat_box}">
                <div style="{_stat_val} color:#ef4444;">{_month_depenses:,.2f} EUR</div>
                <div style="{_stat_lbl}">Depenses ce mois</div>
            </div>
            <div style="{_stat_box}">
                <div style="{_stat_val} color:{_solde_color};">{_solde_icon}{_solde:,.2f} EUR</div>
                <div style="{_stat_lbl}">Solde restant</div>
            </div>
        </div>
        """)

        if _revenu_mensuel > 0 and _month_depenses > 0:
            _pct_spent = min((_month_depenses / _revenu_mensuel) * 100, 100)
            _bar_col = "#ef4444" if _pct_spent > 90 else ("#f59e0b" if _pct_spent > 70 else "#22c55e")
            st.html(f"""
            <div style="background:{_card}; border:1px solid {_border}; border-radius:14px; padding:16px 24px; margin-bottom:24px; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
                <div style="display:flex; justify-content:space-between; margin-bottom:8px;">
                    <span style="font-size:13px; font-weight:600; color:{_text1};">Consommation du revenu</span>
                    <span style="font-size:13px; font-weight:700; color:{_bar_col};">{_pct_spent:.0f}%</span>
                </div>
                <div style="background:{_input_bg}; border-radius:8px; height:10px; overflow:hidden;">
                    <div style="width:{_pct_spent}%; height:100%; background:{_bar_col}; border-radius:8px; transition:width 0.5s;"></div>
                </div>
            </div>
            """)

        # ════════════════════════════════════════════════════════════════
        # 2. DÉPENSES PAR CATÉGORIE
        # ════════════════════════════════════════════════════════════════
        _sec = "display:flex; align-items:center; gap:10px; margin:24px 0 16px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
        _cat_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 2a10 10 0 0 1 10 10h-10z"/></svg>'
        st.html(f"""
        <div style="{_sec}">
            <div style="{_ico}">{svg_img(_cat_svg)}</div>
            <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Depenses par categorie</h2>
        </div>
        """)

        _cat_totals = _tx_all.groupby("auto_category")["amount"].sum().sort_values(ascending=False)
        _cat_totals = _cat_totals[_cat_totals > 0]

        if not _cat_totals.empty:
            _cat_colors = [TRANSACTION_CATEGORIES.get(c, {}).get("color", "#6b7280") for c in _cat_totals.index]
            _cat_icons = [TRANSACTION_CATEGORIES.get(c, {}).get("icon", "📦") for c in _cat_totals.index]
            _grand_total = _cat_totals.sum()

            # Barres horizontales par catégorie
            _cat_html = ""
            for idx, (cat, amount) in enumerate(_cat_totals.items()):
                pct = (amount / _grand_total) * 100 if _grand_total > 0 else 0
                color = TRANSACTION_CATEGORIES.get(cat, {}).get("color", "#6b7280")
                icon = TRANSACTION_CATEGORIES.get(cat, {}).get("icon", "📦")
                _cat_html += f"""
                <div style="margin-bottom:12px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                        <span style="font-size:14px; font-weight:600; color:{_text1};">{icon} {cat}</span>
                        <span style="font-size:14px; font-weight:700; color:{color};">{amount:.2f} EUR <span style="font-size:11px; color:{_text3};">({pct:.0f}%)</span></span>
                    </div>
                    <div style="background:{_input_bg}; border-radius:6px; height:8px; overflow:hidden;">
                        <div style="width:{pct}%; height:100%; background:{color}; border-radius:6px;"></div>
                    </div>
                </div>
                """
            st.html(f"""
            <div style="background:{_card}; border:1px solid {_border}; border-radius:14px; padding:20px 24px; margin-bottom:24px; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
                {_cat_html}
            </div>
            """)

            # Donut chart par catégorie
            _cat_df = pd.DataFrame({"Categorie": _cat_totals.index, "Montant": _cat_totals.values})
            _donut = alt.Chart(_cat_df).mark_arc(
                innerRadius=50, outerRadius=100, stroke=_card, strokeWidth=2,
            ).encode(
                theta=alt.Theta("Montant:Q"),
                color=alt.Color("Categorie:N",
                    scale=alt.Scale(domain=list(_cat_totals.index), range=_cat_colors),
                    legend=alt.Legend(title=None, orient="bottom", labelFontSize=12, columns=3),
                ),
                tooltip=[alt.Tooltip("Categorie:N"), alt.Tooltip("Montant:Q", format=".2f", title="EUR")],
            ).properties(height=300).configure_view(strokeWidth=0)
            st.altair_chart(_donut, use_container_width=True)

        # ════════════════════════════════════════════════════════════════
        # 3. BUDGET PAR CATÉGORIE
        # ════════════════════════════════════════════════════════════════
        _sec = "display:flex; align-items:center; gap:10px; margin:24px 0 16px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
        _budget_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="18" rx="2"/><path d="M2 9h20"/><path d="M9 16h6"/></svg>'
        st.html(f"""
        <div style="{_sec}">
            <div style="{_ico}">{svg_img(_budget_svg)}</div>
            <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Budget par categorie</h2>
        </div>
        """)

        _cat_budgets = load_category_budgets()

        with st.expander("Definir les budgets par categorie"):
            _budget_cols = st.columns(3)
            _new_budgets = dict(_cat_budgets)
            for idx, cat_name in enumerate([c for c in TRANSACTION_CATEGORIES.keys() if c != "Autre"]):
                with _budget_cols[idx % 3]:
                    icon = TRANSACTION_CATEGORIES[cat_name]["icon"]
                    val = st.number_input(
                        f"{icon} {cat_name}",
                        min_value=0.0, step=10.0,
                        value=float(_cat_budgets.get(cat_name, 0)),
                        key=f"budget_cat_{cat_name}",
                    )
                    _new_budgets[cat_name] = val
            if st.button("Sauvegarder les budgets", key="save_cat_budgets"):
                save_category_budgets(_new_budgets)
                st.success("Budgets sauvegardes !")
                st.rerun()

        # Barres de progression par catégorie (ce mois)
        _tx_month_cat = _tx_all[
            (_tx_all["date"].dt.month == _now_bk.month) &
            (_tx_all["date"].dt.year == _now_bk.year)
        ]
        _month_cat_totals = _tx_month_cat.groupby("auto_category")["amount"].sum()

        _has_any_budget = any(v > 0 for v in _cat_budgets.values())
        if _has_any_budget:
            _budget_html = ""
            _alerts = []
            for cat_name, budget_limit in _cat_budgets.items():
                if budget_limit <= 0:
                    continue
                spent = _month_cat_totals.get(cat_name, 0)
                pct = min((spent / budget_limit) * 100, 100) if budget_limit > 0 else 0
                icon = TRANSACTION_CATEGORIES.get(cat_name, {}).get("icon", "📦")
                color = TRANSACTION_CATEGORIES.get(cat_name, {}).get("color", "#6b7280")
                bar_color = "#ef4444" if pct >= 90 else ("#f59e0b" if pct >= 70 else color)
                status = f"Reste {budget_limit - spent:.0f} EUR" if spent <= budget_limit else f"Depasse de {spent - budget_limit:.0f} EUR"
                status_color = "#ef4444" if spent > budget_limit else _text3

                if spent > budget_limit:
                    _alerts.append(f"{icon} {cat_name} : depasse de {spent - budget_limit:.0f} EUR !")

                _budget_html += f"""
                <div style="margin-bottom:14px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                        <span style="font-size:13px; font-weight:600; color:{_text1};">{icon} {cat_name}</span>
                        <span style="font-size:12px; color:{status_color}; font-weight:600;">{status}</span>
                    </div>
                    <div style="background:{_input_bg}; border-radius:6px; height:8px; overflow:hidden;">
                        <div style="width:{pct}%; height:100%; background:{bar_color}; border-radius:6px;"></div>
                    </div>
                    <div style="display:flex; justify-content:space-between; margin-top:3px;">
                        <span style="font-size:11px; color:{_text3};">{spent:.0f} EUR</span>
                        <span style="font-size:11px; color:{_text3};">{budget_limit:.0f} EUR</span>
                    </div>
                </div>
                """
            st.html(f"""
            <div style="background:{_card}; border:1px solid {_border}; border-radius:14px; padding:20px 24px; margin-bottom:16px; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
                <div style="font-size:11px; font-weight:600; color:{_text3}; text-transform:uppercase; letter-spacing:.04em; margin-bottom:12px;">Ce mois-ci</div>
                {_budget_html}
            </div>
            """)
            for alert in _alerts:
                st.warning(alert)

        # ════════════════════════════════════════════════════════════════
        # 4. TENDANCES MULTI-MOIS
        # ════════════════════════════════════════════════════════════════
        _sec = "display:flex; align-items:center; gap:10px; margin:24px 0 16px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
        _trend_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M3 20h18M5 16l4-4 4 4 6-8"/></svg>'
        st.html(f"""
        <div style="{_sec}">
            <div style="{_ico}">{svg_img(_trend_svg)}</div>
            <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Tendances mensuelles</h2>
        </div>
        """)

        _tx_trend = _tx_all.copy()
        _tx_trend["mois"] = _tx_trend["date"].dt.to_period("M").astype(str)
        _trend_by_cat = _tx_trend.groupby(["mois", "auto_category"])["amount"].sum().reset_index()
        _trend_by_cat.columns = ["Mois", "Categorie", "Montant"]

        if len(_trend_by_cat["Mois"].unique()) >= 2:
            _all_cat_colors = [TRANSACTION_CATEGORIES.get(c, {}).get("color", "#6b7280")
                               for c in _trend_by_cat["Categorie"].unique()]
            _trend_chart = alt.Chart(_trend_by_cat).mark_bar(
                cornerRadiusTopLeft=4, cornerRadiusTopRight=4,
            ).encode(
                x=alt.X("Mois:N", title=None, axis=alt.Axis(labelAngle=-45)),
                y=alt.Y("Montant:Q", title="Depenses (EUR)", stack="zero"),
                color=alt.Color("Categorie:N",
                    scale=alt.Scale(
                        domain=list(_trend_by_cat["Categorie"].unique()),
                        range=_all_cat_colors,
                    ),
                    legend=alt.Legend(title=None, orient="bottom", columns=3, labelFontSize=11),
                ),
                tooltip=["Mois", "Categorie", alt.Tooltip("Montant:Q", format=".2f")],
            ).properties(height=300).configure_view(strokeWidth=0).configure(background=_card)
            st.altair_chart(_trend_chart, use_container_width=True)
        else:
            # Total par mois simple
            _trend_total = _tx_trend.groupby("mois")["amount"].sum().reset_index()
            _trend_total.columns = ["Mois", "Total"]
            if not _trend_total.empty:
                _bar_simple = alt.Chart(_trend_total).mark_bar(
                    cornerRadiusTopLeft=6, cornerRadiusTopRight=6, color=_th["accent_text"],
                ).encode(
                    x=alt.X("Mois:N", title=None),
                    y=alt.Y("Total:Q", title="Total (EUR)"),
                    tooltip=["Mois", alt.Tooltip("Total:Q", format=".2f")],
                ).properties(height=250)
                st.altair_chart(_bar_simple, use_container_width=True)

        # ════════════════════════════════════════════════════════════════
        # 5. ABONNEMENTS MENSUELS (existant, réorganisé)
        # ════════════════════════════════════════════════════════════════
        subs = detect_subscriptions(tx_df)
        if not subs.empty:
            _sec = "display:flex; align-items:center; gap:10px; margin:24px 0 16px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
            _sub_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8v4l3 3"/><circle cx="12" cy="12" r="10"/></svg>'
            st.html(f"""
            <div style="{_sec}">
                <div style="{_ico}">{svg_img(_sub_svg)}</div>
                <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Abonnements mensuels</h2>
            </div>
            """)

            _total_subs = subs["dernier_montant"].sum()
            st.html(f"""
            <div style="background:{_card}; border:1px solid {_border}; border-radius:14px; padding:16px 24px; margin-bottom:16px; font-family:system-ui,-apple-system,'Segoe UI',sans-serif; text-align:center;">
                <span style="font-size:13px; color:{_text3};">Total abonnements mensuels : </span>
                <span style="font-size:22px; font-weight:800; color:{_th['accent_text']};">{_total_subs:.2f} EUR/mois</span>
            </div>
            """)

            for _, row in subs.iterrows():
                _sub_box = f"background:{_card}; border:1px solid {_border}; border-radius:12px; padding:14px 18px; margin-bottom:8px; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
                variation = row["variation_pct"]
                if variation > 5:
                    _badge = f'<span style="display:inline-block; padding:2px 8px; border-radius:999px; background:rgba(213,81,129,0.15); color:#d55181; font-size:11px; font-weight:600; margin-left:8px;">+{variation:.1f}%</span>'
                elif variation < -2:
                    _badge = f'<span style="display:inline-block; padding:2px 8px; border-radius:999px; background:rgba(12,163,12,0.15); color:#0ca30c; font-size:11px; font-weight:600; margin-left:8px;">{variation:.1f}%</span>'
                else:
                    _badge = ""

                st.html(f"""
                <div style="{_sub_box}">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <div>
                            <span style="font-size:15px; font-weight:600; color:{_text1};">{row['label']}</span>
                            {_badge}
                            <div style="font-size:12px; color:{_text3}; margin-top:4px;">{int(row['occurrences'])} prelevement(s)</div>
                        </div>
                        <div style="text-align:right;">
                            <div style="font-size:20px; font-weight:700; color:{_th['accent_text']};">{row['dernier_montant']:.2f} EUR</div>
                            <div style="font-size:11px; color:{_text3};">moy. {row['montant_moyen']:.2f} EUR</div>
                        </div>
                    </div>
                </div>
                """)

        # ════════════════════════════════════════════════════════════════
        # 6. RECHERCHE ET FILTRE DES TRANSACTIONS
        # ════════════════════════════════════════════════════════════════
        _sec = "display:flex; align-items:center; gap:10px; margin:24px 0 16px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
        _search_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>'
        st.html(f"""
        <div style="{_sec}">
            <div style="{_ico}">{svg_img(_search_svg)}</div>
            <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Toutes les transactions</h2>
        </div>
        """)

        _search_col, _cat_col, _month_col = st.columns([3, 2, 2])
        with _search_col:
            _search_q = st.text_input("Rechercher", placeholder="Ex: Carrefour, Netflix...", key="tx_search", label_visibility="collapsed")
        with _cat_col:
            _cat_options = ["Toutes"] + sorted([c for c in _tx_all["auto_category"].unique()])
            _cat_filter = st.selectbox("Categorie", _cat_options, key="tx_cat_filter", label_visibility="collapsed")
        with _month_col:
            _month_options = ["Tous les mois"] + sorted(_tx_all["date"].dt.to_period("M").astype(str).unique().tolist(), reverse=True)
            _month_filter = st.selectbox("Mois", _month_options, key="tx_month_filter", label_visibility="collapsed")

        _filtered_tx = _tx_all.copy()
        if _search_q:
            _filtered_tx = _filtered_tx[_filtered_tx["label"].str.contains(_search_q, case=False, na=False)]
        if _cat_filter != "Toutes":
            _filtered_tx = _filtered_tx[_filtered_tx["auto_category"] == _cat_filter]
        if _month_filter != "Tous les mois":
            _filtered_tx = _filtered_tx[_filtered_tx["date"].dt.to_period("M").astype(str) == _month_filter]

        _filtered_tx = _filtered_tx.sort_values("date", ascending=False)
        _display_tx = _filtered_tx[["date", "label", "amount", "auto_category"]].copy()
        _display_tx.columns = ["Date", "Libelle", "Montant (EUR)", "Categorie"]
        _display_tx["Date"] = _display_tx["Date"].dt.strftime("%d/%m/%Y")
        st.dataframe(_display_tx, use_container_width=True, hide_index=True, height=400)
        st.caption(f"{len(_display_tx)} transaction(s) affichee(s)")

        # ════════════════════════════════════════════════════════════════
        # 7. OBJECTIFS D'ÉPARGNE
        # ════════════════════════════════════════════════════════════════
        _sec = "display:flex; align-items:center; gap:10px; margin:24px 0 16px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
        _save_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>'
        st.html(f"""
        <div style="{_sec}">
            <div style="{_ico}">{svg_img(_save_svg)}</div>
            <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Objectifs d'epargne</h2>
        </div>
        """)

        if "savings_goals" not in st.session_state:
            st.session_state.savings_goals = load_savings_goals()

        with st.expander("Ajouter un objectif"):
            _goal_name = st.text_input("Nom de l'objectif", placeholder="Ex: Vacances, Voiture...", key="goal_name")
            _goal_c1, _goal_c2 = st.columns(2)
            with _goal_c1:
                _goal_target = st.number_input("Montant cible (EUR)", min_value=0.0, step=50.0, key="goal_target")
            with _goal_c2:
                _goal_saved = st.number_input("Deja epargne (EUR)", min_value=0.0, step=10.0, key="goal_saved")
            if st.button("Ajouter l'objectif", key="add_goal"):
                if _goal_name and _goal_target > 0:
                    st.session_state.savings_goals.append({
                        "name": _goal_name,
                        "target": _goal_target,
                        "saved": _goal_saved,
                        "created": datetime.now().strftime("%Y-%m-%d"),
                    })
                    save_savings_goals(st.session_state.savings_goals)
                    st.success(f"Objectif '{_goal_name}' ajoute !")
                    st.rerun()

        if st.session_state.savings_goals:
            for g_idx, goal in enumerate(st.session_state.savings_goals):
                _g_pct = min((goal["saved"] / goal["target"]) * 100, 100) if goal["target"] > 0 else 0
                _g_color = "#22c55e" if _g_pct >= 100 else _th["accent_mid"]
                _g_status = "Objectif atteint !" if _g_pct >= 100 else f"Encore {goal['target'] - goal['saved']:.0f} EUR"

                _goal_col, _upd_col, _del_col = st.columns([6, 1, 1])
                with _goal_col:
                    st.html(f"""
                    <div style="background:{_card}; border:1px solid {_border}; border-radius:14px; padding:16px 24px; margin-bottom:8px; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                            <span style="font-size:15px; font-weight:700; color:{_text1};">{"🎉" if _g_pct >= 100 else "🎯"} {goal['name']}</span>
                            <span style="font-size:13px; color:{_g_color}; font-weight:600;">{_g_status}</span>
                        </div>
                        <div style="background:{_input_bg}; border-radius:8px; height:12px; overflow:hidden;">
                            <div style="width:{_g_pct}%; height:100%; background:linear-gradient(90deg, {_th['accent_dark']}, {_g_color}); border-radius:8px;"></div>
                        </div>
                        <div style="display:flex; justify-content:space-between; margin-top:4px;">
                            <span style="font-size:11px; color:{_text3};">{goal['saved']:.0f} EUR epargnes</span>
                            <span style="font-size:11px; color:{_text3};">{goal['target']:.0f} EUR objectif</span>
                        </div>
                    </div>
                    """)
                with _upd_col:
                    _add_amount = st.number_input("Ajouter", min_value=0.0, step=10.0, key=f"add_save_{g_idx}", label_visibility="collapsed")
                with _del_col:
                    if st.button("➕", key=f"btn_add_save_{g_idx}"):
                        if _add_amount > 0:
                            st.session_state.savings_goals[g_idx]["saved"] += _add_amount
                            save_savings_goals(st.session_state.savings_goals)
                            st.rerun()
                    if st.button("🗑️", key=f"btn_del_goal_{g_idx}"):
                        st.session_state.savings_goals.pop(g_idx)
                        save_savings_goals(st.session_state.savings_goals)
                        st.rerun()
        else:
            st.caption("Aucun objectif defini. Ajoute ton premier objectif ci-dessus !")

        # ════════════════════════════════════════════════════════════════
        # 8. EXPORT PDF RÉSUMÉ MENSUEL
        # ════════════════════════════════════════════════════════════════
        _sec = "display:flex; align-items:center; gap:10px; margin:24px 0 16px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
        _pdf_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/><path d="M9 13h6M9 17h4"/></svg>'
        st.html(f"""
        <div style="{_sec}">
            <div style="{_ico}">{svg_img(_pdf_svg)}</div>
            <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Export resume mensuel</h2>
        </div>
        """)

        _export_month = st.selectbox(
            "Choisir le mois",
            sorted(_tx_all["date"].dt.to_period("M").astype(str).unique().tolist(), reverse=True),
            key="export_month",
        )
        if st.button("Generer le resume CSV", type="primary", key="export_csv"):
            _exp = _tx_all[_tx_all["date"].dt.to_period("M").astype(str) == _export_month].copy()
            _exp = _exp.sort_values("date")
            _exp_display = _exp[["date", "label", "amount", "auto_category"]].copy()
            _exp_display.columns = ["Date", "Libelle", "Montant", "Categorie"]
            _exp_display["Date"] = _exp_display["Date"].dt.strftime("%d/%m/%Y")

            # Résumé par catégorie
            _summary = _exp.groupby("auto_category")["amount"].agg(["sum", "count"]).reset_index()
            _summary.columns = ["Categorie", "Total", "Nb transactions"]
            _summary = _summary.sort_values("Total", ascending=False)

            # Créer un CSV avec résumé + détails
            csv_buffer = io.StringIO()
            csv_buffer.write(f"Resume mensuel - {_export_month}\n\n")
            csv_buffer.write("=== RESUME PAR CATEGORIE ===\n")
            _summary.to_csv(csv_buffer, index=False)
            csv_buffer.write(f"\nTotal general: {_exp['amount'].sum():.2f} EUR\n")
            csv_buffer.write(f"Nombre de transactions: {len(_exp)}\n\n")
            csv_buffer.write("=== DETAIL DES TRANSACTIONS ===\n")
            _exp_display.to_csv(csv_buffer, index=False)

            st.download_button(
                label=f"Telecharger le resume {_export_month}",
                data=csv_buffer.getvalue(),
                file_name=f"scribe_resume_{_export_month}.csv",
                mime="text/csv",
                key="download_csv",
            )

        # ── 9. Bilan annuel ──
        _annual_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/><path d="M8 14h.01M12 14h.01M16 14h.01M8 18h.01M12 18h.01"/></svg>'
        st.html(f"""
        <div style="{_sec}">
            <div style="{_ico}">{svg_img(_annual_svg)}</div>
            <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Bilan annuel</h2>
        </div>
        """)

        _available_years = sorted(_tx_all["date"].dt.year.dropna().unique().astype(int).tolist(), reverse=True)
        if _available_years:
            _sel_year = st.selectbox("Annee", _available_years, key="bilan_year")
            _year_tx = _tx_all[_tx_all["date"].dt.year == _sel_year].copy()

            if not _year_tx.empty:
                _year_total = _year_tx["amount"].sum()
                _year_count = len(_year_tx)
                _year_avg_month = _year_total / max(_year_tx["date"].dt.month.nunique(), 1)

                # Revenus annuels depuis fiches de paie
                _payslips_annual = load_payslips()
                _year_revenue = 0.0
                if not _payslips_annual.empty:
                    _payslips_annual["salaire_net"] = pd.to_numeric(_payslips_annual["salaire_net"], errors="coerce")
                    _last_salary = _payslips_annual.dropna(subset=["salaire_net"]).sort_values("date_ajout")
                    if not _last_salary.empty:
                        _nb_months = _year_tx["date"].dt.month.nunique()
                        _year_revenue = _last_salary.iloc[-1]["salaire_net"] * _nb_months

                _year_epargne = _year_revenue - _year_total if _year_revenue > 0 else 0
                _ep_color = "#22c55e" if _year_epargne >= 0 else "#ef4444"
                _ep_sign = "+" if _year_epargne >= 0 else ""

                # Mois le plus cher
                _month_sums = _year_tx.groupby(_year_tx["date"].dt.month)["amount"].sum()
                _most_expensive_month_num = _month_sums.idxmax()
                _month_names_fr = {1:"Janvier",2:"Fevrier",3:"Mars",4:"Avril",5:"Mai",6:"Juin",7:"Juillet",8:"Aout",9:"Septembre",10:"Octobre",11:"Novembre",12:"Decembre"}
                _most_expensive_month = _month_names_fr.get(int(_most_expensive_month_num), "?")
                _most_expensive_amount = _month_sums.max()

                # Catégorie la plus dépensée
                _cat_sums = _year_tx.groupby("auto_category")["amount"].sum().sort_values(ascending=False)
                _top_cat = _cat_sums.index[0] if len(_cat_sums) > 0 else "?"
                _top_cat_amount = _cat_sums.iloc[0] if len(_cat_sums) > 0 else 0
                _top_cat_icon = TRANSACTION_CATEGORIES.get(_top_cat, {}).get("icon", "📦")

                # Stat boxes
                st.html(f"""
                <div style="font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
                  <div style="display:flex; gap:12px; margin-bottom:14px;">
                    <div style="flex:1; background:{_card}; border:1px solid {_border}; border-radius:14px; padding:18px 16px; text-align:center;">
                      <div style="font-size:26px; font-weight:800; color:#ef4444;">{_year_total:,.2f} EUR</div>
                      <div style="font-size:11px; color:{_text3}; text-transform:uppercase; letter-spacing:.04em; margin-top:4px;">Total depense</div>
                    </div>
                    <div style="flex:1; background:{_card}; border:1px solid {_border}; border-radius:14px; padding:18px 16px; text-align:center;">
                      <div style="font-size:26px; font-weight:800; color:{_th['accent_text']};">{_year_count}</div>
                      <div style="font-size:11px; color:{_text3}; text-transform:uppercase; letter-spacing:.04em; margin-top:4px;">Transactions</div>
                    </div>
                    <div style="flex:1; background:{_card}; border:1px solid {_border}; border-radius:14px; padding:18px 16px; text-align:center;">
                      <div style="font-size:26px; font-weight:800; color:#f59e0b;">{_year_avg_month:,.2f} EUR</div>
                      <div style="font-size:11px; color:{_text3}; text-transform:uppercase; letter-spacing:.04em; margin-top:4px;">Moyenne / mois</div>
                    </div>
                  </div>
                """)

                if _year_revenue > 0:
                    st.html(f"""
                  <div style="display:flex; gap:12px; margin-bottom:14px; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
                    <div style="flex:1; background:{_card}; border:1px solid {_border}; border-radius:14px; padding:18px 16px; text-align:center;">
                      <div style="font-size:26px; font-weight:800; color:#22c55e;">{_year_revenue:,.2f} EUR</div>
                      <div style="font-size:11px; color:{_text3}; text-transform:uppercase; letter-spacing:.04em; margin-top:4px;">Revenus estimes</div>
                    </div>
                    <div style="flex:1; background:{_card}; border:1px solid {_border}; border-radius:14px; padding:18px 16px; text-align:center;">
                      <div style="font-size:26px; font-weight:800; color:{_ep_color};">{_ep_sign}{_year_epargne:,.2f} EUR</div>
                      <div style="font-size:11px; color:{_text3}; text-transform:uppercase; letter-spacing:.04em; margin-top:4px;">Epargne estimee</div>
                    </div>
                  </div>
                    """)

                # Highlights
                st.html(f"""
                <div style="font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
                  <div style="display:flex; gap:12px; margin-bottom:20px;">
                    <div style="flex:1; background:{_card}; border:1px solid {_border}; border-radius:14px; padding:18px 16px;">
                      <div style="font-size:12px; color:{_text3}; margin-bottom:6px;">Mois le plus cher</div>
                      <div style="font-size:18px; font-weight:700; color:{_text1};">📅 {_most_expensive_month}</div>
                      <div style="font-size:14px; color:#ef4444; font-weight:600;">{_most_expensive_amount:,.2f} EUR</div>
                    </div>
                    <div style="flex:1; background:{_card}; border:1px solid {_border}; border-radius:14px; padding:18px 16px;">
                      <div style="font-size:12px; color:{_text3}; margin-bottom:6px;">Categorie n1</div>
                      <div style="font-size:18px; font-weight:700; color:{_text1};">{_top_cat_icon} {_top_cat}</div>
                      <div style="font-size:14px; color:#f59e0b; font-weight:600;">{_top_cat_amount:,.2f} EUR</div>
                    </div>
                  </div>
                </div>
                """)

                # Graphique mensuel de l'année
                _monthly_year = _year_tx.groupby(_year_tx["date"].dt.month)["amount"].sum().reset_index()
                _monthly_year.columns = ["Mois", "Montant"]
                _monthly_year["Mois_nom"] = _monthly_year["Mois"].map(_month_names_fr)

                import altair as alt
                _bar_year = alt.Chart(_monthly_year).mark_bar(
                    cornerRadiusTopLeft=6, cornerRadiusTopRight=6,
                    color=_th["accent_mid"]
                ).encode(
                    x=alt.X("Mois_nom:N", sort=list(_month_names_fr.values()), title=None,
                            axis=alt.Axis(labelAngle=-45, labelColor=_text3)),
                    y=alt.Y("Montant:Q", title=None, axis=alt.Axis(labelColor=_text3)),
                    tooltip=[
                        alt.Tooltip("Mois_nom:N", title="Mois"),
                        alt.Tooltip("Montant:Q", title="Depenses", format=",.2f"),
                    ],
                ).properties(height=250, title=f"Depenses mensuelles {_sel_year}")

                st.altair_chart(_bar_year, use_container_width=True)

                # Top 5 catégories en barres
                _top5_cats = _cat_sums.head(5).reset_index()
                _top5_cats.columns = ["Categorie", "Montant"]
                _top5_cats["Icon"] = _top5_cats["Categorie"].apply(
                    lambda c: TRANSACTION_CATEGORIES.get(c, {}).get("icon", "📦") + " " + c
                )
                _cat_colors = {
                    cat: info["color"] for cat, info in TRANSACTION_CATEGORIES.items()
                }

                _bar_cats = alt.Chart(_top5_cats).mark_bar(
                    cornerRadiusTopLeft=6, cornerRadiusTopRight=6,
                ).encode(
                    x=alt.X("Montant:Q", title=None, axis=alt.Axis(labelColor=_text3)),
                    y=alt.Y("Icon:N", sort="-x", title=None, axis=alt.Axis(labelColor=_text1)),
                    color=alt.Color("Categorie:N", scale=alt.Scale(
                        domain=list(_cat_colors.keys()),
                        range=list(_cat_colors.values())
                    ), legend=None),
                    tooltip=[
                        alt.Tooltip("Categorie:N", title="Categorie"),
                        alt.Tooltip("Montant:Q", title="Total", format=",.2f"),
                    ],
                ).properties(height=200, title=f"Top 5 categories {_sel_year}")

                st.altair_chart(_bar_cats, use_container_width=True)

    elif tx_df.empty:
        st.caption("Importe un CSV ou connecte ta banque pour commencer le suivi.")

    st.divider()

    # --------------------------------------------------------------------------
    # Interface — Récupérer mes factures (woob)
    # --------------------------------------------------------------------------

    # On affiche la section seulement s'il y a des fournisseurs avec récup auto
    _auto_providers = {
        name: info for name, info in st.session_state.get("connected_providers", {}).items()
        if info.get("auto")
    }

    if _auto_providers:
        _sec = "display:flex; align-items:center; gap:10px; margin:32px 0 16px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
        _ico = f"width:36px; height:36px; border-radius:10px; background:{_th['accent_bg']}; display:flex; align-items:center; justify-content:center;"
        _dl_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>'
        st.html(f"""
        <div style="{_sec}">
            <div style="{_ico}">
                {svg_img(_dl_svg)}
            </div>
            <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Recuperer mes factures</h2>
        </div>
        """)

        if "fetched_bills" not in st.session_state:
            st.session_state.fetched_bills = []

        providers_list = list(_auto_providers.keys())
        st.caption(f"Fournisseurs connectes : {', '.join(providers_list)}")

        if st.button("Recuperer les factures", type="primary", key="fetch_bills_btn"):
            st.session_state.fetched_bills = []
            all_bills = []
            for prov_name, creds in _auto_providers.items():
                with st.spinner(f"Connexion a {prov_name}..."):
                    bills, error = fetch_bills_from_provider(
                        prov_name, creds["login"], creds["password"]
                    )
                    if error:
                        st.error(f"{prov_name} : {error}")
                    else:
                        all_bills.extend(bills)
                        st.success(f"{PROVIDERS[prov_name]['icon']} {prov_name} : {len(bills)} facture(s) trouvee(s)")
            st.session_state.fetched_bills = all_bills

        if st.session_state.fetched_bills:
            for i, bill in enumerate(st.session_state.fetched_bills):
                icon = PROVIDERS.get(bill["provider"], {}).get("icon", "📄")
                price_str = f"{bill['total_price']:.2f} {bill['currency']}" if bill.get("total_price") else "—"
                date_str = bill.get("date") or "—"

                col_info, col_price, col_date, col_action = st.columns([3, 2, 2, 1])
                col_info.write(f"{icon} **{bill['provider']}** — {bill['label']}")
                col_price.write(price_str)
                col_date.write(date_str)

                if bill.get("has_file") and api_key:
                    if col_action.button("Analyser", key=f"analyze_bill_{i}"):
                        creds = st.session_state.connected_providers.get(bill["provider"], {})
                        with st.spinner(f"Telechargement et analyse de {bill['label']}..."):
                            file_data, dl_error = download_bill(
                                bill["provider"], creds.get("login", ""),
                                creds.get("password", ""), bill["id"]
                            )
                            if dl_error:
                                st.error(f"Erreur : {dl_error}")
                            elif file_data:
                                # Sauvegarde locale
                                safe_name = f"{bill['provider']}_{bill.get('date', 'unknown')}.pdf"
                                bill_path = BILLS_DIR / safe_name
                                bill_path.write_bytes(file_data)

                                # Analyse via Gemini
                                try:
                                    client = genai.Client(api_key=api_key)
                                    part = genai_types.Part.from_bytes(
                                        data=file_data, mime_type="application/pdf"
                                    )
                                    data = extract_invoice_data(client, part)

                                    montant = float(data.get("montant") or 0)
                                    fournisseur = (data.get("fournisseur") or bill["provider"]).strip()

                                    c1, c2, c3 = st.columns(3)
                                    c1.metric("Fournisseur", fournisseur)
                                    c2.metric("Montant", f"{montant:.2f} {data.get('devise', 'EUR')}")
                                    c3.metric("Date", data.get("date_facture") or "—")

                                    history_df_now = load_history()
                                    anomaly = check_anomaly(history_df_now, fournisseur, montant)

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
                                        "fichier": safe_name,
                                    }
                                    save_entry(entry)

                                    if anomaly and anomaly["is_anomaly"]:
                                        st.warning(
                                            f"Hausse detectee : +{anomaly['delta_abs']:.2f} EUR "
                                            f"({anomaly['delta_pct']:.1f}%)"
                                        )
                                    elif anomaly:
                                        st.success(f"Pas d'anomalie ({anomaly['delta_pct']:+.1f}%)")
                                    else:
                                        st.success("Premiere facture enregistree pour ce fournisseur.")
                                except Exception as exc:
                                    st.error(f"Erreur d'analyse : {exc}")

            st.divider()

    # --------------------------------------------------------------------------
    # Interface — Analyser un document
    # --------------------------------------------------------------------------

    _sec = "display:flex; align-items:center; gap:10px; margin:32px 0 16px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
    _ico = f"width:36px; height:36px; border-radius:10px; background:{_th['accent_bg']}; display:flex; align-items:center; justify-content:center;"
    _doc_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/></svg>'
    st.html(f"""
    <div style="{_sec}">
        <div style="{_ico}">
            {svg_img(_doc_svg)}
        </div>
        <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Analyser un document</h2>
    </div>
    """)

    uploaded_file = st.file_uploader(
        "Depose une facture ou un justificatif d'abonnement",
        type=["pdf", "jpg", "jpeg", "png", "webp"],
        label_visibility="collapsed",
    )

    if uploaded_file is not None:
        if not api_key:
            st.warning("Ouvre les Parametres (en haut) et colle ta cle API Gemini pour lancer l'analyse.")
        elif st.button("Analyser ce document", type="primary"):
            try:
                client = genai.Client(api_key=api_key)
                with st.spinner("Lecture du document..."):
                    content_part = file_to_content_part(uploaded_file)
                    data = extract_invoice_data(client, content_part)

                montant = float(data.get("montant") or 0)
                fournisseur = (data.get("fournisseur") or "Fournisseur inconnu").strip()

                col1, col2, col3 = st.columns(3)
                col1.metric("Fournisseur", fournisseur)
                col2.metric("Montant", f"{montant:.2f} {data.get('devise', 'EUR')}")
                col3.metric("Date", data.get("date_facture") or "—")

                if data.get("notes"):
                    st.info(f"Note : {data['notes']}")

                history_df_current = load_history()
                anomaly = check_anomaly(history_df_current, fournisseur, montant)

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
                    "fichier": uploaded_file.name,
                }
                save_entry(entry)

                if anomaly is None:
                    st.success("Premiere facture enregistree pour ce fournisseur — elle sert de reference.")
                elif anomaly["is_anomaly"]:
                    st.warning(
                        f"Hausse detectee : +{anomaly['delta_abs']:.2f} {entry['devise']} "
                        f"({anomaly['delta_pct']:.1f}%) par rapport a la derniere facture "
                        f"({anomaly['previous_montant']:.2f} {entry['devise']} le {anomaly['previous_date']})."
                    )
                    tone = st.selectbox("Ton du mail de reclamation", list(TONE_INSTRUCTIONS.keys()))
                    if st.button("Generer le brouillon de reclamation"):
                        with st.spinner("Redaction du brouillon..."):
                            email_text = draft_email(client, entry, anomaly, tone)
                        st.text_area("Brouillon (a relire avant envoi)", email_text, height=280)
                else:
                    st.success(
                        f"Pas d'anomalie detectee (variation de {anomaly['delta_pct']:+.1f}% "
                        f"par rapport a la derniere facture)."
                    )

            except json.JSONDecodeError:
                st.error("L'IA n'a pas renvoye un JSON exploitable. Reessaie, ou essaie un autre document.")
            except Exception as exc:
                st.error(f"Une erreur est survenue : {exc}")

    # --------------------------------------------------------------------------
    # Interface — Historique
    # --------------------------------------------------------------------------

    _sec = "display:flex; align-items:center; gap:10px; margin:32px 0 16px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
    _ico = f"width:36px; height:36px; border-radius:10px; background:{_th['accent_bg']}; display:flex; align-items:center; justify-content:center;"
    _table_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 4v16"/></svg>'
    st.html(f"""
    <div style="{_sec}">
        <div style="{_ico}">
            {svg_img(_table_svg)}
        </div>
        <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Historique</h2>
    </div>
    """)

    history_df = load_history()
    if history_df.empty:
        st.html(f"""
        <div style="text-align:center; padding:48px 24px; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
            <div style="font-size:48px; margin-bottom:12px; opacity:0.4;">📋</div>
            <p style="font-size:15px; color:{_text3};">Aucune facture analysee pour l'instant.<br>
            Depose ton premier document ci-dessus pour commencer.</p>
        </div>
        """)
    else:
        display_df = history_df.sort_values("date_ajout", ascending=False).rename(columns={
            "date_ajout": "Date", "fournisseur": "Fournisseur", "type_contrat": "Type",
            "montant": "Montant", "devise": "Devise", "date_facture": "Facture du",
            "numero_contrat": "N° contrat", "periode": "Periode", "notes": "Notes", "fichier": "Fichier",
        })
        st.dataframe(display_df, use_container_width=True, hide_index=True)


with tab_paie:
    # --------------------------------------------------------------------------
    # Interface — Fiches de paie
    # --------------------------------------------------------------------------

    _sec = "display:flex; align-items:center; gap:10px; margin:32px 0 16px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
    _ico = f"width:36px; height:36px; border-radius:10px; background:{_th['accent_bg']}; display:flex; align-items:center; justify-content:center;"
    _payslip_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="18" rx="2"/><path d="M2 9h20M2 15h20M8 3v18"/></svg>'
    st.html(f"""
    <div style="{_sec}">
        <div style="{_ico}">
            {svg_img(_payslip_svg)}
        </div>
        <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Fiches de paie</h2>
    </div>
    """)

    payslip_file = st.file_uploader(
        "Depose une fiche de paie",
        type=["pdf", "jpg", "jpeg", "png", "webp"],
        label_visibility="collapsed",
        key="payslip_uploader",
    )

    if payslip_file is not None:
        if not api_key:
            st.warning("Ouvre les Parametres (en haut) et colle ta cle API Gemini pour lancer l'analyse.")
        elif st.button("Analyser cette fiche de paie", type="primary", key="analyze_payslip"):
            try:
                client = genai.Client(api_key=api_key)
                with st.spinner("Lecture de la fiche de paie..."):
                    content_part = file_to_content_part(payslip_file)
                    data = extract_payslip_data(client, content_part)

                salaire_brut = float(data.get("salaire_brut") or 0)
                salaire_net = float(data.get("salaire_net") or 0)
                net_imposable = data.get("net_imposable")
                employeur = (data.get("employeur") or "Employeur inconnu").strip()

                col1, col2, col3 = st.columns(3)
                col1.metric("Employeur", employeur)
                col2.metric("Salaire net", f"{salaire_net:.2f} {data.get('devise', 'EUR')}")
                col3.metric("Mois", data.get("mois") or "—")

                if data.get("notes"):
                    st.info(f"Note : {data['notes']}")

                payslips_df_current = load_payslips()
                anomaly = check_payslip_anomaly(payslips_df_current, salaire_net)

                entry = {
                    "date_ajout": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "employeur": employeur,
                    "mois": data.get("mois"),
                    "salaire_brut": salaire_brut,
                    "salaire_net": salaire_net,
                    "net_imposable": float(net_imposable) if net_imposable else None,
                    "devise": data.get("devise", "EUR"),
                    "date_fiche": data.get("date_fiche"),
                    "notes": data.get("notes"),
                    "fichier": payslip_file.name,
                }
                save_payslip(entry)

                if anomaly is None:
                    st.success("Premiere fiche de paie enregistree — elle sert de reference.")
                elif anomaly["is_anomaly"]:
                    direction = "Hausse" if anomaly["delta_abs"] > 0 else "Baisse"
                    st.warning(
                        f"{direction} detectee : {anomaly['delta_abs']:+.2f} EUR "
                        f"({anomaly['delta_pct']:+.1f}%) par rapport au mois precedent "
                        f"({anomaly['previous_net']:.2f} EUR, {anomaly['previous_mois']})."
                    )
                else:
                    st.success(
                        f"Pas d'ecart significatif ({anomaly['delta_pct']:+.1f}% "
                        f"par rapport au mois precedent)."
                    )

            except json.JSONDecodeError:
                st.error("L'IA n'a pas renvoye un JSON exploitable. Reessaie, ou essaie un autre document.")
            except Exception as exc:
                st.error(f"Une erreur est survenue : {exc}")

    # Stat tiles fiches de paie
    payslips_df = load_payslips()
    if not payslips_df.empty:
        # ── Filtre temporel fiches de paie ──
        pay_period = st.radio(
            "Periode paie",
            ["Tout", "Ce mois", "Cette annee"],
            horizontal=True,
            key="pay_period",
            label_visibility="collapsed",
        )
        payslips_df["date_ajout_dt"] = pd.to_datetime(payslips_df["date_ajout"], errors="coerce")
        _now_pay = pd.Timestamp.now()
        if pay_period == "Ce mois":
            payslips_df = payslips_df[
                (payslips_df["date_ajout_dt"].dt.month == _now_pay.month)
                & (payslips_df["date_ajout_dt"].dt.year == _now_pay.year)
            ]
        elif pay_period == "Cette annee":
            payslips_df = payslips_df[payslips_df["date_ajout_dt"].dt.year == _now_pay.year]

        payslips_df["salaire_net"] = pd.to_numeric(payslips_df["salaire_net"], errors="coerce")
        last_net = payslips_df.dropna(subset=["salaire_net"])
        if not last_net.empty:
            last_net_sorted = last_net.sort_values("date_ajout")
            dernier_salaire = last_net_sorted.iloc[-1]["salaire_net"]
            nb_fiches = len(payslips_df)

            evolution_str = "—"
            if len(last_net_sorted) >= 2:
                prev = last_net_sorted.iloc[-2]["salaire_net"]
                if prev > 0:
                    evo = ((dernier_salaire - prev) / prev) * 100
                    evolution_str = f"{evo:+.1f}%"

            _pay_period_label = {"Tout": "Total analyse", "Ce mois": "Ce mois-ci", "Cette annee": "Cette annee"}
            _stat_box = f"flex:1; background:{_card}; border:1px solid {_border}; border-radius:14px; padding:20px 24px; text-align:center;"
            _stat_val = f"font-size:32px; font-weight:800; color:{_th['accent_text']}; line-height:1.2;"
            _stat_lbl = f"font-size:12px; font-weight:600; color:{_text3}; text-transform:uppercase; letter-spacing:.04em; margin-top:6px;"
            st.html(f"""
            <div style="display:flex; gap:16px; margin-bottom:24px; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
                <div style="{_stat_box}">
                    <div style="{_stat_val}">{dernier_salaire:,.2f} EUR</div>
                    <div style="{_stat_lbl}">Dernier salaire net</div>
                </div>
                <div style="{_stat_box}">
                    <div style="{_stat_val}">{evolution_str}</div>
                    <div style="{_stat_lbl}">Evolution</div>
                </div>
                <div style="{_stat_box}">
                    <div style="{_stat_val}">{nb_fiches}</div>
                    <div style="{_stat_lbl}">Fiches enregistrees</div>
                </div>
            </div>
            """)
        else:
            st.info("Aucune fiche ne correspond a cette periode.")

        # Table des fiches
        st.markdown("##### Historique des fiches de paie")
        display_ps = payslips_df.sort_values("date_ajout", ascending=False).rename(columns={
            "date_ajout": "Date", "employeur": "Employeur", "mois": "Mois",
            "salaire_brut": "Brut", "salaire_net": "Net", "net_imposable": "Net imposable",
            "devise": "Devise", "date_fiche": "Date fiche", "notes": "Notes", "fichier": "Fichier",
        })
        if "date_ajout_dt" in display_ps.columns:
            display_ps = display_ps.drop(columns=["date_ajout_dt"])
        st.dataframe(display_ps, use_container_width=True, hide_index=True)
    else:
        st.html(f"""
        <div style="text-align:center; padding:48px 24px; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
            <div style="font-size:48px; margin-bottom:12px; opacity:0.4;">💰</div>
            <p style="font-size:15px; color:{_text3};">Aucune fiche de paie analysee pour l'instant.<br>
            Depose ta premiere fiche ci-dessus pour commencer le suivi.</p>
        </div>
        """)

# --------------------------------------------------------------------------
# Onglet Parametres
# --------------------------------------------------------------------------
with tab_params:

    # ── Apparence ──
    _sec_p = "display:flex; align-items:center; gap:10px; margin:16px 0 16px 0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
    _ico_p = f"width:36px; height:36px; border-radius:10px; background:{_th['accent_bg']}; display:flex; align-items:center; justify-content:center;"
    _paint_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>'
    st.html(f"""
    <div style="{_sec_p}">
        <div style="{_ico_p}">{svg_img(_paint_svg)}</div>
        <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Apparence</h2>
    </div>
    """)

    _ap1, _ap2 = st.columns(2)
    with _ap1:
        _theme_names = list(ACCENT_THEMES.keys())
        _cur_idx = _theme_names.index(st.session_state.accent_theme)
        _theme_icons = {"Bleu nuit": "🔵", "Emeraude": "🟢", "Violet": "🟣", "Corail": "🟠", "Rose": "🩷", "Or": "🟡"}
        _theme_options = [f"{_theme_icons.get(n, '●')} {n}" for n in _theme_names]
        _sel_theme_full = st.selectbox("Couleur du theme", _theme_options, index=_cur_idx, key="theme_select")
        _sel_theme = _sel_theme_full.split(" ", 1)[1] if " " in _sel_theme_full else _sel_theme_full
        if _sel_theme != st.session_state.accent_theme:
            st.session_state.accent_theme = _sel_theme
            st.rerun()
    with _ap2:
        mode_label = "☀️ Mode clair" if _dark else "🌙 Mode sombre"
        st.html("<div style='height:24px;'></div>")
        if st.button(mode_label, key="toggle_mode", use_container_width=True):
            st.session_state.dark_mode = not _dark
            st.rerun()

    st.divider()

    # ── Cle API & Budget ──
    _key_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/></svg>'
    st.html(f"""
    <div style="{_sec_p}">
        <div style="{_ico_p}">{svg_img(_key_svg)}</div>
        <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">API & Budget</h2>
    </div>
    """)

    _kb1, _kb2 = st.columns(2)
    with _kb1:
        st.markdown("**Cle API Gemini**")
        _api_input = st.text_input(
            "Cle API Gemini",
            value=st.session_state.api_key_val,
            type="password",
            help="Recupere ta cle gratuite sur aistudio.google.com. Elle n'est jamais sauvegardee.",
            label_visibility="collapsed",
        )
        if _api_input != st.session_state.api_key_val:
            st.session_state.api_key_val = _api_input
            st.rerun()
        st.caption(
            "Pas encore de cle ? [aistudio.google.com](https://aistudio.google.com/apikey)"
        )
    with _kb2:
        st.markdown("**Budget mensuel**")
        if "monthly_budget" not in st.session_state:
            st.session_state.monthly_budget = 0.0
        budget_val = st.number_input(
            "Budget (EUR)", min_value=0.0,
            value=st.session_state.monthly_budget,
            step=50.0, key="budget_input",
            label_visibility="collapsed",
        )
        if budget_val != st.session_state.monthly_budget:
            st.session_state.monthly_budget = budget_val
            st.rerun()
        if st.session_state.monthly_budget > 0:
            st.caption(f"Objectif : {st.session_state.monthly_budget:.0f} EUR / mois")
        else:
            st.caption("Definis un budget pour suivre tes depenses.")

    st.divider()

    # ── Fournisseurs ──
    _prov_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16v16H4z"/><path d="M4 10h16M10 4v16"/></svg>'
    st.html(f"""
    <div style="{_sec_p}">
        <div style="{_ico_p}">{svg_img(_prov_svg)}</div>
        <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Connexions</h2>
    </div>
    """)

    _cn1, _cn2 = st.columns(2)
    with _cn1:
        st.markdown("**Mes fournisseurs**")
        if "connected_providers" not in st.session_state:
            st.session_state.connected_providers = {}
        provider_names = sorted(KNOWN_PROVIDERS.keys())
        choices = provider_names + ["── Autre (saisie libre) ──"]
        prov_choice = st.selectbox("Fournisseur", choices, key="prov_select")
        custom_name = None
        if prov_choice.startswith("──"):
            custom_name = st.text_input("Nom du fournisseur", key="prov_custom_name",
                                         placeholder="Ex: Direct Assurance, Leclerc Energie...")
        chosen_name = custom_name if custom_name else prov_choice
        prov_info = KNOWN_PROVIDERS.get(chosen_name, {})
        has_auto = _HAS_WOOB and prov_info.get("module") is not None
        if has_auto:
            prov_login = st.text_input("Identifiant / email", key="prov_login")
            prov_password = st.text_input("Mot de passe", type="password", key="prov_password")
            can_add = bool(prov_login and prov_password and chosen_name)
        else:
            if chosen_name and not prov_choice.startswith("──"):
                st.caption("Pas de recuperation automatique — upload manuel.")
            elif custom_name:
                st.caption("Fournisseur personnalise — upload manuel.")
            prov_login = ""
            prov_password = ""
            can_add = bool(chosen_name and not prov_choice.startswith("──")) or bool(custom_name)
        if st.button("Ajouter", disabled=not can_add):
            icon = prov_info.get("icon", "📄") if prov_info else "📄"
            st.session_state.connected_providers[chosen_name] = {
                "login": prov_login,
                "password": prov_password,
                "auto": has_auto,
                "icon": icon,
            }
            st.success(f"{icon} {chosen_name} ajoute !")
            st.rerun()
        if st.session_state.connected_providers:
            for name in list(st.session_state.connected_providers.keys()):
                info = st.session_state.connected_providers[name]
                icon = info.get("icon", KNOWN_PROVIDERS.get(name, {}).get("icon", "📄"))
                auto_tag = " (auto)" if info.get("auto") else ""
                col_name, col_btn = st.columns([3, 1])
                col_name.caption(f"{icon} {name}{auto_tag}")
                if col_btn.button("✕", key=f"rm_{name}"):
                    del st.session_state.connected_providers[name]
                    st.rerun()
        else:
            st.caption("Aucun fournisseur ajoute.")
        if not _HAS_WOOB:
            st.caption("Pour la recuperation auto :")
            st.code("pip install woob", language="bash")

    with _cn2:
        st.markdown("**Ma banque**")
        if "connected_bank" not in st.session_state:
            st.session_state.connected_bank = None
        bank_names = sorted(KNOWN_BANKS.keys())
        bank_choice = st.selectbox("Banque", bank_names, key="bank_select")
        bank_info = KNOWN_BANKS.get(bank_choice, {})
        bank_has_module = _HAS_WOOB and bank_info.get("module") is not None
        if bank_has_module:
            bank_login = st.text_input("Identifiant bancaire", key="bank_login")
            bank_password = st.text_input("Mot de passe", type="password", key="bank_password")
            can_connect_bank = bool(bank_login and bank_password)
        else:
            st.caption("Pas de connexion automatique pour cette banque.")
            bank_login = ""
            bank_password = ""
            can_connect_bank = False
        if st.button("Connecter", disabled=not can_connect_bank, key="btn_connect_bank"):
            st.session_state.connected_bank = {
                "name": bank_choice,
                "login": bank_login,
                "password": bank_password,
                "icon": bank_info.get("icon", "🏦"),
            }
            st.success(f"🏦 {bank_choice} connectee !")
            st.rerun()
        if st.session_state.connected_bank:
            bk = st.session_state.connected_bank
            col_bk, col_rm = st.columns([3, 1])
            col_bk.caption(f"🏦 {bk['name']}")
            if col_rm.button("✕", key="rm_bank"):
                st.session_state.connected_bank = None
                st.rerun()
        else:
            st.caption("Aucune banque connectee.")

    st.divider()

    # ── Donnees & reinitialisation ──
    _data_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" stroke="{_th["accent_text"]}" fill="none" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>'
    st.html(f"""
    <div style="{_sec_p}">
        <div style="{_ico_p}">{svg_img(_data_svg)}</div>
        <h2 style="font-size:22px; font-weight:700; margin:0; color:{_text1};">Donnees</h2>
    </div>
    """)

    st.caption(f"Historique : `{HISTORY_PATH}`")
    st.caption(f"Transactions : `{BANK_TX_PATH}`")
    st.caption(f"Fiches de paie : `{PAYSLIP_PATH}`")

    confirm = st.checkbox("Je confirme vouloir tout effacer")
    if st.button("Reinitialiser l'historique", disabled=not confirm, type="primary"):
        if HISTORY_PATH.exists():
            HISTORY_PATH.unlink()
        st.success("Historique efface.")
        st.rerun()

    st.divider()

    # ── Retour accueil ──
    if st.button("↩ Retour a la page d'accueil", key="back_to_welcome", type="tertiary"):
        st.session_state.onboarded = False
        _prefs = _load_user_prefs()
        _prefs["onboarded"] = False
        _save_user_prefs(_prefs)
        st.rerun()
