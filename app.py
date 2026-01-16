# ----------------------------------------
# Importation des librairies
# ----------------------------------------
import streamlit as st
import pandas as pd
import folium
from streamlit_folium import st_folium
import re
import os

# ----------------------------------------
# Configuration Streamlit
# ----------------------------------------
st.set_page_config(
    page_title="Carte interactive des villes TGVmax",
    layout="wide"
)

st.title("Carte interactive des destinations labellisées Qualité Tourisme accessibles depuis Paris avec TGVmax")
st.markdown(
    """
    Cette carte présente les villes accessibles depuis Paris pour les détenteurs de l'abonnement TGVmax ainsi que les établissements labellisés Qualité Tourisme s'y situant.
    """
)

# ----------------------------------------
# Vérification des fichiers
# ----------------------------------------
fichiers_requis = ["tgvmax.csv.gz", "etablissements-labellises-qualite-tourisme.csv", "coord_villes.csv"]
fichiers_manquants = [f for f in fichiers_requis if not os.path.exists(f)]

if fichiers_manquants:
    st.error(f"⚠️ Fichiers manquants : {', '.join(fichiers_manquants)}")
    st.info("Assurez-vous que les fichiers CSV sont dans le même dossier que ce script.")
    st.stop()

# ----------------------------------------
# Lecture des fichiers CSV avec gestion d'erreurs
# ----------------------------------------
try:
    tgvmax=pd.read_csv("tgvmax.csv.gz", sep=';', dtype=str, encoding='utf-8', compression='gzip')
    qualite = pd.read_csv("etablissements-labellises-qualite-tourisme.csv", sep=';', dtype=str, encoding='utf-8')
    coords = pd.read_csv("coord_villes.csv", encoding='utf-8')
except Exception as e:
    st.error(f"Erreur lors de la lecture des fichiers : {str(e)}")
    st.stop()

# ----------------------------------------
# Filtrer les destinations depuis Paris
# ----------------------------------------
tgv_paris = tgvmax[
    (tgvmax["Origine"] == "PARIS (intramuros)") &
    (tgvmax["Disponibilité de places MAX JEUNE et MAX SENIOR"] == "OUI")
].copy()

tgv_paris["Destination_clean"] = tgv_paris["Destination"].str.upper().str.strip()

# ----------------------------------------
# Nettoyage des noms de destinations
# ----------------------------------------
def clean_city(x):
    if pd.isna(x):
        return None
    x = x.upper()
    x = re.sub(r"\s*(TGV|CENTRE|VILLE|MIDI|MATABIAU|CHANTIERS|INTRAMUROS)\s*", "", x)
    x = re.sub(r"\bSAINT\b", "ST", x)
    x = re.sub(r"\bSAINTE\b", "STE", x)
    x = re.sub(r"[()]", "", x)
    return x.strip()

tgv_paris["Destination_clean"] = tgv_paris["Destination_clean"].apply(clean_city)

special = {
    "ST RAPHAEL": "SAINT-RAPHAEL",
    "AIX EN PROVENCE": "AIX-EN-PROVENCE",
    "CHALON SUR SAONE": "CHALON-SUR-SAONE",
    "MARSEILLE ST CHARLES": "MARSEILLE"
}
tgv_paris["Destination_clean"] = tgv_paris["Destination_clean"].replace(special)

expanded_rows = []
for _, row in tgv_paris.iterrows():
    if row["Destination_clean"] == "BELFORT MONTBELIARD":
        for ville in ["BELFORT", "MONTBELIARD"]:
            new_row = row.copy()
            new_row["Destination_clean"] = ville
            expanded_rows.append(new_row)
    elif row["Destination_clean"] == "BLOIS CHAMBORD":
        for ville in ["BLOIS", "CHAMBORD"]:
            new_row = row.copy()
            new_row["Destination_clean"] = ville
            expanded_rows.append(new_row)
    else:
        expanded_rows.append(row)

tgv_paris = pd.DataFrame(expanded_rows)

# ----------------------------------------
# Calcul du temps de trajet
# ----------------------------------------
tgv_paris["Heure_depart"] = pd.to_datetime(tgv_paris["Heure_depart"], format="%H:%M", errors="coerce")
tgv_paris["Heure_arrivee"] = pd.to_datetime(tgv_paris["Heure_arrivee"], format="%H:%M", errors="coerce")

