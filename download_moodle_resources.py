#!/usr/bin/env python3
import requests
from bs4 import BeautifulSoup
import os
import json
from urllib.parse import urljoin, urlparse, unquote, parse_qs
import getpass
import argparse
import re
import logging
import mimetypes
import zipfile
import io

logging.basicConfig(
    filename='moodle_downloader.log',
    filemode='a',
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.DEBUG
)

def login_to_moodle(session, login_url, username, password):
    logging.info(f"=== LOGIN ATTEMPT ===")
    logging.debug(f"Login URL: {login_url}")
    logging.debug(f"Username: {username}")
    
    try:
        resp = session.get(login_url, timeout=20)
        logging.debug(f"GET request status: {resp.status_code}")
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        token_input = soup.find('input', attrs={'name': 'logintoken'})
        token = token_input['value'] if token_input else ''
        
        if token:
            logging.debug(f"Login token found: {token[:20]}...")
        else:
            logging.warning(f"No login token found on login page")
        
        data = {'username': username, 'password': password, 'logintoken': token}
        logging.debug(f"Posting login data to {login_url}")
        
        # Important: allow_redirects=True pour suivre les redirections
        post = session.post(login_url, data=data, timeout=20, allow_redirects=True)
        logging.debug(f"POST response status: {post.status_code}")
        logging.debug(f"POST response URL: {post.url}")
        logging.debug(f"POST response length: {len(post.text)} bytes")
        logging.debug(f"Session cookies: {list(session.cookies.keys())}")
        
        # Vérifications plus intelligentes
        post_soup = BeautifulSoup(post.text, 'html.parser')
        login_form = post_soup.find('form', action=True)
        login_token_field = post_soup.find('input', attrs={'name': 'logintoken'})
        
        # Si on trouve encore un formulaire de login ET le token, c'est probablement un échec
        if login_token_field:
            logging.error(f"Login form still present after POST - login likely failed")
            logging.debug(f"Login token field still found: login form detected")
            raise Exception("Login failed. Vérifiez vos identifiants.")
        
        logging.info(f"✅ Login successful - redirected to: {post.url}")
        return session
    
    except Exception as e:
        logging.error(f"Login error: {e}", exc_info=True)
        raise

def is_valid_resource_url(url):
    return (
        'mod/url/view.php' in url or
        'mod/page/view.php' in url or
        'mod/resource/view.php' in url or
        'mod/quiz/view.php' in url or
        # --- AJOUT ---
        # Gérer aussi les dossiers
        'mod/folder/view.php' in url or 
        re.search(r'\.(pdf|docx?|xlsx?|pptx?|zip|txt|jpg|jpeg|png|mp4|webm|ogg|mov|avi)$', url, re.IGNORECASE)
    )

def is_downloadable_content(content_type, url):
    if content_type:
        return (
            'application/' in content_type or
            'text/' in content_type or
            'image/' in content_type or
            'audio/' in content_type or
            'video/' in content_type
        )
    else:
        return bool(re.search(r'\.(pdf|docx?|xlsx?|pptx?|zip|txt|jpg|jpeg|png|mp4|webm|ogg|mov|avi)$', url, re.IGNORECASE))

def get_final_file_url(session, url):
    try:
        response = session.get(url, timeout=20)
        soup = BeautifulSoup(response.text, 'html.parser')

        # Cas 1: 'mod/resource/view.php' - Lien de téléchargement direct
        download_link = soup.find('a', href=re.compile(r'/mod/resource/.*&redirect=1'))
        if download_link:
            final_url = urljoin(url, download_link['href'])
            logging.debug(f"get_final_file_url: Found resource link: {final_url}")
            return final_url
            
        # Cas 2: 'mod/page/view.php' - Sauvegarder le contenu de la page
        if 'mod/page/view.php' in url:
            logging.debug(f"get_final_file_url: mod/page/view.php detected")
            return url

        # Cas 3: 'mod/url/view.php' - Lien externe
        external_link = soup.find('div', class_='urlworkaround')
        if external_link and external_link.find('a'):
            final_url = external_link.find('a')['href']
            logging.debug(f"get_final_file_url: Found external URL: {final_url}")
            return final_url

        # Cas 4: iframe (pour PDF ou vidéo)
        iframe = soup.find('iframe', src=True)
        if iframe:
            iframe_src = urljoin(url, iframe['src'])
            if re.search(r'\.(pdf|mp4|webm|ogg|mov|avi)$', iframe_src, re.IGNORECASE):
                logging.debug(f"get_final_file_url: Found iframe: {iframe_src}")
                return iframe_src

        # Cas 5: Lien direct vers un fichier sur la page
        file_link = soup.find('a', href=re.compile(r'\.(pdf|docx?|xlsx?|pptx?|zip|txt|jpg|jpeg|png|mp4|webm|ogg|mov|avi)$', re.IGNORECASE))
        if file_link:
            final_url = urljoin(url, file_link['href'])
            logging.debug(f"get_final_file_url: Found file link: {final_url}")
            return final_url

        # Cas 6: Source vidéo/audio
        source_tag = soup.find('source', src=True)
        if source_tag:
            final_url = urljoin(url, source_tag['src'])
            logging.debug(f"get_final_file_url: Found source tag: {final_url}")
            return final_url

        # Si c'est déjà un lien de fichier direct
        if re.search(r'\.(pdf|docx?|xlsx?|pptx?|zip|txt|jpg|jpeg|png|mp4|webm|ogg|mov|avi)$', url, re.IGNORECASE):
            logging.debug(f"get_final_file_url: Direct file link: {url}")
            return url
        
        logging.warning(f"get_final_file_url: Could not find final URL for {url}")
        return None
    except Exception as e:
        logging.error(f"get_final_file_url error for {url}: {e}", exc_info=True)
        return None

def get_clean_filename(url, response_headers=None):
    path = urlparse(url).path
    filename = unquote(os.path.basename(path))

    # Essayer d'obtenir le nom depuis l'en-tête Content-Disposition
    if response_headers and 'Content-Disposition' in response_headers:
        disp = response_headers['Content-Disposition']
        match = re.search(r'filename="?([^"]+)"?', disp)
        if match:
            filename = unquote(match.group(1))

    # Si le nom n'est toujours pas bon
    if not filename or '.' not in filename or 'view.php' in filename:
        content_type = response_headers.get('Content-Type', '') if response_headers else ''
        if not content_type:
            try:
                content_type = requests.head(url, allow_redirects=True, timeout=10).headers.get('Content-Type', '')
            except Exception:
                content_type = ''
                
        ext = mimetypes.guess_extension(content_type.split(';')[0]) if content_type else '.bin'
        ext = ext if ext else '.bin'
        
        # Cas spécial pour les pages HTML
        if 'text/html' in content_type:
            ext = '.html'
            
        # Utiliser un hash ou un identifiant de l'URL pour un nom unique
        url_params = urlparse(url).query
        url_id = re.search(r'id=(\d+)', url_params)
        if url_id:
            base_name = f"page_{url_id.group(1)}"
        else:
            base_name = f"file_{hash(url)}"
        
        filename = f"{base_name}{ext}"

    return re.sub(r'[^\w\-_.]', '_', filename) # Nettoyer le nom

