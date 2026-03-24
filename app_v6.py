import os
import re
import unicodedata
from datetime import datetime, date

import pandas as pd
import requests
import streamlit as st
import folium
from streamlit_folium import st_folium
from google import genai

# ============================================================
# CONFIG + HELPERS
# ============================================================

st.set_page_config(page_title="Carte interactive des villes TGVmax", layout="wide")

def get_secret_or_env(name: str, default: str = "") -> str:
    try:
        return st.secrets[name]
    except Exception:
        return os.getenv(name, default)

NAVITIA_TOKEN = "f336400b-e2a7-42db-877d-5c1e06eef91b"
COVERAGE = "sncf"
BASE_URL = f"https://api.navitia.io/v1/coverage/{COVERAGE}"
GEMINI_MODEL = "gemini-2.5-flash"
TGVMAX_FILE = "tgvmax.csv.gz"

# ============================================================
# SESSION STATE
# ============================================================

if "selected_city" not in st.session_state:
    st.session_state.selected_city = None

if "generated_program" not in st.session_state:
    st.session_state.generated_program = ""

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

if "last_chat_signature" not in st.session_state:
    st.session_state.last_chat_signature = None

if "itinerary_rows" not in st.session_state:
    st.session_state.itinerary_rows = []

if "itinerary_by_city" not in st.session_state:
    st.session_state.itinerary_by_city = {}

# ============================================================
# TITRE
# ============================================================

st.title("Carte interactive des destinations labellisées Qualité Tourisme accessibles depuis Paris avec TGVmax")
st.markdown(
    """
    Cette carte présente les villes accessibles depuis Paris pour les détenteurs de l'abonnement TGVmax
    ainsi que les établissements labellisés Qualité Tourisme qui s'y situent.
    """
)

# ============================================================
# CHECK FILES
# ============================================================

fichiers_requis = [
    TGVMAX_FILE,
    "etablissements-labellises-qualite-tourisme.csv",
    "coord_villes.csv",
]
fichiers_manquants = [f for f in fichiers_requis if not os.path.exists(f)]

if fichiers_manquants:
    st.error(f"Fichiers manquants : {', '.join(fichiers_manquants)}")
    st.info("Assurez-vous que les fichiers sont dans le même dossier que ce script.")
    st.stop()

# ============================================================
# NORMALIZATION
# ============================================================

def normalize_str(s: str) -> str:
    if pd.isna(s) or s is None:
        return ""
    s = str(s).strip().upper()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.replace("’", "'")
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def clean_city(x: str):
    if pd.isna(x) or x is None:
        return None
    x = normalize_str(x)
    x = re.sub(r"\s*(TGV|CENTRE|VILLE|MIDI|MATABIAU|CHANTIERS|INTRAMUROS)\s*", " ", x)
    x = re.sub(r"\bSAINT\b", "ST", x)
    x = re.sub(r"\bSAINTE\b", "STE", x)
    x = re.sub(r"[()]", "", x)
    x = re.sub(r"\s+", " ", x).strip()
    return x

SPECIAL = {
    "ST RAPHAEL": "SAINT-RAPHAEL",
    "AIX EN PROVENCE": "AIX-EN-PROVENCE",
    "CHALON SUR SAONE": "CHALON-SUR-SAONE",
    "MARSEILLE ST CHARLES": "MARSEILLE",
}

def normalize_special(s: str) -> str:
    s2 = clean_city(s) or ""
    return SPECIAL.get(s2, s2)

# ============================================================
# DATE / TIME HELPERS
# ============================================================

