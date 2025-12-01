import logging
import re
import unicodedata
from urllib.parse import urljoin, urlparse, unquote
import mimetypes
import requests
from bs4 import BeautifulSoup
import os

MAX_VIDEO_BYTES = 20 * 1024 * 1024
VIDEO_EXTENSIONS = {'.mp4', '.webm', '.ogg', '.mov', '.avi', '.mkv'}

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

def sanitize_section_name(title):
    if not title:
        return None
    normalized = unicodedata.normalize('NFKD', title)
    ascii_title = normalized.encode('ascii', 'ignore').decode('ascii')
    cleaned = re.sub(r'[^\w\s\-_.]', '_', ascii_title).strip()
    cleaned = re.sub(r'\s+', '_', cleaned)
    return cleaned or None

def extract_section_title(section):
    """Return the most likely human-friendly title for a Moodle section."""
    primary_selectors = [
        '.sectionname',
        '.section-title',
        '.sectiontitle',
        'h3',
        'h2',
        'h4'
    ]

    for selector in primary_selectors:
        tag = section.select_one(selector)
        if tag:
            text = tag.get_text(strip=True)
            if text:
                return text

    aria_label = section.get('aria-label')
    if aria_label:
        aria_label = aria_label.strip()
        if aria_label:
            return aria_label

    accesshide = section.select_one('span.accesshide')
    if accesshide:
        text = accesshide.get_text(strip=True)
        generic_labels = {'url', 'page', 'fichier', 'resource', 'ressource'}
        if text and text.lower() not in generic_labels:
            return text

    return None

def is_video_resource(content_type, url):
    content_type = (content_type or '').lower()
    if content_type.startswith('video/'):
        return True
    path = urlparse(url).path
    _, ext = os.path.splitext(path)
    return ext.lower() in VIDEO_EXTENSIONS

def format_bytes(size):
    return f"{size / (1024 * 1024):.2f} Mo"

def collect_section_nodes(soup):
    selectors = [
        'li[id^="section-"]',
        'div[id^="section-"]'
    ]
    seen = set()
    sections = []
    for selector in selectors:
        for node in soup.select(selector):
            key = (node.get('id') or '', node.name)
            if key in seen:
                continue
            seen.add(key)
            sections.append(node)
    if sections:
        return sections

    data_sections = []
    for node in soup.select('[data-sectionid]'):
        section_id = node.get('data-sectionid')
        if not section_id:
            continue
        key = (section_id, node.name)
        if key in seen:
            continue
        seen.add(key)
        data_sections.append(node)
    return data_sections

def find_course_sections(soup):
    main_region = soup.find('section', id='region-main')
    targets = []
    if main_region:
        targets.append(main_region)
        course_content = main_region.select_one('.course-content')
        if course_content:
            targets.append(course_content)
    course_content = soup.select_one('.course-content')
    if course_content:
        targets.append(course_content)

    for target in targets:
        sections = target.select('li[id^="section-"]')
        if sections:
            return sections
        sections = target.select('div[id^="section-"]')
        if sections:
            return sections
    return collect_section_nodes(soup)