def extract_moodle_markdown(soup):
    """Extrait une version structurée en Markdown du contenu principal d'une page de cours Moodle."""
    lines = []
    extracted_links = []
    logging.debug("extract_moodle_markdown: Starting extraction")
    
    sections = soup.find_all('li', class_=re.compile(r'section'))
    logging.debug(f"extract_moodle_markdown: Found {len(sections)} sections")
    
    for section in sections:
        section_title = section.find(class_=re.compile(r'sectionname|accesshide'))
        if section_title:
            title = section_title.get_text(strip=True)
            lines.append(f"# {title}")
            logging.debug(f"extract_moodle_markdown: Section title: {title}")
        
        content = section.find('ul', class_=re.compile(r'section|img-text|topics|ctopics'))
        if not content:
            content = section.find('div', class_='content')
        
        if content:
            for li in content.find_all('li', recursive=False):
                activity = li.find('div', class_='activityinstance')
                if activity:
                    label = activity.get_text(" ", strip=True)
                    icon = activity.find('img', class_='iconlarge')
                    if icon and icon.get('alt'):
                        label_type = icon.get('alt').strip()
                        if label_type:
                            label += f" _{label_type}_"
                    a_tag = activity.find('a', href=True)
                    if a_tag:
                        url = a_tag['href']
                        extracted_links.append((label, url))
                        local_filename = f"{label.replace(' ', '_')}.html"
                        lines.append(f"- [{label}]({local_filename})")
                        logging.debug(f"extract_moodle_markdown: Added link: {label} -> {url}")
                    else:
                        lines.append(f"- {label}")
                else:
                    label = li.get_text(" ", strip=True)
                    if label:
                        lines.append(f"- {label}")
        lines.append("")
    
    if not lines:
        main = soup.find('div', role='main') or soup.find('div', class_='page-content')
        if main:
            text = main.get_text("\n", strip=True)
            lines.append(text)
            logging.debug("extract_moodle_markdown: Fallback to main content")
    
    result_markdown = '\n'.join(lines)
    logging.debug(f"extract_moodle_markdown: Extracted {len(extracted_links)} links")
    return result_markdown, extracted_links