def navitia_datetime(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")

def pretty_time(navitia_dt):
    if not navitia_dt:
        return "?"
    try:
        return datetime.strptime(navitia_dt, "%Y%m%dT%H%M%S").strftime("%H:%M")
    except Exception:
        return "?"

def format_duration(seconds: int) -> str:
    minutes = seconds // 60
    h = minutes // 60
    m = minutes % 60
    return f"{h}h{m:02d}" if h else f"{m} min"

def to_timedelta_hhmm(s: str):
    if pd.isna(s) or not s:
        return pd.NaT
    s = str(s).strip()
    if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", s):
        parts = s.split(":")
        hh = parts[0].zfill(2)
        mm = parts[1]
        return pd.to_timedelta(f"{hh}:{mm}:00", errors="coerce")
    return pd.NaT

def format_timedelta(td) -> str:
    if td is pd.NaT or pd.isna(td):
        return "Non disponible"
    total_minutes = int(td.total_seconds() // 60)
    return f"{total_minutes//60}h {total_minutes%60:02d}min"

def parse_tgvmax_date(value):
    if pd.isna(value) or value is None:
        return pd.NaT
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return pd.Timestamp(datetime.strptime(s, fmt).date())
        except ValueError:
            pass
    try:
        return pd.to_datetime(s, dayfirst=True, errors="coerce").normalize()
    except Exception:
        return pd.NaT

def parse_hhmm_to_time(value):
    if pd.isna(value) or value is None:
        return None
    s = str(value).strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            pass
    return None

def hhmm(value) -> str:
    t = parse_hhmm_to_time(value)
    return t.strftime("%H:%M") if t else "?"

def minutes_diff_after(selected_time, candidate_time):
    if selected_time is None or candidate_time is None:
        return 10**9
    sel = selected_time.hour * 60 + selected_time.minute
    cand = candidate_time.hour * 60 + candidate_time.minute
    if cand < sel:
        return 10**9
    return cand - sel

def time_to_minutes(t: str) -> int:
    if t == "Non disponible" or not t:
        return 0
    try:
        h_part, rest = t.split("h")
        h = int(h_part.strip())
        m = int(rest.replace("min", "").strip())
        return h * 60 + m
    except Exception:
        return 0

# ============================================================
# LOAD DATA
# ============================================================

@st.cache_data(show_spinner=False)
def load_data():
    tgvmax_df = pd.read_csv(TGVMAX_FILE, sep=";", dtype=str, encoding="utf-8", compression="gzip")
    qualite_df = pd.read_csv("etablissements-labellises-qualite-tourisme.csv", sep=";", dtype=str, encoding="utf-8")
    coords_df = pd.read_csv("coord_villes.csv", encoding="utf-8")
    return tgvmax_df, qualite_df, coords_df

try:
    tgvmax, qualite, coords = load_data()
except Exception as e:
    st.error(f"Erreur lors de la lecture des fichiers : {str(e)}")
    st.stop()

# ============================================================
# PREPARE TGVMAX LOCAL
# ============================================================

@st.cache_data(show_spinner=False)
def prepare_tgvmax_local(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["DATE_norm"] = df["DATE"].apply(parse_tgvmax_date)
    df["Origine_clean"] = df["Origine"].apply(normalize_special)
    df["Destination_clean"] = df["Destination"].apply(normalize_special)
    df["Heure_depart_obj"] = df["Heure_depart"].apply(parse_hhmm_to_time)
    df["Heure_arrivee_obj"] = df["Heure_arrivee"].apply(parse_hhmm_to_time)
    df["MAX_OK"] = (
        df["Disponibilité de places MAX JEUNE et MAX SENIOR"]
        .astype(str)
        .str.strip()
        .str.upper()
        .eq("OUI")
    )
    return df

tgvmax = prepare_tgvmax_local(tgvmax)

# ============================================================
# FILTRER DEPUIS PARIS + DUREE
# ============================================================

tgv_paris = tgvmax[
    (tgvmax["Origine"] == "PARIS (intramuros)") &
    (tgvmax["MAX_OK"])
].copy()

expanded_rows = []
for _, row in tgv_paris.iterrows():
    dest = row["Destination_clean"]
    if dest == "BELFORT MONTBELIARD":
        for ville in ["BELFORT", "MONTBELIARD"]:
            new_row = row.copy()
            new_row["Destination_clean"] = ville
            expanded_rows.append(new_row)
    elif dest == "BLOIS CHAMBORD":
        for ville in ["BLOIS", "CHAMBORD"]:
            new_row = row.copy()
            new_row["Destination_clean"] = ville
            expanded_rows.append(new_row)
    else:
        expanded_rows.append(row)

tgv_paris = pd.DataFrame(expanded_rows)

tgv_paris["Heure_depart_td"] = tgv_paris["Heure_depart"].apply(to_timedelta_hhmm)
tgv_paris["Heure_arrivee_td"] = tgv_paris["Heure_arrivee"].apply(to_timedelta_hhmm)

def compute_duration_td(row):
    dep = row["Heure_depart_td"]
    arr = row["Heure_arrivee_td"]
    if pd.isna(dep) or pd.isna(arr):
        return pd.NaT
    if arr < dep:
        arr = arr + pd.Timedelta(days=1)
    return arr - dep

tgv_paris["Temps_trajet"] = tgv_paris.apply(compute_duration_td, axis=1)

# ============================================================
# QUALITE TOURISME
# ============================================================

qualite["Ville_clean"] = qualite["Ville"].apply(normalize_special)

mapping_secteurs = {
    "HOTEL": "Hébergement",
    "HOTEL-RESTAURANT": "Hébergement",
    "CAMPING": "Hébergement",
    "CHAMBRE D'HOTES": "Hébergement",
    "VILLAGE DE VACANCES": "Hébergement",
    "RESIDENCE DE TOURISME": "Hébergement",
    "HEBERGEMENT COLLECTIF": "Hébergement",
    "OFFICE DE TOURISME": "Services touristiques",
    "RESTAURANT": "Restauration",
    "RESTAURANT DE PLAGE": "Restauration",
    "CAFE, BAR, BRASSERIE": "Restauration",
    "PARC DE LOISIR": "Loisirs & activités",
    "SPORT DE NATURE": "Loisirs & activités",
    "SORTIE NATURE": "Loisirs & activités",
    "ETABLISSEMENT DE LOISIR": "Loisirs & activités",
    "VTC - LIMOUSINE": "Loisirs & activités",
    "AGENCE DE LOCATIONS SAISONNIERES": "Loisirs & activités",
    "SEMINAIRE": "Loisirs & activités",
    "PORT DE PLAISANCE": "Loisirs & activités",
    "PARC A THEME": "Loisirs & activités",
    "LIEU DE VISITE": "Culture & patrimoine",
    "SITE DE MEMOIRE": "Culture & patrimoine",
    "CAVEAUX ET POINTS DE VENTE": "Culture & patrimoine",
    "VISITE D'ENTREPRISE": "Culture & patrimoine",
    "COMMERCE": "Culture & patrimoine",
    "ECOMUSEE": "Culture & patrimoine",
    "VISITE GUIDEE": "Culture & patrimoine",
    "MAISON D'ECRIVAIN": "Culture & patrimoine",
    "SITE DE PREHISTOIRE": "Culture & patrimoine",
}

qualite["Activite_clean"] = qualite["Activité du professionnel"].apply(normalize_str)
qualite["Categorie"] = qualite["Activite_clean"].map(mapping_secteurs).fillna("Autres")

# ============================================================
# VILLES COMMUNES
# ============================================================

villes_finales = sorted(set(tgv_paris["Destination_clean"]) & set(qualite["Ville_clean"]))
qt_final = qualite[qualite["Ville_clean"].isin(villes_finales)].copy()
tgv_final = tgv_paris[tgv_paris["Destination_clean"].isin(villes_finales)].copy()

# ============================================================
# POPUP DATA
# ============================================================

def format_etabs(ville_clean: str) -> str:
    ville_data = qt_final[qt_final["Ville_clean"] == ville_clean]
    html = ""
    for cat in ville_data["Categorie"].dropna().unique():
        cat_data = ville_data[ville_data["Categorie"] == cat]
        html += f"<b>{cat}</b><br>"
        for nom in cat_data["Nom du professionnel"].fillna(""):
            html += f"• {nom}<br>"
        html += "<br>"
    return html

etabs = {ville: format_etabs(ville) for ville in qt_final["Ville_clean"].unique()}
temps = tgv_final.groupby("Destination_clean")["Temps_trajet"].min().to_dict()

# ============================================================
# COORDS
# ============================================================

coords["Ville_clean"] = coords["Ville"].apply(normalize_special)
coord_villes = coords[coords["Ville_clean"].isin(villes_finales)].copy()

coord_villes["temps"] = coord_villes["Ville_clean"].apply(lambda v: format_timedelta(temps.get(v)))
coord_villes["etabs"] = coord_villes["Ville_clean"].apply(lambda v: etabs.get(v, "Aucun établissement"))
coord_villes["minutes"] = coord_villes["temps"].apply(time_to_minutes)
coord_villes["nb_etabs"] = coord_villes["Ville_clean"].apply(lambda v: int((qt_final["Ville_clean"] == v).sum()))
coord_villes["radius"] = coord_villes["nb_etabs"].apply(lambda x: 4 + (x ** 0.5) * 6)

def color_by_time(m: int) -> str:
    if m < 60:
        return "#006400"
    elif m < 120:
        return "#32CD32"
    elif m < 180:
        return "#FFFF00"
    elif m < 240:
        return "#FFA500"
    elif m < 360:
        return "#FF6347"
    else:
        return "#8B0000"

# ============================================================
# SIDEBAR FILTERS
# ============================================================

st.sidebar.header("Personnalisez votre voyage")

duree_sejour = st.sidebar.radio(
    "Combien de temps restez-vous sur place ?",
    ["Journée", "Weekend", "Semaine"],
    index=0
)

min_etabs = 0
if duree_sejour == "Weekend":
    min_etabs = 3
elif duree_sejour == "Semaine":
    min_etabs = 5

temps_max = st.sidebar.selectbox(
    "Quel temps de trajet maximum depuis Paris acceptez-vous ?",
    ["Toutes les durées", "Moins d'1h", "Moins de 2h", "Moins de 3h", "Moins de 4h", "Moins de 6h", "Plus de 6h"],
    index=0,
)

centres_interets = st.sidebar.radio(
    "Quels sont vos principaux centres d'intérêts ?",
    ["Tous", "Loisirs & activités", "Culture & patrimoine", "Restauration", "Hébergement", "Services touristiques"],
    index=0,
)

temps_max_minutes = float("inf")
if temps_max == "Moins d'1h":
    temps_max_minutes = 60
elif temps_max == "Moins de 2h":
    temps_max_minutes = 120
elif temps_max == "Moins de 3h":
    temps_max_minutes = 180
elif temps_max == "Moins de 4h":
    temps_max_minutes = 240
elif temps_max == "Moins de 6h":
    temps_max_minutes = 360

if temps_max == "Plus de 6h":
    coord_villes_filtrees = coord_villes[
        (coord_villes["nb_etabs"] >= min_etabs) & (coord_villes["minutes"] >= 360)
    ].copy()
else:
    coord_villes_filtrees = coord_villes[
        (coord_villes["nb_etabs"] >= min_etabs) & (coord_villes["minutes"] <= temps_max_minutes)
    ].copy()

if centres_interets != "Tous":
    villes_avec_categorie = qt_final[qt_final["Categorie"] == centres_interets]["Ville_clean"].unique()
    coord_villes_filtrees = coord_villes_filtrees[
        coord_villes_filtrees["Ville_clean"].isin(villes_avec_categorie)
    ].copy()

st.sidebar.markdown("---")
st.sidebar.metric("Villes correspondantes", len(coord_villes_filtrees))

if len(coord_villes_filtrees) == 0:
    st.warning("Aucune ville ne correspond à vos critères. Veuillez ajuster vos filtres.")
    st.stop()

# si la ville sélectionnée n'est plus visible après filtrage
if st.session_state.selected_city not in coord_villes_filtrees["Ville_clean"].tolist():
    st.session_state.selected_city = None
    st.session_state.generated_program = ""
    st.session_state.chat_history = []
    st.session_state.last_chat_signature = None

# ============================================================
# GEMINI CHATBOT
# ============================================================

@st.cache_resource(show_spinner=False)
def get_gemini_client():
    api_key = "AIzaSyCEWK-pVvtvaU1817yCKO17ttmT67H3NKM"
    if not api_key:
        return None
    return genai.Client(api_key=api_key)

def get_itinerary_context_for_city(ville: str) -> str:
    info = st.session_state.itinerary_by_city.get(ville)
    if not info:
        return "Aucun itinéraire calculé pour cette ville dans la session actuelle."

    tgv_row = info.get("tgvmax_row")
    journey = info.get("journey")
    gare_depart = info.get("gare_depart")

    if not tgv_row:
        return "Aucun train TGV Max retenu pour cette ville dans la session actuelle."

    parts = [
        f"Train TGV Max retenu : n° {tgv_row.get('TRAIN_NO', '?')}",
        f"Départ prévu : {hhmm(tgv_row.get('Heure_depart'))}",
        f"Arrivée prévue : {hhmm(tgv_row.get('Heure_arrivee'))}",
    ]

    if gare_depart:
        parts.append(f"Gare parisienne estimée : {gare_depart}")

    if journey:
        parts.append(f"Correspondances Navitia : {int(journey.get('nb_transfers', 0) or 0)}")
        parts.append(f"Durée Navitia : {format_duration(int(journey.get('duration', 0)))}")

    return " | ".join(parts)

def get_city_data_for_llm(ville: str, centre_interet: str):
    ville_qt = qt_final[qt_final["Ville_clean"] == ville].copy()

    if centre_interet != "Tous":
        ville_qt = ville_qt[ville_qt["Categorie"] == centre_interet].copy()

    categories = {}
    for cat in ville_qt["Categorie"].dropna().unique():
        noms = (
            ville_qt[ville_qt["Categorie"] == cat]["Nom du professionnel"]
            .dropna()
            .astype(str)
            .unique()
            .tolist()
        )
        categories[cat] = noms[:10]

    temps_ville = coord_villes[coord_villes["Ville_clean"] == ville]["temps"]
    temps_ville = temps_ville.iloc[0] if not temps_ville.empty else "Non disponible"

    nb_etabs_ville = coord_villes[coord_villes["Ville_clean"] == ville]["nb_etabs"]
    nb_etabs_ville = int(nb_etabs_ville.iloc[0]) if not nb_etabs_ville.empty else 0

    return {
        "ville": ville,
        "temps_trajet_estime_depuis_Paris": temps_ville,
        "nombre_total_etablissements": nb_etabs_ville,
        "centre_interet_selectionne": centre_interet,
        "duree_sejour": duree_sejour,
        "etablissements_par_categorie": categories,
        "itineraire_calcule_pour_cette_ville": get_itinerary_context_for_city(ville),
    }

def build_program_prompt(city_context: dict, chat_history: list[dict] | None = None) -> str:
    history_text = ""
    if chat_history:
        history_text = "\nHistorique de la conversation :\n"
        for msg in chat_history:
            role = "Utilisateur" if msg["role"] == "user" else "Assistant"
            history_text += f"{role} : {msg['content']}\n"

    return f"""
Tu es un assistant de voyage interactif intégré à une application Streamlit.

Règles impératives :
- Tu utilises uniquement les établissements fournis.
- Tu n'inventes jamais de lieux, restaurants, monuments ou activités.
- Tu tiens compte de toute la conversation précédente.
- Si l'utilisateur demande une modification, tu modifies le programme précédent au lieu de repartir de zéro.
- Si les données sont insuffisantes, tu le dis clairement.
- Tu écris en français clair.

Contexte disponible :
{city_context}

{history_text}

Consigne :
- Si c'est le début de la conversation, propose un programme complet.
- Si la conversation a déjà commencé, adapte le dernier programme en tenant compte de la dernière demande utilisateur.
- Ne repars pas de zéro si l'utilisateur demande juste un ajustement.
- Si un itinéraire calculé est disponible, tu peux t'en servir comme contexte pratique, mais sans inventer de détails non fournis.

Structure attendue :
- 2 ou 3 phrases d'introduction expliquant pourquoi la ville convient
- programme détaillé selon la durée :
  * Journée : Matin / Après-midi / Soir si pertinent
  * Weekend : Jour 1 / Jour 2
  * Semaine : Jour 1 à Jour 7
- finir par "Conseil pratique"
"""

def generate_program_with_gemini(ville: str, centre_interet: str, chat_history: list[dict] | None = None) -> str:
    client = get_gemini_client()
    if client is None:
        return (
            "Clé Gemini absente. Ajoute GEMINI_API_KEY dans tes variables d'environnement "
            "ou dans .streamlit/secrets.toml."
        )

    city_context = get_city_data_for_llm(ville, centre_interet)

    if centre_interet != "Tous" and len(city_context["etablissements_par_categorie"]) == 0:
        return (
            f"Aucun établissement correspondant à la catégorie '{centre_interet}' "
            f"n'est disponible pour {ville} dans les données."
        )

    prompt = build_program_prompt(city_context, chat_history=chat_history)

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )
        return response.text
    except Exception as e:
        return f"Erreur Gemini : {e}"

# ============================================================
# MAP
# ============================================================

with st.spinner("Création de la carte en cours..."):
    m = folium.Map(location=[48.8566, 2.3522], zoom_start=6)

    for _, row in coord_villes_filtrees.iterrows():
        popup_html = f"""
        <b>{row['Ville_clean']}</b><br>
        <b>Temps de trajet :</b> {row['temps']}<br>
        <b>Nombre d'établissements :</b> {row['nb_etabs']}<br><br>
        <b>Établissements Qualité Tourisme :</b><br><br>
        {row['etabs']}
        """
        folium.CircleMarker(
            location=[row["lat"], row["lon"]],
            radius=float(row["radius"]),
            color=color_by_time(int(row["minutes"])),
            fill=True,
            fill_opacity=0.8,
            popup=folium.Popup(popup_html, max_width=350),
            tooltip=row["Ville_clean"],
        ).add_to(m)

    folium.Marker(
        [48.8566, 2.3522],
        tooltip="PARIS - Point de départ",
        popup="Point de départ : Paris",
        icon=folium.Icon(color="blue", icon="info-sign"),
    ).add_to(m)

col_carte, col_info = st.columns([3, 1])

with col_carte:
    st.info("Clique sur une ville de la carte pour générer un programme personnalisé.")
    map_data = st_folium(
        m,
        width=900,
        height=700,
        returned_objects=["last_object_clicked_tooltip"]
    )

    clicked_city = None
    if map_data and map_data.get("last_object_clicked_tooltip"):
        clicked_city = map_data["last_object_clicked_tooltip"]
        if clicked_city == "PARIS - Point de départ":
            clicked_city = None
        if clicked_city not in coord_villes_filtrees["Ville_clean"].tolist():
            clicked_city = None

    if clicked_city and clicked_city != st.session_state.selected_city:
        st.session_state.selected_city = clicked_city
        st.session_state.generated_program = ""
        st.session_state.chat_history = []
        st.session_state.last_chat_signature = None

    st.markdown("### Temps de trajet (couleurs)")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    with c1:
        st.markdown("🟢 < 1h")
    with c2:
        st.markdown("🟩 1–2h")
    with c3:
        st.markdown("🟡 2–3h")
    with c4:
        st.markdown("🟠 3–4h")
    with c5:
        st.markdown("🟥 4–6h")
    with c6:
        st.markdown("🟤 > 6h")

    st.markdown("La taille des cercles est proportionnelle au nombre d'établissements Qualité Tourisme.")

with col_info:
    st.subheader("Informations")
    st.markdown("### Label Qualité Tourisme")
    st.markdown(
        """
Le label Qualité Tourisme™ est la seule marque d'État attribuée aux professionnels du tourisme
pour la qualité de leur accueil et de leurs prestations.
"""
    )
    st.markdown("---")
    st.markdown("### TGVmax")
    st.markdown(
        """
Le calculateur ci-dessous utilise le fichier local TGV Max pour trouver un train disponible
et l'API Navitia pour enrichir le trajet quand c'est possible.
"""
    )

# ============================================================
# CHATBOT SECTION
# ============================================================

st.markdown("---")
st.header("Assistant voyage")

if st.session_state.selected_city is None:
    st.info("Clique sur une ville de la carte pour lancer l'assistant.")
else:
    st.success(f"Ville sélectionnée : {st.session_state.selected_city}")

    chat_signature = (
        st.session_state.selected_city,
        centres_interets,
        duree_sejour,
        temps_max,
    )

    if (
        st.session_state.generated_program == ""
        or st.session_state.last_chat_signature != chat_signature
    ):
        with st.spinner("Génération du programme..."):
            program = generate_program_with_gemini(
                st.session_state.selected_city,
                centres_interets,
                chat_history=[]
            )
            st.session_state.generated_program = program
            st.session_state.chat_history = [{"role": "assistant", "content": program}]
            st.session_state.last_chat_signature = chat_signature

    col_chat, col_tools = st.columns([2, 1])

    with col_chat:
        for msg in st.session_state.chat_history:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

    with col_tools:
        st.write(f"Durée : {duree_sejour}")
        st.write(f"Centre d'intérêt : {centres_interets}")

        if st.button("Régénérer", use_container_width=True):
            with st.spinner("Régénération..."):
                program = generate_program_with_gemini(
                    st.session_state.selected_city,
                    centres_interets,
                    chat_history=[]
                )
                st.session_state.generated_program = program
                st.session_state.chat_history = [{"role": "assistant", "content": program}]
                st.session_state.last_chat_signature = chat_signature
            st.rerun()

        if st.button("Repartir de zéro", use_container_width=True):
            st.session_state.generated_program = ""
            st.session_state.chat_history = []
            st.session_state.last_chat_signature = None
            st.rerun()

    user_msg = st.chat_input("Exemple : rends le programme plus calme, plus culturel, ou ajoute plus de restauration")

    if user_msg:
        st.session_state.chat_history.append({"role": "user", "content": user_msg})
        reply = generate_program_with_gemini(
            st.session_state.selected_city,
            centres_interets,
            chat_history=st.session_state.chat_history
        )
        st.session_state.generated_program = reply
        st.session_state.chat_history.append({"role": "assistant", "content": reply})
        st.rerun()

# ============================================================
# NAVITIA — SESSION + API
# ============================================================

@st.cache_resource(show_spinner=False)
def navitia_session():
    sess = requests.Session()
    if NAVITIA_TOKEN:
        sess.auth = (NAVITIA_TOKEN, "")
    return sess

def navitia_get(path: str, params: dict) -> dict:
    if not NAVITIA_TOKEN:
        raise ValueError("NAVITIA_TOKEN absent.")
    url = f"{BASE_URL}{path}"
    r = navitia_session().get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()

@st.cache_data(show_spinner=False)
def find_places(query: str, limit: int = 10) -> list[dict]:
    data = navitia_get("/places", params={"q": query, "count": limit})
    results = []
    for p in data.get("places", []):
        embedded_type = p.get("embedded_type")
        place = p.get("place") or (p.get(embedded_type) if embedded_type else {}) or {}
        pid = place.get("id")
        name = place.get("name")
        label = place.get("label") or name or pid or "?"
        results.append({"id": pid, "name": name, "label": label, "type": embedded_type})
    return results

@st.cache_data(show_spinner=False)
def get_stop_area(stop_area_id: str) -> dict:
    return navitia_get(f"/stop_areas/{stop_area_id}", params={})

def is_rail_stop_area(stop_area_id: str) -> bool:
    try:
        data = get_stop_area(stop_area_id)
        sa = data.get("stop_area", {}) if isinstance(data, dict) else {}

        phys = sa.get("physical_modes", []) or []
        comm = sa.get("commercial_modes", []) or []

        phys_ids = {m.get("id") for m in phys if isinstance(m, dict) and m.get("id")}
        comm_ids = {m.get("id") for m in comm if isinstance(m, dict) and m.get("id")}

        if "physical_mode:Train" in phys_ids:
            return True

        rail_comm = {
            "commercial_mode:TGV",
            "commercial_mode:TER",
            "commercial_mode:INTERCITES",
            "commercial_mode:TRANSILIEN",
            "commercial_mode:OUIGO",
        }
        if comm_ids & rail_comm:
            return True

        name = normalize_str(sa.get("name", "") or "")
        if "GARE" in name and all(x not in name for x in ["HAUSSMANN", "MAGENTA", "CHATELET", "LES HALLES"]):
            return True

        return False
    except Exception:
        return True

@st.cache_data(show_spinner=False)
def resolve_city_to_stop_area_id(city: str):
    city = normalize_special(city)
    for q in [f"{city} gare", f"{city} station", f"{city}"]:
        results = find_places(q, limit=10)
        stop_areas = [r for r in results if r.get("type") == "stop_area" and r.get("id")]
        if stop_areas:
            return stop_areas[0]["id"]
        if results and results[0].get("id"):
            return results[0]["id"]
    return None

@st.cache_data(show_spinner=False)
def resolve_city_to_rail_stop_area_id(city: str):
    city = normalize_special(city)
    for q in [f"{city} gare", f"{city}"]:
        results = find_places(q, limit=12)
        stop_areas = [r for r in results if r.get("type") == "stop_area" and r.get("id")]
        stop_areas = sorted(stop_areas, key=lambda r: (0 if "GARE" in normalize_str(r.get("label", "")) else 1))

        rail_only = [r for r in stop_areas if is_rail_stop_area(r["id"])]
        if rail_only:
            return rail_only[0]["id"]
        if stop_areas:
            return stop_areas[0]["id"]
    return None

@st.cache_data(show_spinner=False)
def compute_journeys_rail_only(from_id: str, to_id: str, dt: str | None, count: int = 2) -> dict:
    params = {
        "from": from_id,
        "to": to_id,
        "count": count,
        "first_section_mode[]": ["walking"],
        "last_section_mode[]": ["walking"],
        "forbidden_uris[]": [
            "physical_mode:Metro",
            "physical_mode:RapidTransit",
            "physical_mode:Tramway",
            "physical_mode:Bus",
            "physical_mode:Coach",
        ],
    }
    if dt:
        params["datetime"] = dt
        params["datetime_represents"] = "departure"
    return navitia_get("/journeys", params=params)

def journey_train_sections_only(journey: dict) -> list[dict]:
    return [s for s in journey.get("sections", []) if s.get("type") == "public_transport"]

PARIS_MAJOR_STATIONS = [
    "Paris Gare du Nord",
    "Paris Gare de Lyon",
    "Paris Montparnasse",
    "Paris Gare de l'Est",
    "Paris Austerlitz",
    "Paris Saint-Lazare",
    "Paris Bercy",
]

PARIS_PRIORITY_ORDER = [
    "Paris Gare du Nord",
    "Paris Gare de Lyon",
    "Paris Montparnasse",
    "Paris Gare de l'Est",
    "Paris Austerlitz",
    "Paris Saint-Lazare",
    "Paris Bercy",
]

@st.cache_data(show_spinner=False)
def resolve_paris_stations_rail() -> dict:
    mapping = {}
    for label in PARIS_MAJOR_STATIONS:
        sid = resolve_city_to_rail_stop_area_id(label)
        if not sid:
            sid = resolve_city_to_stop_area_id(label)
        if sid:
            mapping[label] = sid
    return {k: mapping[k] for k in PARIS_PRIORITY_ORDER if k in mapping}

def score_journey_rail(journey: dict) -> int:
    if not journey_train_sections_only(journey):
        return 10**9
    dur_min = int(journey.get("duration", 0)) // 60
    transfers = int(journey.get("nb_transfers", 0) or 0)
    return dur_min + transfers * 12

@st.cache_data(show_spinner=False)
def best_journey_from_best_paris_station(dest_stop_area_id: str, paris_map: dict, dt_navitia: str | None):
    best_j = None
    best_label = None
    best_score = 10**9

    for label, from_id in paris_map.items():
        try:
            data = compute_journeys_rail_only(from_id, dest_stop_area_id, dt_navitia, count=2)
            journeys = data.get("journeys", []) or []
            if not journeys:
                continue

            for j in journeys[:2]:
                sc = score_journey_rail(j)
                if sc < best_score:
                    best_score = sc
                    best_j = j
                    best_label = label

                    nb_t = int(j.get("nb_transfers", 0) or 0)
                    dur_min = int(j.get("duration", 0)) // 60
                    if nb_t <= 1 and dur_min <= 180:
                        return best_j, best_label, best_score
        except Exception:
            continue

    return best_j, best_label, best_score

def expand_special_destinations(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    expanded_rows = []
    for _, row in df.iterrows():
        dest = row["Destination_clean"]
        if dest == "BELFORT MONTBELIARD":
            for ville in ["BELFORT", "MONTBELIARD"]:
                new_row = row.copy()
                new_row["Destination_clean"] = ville
                expanded_rows.append(new_row)
        elif dest == "BLOIS CHAMBORD":
            for ville in ["BLOIS", "CHAMBORD"]:
                new_row = row.copy()
                new_row["Destination_clean"] = ville
                expanded_rows.append(new_row)
        else:
            expanded_rows.append(row)

    return pd.DataFrame(expanded_rows)

def pick_closest_tgvmax_train(df_prepared: pd.DataFrame, ville: str, selected_date: date, selected_time):
    target_date = pd.Timestamp(selected_date)

    subset = df_prepared[
        (df_prepared["Origine_clean"].str.contains("PARIS", na=False)) &
        (df_prepared["Destination_clean"] == normalize_special(ville)) &
        (df_prepared["MAX_OK"]) &
        (df_prepared["DATE_norm"] == target_date)
    ].copy()

    if subset.empty:
        return None

    subset["gap_min"] = subset["Heure_depart_obj"].apply(lambda t: minutes_diff_after(selected_time, t))
    subset = subset[subset["gap_min"] < 10**9].copy()

    if subset.empty:
        return None

    subset = subset.sort_values(["gap_min", "Heure_depart_obj"])
    return subset.iloc[0].to_dict()

# ============================================================
# ITINERAIRE UI
# ============================================================

st.markdown("---")
st.header("Calculateur d’itinéraires TGV Max")
st.caption(
    "Le calculateur utilise le fichier local TGV Max pour repérer un train disponible, "
    "puis Navitia pour enrichir le trajet ferroviaire."
)

if not NAVITIA_TOKEN:
    st.warning("NAVITIA_TOKEN absent. La carte et le chatbot fonctionnent, mais le calculateur Navitia est désactivé.")

colA, colB, colC = st.columns([1, 1, 2])
with colA:
    depart_date = st.date_input("Date de départ", value=date.today())
with colB:
    depart_time = st.time_input("Heure de départ", value=datetime.now().time().replace(second=0, microsecond=0))
with colC:
    max_villes = st.slider(
        "Nombre de destinations à calculer",
        1,
        min(20, max(1, len(coord_villes_filtrees))),
        min(10, max(1, len(coord_villes_filtrees)))
    )

villes_options = coord_villes_filtrees["Ville_clean"].sort_values().tolist()
selected_villes = st.multiselect(
    "Destinations",
    options=villes_options,
    default=villes_options[:max_villes],
)
selected_villes = selected_villes[:max_villes]

run = st.button("Lancer le calcul d’itinéraires", type="primary", disabled=not bool(NAVITIA_TOKEN))

if run and NAVITIA_TOKEN:
    if not selected_villes:
        st.warning("Sélectionnez au moins une destination.")
    else:
        with st.spinner("Recherche des trains TGV Max + enrichissement Navitia..."):
            tgvmax_search = expand_special_destinations(tgvmax)
            paris_map = resolve_paris_stations_rail()

            if not paris_map:
                st.error("Impossible de résoudre les grandes gares parisiennes via Navitia.")
            else:
                rows = []
                best_by_city = {}
                dest_id_cache = {v: resolve_city_to_rail_stop_area_id(v) for v in selected_villes}

                for ville in selected_villes:
                    best_tgvmax = pick_closest_tgvmax_train(
                        tgvmax_search,
                        ville=ville,
                        selected_date=depart_date,
                        selected_time=depart_time,
                    )

                    if not best_tgvmax:
                        rows.append({
                            "Ville": ville,
                            "Statut": "Aucun TGV Max après cette heure",
                            "Gare de départ": "",
                            "Départ TGV Max": "",
                            "Arrivée TGV Max": "",
                            "Durée TGV Max": "",
                            "Train": "",
                            "Correspondances": "",
                        })
                        best_by_city[ville] = {
                            "tgvmax_row": None,
                            "journey": None,
                            "gare_depart": None,
                        }
                        continue

                    to_id = dest_id_cache.get(ville)
                    best_journey = None
                    best_station_label = None

                    train_dep_time = best_tgvmax.get("Heure_depart_obj")
                    if to_id and train_dep_time:
                        nav_dt = navitia_datetime(datetime.combine(depart_date, train_dep_time))
                        best_journey, best_station_label, _ = best_journey_from_best_paris_station(
                            to_id, paris_map, nav_dt
                        )

                    dep_txt = hhmm(best_tgvmax.get("Heure_depart"))
                    arr_txt = hhmm(best_tgvmax.get("Heure_arrivee"))

                    duree_txt = ""
                    try:
                        dep_td = to_timedelta_hhmm(best_tgvmax.get("Heure_depart"))
                        arr_td = to_timedelta_hhmm(best_tgvmax.get("Heure_arrivee"))
                        if pd.notna(dep_td) and pd.notna(arr_td):
                            if arr_td < dep_td:
                                arr_td += pd.Timedelta(days=1)
                            duree_txt = format_timedelta(arr_td - dep_td)
                    except Exception:
                        duree_txt = ""

                    best_by_city[ville] = {
                        "tgvmax_row": best_tgvmax,
                        "journey": best_journey,
                        "gare_depart": best_station_label,
                    }

                    rows.append({
                        "Ville": ville,
                        "Statut": "OK",
                        "Gare de départ": best_station_label or "Non déterminée",
                        "Départ TGV Max": dep_txt,
                        "Arrivée TGV Max": arr_txt,
                        "Durée TGV Max": duree_txt,
                        "Train": best_tgvmax.get("TRAIN_NO", ""),
                        "Correspondances": int(best_journey.get("nb_transfers", 0) or 0) if best_journey else "",
                    })

                st.session_state.itinerary_rows = rows
                st.session_state.itinerary_by_city = best_by_city

# afficher les derniers résultats calculés
if st.session_state.itinerary_rows:
    df_it = pd.DataFrame(st.session_state.itinerary_rows)
    st.subheader("Résumé")
    st.dataframe(df_it, use_container_width=True)

    st.subheader("Détails")
    for ville in df_it["Ville"].tolist():
        info = st.session_state.itinerary_by_city.get(ville, {})
        tgv_row = info.get("tgvmax_row")
        journey = info.get("journey")
        gare_label = info.get("gare_depart")

        with st.expander(f"Itinéraire retenu pour {ville}"):
            if not tgv_row:
                st.info("Aucun train TGV Max retenu pour cette ville avec les paramètres choisis.")
                continue

            st.markdown(
                f"Train TGV Max retenu — n° {tgv_row.get('TRAIN_NO', '?')} — "
                f"{hhmm(tgv_row.get('Heure_depart'))} → {hhmm(tgv_row.get('Heure_arrivee'))}"
            )

            if "DATE" in tgv_row:
                st.markdown(f"Date : {tgv_row.get('DATE', '')}")

            if gare_label:
                st.markdown(f"Gare parisienne estimée : {gare_label}")

            if not journey:
                st.info("Train TGV Max trouvé, mais itinéraire détaillé Navitia indisponible pour cette destination.")
                continue

            dep = pretty_time(journey.get("departure_date_time"))
            arr = pretty_time(journey.get("arrival_date_time"))
            duree = format_duration(int(journey.get("duration", 0)))
            nb_transfers = int(journey.get("nb_transfers", 0) or 0)

            st.markdown(
                f"Itinéraire Navitia le plus proche — {dep} → {arr} — {duree} — "
                f"{nb_transfers} correspondance(s) — départ depuis {gare_label}"
            )

            for sec in journey_train_sections_only(journey):
                info_sec = sec.get("display_informations", {}) or {}
                s_dep = pretty_time(sec.get("departure_date_time"))
                s_arr = pretty_time(sec.get("arrival_date_time"))
                st.write(
                    f"{s_dep} → {s_arr} | "
                    f"{info_sec.get('label', 'Transport')} → {info_sec.get('direction', '')} | "
                    f"{sec.get('from', {}).get('name', '?')} → {sec.get('to', {}).get('name', '?')}"
                )

# ============================================================
# TABLE DETAILS + DOWNLOAD
# ============================================================

with st.expander("Voir la liste détaillée des villes (vous pouvez télécharger votre tableau personnalisé)"):
    villes_detail_data = []
    for _, row in coord_villes_filtrees.iterrows():
        ville = row["Ville_clean"]
        temps_trajet = row["temps"]
        nb_etabs = int(row["nb_etabs"])

        etabs_ville = qt_final[qt_final["Ville_clean"] == ville]
        etablissements_html = ""
        for cat in etabs_ville["Categorie"].dropna().unique():
            cat_data = etabs_ville[etabs_ville["Categorie"] == cat]
            etablissements_html += f"<b>{cat}</b><br>"
            for nom in cat_data["Nom du professionnel"].fillna(""):
                etablissements_html += f"• {nom}<br>"
            etablissements_html += "<br>"

        villes_detail_data.append({
            "Ville": ville,
            "Temps de trajet": temps_trajet,
            "Nb établissements": nb_etabs,
            "Établissements labellisés": etablissements_html if etablissements_html else "Aucun",
        })

    villes_detail = pd.DataFrame(villes_detail_data)
    if not villes_detail.empty:
        villes_detail = villes_detail.sort_values("Temps de trajet")

    villes_detail_csv = villes_detail.copy()
    if not villes_detail_csv.empty:
        villes_detail_csv["Établissements labellisés"] = (
            villes_detail_csv["Établissements labellisés"]
            .astype(str)
            .str.replace("<br>", "\n", regex=False)
            .str.replace("<b>", "", regex=False)
            .str.replace("</b>", "", regex=False)
            .str.replace("• ", "", regex=False)
        )

    csv = villes_detail_csv.to_csv(index=False, encoding="utf-8-sig")
    st.download_button(
        label="Télécharger le tableau en CSV",
        data=csv,
        file_name="vos_destinations_labellisees.csv",
        mime="text/csv",
    )

    st.markdown("---")
    st.markdown(
        """
        <style>
        table { width: 100%; }
        th { text-align: left !important; }
        td { text-align: left !important; vertical-align: top !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(villes_detail.to_html(escape=False, index=False), unsafe_allow_html=True)

# ============================================================
# EXPLICATIONS
# ============================================================

st.subheader("À propos des filtres possibles")
st.markdown(
    """
- Durée du séjour :
Permet de filtrer les villes en fonction du nombre minimum d'établissements labellisés Qualité Tourisme.

- Journée : toutes les villes conviennent
- Weekend : villes avec au moins 3 lieux labellisés
- Semaine : villes avec au moins 5 lieux labellisés

- Temps de voyage maximum :
Permet de sélectionner les villes en fonction du temps de trajet depuis Paris.

- Centres d'intérêts :
Permet de choisir les catégories d'établissements qui vous intéressent le plus.
"""
)

st.markdown("---")
st.markdown("Note : cette page a été mise au point par des étudiants dans le cadre d'un projet.")