def compute_duration(row):
    dep = row["Heure_depart"]
    arr = row["Heure_arrivee"]
    if pd.isna(dep) or pd.isna(arr):
        return None
    if arr < dep:
        arr += pd.Timedelta(days=1)
    return arr - dep

tgv_paris["Temps_trajet"] = tgv_paris.apply(compute_duration, axis=1)

# ----------------------------------------
# Nettoyage Qualité Tourisme
# ----------------------------------------
qualite["Ville_clean"] = qualite["Ville"].str.upper().str.strip()
qualite["Ville_clean"] = qualite["Ville_clean"].replace(special)

# ----------------------------------------
# Regroupement des secteurs
# ----------------------------------------
mapping_secteurs = {
    "HOTEL": "Hébergement",
    "HÔTEL": "Hébergement",
    "HÔTEL-RESTAURANT": "Hébergement",
    "CAMPING": "Hébergement",
    "CHAMBRE D'HOTES": "Hébergement",
    "CHAMBRE D'HÔTES": "Hébergement",
    "VILLAGE DE VACANCES": "Hébergement",
    "RÉSIDENCE DE TOURISME": "Hébergement",
    "HÉBERGEMENT COLLECTIF": "Hébergement",
    "OFFICE DE TOURISME": "Services touristiques",
    "RESTAURANT": "Restauration",
    "RESTAURANT DE PLAGE": "Restauration",
    "CAFÉ, BAR, BRASSERIE": "Restauration",
    "PARC DE LOISIR": "Loisirs & activités",
    "SPORT DE NATURE": "Loisirs & activités",
    "SORTIE NATURE": "Loisirs & activités",
    "ETABLISSEMENT DE LOISIR": "Loisirs & activités",
    "ÉTABLISSEMENT DE LOISIR": "Loisirs & activités",
    "PARC DE LOISIR": "Loisirs & activités",
    "VTC - LIMOUSINE": "Loisirs & activités",
    "AGENCE DE LOCATIONS SAISONNIÈRES": "Loisirs & activités",
    "SÉMINAIRE": "Loisirs & activités",
    "PORT DE PLAISANCE": "Loisirs & activités",
    "PARC A THEME": "Loisirs & activités",
    "LIEU DE VISITE": "Culture & patrimoine",
    "SITE DE MÉMOIRE": "Culture & patrimoine",
    "CAVEAUX ET POINTS DE VENTE": "Culture & patrimoine",
    "VISITE D'ENTREPRISE": "Culture & patrimoine",
    "COMMERCE": "Culture & patrimoine",
    "ECOMUSÉE": "Culture & patrimoine",
    "VISITE GUIDEE": "Culture & patrimoine",
    "MAISON D'ÉCRIVAIN" : "Culture & patrimoine",
    "SITE DE PRÉHISTOIRE": "Culture & patrimoine"}

qualite["Categorie"] = (
    qualite["Activité du professionnel"]
    .str.upper()
    .str.strip()
    .map(mapping_secteurs)
    .fillna("Autres")
)

# ----------------------------------------
# Filtrage villes communes
# ----------------------------------------
villes_finales = sorted(
    set(tgv_paris["Destination_clean"]) &
    set(qualite["Ville_clean"])
)

qt_final = qualite[qualite["Ville_clean"].isin(villes_finales)]
tgv_final = tgv_paris[tgv_paris["Destination_clean"].isin(villes_finales)]

# ----------------------------------------
# Préparation données popup
# ----------------------------------------
def format_etabs(ville):
    ville_data = qt_final[qt_final["Ville_clean"] == ville]
    html = ""
    for cat in ville_data["Categorie"].unique():
        cat_data = ville_data[ville_data["Categorie"] == cat]
        html += f"<b>{cat}</b><br>"
        for nom in cat_data["Nom du professionnel"]:
            html += f"• {nom}<br>"
        html += "<br>"
    return html

etabs = {ville: format_etabs(ville) for ville in qt_final["Ville_clean"].unique()}
temps = tgv_final.groupby("Destination_clean")["Temps_trajet"].first().to_dict()