def get_resource_links(session, course_url, visited=None, base_folder='resources'):
    if visited is None:
        visited = set()
    if course_url in visited:
        # On retourne des listes vides car on a déjà visité
        return [], []
    visited.add(course_url)
    print(f"📂 Crawling: {course_url}")
    logging.info(f"Crawling: {course_url}")

    try:
        response = session.get(course_url, timeout=20)
        response.raise_for_status() # Vérifie les erreurs HTTP
    except requests.exceptions.RequestException as e:
        print(f"❌ Erreur lors de l'accès à {course_url}: {e}")
        logging.error(f"Erreur lors de l'accès à {course_url}: {e}")
        return [], [] # Retourner des listes vides en cas d'échec

    soup = BeautifulSoup(response.text, 'html.parser')

    title = soup.title.string.strip() if soup.title else "course"
    safe_title = re.sub(r'[^\w\-_.]', '_', title)
    
    # Si c'est un dossier, on ajuste le 'base_folder'
    if 'mod/folder/view.php' in course_url:
        # Le titre de la page est le nom du dossier, on l'utilise
        target_folder = os.path.join(base_folder, safe_title)
    else:
        # C'est la page principale du cours
        target_folder = os.path.join(base_folder, safe_title)


    os.makedirs(target_folder, exist_ok=True)


    # --- NOUVEAU BLOC POUR SAUVEGARDER LA PAGE DU COURS EN HTML ET EN MARKDOWN ---
    # On détermine un nom de fichier pour cette page HTML et .md
    if 'mod/folder/view.php' in course_url:
        presentation_filename = f"dossier_{safe_title}.html"
        md_filename = f"dossier_{safe_title}.md"
    elif 'course/view.php' in course_url:
        presentation_filename = "presentation_cours.html"
        md_filename = "presentation_cours.md"
    else:
        presentation_filename = f"{safe_title}.html"
        md_filename = f"{safe_title}.md"

    presentation_filepath = os.path.join(target_folder, presentation_filename)
    md_filepath = os.path.join(target_folder, md_filename)

    # Si c'est une page de type 'mod/page/view.php', on sauvegarde le contenu principal
    if 'mod/page/view.php' in course_url:
        page_content = soup.find('div', role='main') or soup.find('div', class_='page-content') or soup
        content_to_save = page_content.prettify()
        print_name = presentation_filename
    else:
        content_to_save = response.text
        print_name = presentation_filename

    # --- LOG: Vérifier si la page sauvegardée est un formulaire de login ---
    login_form_detected = False
    login_token = soup.find('input', attrs={'name': 'logintoken'})
    login_form = soup.find('form', action=True)
    if login_token or (login_form and 'login' in login_form.get('action', '').lower()):
        login_form_detected = True
        print(f"⚠️ ATTENTION: La page sauvegardée semble être un formulaire de login !")
        logging.warning(f"La page sauvegardée ({presentation_filepath}) semble être un formulaire de login. Vérifiez l'authentification et l'accès au cours.")

    try:
        with open(presentation_filepath, 'w', encoding='utf-8') as f:
            f.write(content_to_save)
        print(f"✅ Page HTML sauvegardée: {print_name}")
        logging.info(f"Page HTML sauvegardée: {presentation_filepath}")
    except Exception as e:
        print(f"⚠️ Impossible de sauvegarder la page HTML {presentation_filename}: {e}")
        logging.warning(f"Impossible de sauvegarder la page {presentation_filename}: {e}", exc_info=True)

    # --- NOUVEAU : Extraction structurée et sauvegarde en Markdown ---
    try:
        md_content, extracted_links = extract_moodle_markdown(soup)
        # Dictionnaire pour remplacer les liens dans le markdown
        link_replacements = {}
        for label, url in extracted_links:
            # Télécharger la ressource liée (page, fichier, etc.)
            final_url = get_final_file_url(session, url)
            if not final_url:
                continue
            # Déterminer le nom du fichier local
            if 'mod/page/view.php' in final_url:
                # Télécharger la page comme HTML
                sub_soup = BeautifulSoup(session.get(final_url, timeout=20).text, 'html.parser')
                sub_title = sub_soup.title.string.strip() if sub_soup.title else label
                safe_sub_title = re.sub(r'[^\w\-_.]', '_', sub_title)
                sub_filename = f"dossier_{safe_sub_title}.html" if 'mod/folder/view.php' in final_url else f"{safe_sub_title}.html"
                sub_filepath = os.path.join(target_folder, sub_filename)
                with open(sub_filepath, 'w', encoding='utf-8') as f:
                    f.write(sub_soup.prettify())
                link_replacements[label] = sub_filename
            else:
                # Fichier ou autre ressource
                resp = session.get(final_url, timeout=30, allow_redirects=True)
                local_filename = get_clean_filename(final_url, resp.headers)
                local_filepath = os.path.join(target_folder, local_filename)
                with open(local_filepath, 'wb') as f:
                    f.write(resp.content)
                link_replacements[label] = local_filename
        # Mise à jour du markdown avec les bons liens
        for label, local_file in link_replacements.items():
            md_content = re.sub(rf'\[{re.escape(label)}\]\([^)]+\)', f'[{label}]({local_file})', md_content)
        with open(md_filepath, 'w', encoding='utf-8') as f:
            f.write(md_content)
        print(f"✅ Page Markdown sauvegardée: {md_filename}")
        logging.info(f"Page Markdown sauvegardée: {md_filepath}")
    except Exception as e:
        print(f"⚠️ Impossible de sauvegarder la page Markdown {md_filename}: {e}")
        logging.warning(f"Impossible de sauvegarder la page Markdown {md_filename}: {e}", exc_info=True)
    # --- FIN DU NOUVEAU BLOC ---

    # Collecte des ressources et quiz
    resource_files = []
    quiz_files = []
    h5p_links = []
    
    # Si c'est une page 'mod/page/view.php', on n'ira pas plus loin
    if 'mod/page/view.php' in course_url:
        logging.debug("Page Moodle detected, not processing further links")
        resource_files.append((course_url, presentation_filename, target_folder, 'page'))
        return resource_files, quiz_files

    # Parcourir tous les liens

    for link in soup.find_all('a', href=True):
        href = link['href']
        full_url = urljoin(course_url, href)
        # Conserver une version sans fragment pour les vérifications (evite d'ignorer #h5pbookid...)
        full_url_no_fragment = full_url.split('#')[0]

        # Éviter les boucles et les liens inutiles : ignorer les ancres locales (href commençant par '#')
        if full_url_no_fragment in visited or href.strip().lower().startswith('javascript:void') or href.strip().startswith('#'):
            continue

        # Détection H5P même si l'URL contient des fragments ; utiliser la version sans fragment pour
        # les tests d'appartenance aux chemins mais conserver la version complète (avec #) pour
        # l'ajout à la liste afin de pouvoir référencer des chapitres précis.
        if not is_valid_resource_url(full_url_no_fragment):
            if 'mod/h5pactivity/view.php' in full_url_no_fragment:
                link_text = link.get_text(strip=True)
                print(f"🎲 H5P trouvé: {link_text} -> {full_url}")
                logging.info(f"H5P found: {link_text} -> {full_url}")
                h5p_links.append({'title': link_text, 'url': full_url, 'folder': target_folder})
            continue

        try:
            # Utiliser la version sans fragment pour déterminer le type de ressource
            if 'mod/quiz/view.php' in full_url_no_fragment:
                if full_url_no_fragment not in [q['url'] for q in quiz_files]:
                    quiz_data = {'url': full_url_no_fragment, 'folder': target_folder}
                    quiz_files.append(quiz_data)
                    link_text = link.get_text(strip=True)
                    print(f"✅ Quiz trouvé: {link_text}")
                    logging.info(f"Quiz found: {full_url_no_fragment}")

            elif 'mod/h5pactivity/view.php' in full_url_no_fragment:
                link_text = link.get_text(strip=True)
                print(f"🎲 H5P trouvé: {link_text} -> {full_url}")
                logging.info(f"H5P found: {link_text} -> {full_url}")
                # Stocker l'URL complète (avec fragment) pour permettre d'accéder à des chapitres précis
                h5p_links.append({'title': link_text, 'url': full_url, 'folder': target_folder})

            elif 'mod/folder/view.php' in full_url_no_fragment:
                print(f"📁 Entering folder: {full_url_no_fragment}")
                logging.info(f"Entering folder: {full_url_no_fragment}")
                nested_resources, nested_quizzes = get_resource_links(session, full_url_no_fragment, visited, target_folder)
                resource_files.extend(nested_resources)
                quiz_files.extend(nested_quizzes)

            elif 'mod/page/view.php' in full_url_no_fragment:
                print(f"📄 Page trouvée: {full_url_no_fragment}")
                logging.info(f"Page found: {full_url_no_fragment}")
                nested_resources, nested_quizzes = get_resource_links(session, full_url_no_fragment, visited, target_folder)
                resource_files.extend(nested_resources)
                quiz_files.extend(nested_quizzes)

            else:
                # C'est un lien de ressource (fichier, url externe, etc.)
                if (full_url_no_fragment, target_folder) not in [(r[0], r[2]) for r in resource_files]:
                    resource_files.append((full_url_no_fragment, None, target_folder, 'file'))
                    link_text = link.get_text(strip=True)
                    print(f"✅ Ressource trouvée: {link_text}")
                    logging.info(f"Resource found: {full_url_no_fragment}")

        except Exception as e:
            print(f"⚠️ Skipping {full_url} due to error: {e}")
            logging.warning(f"Skipping {full_url} due to error: {e}", exc_info=True)

    logging.info(f"Found {len(resource_files)} resources, {len(quiz_files)} quizzes, {len(h5p_links)} h5p links")
    return resource_files, quiz_files, h5p_links


    """
    Extrait une version structurée en Markdown du contenu principal d'une page de cours Moodle.
    """
    lines = []
    extracted_links = []  # Liste des tuples (texte, url)
    logging.debug("extract_moodle_markdown: Starting extraction")
    
    # Chercher les sections principales (li.section)
    sections = soup.find_all('li', class_=re.compile(r'section'))
    logging.debug(f"extract_moodle_markdown: Found {len(sections)} sections")
    
    for section in sections:
        # Titre de la section
        section_title = section.find(class_=re.compile(r'sectionname|accesshide'))
        if section_title:
            title = section_title.get_text(strip=True)
            lines.append(f"# {title}")
            logging.debug(f"extract_moodle_markdown: Section title: {title}")
        
        # Contenu de la section
        content = section.find('ul', class_=re.compile(r'section|img-text|topics|ctopics'))
        if not content:
            content = section.find('div', class_='content')
        
        if content:
            for li in content.find_all('li', recursive=False):
                # Activité ou ressource
                activity = li.find('div', class_='activityinstance')
                if activity:
                    label = activity.get_text(" ", strip=True)
                    # Déterminer le type (Forum, URL, Fichier, Test, etc.)
                    icon = activity.find('img', class_='iconlarge')
                    if icon and icon.get('alt'):
                        label_type = icon.get('alt').strip()
                        if label_type:
                            label += f" _{label_type}_"
                    # Chercher le lien
                    a_tag = activity.find('a', href=True)
                    if a_tag:
                        url = a_tag['href']
                        extracted_links.append((label, url))
                        # Nom de fichier local (sera déterminé après téléchargement)
                        local_filename = f"{label.replace(' ', '_')}.html"  # Valeur par défaut
                        # On met un lien Markdown (sera corrigé après téléchargement)
                        lines.append(f"- [{label}]({local_filename})")
                        logging.debug(f"extract_moodle_markdown: Added link: {label} -> {url}")
                    else:
                        lines.append(f"- {label}")
                else:
                    # Label ou texte simple
                    label = li.get_text(" ", strip=True)
                    if label:
                        lines.append(f"- {label}")
        # Ajouter une ligne vide entre sections
        lines.append("")
    
    # Si aucune section trouvée, fallback sur le contenu principal
    if not lines:
        main = soup.find('div', role='main') or soup.find('div', class_='page-content')
        if main:
            text = main.get_text("\n", strip=True)
            lines.append(text)
            logging.debug("extract_moodle_markdown: Fallback to main content")
    
    result_markdown = '\n'.join(lines)
    logging.debug(f"extract_moodle_markdown: Extracted {len(extracted_links)} links")
    return result_markdown, extracted_links

