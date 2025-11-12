# Moodle Resource Downloader

Ce projet permet de télécharger automatiquement les ressources et quiz d'un cours Moodle à partir de l'URL du cours et des identifiants de connexion.

## Fonctionnalités
- Téléchargement des ressources (PDF, HTML, etc.) d'un cours Moodle
- Téléchargement des quiz Moodle
- Gestion de l'authentification via un fichier `credentials.txt`
- Organisation des ressources téléchargées dans le dossier `downloaded_resources/`
- Support des contenus H5P

## Prérequis
- Python 3.7 ou supérieur
- Les dépendances listées dans `requirements.txt`

## Installation
1. Clonez ce dépôt :
   ```bash
   git clone https://github.com/maelgoujon/moodle-resource-downloader.git
   cd moodle-resource-downloader
   ```
2. Installez les dépendances :
   ```bash
   pip install -r requirements.txt
   ```

## Utilisation
1. Renseignez vos identifiants Moodle dans le fichier `credentials.txt` :
   ```
   username=VOTRE_IDENTIFIANT
   password=VOTRE_MOT_DE_PASSE
   ```
2. Téléchargez les ressources d'un cours :
   ```bash
   python3 download_moodle_resources.py --login-url 'URL_LOGIN' --course-url 'URL_COURS'
   ```
3. Téléchargez les quiz d'un cours :
   ```bash
   python3 download_moodle_quizzes.py --login-url 'URL_LOGIN' --course-url 'URL_COURS'
   ```

Les ressources seront enregistrées dans le dossier `downloaded_resources/`.

## Structure du projet
- `download_moodle_resources.py` : Script principal pour télécharger les ressources
- `download_moodle_quizzes.py` : Script pour télécharger les quiz
- `resources.py`, `quizzes.py`, `login.py`, `h5p.py` : Modules internes
- `credentials.txt` : Fichier contenant vos identifiants Moodle
- `requirements.txt` : Dépendances Python
- `downloaded_resources/` : Dossier de sortie des ressources téléchargées

## Avertissement
Ce projet est fourni à des fins éducatives. Respectez les conditions d'utilisation de votre plateforme Moodle.

## Auteur
- [maelgoujon](https://github.com/maelgoujon)