def format_timedelta(td):
    if pd.isna(td):
        return "Non disponible"
    total_minutes = int(td.total_seconds() // 60)
    return f"{total_minutes//60}h {total_minutes%60:02d}min"

# ----------------------------------------
# Préparation coordonnées
# ----------------------------------------
coords["Ville"] = coords["Ville"].str.upper()
coord_villes = coords[coords["Ville"].isin(villes_finales)].copy()

# Ajouter le temps de trajet et les établissements
coord_villes["temps"] = coord_villes["Ville"].apply(lambda v: format_timedelta(temps.get(v)))
coord_villes["etabs"] = coord_villes["Ville"].apply(lambda v: etabs.get(v, "Aucun établissement"))

# ----------------------------------------
# Couleurs et taille
# ----------------------------------------
def time_to_minutes(t):
    if t == "Non disponible":
        return 0
    h, m = t.split("h")
    return int(h)*60 + int(m.replace("min",""))

coord_villes["minutes"] = coord_villes["temps"].apply(time_to_minutes)
coord_villes["nb_etabs"] = coord_villes["Ville"].apply(lambda v: len(qt_final[qt_final["Ville_clean"] == v]))
coord_villes["radius"] = coord_villes["nb_etabs"].apply(lambda x: 4 + x*2)

def color_by_time(m):
    if m < 60: return "#006400"
    elif m < 120: return "#32CD32"
    elif m < 180: return "#FFFF00"
    elif m < 240: return "#FFA500"
    elif m < 360: return "#FF6347"
    else: return "#8B0000"

# ----------------------------------------
# FILTRES UTILISATEUR
# ----------------------------------------
st.sidebar.header("Personnalisez votre voyage")

# Filtre 1: Durée du séjour
duree_sejour = st.sidebar.radio(
    "*Combien de temps restez-vous sur place ?*",
    options=["Journée", "Weekend", "Semaine"],
    index=0
)

# Définir le nombre minimum d'établissements selon la durée
min_etabs = 0
if duree_sejour == "Journée":
    min_etabs = 0
elif duree_sejour == "Weekend":
    min_etabs = 3
elif duree_sejour == "Semaine":
    min_etabs = 5

# Filtre 2: Temps de voyage maximum
temps_max = st.sidebar.selectbox(
    "*Quel temps de trajet maximum depuis Paris acceptez-vous?*",
    options=[
        "Toutes les durées",
        "Moins d'1h",
        "Moins de 2h",
        "Moins de 3h",
        "Moins de 4h",
        "Moins de 6h",
        "Plus de 6h"
    ],
    index=0
)

# Filtre 3: vos centres d'intérêts
centres_interets = st.sidebar.radio(
    "*Quels sont vos principaux centres d'intérêts ?*",
    options=["Tous","Loisirs & activités", "Culture & patrimoine", "Restauration", "Hébergement", "Services touristiques"],
    index=0
)

# Convertir le temps max en minutes
temps_max_minutes = float('inf')
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

# ----------------------------------------
# Application des filtres
# ----------------------------------------
if temps_max == "Plus de 6h":
    coord_villes_filtrees = coord_villes[
        (coord_villes["nb_etabs"] >= min_etabs) &
        (coord_villes["minutes"] >= 360)
    ].copy()
else:
    coord_villes_filtrees = coord_villes[
        (coord_villes["nb_etabs"] >= min_etabs) &
        (coord_villes["minutes"] <= temps_max_minutes)
    ].copy()

if centres_interets != "Tous":
    villes_avec_categorie = qt_final[qt_final["Categorie"] == centres_interets]["Ville_clean"].unique()
    coord_villes_filtrees = coord_villes_filtrees[
        coord_villes_filtrees["Ville"].isin(villes_avec_categorie)
    ].copy()

# ----------------------------------------
# Affichage du nombre de villes correspondantes
# ----------------------------------------
st.sidebar.markdown("---")
st.sidebar.metric(
    "Villes correspondantes",
    len(coord_villes_filtrees),
    delta=f"{len(coord_villes_filtrees) - len(coord_villes)} villes" if len(coord_villes_filtrees) != len(coord_villes) else None
)

if len(coord_villes_filtrees) == 0:
    st.warning("Aucune ville ne correspond à vos critères. Veuillez ajuster vos filtres.")
    st.stop()

# ----------------------------------------
# Création de la carte
# ----------------------------------------
with st.spinner('Création de la carte en cours...'):
    m = folium.Map(location=[48.8566, 2.3522], zoom_start=6)

    # Ajout des marqueurs pour chaque ville filtrée
    for _, row in coord_villes_filtrees.iterrows():
        popup_html = f"""
        <b>{row['Ville']}</b><br>
        <b>Temps de trajet :</b> {row['temps']}<br>
        <b>Nombre d'établissements :</b> {row['nb_etabs']}<br><br>
        <b>Établissements Qualité Tourisme :</b><br><br>
        {row['etabs']}
        """
        folium.CircleMarker(
            location=[row["lat"], row["lon"]],
            radius=row["radius"],
            color=color_by_time(row["minutes"]),
            fill=True,
            fill_opacity=0.8,
            popup=folium.Popup(popup_html, max_width=350),
            tooltip=f"{row['Ville']} - {row['temps']}"
        ).add_to(m)

    # Marker Paris
    folium.Marker(
        [48.8566, 2.3522],
        tooltip="PARIS - Point de départ",
        popup="Point de départ : Paris",
        icon=folium.Icon(color="blue", icon="info-sign")
    ).add_to(m)

col_carte, col_info = st.columns([3, 1])

with col_carte:
    st.info("Survolez et cliquez sur les cercles pour voir les détails de chaque destination")
    st_folium(m, width=900, height=700, returned_objects=[])
    st.markdown("### Temps de trajet:")
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    with col1:
        st.markdown("🟢 **< 1h**")
    with col2:
        st.markdown("🟢 **1-2h**")
    with col3:
        st.markdown("🟡 **2-3h**")
    with col4:
        st.markdown("🟠 **3-4h**")
    with col5:
        st.markdown("🔴 **4-6h**")
    with col6:
        st.markdown("🔴 **> 6h**")

    st.markdown("**La taille des cercles** est proportionnelle au nombre d'établissements Qualité Tourisme")

    st.subheader("Statistiques des villes affichées")
    col_stat1, col_stat2 = st.columns(2)
    with col_stat1:
        st.metric("Villes affichées", len(coord_villes_filtrees))
    with col_stat2:
        etabs_filtres = qt_final[qt_final["Ville_clean"].isin(coord_villes_filtrees["Ville"])]
        st.metric("Établissements totaux", len(etabs_filtres))

with col_info:
    st.subheader("Informations")
    
    # Label Qualité Tourisme
    st.markdown("### ⭐ Label Qualité Tourisme")
    st.markdown("""
    Le label **Qualité Tourisme™** est la seule marque d'État attribuée aux professionnels du tourisme pour la qualité de leur accueil et de leurs prestations.
    
    **Il garantit :**
    - Un accueil personnalisé
    - Des prestations de qualité
    - Des informations fiables
    - Un personnel qualifié
    """)
    
    st.markdown("---")
    
    # TGVmax
    st.markdown("### 🚄 TGVmax")
    st.markdown("""
    L'abonnement **TGVmax** permet aux jeunes (16-27 ans) et seniors (60+) de voyager en TGV à moindre coût.
    
    **Avantages :**
    - Certains billets gratuits en 2nde classe
    - Billets à tarif réduit en 1ère classe et 2nde classe
    - Nombreuses lignes TGV éligibles
    - Application mobile
    """)
    
    st.markdown("---")
    
    # Sources
    st.markdown("### 📚 Sources")
    st.markdown("""
    - [SNCF TGVmax](https://www.sncf.com)
    - [Qualité Tourisme](https://www.qualite-tourisme.gouv.fr)
    - Données OpenData et Fondation SNCF
    """)

# ----------------------------------------
# Liste des villes affichées
# ----------------------------------------
with st.expander("Voir la liste détaillée des villes (vous pouvez télécharger votre tableau personnalisé)"):
    # Créer une liste pour stocker les données détaillées
    villes_detail_data = []
    
    for _, row in coord_villes_filtrees.iterrows():
        ville = row["Ville"]
        temps_trajet = row["temps"]
        nb_etabs = row["nb_etabs"]
        
        # Récupérer les établissements de cette ville
        etabs_ville = qt_final[qt_final["Ville_clean"] == ville]
        
        # Créer une liste des noms d'établissements classés par catégorie
        etablissements_html = ""
        for cat in etabs_ville["Categorie"].unique():
            cat_data = etabs_ville[etabs_ville["Categorie"] == cat]
            etablissements_html += f"<b>{cat}</b><br>"
            for nom in cat_data["Nom du professionnel"]:
                etablissements_html += f"• {nom}<br>"
            etablissements_html += "<br>"
        
        etablissements_str = etablissements_html if etablissements_html else "Aucun"
        
        villes_detail_data.append({
            "Ville": ville,
            "Temps de trajet": temps_trajet,
            "Nb établissements": nb_etabs,
            "Établissements labellisés": etablissements_str
        })
    
    # Créer le DataFrame
    villes_detail = pd.DataFrame(villes_detail_data)
    villes_detail = villes_detail.sort_values("Temps de trajet")
    
    # Créer une version pour le téléchargement (sans HTML)
    villes_detail_csv = villes_detail.copy()
    # Nettoyer le HTML pour le CSV
    villes_detail_csv["Établissements labellisés"] = villes_detail_csv["Établissements labellisés"].str.replace("<br>", "\n").str.replace("<b>", "").str.replace("</b>", "").str.replace("• ", "")
    
    # Bouton de téléchargement
    csv = villes_detail_csv.to_csv(index=False, encoding='utf-8-sig')
    st.download_button(
        label="📥 Télécharger le tableau en CSV",
        data=csv,
        file_name="vos_destinations_labellisees.csv",
        mime="text/csv"
    )
    
    st.markdown("---")
    
    # Afficher avec le rendu HTML et style CSS pour aligner à gauche
    st.markdown("""
    <style>
    table {
        width: 100%;
    }
    th {
        text-align: left !important;
    }
    td {
        text-align: left !important;
        vertical-align: top !important;
    }
    </style>
    """, unsafe_allow_html=True)
    
    st.markdown(villes_detail.to_html(escape=False, index=False), unsafe_allow_html=True)

st.subheader("A propos des filtres possibles")
st.markdown(
    """
    - #### **Durée du séjour** :
    Permet de filtrer les villes en fonction du nombre minimum d'établissements labellisés Qualité Tourisme, adapté à la durée de votre séjour:


    \- **Journée**: toutes les villes conviennent

    \- **Weekend**: nous vous proposerons des villes avec au moins 3 lieux labellisés

    \- **Semaine**: nous vous proposerons des villes avec au moins 5 lieux labellisés

    - #### **Temps de voyage maximum** :
    Permet de sélectionner les villes en fonction du temps de trajet depuis Paris, allant de toutes les durées à plus de 6 heures.
    - #### **Centres d'intérêts** :
    Permet de choisir les catégories d'établissements qui vous intéressent le plus:


    \- **Loisirs & activités**: Nous vous proposons alors des destinations avec au moins un établissement dans cette catégorie parmi des parcs de loisirs, des activités de sport de nature, des sorties nature, des établissements de loisir, des VTC-limousine, des agences de locations saisonnières, des séminaires, des ports de plaisance et des parcs à thème.

    \- **Culture & patrimoine**: Nous vous proposons alors des destinations avec au moins un établissement dans cette catégorie parmi des sites de mémoire, des lieux de visite, des caveaux et points de vente, des visites d'entreprise, des commerces, des écomusées, des visites guidées, des maisons d'écrivain et des sites de préhistoire.

    \- **Restauration**: Nous vous proposons alors des destinations avec au moins un établissement dans cette catégorie parmi des restaurants, des restaurants de plage, des cafés, bars et brasseries. 
    \- **Hébergement**: Nous vous proposons alors des destinations avec au moins un établissement dans cette catégorie parmi des hôtels, des hôtels-restaurants, des campings, des chambres d'hôtes, des villages de vacances, des résidences de tourisme et hébergements collectifs.

    \- **Services touristiques**: Nous vous proposons alors des destinations avec au moins un office de tourisme.

    \- **Tous**: Nous vous proposons toutes les destinations correspondant aux autres filtres sélectionnés.
    """)

st.markdown("---")

st.markdown(
    """
    *Note: cette page a été mise au point par des étudiants dans le cadre d'un projet.*

    """
)