def extract_questions(question_soup):
    """Extrait les questions d'un quiz Moodle"""
    questions = []
    question_divs = question_soup.find_all('div', class_=re.compile(r'que|question|formulation'))
    for question_div in question_divs:
        question_text_tag = question_div.find(class_=re.compile(r'qtext|formulation|prompt'))
        if not question_text_tag:
            continue
        question_text = question_text_tag.get_text(strip=True)

        answers = []
        answer_divs = question_div.find_all(class_=re.compile(r'answer|réponse|choice'))
        for answer_div in answer_divs:
            label = answer_div.find('label')
            if label:
                text = label.get_text(strip=True)
            else:
                text = answer_div.get_text(strip=True)
            
            if text:
                answers.append(text)

        if question_text:
            questions.append({'question': question_text, 'answers': answers})

    return questions

def download_quiz(session, quiz_url, target_folder):
    """Télécharge les questions d'un quiz Moodle"""
    try:
        logging.info(f"download_quiz: Starting for {quiz_url}")
        quiz_page = session.get(quiz_url, timeout=20)
        quiz_soup = BeautifulSoup(quiz_page.text, 'html.parser')
        
        quiz_title = quiz_soup.find('h1').get_text(strip=True) if quiz_soup.find('h1') else "quiz"
        safe_quiz_title = re.sub(r'[^\w\-_.]', '_', quiz_title)
        filename = f"QUIZ_{safe_quiz_title}.json"
        filepath = os.path.join(target_folder, filename)


        if os.path.exists(filepath):
            print(f"ℹ️ Quiz déjà sauvegardé: {filename}")
            logging.info(f"Quiz already saved: {filename}")
            return {'filepath': filepath, 'filename': filename}

        # Si le quiz est fermé, sauvegarder la page HTML pour référence
        if "Ce test est fermé" in quiz_soup.get_text():
            print(f"⚠️ Quiz est fermé: {quiz_url}")
            logging.warning(f"Quiz fermé: {quiz_url}")
            html_path = os.path.join(target_folder, f"{safe_quiz_title}_FERME.html")
            try:
                with open(html_path, 'w', encoding='utf-8') as f:
                    f.write(quiz_page.text)
                print(f"📄 Page HTML du quiz fermé sauvegardée: {html_path}")
                logging.info(f"Quiz fermé HTML saved: {html_path}")
            except Exception as e:
                print(f"⚠️ Impossible de sauvegarder la page HTML du quiz fermé: {e}")
                logging.warning(f"Impossible de sauvegarder la page HTML du quiz fermé: {e}", exc_info=True)
            return None

        # Tenter de trouver une tentative existante (relecture)
        review_link = quiz_soup.find('a', href=re.compile(r'review\.php\?attempt='))
        if review_link:
            print(f"ℹ️ Tentative de relecture trouvée pour {safe_quiz_title}")
            review_url = urljoin(quiz_url, review_link['href'])
            attempt_page = session.get(review_url, timeout=20)
            logging.info(f"Using existing review attempt for {safe_quiz_title}")
        else:
            # Sinon, démarrer une nouvelle tentative
            start_form = quiz_soup.find('form', {'method': 'post'}, action=re.compile(r'attempt\.php'))
            if not start_form:
                print(f"⚠️ Pas de formulaire de démarrage de quiz trouvé sur {quiz_url}")
                logging.warning(f"Pas de formulaire de démarrage de quiz trouvé sur {quiz_url}")
                # Sauvegarder la page HTML du quiz pour référence
                html_path = os.path.join(target_folder, f"{safe_quiz_title}_INACCESSIBLE.html")
                try:
                    with open(html_path, 'w', encoding='utf-8') as f:
                        f.write(quiz_page.text)
                    print(f"📄 Page HTML du quiz inaccessible sauvegardée: {html_path}")
                    logging.info(f"Quiz inaccessible HTML saved: {html_path}")
                except Exception as e:
                    print(f"⚠️ Impossible de sauvegarder la page HTML du quiz inaccessible: {e}")
                    logging.warning(f"Impossible de sauvegarder la page HTML du quiz inaccessible: {e}", exc_info=True)
                return None

        questions = []
        current_url = attempt_page.url
        current_soup = BeautifulSoup(attempt_page.text, 'html.parser')

        # Gérer les quiz sur une seule page vs plusieurs pages
        pagination = current_soup.find('div', class_='qn_buttons')
        if pagination:
            # Plusieurs pages
            pages = pagination.find_all('a', href=re.compile(r'page='))
            page_urls = set([urljoin(current_url, p['href']) for p in pages])
            page_urls.add(current_url)
            logging.info(f"Quiz has {len(page_urls)} pages")
            
            for page_url in sorted(list(page_urls)):
                if page_url != current_url:
                    page_resp = session.get(page_url, timeout=20)
                    question_soup = BeautifulSoup(page_resp.text, 'html.parser')
                else:
                    question_soup = current_soup
                page_questions = extract_questions(question_soup)
                questions.extend(page_questions)
                logging.debug(f"Extracted {len(page_questions)} questions from page {page_url}")
        else:
            # Une seule page
            questions = extract_questions(current_soup)
            logging.info(f"Quiz has 1 page with {len(questions)} questions")


        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump({'quiz_title': quiz_title, 'questions': questions}, f, ensure_ascii=False, indent=2)

        print(f"✅ Quiz sauvegardé: {filename} ({len(questions)} questions)")
        logging.info(f"Quiz saved: {filename} with {len(questions)} questions")
        return {'filepath': filepath, 'filename': filename}

    except Exception as e:
        print(f"⚠️ Échec du téléchargement du quiz {quiz_url}: {e}")
        logging.error(f"Échec du téléchargement du quiz {quiz_url}: {e}", exc_info=True)
        return None

