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
from login import login_to_moodle
from resources import download_resources, get_resource_links
from quizzes import download_quiz
from h5p import download_h5p_activities, extract_h5p_text, generate_h5p_summary

# Configure logging to debug level
logging.basicConfig(
    filename='moodle_downloader.log',
    filemode='a',
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.DEBUG
)

# Update logging configuration to include console output
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
console_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
console_handler.setFormatter(console_formatter)
logging.getLogger().addHandler(console_handler)

logging.debug("Script started")

# Adding detailed logging to track the flow and identify issues
logging.info("Starting the Moodle resource downloader script")

def login_to_moodle(session, login_url, username, password):
    logging.info(f"=== LOGIN ATTEMPT ===")
    logging.debug(f"Login URL: {login_url}")
    logging.debug(f"Username: {username}")
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': 'https://www.stri.fr',
            'Referer': 'https://www.stri.fr/eformation/',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-User': '?1',
            'Priority': 'u=0, i',
            'TE': 'trailers'
        }

        resp = session.get(login_url, headers=headers, timeout=20)
        logging.debug(f"GET request status: {resp.status_code}")

        # Forcer le décodage correct de la réponse HTML
        # Check Content-Encoding and handle decompression gracefully
        if 'Content-Encoding' in resp.headers:
            try:
                if 'gzip' in resp.headers['Content-Encoding']:
                    import gzip
                    resp._content = gzip.decompress(resp.content)
                elif 'br' in resp.headers['Content-Encoding']:
                    import brotli
                    resp._content = brotli.decompress(resp.content)
                elif 'deflate' in resp.headers['Content-Encoding']:
                    import zlib
                    resp._content = zlib.decompress(resp.content)
            except Exception as decompress_error:
                logging.error(f"Decompression failed: {decompress_error}")
                logging.debug("Falling back to raw response content.")
                resp._content = resp.content
        else:
            logging.debug("No Content-Encoding header found. Using raw response content.")

        soup = BeautifulSoup(resp.text, 'html.parser')
        token_input = soup.find('input', attrs={'name': 'logintoken'})
        token = token_input['value'] if token_input else ''

        if token:
            logging.debug(f"Login token found: {token[:20]}...")
        else:
            logging.warning("No login token found on login page")
            logging.debug("Login page HTML content:")
            with open('login_page_debug.html', 'w', encoding='utf-8') as f:
                f.write(resp.text)
            logging.debug("Login page saved to login_page_debug.html for manual inspection.")

        data = {'username': username, 'password': password, 'logintoken': token}
        logging.debug(f"Posting login data to {login_url}")

        post = session.post(login_url, data=data, headers=headers, timeout=20, allow_redirects=True)
        logging.debug(f"POST response status: {post.status_code}")
        logging.debug(f"POST response URL: {post.url}")
        logging.debug(f"POST response length: {len(post.text)} bytes")
        logging.debug(f"Session cookies: {list(session.cookies.keys())}")

        # Vérifier si la redirection mène encore à une page de connexion
        post_soup = BeautifulSoup(post.text, 'html.parser')
        if post_soup.find('form', attrs={'action': login_url}):
            logging.error("Login failed: Still on login page after POST.")
            with open('post_login_debug.html', 'w', encoding='utf-8') as f:
                f.write(post.text)
            logging.debug("Post-login page saved to post_login_debug.html for manual inspection.")
            raise Exception("Login failed. Check credentials or login flow.")

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