def get_final_file_url(session, url):
    try:
        response = session.get(url, timeout=20)
        soup = BeautifulSoup(response.text, 'html.parser')

        download_link = soup.find('a', href=re.compile(r'/mod/resource/.*&redirect=1'))
        if download_link:
            final_url = urljoin(url, download_link['href'])
            logging.debug(f"get_final_file_url: Found resource link: {final_url}")
            return final_url

        if 'mod/page/view.php' in url:
            logging.debug(f"get_final_file_url: mod/page/view.php detected")
            return url

        external_link = soup.find('div', class_='urlworkaround')
        if external_link and external_link.find('a'):
            final_url = external_link.find('a')['href']
            logging.debug(f"get_final_file_url: Found external URL: {final_url}")
            return final_url

        iframe = soup.find('iframe', src=True)
        if iframe:
            iframe_src = urljoin(url, iframe['src'])
            if re.search(r'\.(pdf|mp4|webm|ogg|mov|avi)$', iframe_src, re.IGNORECASE):
                logging.debug(f"get_final_file_url: Found iframe: {iframe_src}")
                return iframe_src

        file_link = soup.find('a', href=re.compile(r'\.(pdf|docx?|xlsx?|pptx?|zip|txt|jpg|jpeg|png|mp4|webm|ogg|mov|avi)$', re.IGNORECASE))
        if file_link:
            final_url = urljoin(url, file_link['href'])
            logging.debug(f"get_final_file_url: Found file link: {final_url}")
            return final_url

        source_tag = soup.find('source', src=True)
        if source_tag:
            final_url = urljoin(url, source_tag['src'])
            logging.debug(f"get_final_file_url: Found source tag: {final_url}")
            return final_url

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

            response = session.get(url, timeout=30, allow_redirects=True, stream=True)
            response.raise_for_status()

            final_url = response.url
            headers = response.headers
            content_type = headers.get('Content-Type', '').lower()

            # Try to resolve embedded viewers (e.g., mod/resource/pdf viewer) to the actual file
            if 'text/html' in content_type:
                resolved_url = get_final_file_url(session, response.url)
                response.close()
                if resolved_url and resolved_url != final_url:
                    logging.info(f"Resolved viewer link {final_url} -> {resolved_url}")
                    response = session.get(resolved_url, timeout=30, allow_redirects=True, stream=True)
                    response.raise_for_status()
                    final_url = response.url
                    headers = response.headers
                    content_type = headers.get('Content-Type', '').lower()
                else:
                    logging.warning(f"Viewer page did not expose downloadable link: {url}")
                    continue

            # Determine the filename
            filename = filename_hint if filename_hint else get_clean_filename(final_url, headers)

            is_video = is_video_resource(content_type, final_url)
            content_length = headers.get('Content-Length')
            if is_video and content_length:
                try:
                    size_bytes = int(content_length)
                    if size_bytes > MAX_VIDEO_BYTES:
                        response.close()
                        logging.info(
                            f"Skipping video larger than 20Mo: {final_url} ({format_bytes(size_bytes)})"
                        )
                        print(f"⏭️ Vidéo ignorée (>20 Mo): {filename}")
                        continue
                except ValueError:
                    pass

            # Handle external links for 'mod/url/view.php'
            if 'mod/url/view.php' in url and not is_downloadable_content(headers.get('Content-Type', ''), final_url):
                filename = f"LIEN_{filename}.url"
                path = os.path.join(folder, filename)
                content = f"[InternetShortcut]\nURL={final_url}\n"
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                print(f"🔗 Lien externe sauvegardé: {filename}")
                logging.info(f"Lien externe sauvegardé: {final_url} -> {path}")
                response.close()
            else:
                # Skip saving raw HTML pages or derived markdown files
                if 'text/html' in headers.get('Content-Type', '').lower() or filename.endswith('.html'):
                    logging.info(f"Skipping HTML resource storage for {final_url}")
                    print(f"⚠️ Ressource HTML ignorée: {filename}")
                    response.close()
                    continue

                path = os.path.join(folder, filename)
                if os.path.exists(path):
                    response.close()
                    print(f"ℹ️ Fichier déjà téléchargé: {filename}")
                    continue

                print(f"📥 Téléchargement {filename} -> {folder}")
                logging.info(f"Téléchargement {final_url} -> {path}")

                bytes_written = 0
                skip_large_video = False
                with open(path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if not chunk:
                            continue
                        bytes_written += len(chunk)
                        if is_video and bytes_written > MAX_VIDEO_BYTES:
                            skip_large_video = True
                            break
                        f.write(chunk)

                response.close()

                if skip_large_video:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    logging.info(
                        f"Stopped video download once size exceeded 20Mo: {final_url} ({format_bytes(bytes_written)})"
                    )
                    print(f"⏭️ Vidéo interrompue (>20 Mo): {filename}")
                    continue

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
    seen_urls = set()

    sections = find_course_sections(soup)
    if not sections:
        sections = [soup]

    for index, section in enumerate(sections, start=1):
        title_text = extract_section_title(section)
        sanitized_title = sanitize_section_name(title_text)
        folder_name = sanitized_title if sanitized_title else f"section_{index}"
        section_folder = os.path.join(base_folder, folder_name)
        logging.debug(f"Section {index}: title={title_text or 'n/a'} folder={section_folder}")

        for link in section.find_all('a', href=True):
            href = link['href']
            full_url = urljoin(course_url, href)
            if not full_url or full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            link_text = link.get_text(strip=True) or 'Ressource'

            if is_valid_resource_url(href):
                if 'mod/quiz' in href:
                    quiz_links.append({'url': full_url, 'folder': section_folder})
                elif 'mod/hvp/view.php' in href or 'mod/h5pactivity/view.php' in href:
                    h5p_links.append({'url': full_url, 'folder': section_folder, 'title': link_text})
                else:
                    resource_links.append({'url': full_url, 'folder': section_folder, 'type': 'resource'})
            elif 'mod/hvp/view.php' in href or 'mod/h5pactivity/view.php' in href:
                h5p_links.append({'url': full_url, 'folder': section_folder, 'title': link_text})
            else:
                logging.debug(f"Ressource ignorée: {href}")

    return resource_links, quiz_links, h5p_links