def download_resources(session, resource_files):
    downloaded_files = 0


    for url, filename_hint, folder, rtype in resource_files:
        # Cas spécial: 'mod/page/view.php' a déjà été sauvegardé dans get_resource_links
        if rtype == 'page':
            downloaded_files += 1
            continue

        try:
            response = session.get(url, timeout=30, allow_redirects=True)
            response.raise_for_status()

            final_url = response.url
            headers = response.headers

            # Déterminer le nom du fichier
            filename = filename_hint if filename_hint else get_clean_filename(final_url, headers)

            # Si c'est un fichier HTML, essayer d'utiliser le titre de la page comme nom
            content_type = headers.get('Content-Type', '')
            if 'text/html' in content_type:
                try:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    page_title = soup.title.string.strip() if soup.title and soup.title.string else None
                    if page_title:
                        safe_title = re.sub(r'[^\w\-_.]', '_', page_title)
                        filename = f"{safe_title}.html"
                except Exception as e:
                    logging.warning(f"Could not extract title for HTML file: {e}")

            # Gérer les liens externes 'mod/url/view.php'
            if 'mod/url/view.php' in url and not is_downloadable_content(content_type, final_url):
                # C'est un lien vers une page web externe, pas un fichier
                filename = f"LIEN_{filename}.url"
                path = os.path.join(folder, filename)
                # Créer un fichier .url de raccourci Internet
                content = f"[InternetShortcut]\nURL={final_url}\n"
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                print(f"🔗 Lien externe sauvegardé: {filename}")
                logging.info(f"Lien externe sauvegardé: {final_url} -> {path}")
            else:
                # C'est un fichier téléchargeable
                path = os.path.join(folder, filename)
                if os.path.exists(path):
                    print(f"ℹ️ Fichier déjà téléchargé: {filename}")
                    continue

                print(f"📥 Téléchargement {filename} -> {folder}")
                logging.info(f"Téléchargement {final_url} -> {path}")

                with open(path, 'wb') as f:
                    f.write(response.content)

                # Générer un markdown pour chaque ressource HTML téléchargée
                if 'text/html' in content_type or filename.endswith('.html'):
                    md_filename = filename.rsplit('.', 1)[0] + '.md'
                    md_path = os.path.join(folder, md_filename)
                    md_content = f"# {filename.rsplit('.', 1)[0].replace('_', ' ')}\n\n[Voir la ressource HTML]({filename})\n"
                    with open(md_path, 'w', encoding='utf-8') as f:
                        f.write(md_content)
                    print(f"📝 Markdown généré: {md_filename}")
                    logging.info(f"Markdown généré: {md_path}")

            downloaded_files += 1

        except Exception as e:
            logging.error(f"Échec du téléchargement {url}: {e}", exc_info=True)
            print(f"❌ Échec du téléchargement {url}: {e}")

    return downloaded_files