def main():
    logging.debug("Entering main function")
    # Read credentials from file or prompt user
    credentials_file = 'credentials.txt'
    username, password = None, None
    if os.path.exists(credentials_file):
        with open(credentials_file, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('username='):
                    username = line.strip().split('=', 1)[1]
                elif line.startswith('password='):
                    password = line.strip().split('=', 1)[1]
    if not username:
        username = input("Nom d'utilisateur Moodle: ")
    if not password:
        password = getpass.getpass("Mot de passe Moodle: ")

    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description="Download all resources (PDFs, videos, documents, quizzes) from a Moodle course page after logging in."
    )
    parser.add_argument('--login-url', required=True, help='The Moodle login URL (e.g., https://moodle.example.com/login/index.php)')
    parser.add_argument('--course-url', required=True, help='The Moodle course URL (e.g., https://moodle.example.com/course/view.php?id=123)')
    parser.add_argument('--out', default='downloaded_resources', help='Dossier de sortie principal')
    args = parser.parse_args()

    # Add detailed logs to trace execution
    logging.debug("Parsing command-line arguments")
    logging.debug(f"Output folder: {args.out}")

    # Ensure the output directory exists
    os.makedirs(args.out, exist_ok=True)
    logging.debug(f"Output directory ensured: {args.out}")

    # Wrap main execution in try-except to catch silent exceptions
    try:
        logging.debug("Initializing session")
        with requests.Session() as session:
            logging.debug("Session initialized")
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

            logging.debug("Attempting to log in")
            login_to_moodle(session, args.login_url, username, password)
            logging.debug("Login successful")

            logging.debug("Fetching resource links")
            # Additional debug logs for resource extraction
            resource_files, quiz_links, h5p_links = get_resource_links(session, args.course_url, visited=set(), base_folder=args.out)
            logging.debug(f"Resources extracted: {len(resource_files)} files, {len(quiz_links)} quizzes, {len(h5p_links)} H5P links")
            logging.debug("Resource links fetched successfully")

            logging.debug("Downloading resources")
            # Download resources
            downloaded_files_count = download_resources(session, resource_files)

            logging.debug("Downloading quizzes")
            # Download quizzes
            downloaded_quizzes_count = 0
            quiz_filenames = []
            for quiz in quiz_links:
                result = download_quiz(session, quiz['url'], quiz['folder'])
                if result:
                    downloaded_quizzes_count += 1
                    quiz_filenames.append(result['filename'])

            # Download and process H5P activities
            h5p_downloaded = download_h5p_activities(session, h5p_links)
            if h5p_downloaded:
                h5p_extracted = extract_h5p_text(h5p_downloaded)
                generate_h5p_summary(h5p_extracted, args.out)
            else:
                generate_h5p_summary([], args.out)

            logging.debug("Resources downloaded successfully")
    except Exception as e:
        logging.error(f"An error occurred: {e}")
        raise

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

        try:
            # Login to Moodle
            logging.info("Attempting to log in to Moodle")
            login_to_moodle(session, args.login_url, username, password)
            print(f"✅ Logged in successfully. (login_url: {args.login_url})")
            logging.info(f"Logged in successfully. (login_url: {args.login_url})")

            # Set output folder
            base_folder = os.path.abspath(args.out)

            # Get resource links
            visited_urls = set()
            session.headers['Referer'] = args.login_url
            resource_files, quiz_links, h5p_links = get_resource_links(session, args.course_url, visited=visited_urls, base_folder=base_folder)

            logging.info("Extracting resources from Moodle")
            if not resource_files and not quiz_links and not h5p_links:
                print("⚠️ No resource files, quizzes, or H5P found on the course page.")
                logging.warning("No resource files, quizzes, or H5P found on the course page.")
                generate_h5p_summary([], base_folder)
                return

            # Download resources
            downloaded_files_count = download_resources(session, resource_files)

            # Download quizzes
            downloaded_quizzes_count = 0
            quiz_filenames = []
            for quiz in quiz_links:
                result = download_quiz(session, quiz['url'], quiz['folder'])
                if result:
                    downloaded_quizzes_count += 1
                    quiz_filenames.append(result['filename'])

            # Download and process H5P activities
            h5p_downloaded = download_h5p_activities(session, h5p_links)
            if h5p_downloaded:
                h5p_extracted = extract_h5p_text(h5p_downloaded)
                generate_h5p_summary(h5p_extracted, base_folder)
            else:
                generate_h5p_summary([], base_folder)

            # Print summary
            print("\n--- RÉSUMÉ ---")
            print(f"✅ {downloaded_files_count} fichiers et liens téléchargés/sauvegardés.")
            print(f"✅ {downloaded_quizzes_count} quizzes sauvegardés (questions uniquement).")
            logging.info(f"Downloaded {downloaded_files_count} files and {downloaded_quizzes_count} quizzes.")

        except Exception as e:
            print(f"❌ Erreur: {e}")
            logging.error(f"Error: {e}", exc_info=True)

        # Enhanced debugging for response content
        logging.debug("Fetching course page HTML")
        try:
            response = session.get(args.course_url, timeout=20)
            logging.debug(f"HTTP status code: {response.status_code}")
            logging.debug(f"Response headers: {response.headers}")
            content_type = response.headers.get('Content-Type', '')
            logging.debug(f"Content-Type: {content_type}")

            # Save raw response content for debugging
            raw_content_path = os.path.join(base_folder, 'course', 'raw_presentation_cours.bin')
            os.makedirs(os.path.dirname(raw_content_path), exist_ok=True)
            with open(raw_content_path, 'wb') as raw_file:
                raw_file.write(response.content)
            logging.info(f"Raw response content saved at {raw_content_path}")

            # Log the final URL after redirection
            logging.debug(f"Final URL after redirection: {response.url}")

            # Check if the response is redirected to the login page
            if 'login' in response.url:
                logging.warning("Request was redirected to the login page. Authentication might have failed.")

            # Check if the content is binary or text
            if 'text/html' in content_type:
                logging.debug("Detected HTML content. Saving as text.")
                course_page_path = os.path.join(base_folder, 'course', 'presentation_cours.html')
                with open(course_page_path, 'w', encoding='utf-8') as f:
                    f.write(response.text)
                logging.info(f"Course page HTML saved successfully at {course_page_path}")
            else:
                logging.warning("Content is not HTML. Saving as binary with .bin extension.")
                course_page_path = os.path.join(base_folder, 'course', 'presentation_cours.bin')
                with open(course_page_path, 'wb') as f:
                    f.write(response.content)
                logging.info(f"Course page binary content saved successfully at {course_page_path}")
        except Exception as e:
            logging.error(f"Failed to fetch or save course page content: {e}", exc_info=True)
            print(f"❌ Failed to fetch or save course page content: {e}")
    logging.debug("Exiting main function")

if __name__ == "__main__":
    logging.debug("Starting script execution")
    main()
    logging.debug("Script execution completed")