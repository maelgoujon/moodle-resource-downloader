import logging
import re
from urllib.parse import urljoin, urlparse, unquote
import mimetypes
import requests
from bs4 import BeautifulSoup
import os

def is_valid_resource_url(url):
    return (
        'mod/url/view.php' in url or
        'mod/page/view.php' in url or
        'mod/resource/view.php' in url or
        'mod/quiz/view.php' in url or
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

def download_resources(session, resource_files):
    downloaded_files = 0

    for resource in resource_files:
        try:
            # Validate resource dictionary structure
            if not isinstance(resource, dict) or 'url' not in resource or 'folder' not in resource:
                raise ValueError(f"Invalid resource format: {resource}")

            # Extract values from the dictionary
            url = resource['url']
            folder = resource['folder']
            filename_hint = resource.get('filename_hint', None)
            rtype = resource.get('type', 'resource')

            # Sanitize URL
            if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
                raise ValueError(f"Invalid URL: {url}")

            # Ensure the folder exists
            os.makedirs(folder, exist_ok=True)

            # Special case: 'mod/page/view.php' already saved in get_resource_links
            if rtype == 'page':
                downloaded_files += 1
                continue

            response = session.get(url, timeout=30, allow_redirects=True)
            response.raise_for_status()

            final_url = response.url
            headers = response.headers

            # Determine the filename
            filename = filename_hint if filename_hint else get_clean_filename(final_url, headers)

            # Handle external links for 'mod/url/view.php'
            if 'mod/url/view.php' in url and not is_downloadable_content(headers.get('Content-Type', ''), final_url):
                filename = f"LIEN_{filename}.url"
                path = os.path.join(folder, filename)
                content = f"[InternetShortcut]\nURL={final_url}\n"
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                print(f"🔗 Lien externe sauvegardé: {filename}")
                logging.info(f"Lien externe sauvegardé: {final_url} -> {path}")
            else:
                # Download the file
                path = os.path.join(folder, filename)
                if os.path.exists(path):
                    print(f"ℹ️ Fichier déjà téléchargé: {filename}")
                    continue

                print(f"📥 Téléchargement {filename} -> {folder}")
                logging.info(f"Téléchargement {final_url} -> {path}")

                with open(path, 'wb') as f:
                    f.write(response.content)

                # Generate markdown for HTML resources
                if 'text/html' in headers.get('Content-Type', '') or filename.endswith('.html'):
                    md_filename = filename.rsplit('.', 1)[0] + '.md'
                    md_path = os.path.join(folder, md_filename)
                    md_content = f"# {filename.rsplit('.', 1)[0].replace('_', ' ')}\n\n[Voir la ressource HTML]({filename})\n"
                    with open(md_path, 'w', encoding='utf-8') as f:
                        f.write(md_content)
                    print(f"📝 Markdown généré: {md_filename}")
                    logging.info(f"Markdown généré: {md_path}")

            downloaded_files += 1

        except Exception as e:
            logging.error(f"Échec du téléchargement {resource}: {e}", exc_info=True)
            print(f"❌ Échec du téléchargement {resource}: {e}")

    return downloaded_files

def get_resource_links(session, course_url, visited=None, base_folder='resources'):
    if visited is None:
        visited = set()
    if course_url in visited:
        return [], [], []  # Retourne trois listes vides si déjà visité
    visited.add(course_url)

    print(f"📂 Crawling: {course_url}")
    logging.info(f"Crawling: {course_url}")

    try:
        response = session.get(course_url, timeout=20)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logging.error(f"Erreur lors de l'accès à {course_url}: {e}")
        return [], [], []

    soup = BeautifulSoup(response.text, 'html.parser')


    resource_links = []
    quiz_links = []
    h5p_links = []
    for link in soup.find_all('a', href=True):
        href = link['href']
        full_url = urljoin(course_url, href)
        link_text = link.get_text(strip=True) or 'H5P Activity'
        if is_valid_resource_url(href):
            if 'mod/quiz' in href:
                quiz_links.append({'url': full_url, 'folder': base_folder})
            elif 'mod/hvp/view.php' in href or 'mod/h5pactivity/view.php' in href:
                h5p_links.append({'url': full_url, 'folder': base_folder, 'title': link_text})
            else:
                resource_links.append({'url': full_url, 'folder': base_folder, 'type': 'resource'})
        elif 'mod/hvp/view.php' in href or 'mod/h5pactivity/view.php' in href:
            h5p_links.append({'url': full_url, 'folder': base_folder, 'title': link_text})
        else:
            logging.debug(f"Ressource ignorée: {href}")

    embedded_resources = []
    for media in soup.find_all(['img', 'video', 'audio'], src=True):
        src = media['src']
        full_url = urljoin(course_url, src)
        if is_valid_resource_url(src):
            embedded_resources.append({'url': full_url, 'folder': base_folder, 'type': 'embedded'})
        else:
            logging.debug(f"Ressource intégrée ignorée: {src}")

    return resource_links, quiz_links, h5p_links