def main():
    def generate_h5p_summary(h5p_extracted, out_folder):
        md_lines = ["# Résumé des activités H5P\n"]
        summary = []
        for h5p in h5p_extracted:
            md_lines.append(f"## {h5p['title']}")
            md_lines.append(f"- URL: {h5p['url']}")
            md_lines.append(f"- Fichier: {h5p['file']}")
            md_lines.append(f"- Type: {h5p['type']}")
            if h5p['type'] == 'html':
                md_lines.append(f"- Extrait de texte :\n\n    {h5p['text'][:300]}{'...' if len(h5p['text'])>300 else ''}")
                if h5p['interactions']:
                    md_lines.append(f"- Interactions principales :")
                    for inter in h5p['interactions'][:10]:
                        md_lines.append(f"    - {inter}")
            md_lines.append("")
            summary.append({k: h5p[k] for k in ['title','url','file','folder','type','text','interactions']})
        # Écriture du markdown
        md_path = os.path.join(out_folder, 'H5P_SUMMARY.md')
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(md_lines))
        # Écriture du JSON
        json_path = os.path.join(out_folder, 'H5P_SUMMARY.json')
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"📝 Résumé H5P généré: {md_path} et {json_path}")
        logging.info(f"H5P summary generated: {md_path} and {json_path}")

    def extract_h5p_text(h5p_downloaded):
        extracted = []
        for h5p in h5p_downloaded:
            if h5p['type'] == 'html':
                html_path = os.path.join(h5p['folder'], h5p['file'])
                try:
                    with open(html_path, 'r', encoding='utf-8') as f:
                        html = f.read()
                    soup = BeautifulSoup(html, 'html.parser')
                    # Extraire le texte visible (hors scripts/styles)
                    for script in soup(['script', 'style']):
                        script.decompose()
                    text = soup.get_text(separator='\n', strip=True)
                    # Optionnel : extraire des interactions spécifiques (questions, boutons, etc.)
                    interactions = []
                    for el in soup.find_all(['button', 'label', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'p']):
                        t = el.get_text(strip=True)
                        if t and t not in interactions:
                            interactions.append(t)
                    extracted.append({
                        'title': h5p['title'],
                        'url': h5p['url'],
                        'file': h5p['file'],
                        'folder': h5p['folder'],
                        'type': 'html',
                        'text': text,
                        'interactions': interactions
                    })
                except Exception as e:
                    print(f"❌ Erreur extraction texte H5P: {h5p['file']} : {e}")
                    logging.warning(f"Erreur extraction texte H5P: {h5p['file']} : {e}")
            else:
                # Pour les .h5p, on ne traite pas ici (nécessite une lib spéciale)
                extracted.append({
                    'title': h5p['title'],
                    'url': h5p['url'],
                    'file': h5p['file'],
                    'folder': h5p['folder'],
                    'type': 'h5p',
                    'text': '',
                    'interactions': []
                })
        return extracted

    # h5p_extracted sera défini après le téléchargement H5P, pas ici
    # Téléchargement des activités H5P
    def download_h5p_activities(session, h5p_links):
        downloaded = []
        for h5p in h5p_links:
            url = h5p['url']
            title = h5p['title']
            folder = h5p['folder']
            try:
                resp = session.get(url, timeout=30, allow_redirects=True)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, 'html.parser')
                # Chercher un lien direct vers un .h5p
                h5p_file_link = soup.find('a', href=re.compile(r'\.h5p($|\?)'))
                # Chercher un iframe embed.php avec paramètre url=... (cas courant H5P)
                iframe_embed = None
                if not h5p_file_link:
                    for iframe in soup.find_all('iframe', src=True):
                        if 'embed.php' in iframe['src'] and 'url=' in iframe['src']:
                            iframe_embed = iframe
                            break
                # Create a dedicated subfolder for this H5P activity
                safe_title = re.sub(r"[^\w\-_.]", '_', title) or 'h5p_activity'
                h5p_dir = os.path.join(folder, safe_title)
                os.makedirs(h5p_dir, exist_ok=True)

                def _download_media(media_url, dest_folder):
                    try:
                        full = urljoin(url, media_url)

                        if 'vimeo.com' in full:
                            try:
                                player_page_resp = session.get(full, timeout=20)
                                player_page_resp.raise_for_status()
                                player_page = player_page_resp.text
                                
                                match = re.search(r'var config = ({.+?});', player_page, re.DOTALL)
                                if not match:
                                    # Alternative regex for different script layouts
                                    match = re.search(r'window.playerConfig = ({.+?});', player_page, re.DOTALL)

                                if match:
                                    config_json_str = match.group(1)
                                    config_json = json.loads(config_json_str)
                                    
                                    progressive_files = config_json.get('request', {}).get('files', {}).get('progressive', [])
                                    if progressive_files:
                                        # Sort by height to get best quality
                                        best_quality_video = sorted(progressive_files, key=lambda x: x.get('height', 0), reverse=True)[0]
                                        video_url = best_quality_video['url']
                                        
                                        video_id_match = re.search(r'video/(\d+)', full)
                                        video_id = video_id_match.group(1) if video_id_match else 'video'
                                        
                                        fname = f"vimeo_{video_id}_{best_quality_video.get('height', 'hq')}.mp4"
                                        dest = os.path.join(dest_folder, fname)

                                        if not os.path.exists(dest):
                                            print(f"📥 Téléchargement du média (Vimeo): {fname}")
                                            logging.info(f"Downloading Vimeo video: {video_url} -> {dest}")
                                            
                                            with session.get(video_url, timeout=120, allow_redirects=True, stream=True) as r:
                                                r.raise_for_status()
                                                with open(dest, 'wb') as mf:
                                                    for chunk in r.iter_content(chunk_size=8192):
                                                        mf.write(chunk)
                                            
                                            logging.info(f"Downloaded Vimeo video: {video_url} -> {dest}")
                                            print(f"✅ Média (Vimeo) téléchargé: {fname}")
                                        else:
                                            logging.debug(f"Vimeo media already exists: {dest}")
                                        
                                        return os.path.basename(dest)
                            except Exception as e:
                                logging.warning(f"Failed to process Vimeo URL {full}: {e}")
                                # Fall through to default behavior

                        r = session.get(full, timeout=30, allow_redirects=True)
                        r.raise_for_status()
                        ct = r.headers.get('Content-Type', '')
                        # Only download if it looks like video or binary
                        if 'video' in ct or re.search(r'\.(mp4|webm|ogg|mov|avi)$', full, re.IGNORECASE) or not ct.startswith('text'):
                            fname = get_clean_filename(full, r.headers)
                            dest = os.path.join(dest_folder, fname)
                            if not os.path.exists(dest):
                                with open(dest, 'wb') as mf:
                                    mf.write(r.content)
                                logging.info(f"Downloaded media: {full} -> {dest}")
                                print(f"📥 Média téléchargé: {fname}")
                            else:
                                logging.debug(f"Media already exists: {dest}")
                            return os.path.basename(dest)
                        else:
                            logging.debug(f"Skipping non-media URL: {full} (Content-Type: {ct})")
                            return None
                    except Exception as e:
                        logging.warning(f"Failed to download media {media_url}: {e}")
                        return None

                if h5p_file_link or iframe_embed:
                    if h5p_file_link:
                        h5p_url = urljoin(url, h5p_file_link['href'])
                    else:
                        # Extraire le paramètre url=... de l'iframe embed
                        from urllib.parse import urlparse, parse_qs, unquote
                        src = iframe_embed['src']
                        parsed = urlparse(src)
                        qs = parse_qs(parsed.query)
                        h5p_url = qs.get('url', [None])[0]
                        if h5p_url:
                            h5p_url = unquote(h5p_url)
                        else:
                            print(f"❌ Impossible d'extraire l'URL H5P depuis l'iframe embed dans {url}")
                            continue
                    h5p_filename = f"{safe_title}.h5p"
                    h5p_path = os.path.join(h5p_dir, h5p_filename)
                    if not os.path.exists(h5p_path):
                        file_resp = session.get(h5p_url, timeout=30, allow_redirects=True)
                        with open(h5p_path, 'wb') as f:
                            f.write(file_resp.content)
                        print(f"📥 H5P téléchargé: {h5p_filename}")
                        logging.info(f"H5P file downloaded: {h5p_url} -> {h5p_path}")
                    else:
                        print(f"ℹ️ H5P déjà téléchargé: {h5p_filename}")
                    # Conversion automatique en markdown après téléchargement du .h5p
                    try:
                        import zipfile, json
                        with zipfile.ZipFile(h5p_path, 'r') as h5p_zip:
                            main_library = ''
                            if 'h5p.json' in h5p_zip.namelist():
                                with h5p_zip.open('h5p.json') as h5p_json_file:
                                    h5p_json = json.load(h5p_json_file)
                                    main_library = h5p_json.get('mainLibrary')
                            content_json = None
                            if 'content/content.json' in h5p_zip.namelist():
                                with h5p_zip.open('content/content.json') as content_file:
                                    content_json = json.load(content_file)
                            if content_json:
                                def render_to_md(item):
                                    lines = []
                                    # Si l'item est une liste, traiter chaque élément
                                    if isinstance(item, list):
                                        for sub in item:
                                            lines.extend(render_to_md(sub))
                                        return lines
                                    # Si l'item est un dict
                                    if isinstance(item, dict):
                                        lib = item.get('library', '').split(' ')[0]
                                        params = item.get('params', {})
                                        # Extraction de tout champ 'text' dans params (H5P.Text, H5P.AdvancedText, etc.)
                                        if isinstance(params, dict) and 'text' in params and params['text']:
                                            soup = BeautifulSoup(params['text'], 'html.parser')
                                            lines.append(soup.get_text('\n'))
                                        # Images
                                        if lib == 'H5P.Image':
                                            if params.get('file', {}).get('path'):
                                                img_path = params['file']['path']
                                                img_filename = os.path.basename(img_path)
                                                img_local_path = os.path.join(h5p_dir, img_filename)
                                                found = False
                                                for member in h5p_zip.namelist():
                                                    if member.endswith('/' + img_path):
                                                        if not os.path.exists(img_local_path):
                                                            with h5p_zip.open(member) as zf, open(img_local_path, 'wb') as f: f.write(zf.read())
                                                        found = True
                                                        break
                                                if found:
                                                    lines.append(f"![{params.get('alt', img_filename)}]({img_filename})")
                                        # Vidéos
                                        if lib == 'H5P.Video':
                                            if params.get('sources'):
                                                video_path = params['sources'][0]['path']
                                                if video_path.startswith('http'):
                                                    lines.append(f"[Vidéo externe]({video_path})")
                                                else:
                                                    video_filename = os.path.basename(video_path)
                                                    found = False
                                                    for member in h5p_zip.namelist():
                                                        if member.endswith('/' + video_path):
                                                            if not os.path.exists(os.path.join(h5p_dir, video_filename)):
                                                                with h5p_zip.open(member) as zf, open(os.path.join(h5p_dir, video_filename), 'wb') as f: f.write(zf.read())
                                                            found = True
                                                            break
                                                    if found:
                                                        lines.append(f"[Vidéo locale]({video_filename})")
                                        # Descendre récursivement dans tous les champs 'content' (à la racine, dans params, ou dans tout sous-objet)
                                        for k, v in item.items():
                                            if k == 'content' and isinstance(v, (list, dict)):
                                                lines.extend(render_to_md(v))
                                            elif isinstance(v, (list, dict)):
                                                lines.extend(render_to_md(v))
                                        if isinstance(params, dict):
                                            for k, v in params.items():
                                                if k == 'content' and isinstance(v, (list, dict)):
                                                    lines.extend(render_to_md(v))
                                                elif isinstance(v, (list, dict)):
                                                    lines.extend(render_to_md(v))
                                    if lines:
                                        lines.append('\n---\n')
                                    return lines
                                # Génération du markdown
                                if main_library == 'H5P.InteractiveBook':
                                    chapters = content_json.get('chapters', [])
                                    for i, chapter in enumerate(chapters):
                                        chapter_title = chapter.get('title', f'Chapitre {i+1}')
                                        safe_chapter_title = re.sub(r'[^\w\-_.]', '_', chapter_title)
                                        chapter_filename = f"CHAP_{i+1}_{safe_chapter_title}.md"
                                        chapter_filepath = os.path.join(h5p_dir, chapter_filename)
                                        md_lines = [f"# {chapter_title}\n"]
                                        # Correction : parcourir chapter['params']['content']
                                        for content_item in chapter.get('params', {}).get('content', []):
                                            md_lines.extend(render_to_md(content_item))
                                        with open(chapter_filepath, 'w', encoding='utf-8') as f:
                                            f.write('\n'.join(md_lines))
                                        print(f"  - Chapitre sauvegardé: {chapter_filename}")
                                else:
                                    md_lines = [f"# {title}\n"]
                                    md_lines.extend(render_to_md(content_json))
                                    md_filename = f"{safe_title}.md"
                                    md_filepath = os.path.join(h5p_dir, md_filename)
                                    with open(md_filepath, 'w', encoding='utf-8') as f:
                                        f.write('\n'.join(md_lines))
                                    print(f"  - Contenu H5P sauvegardé: {md_filename}")
                    except Exception as e:
                        print(f"❌ Erreur extraction markdown H5P: {h5p_filename} : {e}")
                        logging.warning(f"Erreur extraction markdown H5P: {h5p_filename} : {e}")
                    downloaded.append({'title': title, 'url': url, 'file': h5p_filename, 'folder': h5p_dir, 'type': 'h5p'})
                else:
                    # Sinon, sauvegarder la page HTML **dans le dossier H5P dédié**
                    html_filename = f"{safe_title}_H5P.html"
                    html_path = os.path.join(h5p_dir, html_filename)
                    with open(html_path, 'w', encoding='utf-8') as f:
                        f.write(resp.text)
                    print(f"📄 Page HTML H5P sauvegardée: {html_filename}")
                    logging.info(f"H5P HTML saved: {url} -> {html_path}")

                    # Detect media sources (iframe, video, source)
                    media_candidates = set()
                    for tag in soup.find_all(['iframe', 'video', 'source']):
                        src = None
                        if tag.name == 'iframe' and tag.has_attr('src'):
                            src = tag['src']
                        elif tag.name == 'video' and tag.has_attr('src'):
                            src = tag['src']
                        elif tag.name == 'source' and tag.has_attr('src'):
                            src = tag['src']
                        if src:
                            media_candidates.add(src)

                    for m in list(media_candidates):
                        # If iframe points to an embed page, parse it for .h5p file link
                        full_m = urljoin(url, m)
                        if re.search(r'h5p/embed', full_m):
                            try:
                                parsed_embed_url = urlparse(full_m)
                                query_params = parse_qs(parsed_embed_url.query)
                                if 'url' in query_params:
                                    h5p_file_url = query_params['url'][0]
                                    
                                    h5p_filename = get_clean_filename(h5p_file_url)
                                    if not h5p_filename.endswith('.h5p'):
                                        h5p_filename = f"{os.path.splitext(h5p_filename)[0]}.h5p"
                                    
                                    h5p_path = os.path.join(h5p_dir, h5p_filename)

                                    if not os.path.exists(h5p_path):
                                        print(f"📥 Téléchargement du package H5P: {h5p_filename}")
                                        logging.info(f"Downloading H5P package: {h5p_file_url}")
                                        file_resp = session.get(h5p_file_url, timeout=60, allow_redirects=True)
                                        file_resp.raise_for_status()
                                        with open(h5p_path, 'wb') as f:
                                            f.write(file_resp.content)
                                    else:
                                        print(f"ℹ️ Package H5P déjà téléchargé: {h5p_filename}")

                                    print(f"🔍 Inspection du package H5P: {h5p_filename}")
                                    logging.info(f"Inspecting H5P package: {h5p_filename}")
                                    with zipfile.ZipFile(h5p_path, 'r') as h5p_zip:
                                        main_library = ''
                                        if 'h5p.json' in h5p_zip.namelist():
                                            with h5p_zip.open('h5p.json') as h5p_json_file:
                                                h5p_json = json.load(h5p_json_file)
                                                main_library = h5p_json.get('mainLibrary')

                                        content_json = None
                                        if 'content/content.json' in h5p_zip.namelist():
                                            with h5p_zip.open('content/content.json') as content_file:
                                                content_json = json.load(content_file)

                                        if not content_json:
                                            continue

                                        def render_to_md(item):
                                            lines = []
                                            lib = item.get('library', '').split(' ')[0]
                                            params = item.get('params', {})

                                            if lib == 'H5P.Text':
                                                text = params.get('text', '')
                                                if text:
                                                    soup = BeautifulSoup(text, 'html.parser')
                                                    lines.append(soup.get_text('\n'))
                                            
                                            elif lib == 'H5P.Image':
                                                if params.get('file', {}).get('path'):
                                                    img_path = params['file']['path']
                                                    img_filename = os.path.basename(img_path)
                                                    img_local_path = os.path.join(h5p_dir, img_filename)
                                                    
                                                    found = False
                                                    for member in h5p_zip.namelist():
                                                        if member.endswith('/' + img_path):
                                                            if not os.path.exists(img_local_path):
                                                                with h5p_zip.open(member) as zf, open(img_local_path, 'wb') as f: f.write(zf.read())
                                                            found = True
                                                            break
                                                    if found:
                                                        lines.append(f"![{params.get('alt', img_filename)}]({img_filename})")

                                            elif lib == 'H5P.Video':
                                                if params.get('sources'):
                                                    video_path = params['sources'][0]['path']
                                                    if video_path.startswith('http'):
                                                        lines.append(f"[Vidéo externe]({video_path})")
                                                        media_candidates.add(video_path)
                                                    else:
                                                        video_filename = os.path.basename(video_path)
                                                        found = False
                                                        for member in h5p_zip.namelist():
                                                            if member.endswith('/' + video_path):
                                                                if not os.path.exists(os.path.join(h5p_dir, video_filename)):
                                                                    with h5p_zip.open(member) as zf, open(os.path.join(h5p_dir, video_filename), 'wb') as f: f.write(zf.read())
                                                                found = True
                                                                break
                                                        if found:
                                                            lines.append(f"[Vidéo locale]({video_filename})")
                                            
                                            elif lib in ['H5P.Column', 'H5P.CoursePresentation']:
                                                sub_content = params.get('content', [])
                                                if not sub_content and 'slides' in params:
                                                    sub_content = [slide['elements'] for slide in params.get('slides', [])]
                                                    sub_content = [item for sublist in sub_content for item in sublist]

                                                for sub_item in sub_content:
                                                    lines.extend(render_to_md(sub_item))

                                            if lines:
                                                lines.append('\n---\n')
                                            return lines

                                        if main_library == 'H5P.InteractiveBook':
                                            print(f"  - Livre interactif détecté. Extraction des chapitres.")
                                            chapters = content_json.get('chapters', [])
                                            for i, chapter in enumerate(chapters):
                                                chapter_title = chapter.get('title', f'Chapitre {i+1}')
                                                safe_chapter_title = re.sub(r'[^\w\-_.]', '_', chapter_title)
                                                chapter_filename = f"CHAP_{i+1}_{safe_chapter_title}.md"
                                                chapter_filepath = os.path.join(h5p_dir, chapter_filename)
                                                
                                                md_lines = [f"# {chapter_title}\n"]
                                                for content_item in chapter.get('content', []):
                                                    md_lines.extend(render_to_md(content_item))

                                                with open(chapter_filepath, 'w', encoding='utf-8') as f:
                                                    f.write('\n'.join(md_lines))
                                                print(f"  - Chapitre sauvegardé: {chapter_filename}")
                                        else:
                                            md_lines = [f"# {title}\n"]
                                            md_lines.extend(render_to_md(content_json))
                                            md_filename = f"{safe_title}.md"
                                            md_filepath = os.path.join(h5p_dir, md_filename)
                                            with open(md_filepath, 'w', encoding='utf-8') as f:
                                                f.write('\n'.join(md_lines))
                                            print(f"  - Contenu H5P sauvegardé: {md_filename}")
                                
                                media_candidates.remove(m)

                            except Exception as e:
                                logging.error(f"Failed to process H5P embed link {full_m}: {e}", exc_info=True)

                    downloaded_media = []

                    for media_url in media_candidates:
                        mf = _download_media(media_url, h5p_dir)
                        if mf:
                            downloaded_media.append(mf)



                    downloaded.append({'title': title, 'url': url, 'file': html_filename, 'folder': h5p_dir, 'type': 'html'})
            except Exception as e:
                print(f"❌ Erreur téléchargement H5P: {title} -> {url} : {e}")
                logging.warning(f"Erreur téléchargement H5P: {title} -> {url} : {e}")
        return downloaded

    # Téléchargement H5P et extraction déplacés après le crawl principal
    # ...existing code...

    # Lecture des identifiants depuis credentials.txt si présent
    credentials_file = 'credentials.txt'
    username = None
    password = None
    # Lecture des identifiants depuis credentials.txt si présent
    if os.path.exists(credentials_file):
        with open(credentials_file, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('username='):
                    username = line.strip().split('=',1)[1]
                elif line.startswith('password='):
                    password = line.strip().split('=',1)[1]
    # Si pas trouvés, demander à l'utilisateur
    if not username:
        username = input('Nom d\'utilisateur Moodle: ')
    if not password:
        password = getpass.getpass('Mot de passe Moodle: ')
    parser = argparse.ArgumentParser(
        description="Download all resources (PDFs, videos, documents, quizzes) from a Moodle course page after logging in."
    )
    parser.add_argument(
        '--login-url', required=True, help='The Moodle login URL (e.g., https://moodle.example.com/login/index.php)'
    )
    parser.add_argument(
        '--course-url', required=True, help='The Moodle course URL (e.g., https://moodle.example.com/course/view.php?id=123)'
    )
    parser.add_argument(
        '--out', default='downloaded_resources', help='Dossier de sortie principal'
    )

    args = parser.parse_args()

    with requests.Session() as session:
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-GPC': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-User': '?1',
            'Priority': 'u=0, i'
        })
        # Autoriser la décompression automatique
        session.headers['Accept-Encoding'] = 'gzip, deflate'
        try:
            login_to_moodle(session, args.login_url, username, password)
            print(f"✅ Logged in successfully. (login_url: {args.login_url})")
            logging.info(f"Logged in successfully. (login_url: {args.login_url})")

            # Le 'base_folder' est maintenant le dossier de sortie
            base_folder = os.path.abspath(args.out)

            # On utilise un set global pour les URLs visitées
            visited_urls = set()

            # Ajoute un Referer pour la première requête sur la page de cours
            session.headers['Referer'] = args.login_url

            resource_files, quiz_links, h5p_links = get_resource_links(session, args.course_url, visited=visited_urls, base_folder=base_folder)

            if not resource_files and not quiz_links and not h5p_links:
                print("⚠️ No resource files, quizzes, or H5P found on the course page.")
                logging.warning("No resource files, quizzes, or H5P found on the course page.")
                # Générer un résumé H5P vide
                generate_h5p_summary([], base_folder)
                return

            downloaded_files_count = download_resources(session, resource_files)

            downloaded_quizzes_count = 0
            quiz_filenames = []
            for quiz in quiz_links:
                result = download_quiz(session, quiz['url'], quiz['folder'])
                if result:
                    downloaded_quizzes_count += 1
                    quiz_filenames.append(result['filename'])

            # Téléchargement et extraction H5P APRÈS le crawl principal
            h5p_downloaded = download_h5p_activities(session, h5p_links)
            if h5p_downloaded:
                h5p_extracted = extract_h5p_text(h5p_downloaded)
                generate_h5p_summary(h5p_extracted, base_folder)
            else:
                generate_h5p_summary([], base_folder)

            print("\n--- RÉSUMÉ ---")
            print(f"✅ Page(s) HTML principale(s) sauvegardée(s).")
            print(f"✅ {downloaded_files_count} fichiers et liens téléchargés/sauvegardés.")
            print(f"✅ {downloaded_quizzes_count} quizzes sauvegardés (questions uniquement).")
            logging.info(f"Downloaded {downloaded_files_count} files and {downloaded_quizzes_count} quizzes.")

        except Exception as e:
            print(f"❌ Erreur: {e}")
            logging.error(f"Error: {e}", exc_info=True)

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"❌ An unexpected error occurred. See 'moodle_downloader.log' for details.")
        logging.exception("Unhandled exception occurred:")