# Scribe — ton ombudsman personnel

Scribe analyse tes factures et abonnements, repère les hausses de prix ou anomalies par rapport à ton historique, et rédige un brouillon de mail de réclamation quand c'est nécessaire. Tout reste sur ton ordinateur : les documents ne sont envoyés qu'à l'API Google Gemini pour être lus, et ton historique est stocké dans un simple fichier (`data/history.csv`) que toi seul possèdes.

## Ce dont tu as besoin avant de commencer

Il te faut deux choses installées sur ton ordinateur : **Python** (le langage dans lequel l'appli est écrite) et une **clé API Gemini** gratuite (le "moteur" qui lit tes documents).

### 1. Installer Python (si ce n'est pas déjà fait)

Ouvre un terminal et tape :

```
python3 --version
```

Si une version s'affiche (par exemple `Python 3.11.4`), tu as déjà Python, passe à l'étape suivante. Sinon, télécharge-le sur [python.org/downloads](https://www.python.org/downloads/) et installe-le normalement (coche bien la case "Add Python to PATH" si tu es sur Windows).

### 2. Créer ta clé API Gemini (gratuit)

Va sur [aistudio.google.com/apikey](https://aistudio.google.com/apikey), connecte-toi avec ton compte Google, puis clique sur **Create API Key**. Copie la clé — tu en auras besoin à chaque lancement de l'appli, colle-la donc quelque part de sûr pour ne pas avoir à la régénérer.

L'utilisation est gratuite pour un usage personnel. Tu peux suivre ta consommation sur la même page.

## Installer l'application

Dans un terminal, place-toi dans le dossier `scribe-app` (celui qui contient ce fichier), puis installe les dépendances :

```
cd chemin/vers/scribe-app
pip install -r requirements.txt
```

Si `pip` seul ne fonctionne pas, essaie `pip3 install -r requirements.txt` ou `py -m pip install -r requirements.txt` sur Windows.

## Lancer l'application

Toujours depuis le dossier `scribe-app` :

```
streamlit run app.py
```

Une page va s'ouvrir automatiquement dans ton navigateur (sinon, l'adresse s'affiche dans le terminal, généralement `http://localhost:8501`). Colle ta clé API dans la barre latérale, dépose une facture (PDF ou photo), clique sur **Analyser ce document**, et regarde ce que Scribe en tire.

Pour arrêter l'application, retourne dans le terminal et fais `Ctrl+C`.

## Comment ça marche, concrètement

Chaque document que tu déposes est lu par l'IA, qui en extrait le fournisseur, le montant, la date et quelques autres informations. Ces données sont ajoutées à ton historique local. Dès que tu déposes une deuxième facture du même fournisseur, Scribe compare automatiquement le nouveau montant à l'ancien : si la hausse dépasse 5% et 2€, une alerte apparaît et tu peux demander à l'IA de rédiger un brouillon de mail de réclamation, dans le ton de ton choix. Le mail n'est jamais envoyé automatiquement — c'est toujours toi qui relis et décides.

## Prochaines étapes possibles

Une fois que tu es à l'aise avec cette version, on pourra faire évoluer Scribe : transfert automatique par email plutôt que dépôt manuel, comparaison avec les meilleures offres du marché, ou export de l'historique en tableau. Pour l'instant, cette version couvre le cœur du projet — et elle fonctionne dès aujourd'hui.
