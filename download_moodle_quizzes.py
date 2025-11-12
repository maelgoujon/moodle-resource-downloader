import requests
from bs4 import BeautifulSoup
import os
import json
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse
import getpass
import argparse
import re
import logging

logging.basicConfig(
    filename='moodle_quizzes.log',
    filemode='a',
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.INFO
)

def login_to_moodle(session, login_url, username, password):
    resp = session.get(login_url, timeout=20)
    soup = BeautifulSoup(resp.text, 'html.parser')
    token_input = soup.find('input', attrs={'name': 'logintoken'})
    token = token_input['value'] if token_input else ''
    data = {'username': username, 'password': password, 'logintoken': token}
    post = session.post(login_url, data=data, timeout=20)
    if "login" in post.url.lower():
        raise Exception("Login failed. Vérifiez vos identifiants.")
    return session

def sanitize_name(name):
    return re.sub(r'[^\w\-_\.]', '_', name).strip('_')

def find_quiz_links(session, course_url):
    resp = session.get(course_url, timeout=20)
    soup = BeautifulSoup(resp.text, 'html.parser')
    links = set()
    for a in soup.find_all('a', href=True):
        href = a['href']
        full = urljoin(course_url, href)
        if 'mod/quiz/view.php' in full:
            links.add(full.split('#')[0])
    return sorted(links)

def _split_concatenated_options(s):
    """
    Split heuristique pour options concaténées.
    - Priorise retours à la ligne et délimiteurs explicites.
    - Ne coupe pas sur mots capitalisés (évite de fragmenter "Wireless Private Area Network").
    - Fusionne fragments très courts (ex: "à") avec voisin utile.
    """
    if not s:
        return []
    s = s.strip()

    # 1) split on explicit newlines or multiple-line breaks
    parts = re.split(r'\r\n|\n{1,}|\r', s)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) > 1:
        return _merge_short_fragments(parts)

    # 2) split on obvious delimiters (double spaces, ;, |, bullets, dashes)
    parts = re.split(r'\s{2,}|;|\||•|–|—|--', s)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) > 1:
        return _merge_short_fragments(parts)

    # 3) split on numbered / bulleted single-line lists (e.g. "1) foo 2) bar")
    parts = re.split(r'\s*\d+\)\s*', s)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) > 1:
        return _merge_short_fragments(parts)

    # fallback: keep whole string (avoid splitting on capitalized words)
    return [s]

def _merge_short_fragments(parts):
    """
    Merge fragments that are obviously too short (single character like 'à', 'a', or artifact 'end')
    with a neighbor to reconstruct sensible options.
    Also filter out known junk tokens.
    """
    if not parts:
        return []
    cleaned = []
    i = 0
    while i < len(parts):
        cur = parts[i].strip()
        # drop common artifacts
        if cur.lower() in {'end', 'retirer la marque', 'effacer mon choix', 'remove choice', 'clear selection', 'marquer la question'}:
            i += 1
            continue
        # if fragment is very short (1-2 chars) and not an acronym (uppercase) -> merge
        if len(cur) <= 2 and not (cur.isupper() and len(cur) <= 3):
            # try merge with previous if exists
            if cleaned:
                cleaned[-1] = (cleaned[-1] + ' ' + cur).strip()
            else:
                # else merge with next if exists
                if i + 1 < len(parts):
                    parts[i+1] = (cur + ' ' + parts[i+1]).strip()
                else:
                    cleaned.append(cur)
            i += 1
            continue
        # remove single-word non-informative items like stray punctuation
        if re.fullmatch(r'[\W_]+', cur):
            i += 1
            continue
        cleaned.append(cur)
        i += 1

    # final pass: remove items that are too short (<=1) or duplicates while preserving order
    seen = set()
    out = []
    for x in cleaned:
        if not x or len(x.strip()) == 0:
            continue
        if len(x.strip()) <= 1 and not (x.isupper() and len(x.strip()) <= 3):
            continue
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def extract_questions(question_soup):
    """
    Extract questions and options from a quiz page block.
    Strategy:
    - Prefer labels linked to radio/checkbox inputs.
    - Otherwise look for list items (li), .answer/.choice blocks, or tail text after markers like "Veuillez choisir".
    - Use _split_concatenated_options to separate options, then normalize and dedupe.
    """
    questions = []
    qblocks = question_soup.find_all('div', class_=re.compile(r'que|question|formulation'), recursive=True)
    for qb in qblocks:
        # Get raw question text (prefer qtext-like container)
        qtext_tag = qb.find(class_=re.compile(r'qtext|formulation|prompt')) or qb.find(['h3', 'h4', 'legend'])
        raw_qtext = qtext_tag.get_text(separator='\n', strip=True) if qtext_tag else qb.get_text(separator='\n', strip=True)

        # remove pagination suffixes like "(page X sur Y)" from title-like texts
        raw_qtext = re.sub(r'\(page\s*\d+\s*(?:sur|of)\s*\d+\)', '', raw_qtext, flags=re.I).strip()

        # detect tail markers that indicate options follow in same block
        marker_re = re.compile(r'(Veuillez choisir|Veuillez cocher|Choisissez|Veuillez sélectionner|Veuillez choisir au moins|VRAI\s+FAUX|Vrai\s+Faux)', re.I)
        m = marker_re.search(raw_qtext)
        question_text = raw_qtext
        tail_text = ''
        if m:
            question_text = raw_qtext[:m.start()].strip()
            tail_text = raw_qtext[m.start():].strip()

        # 1) Prefer labels linked to inputs
        answers = []
        for inp in qb.find_all('input', {'type': re.compile(r'radio|checkbox', re.I)}):
            label_text = ''
            iid = inp.get('id')
            if iid:
                lab = qb.find('label', attrs={'for': iid})
                if lab:
                    label_text = lab.get_text(separator=' ', strip=True)
            if not label_text:
                # label may wrap input
                parent_label = inp.find_parent('label')
                if parent_label:
                    label_text = parent_label.get_text(separator=' ', strip=True)
            if not label_text:
                # sometimes text is next sibling
                nxt = inp.next_sibling
                if nxt and isinstance(nxt, str):
                    label_text = nxt.strip()
                elif nxt:
                    label_text = (nxt.get_text(separator=' ', strip=True) if hasattr(nxt, 'get_text') else '').strip()
            if label_text:
                answers.append(label_text)

        # 2) If no inputs found, look for list items or dedicated answer blocks
        if not answers:
            # look for <li> inside the question block
            for li in qb.find_all('li'):
                txt = li.get_text(separator=' ', strip=True)
                if txt:
                    answers.append(txt)
        if not answers:
            for ans_tag in qb.find_all(class_=re.compile(r'answer|réponse|choice|option|response|choices|single', re.I), recursive=True):
                txt = ans_tag.get_text(separator=' ', strip=True)
                if txt:
                    answers.append(txt)

        # 3) If still empty and tail_text present, split tail_text
        if not answers and tail_text:
            tail_clean = re.sub(r'^(Veuillez choisir.*?:?|Choisissez:?)', '', tail_text, flags=re.I).strip()
            candidates = _split_concatenated_options(tail_clean)
            answers.extend(candidates)

        # 4) If we have a single long candidate that still contains newlines, split it
        if len(answers) == 1:
            cand = answers[0]
            if '\n' in cand:
                parts = [p.strip() for p in cand.splitlines() if p.strip()]
                if len(parts) > 1:
                    answers = parts
            else:
                parts = _split_concatenated_options(cand)
                if len(parts) > 1:
                    answers = parts

        # final cleaning: remove junk, strip, dedupe preserving order
        cleaned = []
        seen = set()
        for a in answers:
            if not a:
                continue
            a2 = re.sub(r'\b(Retirer la marque|Effacer mon choix|Remove choice|Clear selection|Marquer la question)\b', '', a, flags=re.I).strip()
            # ignore tiny fragments (1-2 chars) unless valid acronyms (e.g. LAN)
            if len(a2) <= 2 and not (a2.isupper() and 2 <= len(a2) <= 4):
                continue
            if a2 and a2 not in seen:
                seen.add(a2)
                cleaned.append(a2)

        # fallback: if no answers found but qtext mentions Vrai/Faux => generate those options
        if not cleaned and re.search(r'\b(Vrai|Faux|VRAI|FAUX)\b', raw_qtext, re.I):
            cleaned = ['VRAI', 'FAUX']

        # fallback: if still empty, try to extract short lines after the question mark as options
        if not cleaned and '\n' in raw_qtext:
            lines = [l.strip() for l in raw_qtext.splitlines() if l.strip()]
            # assume first line is question
            if len(lines) > 1:
                possible_opts = lines[1:]
                possible_opts = [x for x in possible_opts if len(x) > 2]
                for x in possible_opts:
                    if x not in seen:
                        cleaned.append(x)
                        seen.add(x)

        # ensure we have a question_text
        if not question_text:
            # try header inside block
            h = qb.find(['h3', 'h4', 'legend'])
            if h:
                question_text = h.get_text(separator=' ', strip=True)
            else:
                # fallback first non-empty line
                question_text = raw_qtext.splitlines()[0].strip() if raw_qtext else ''

        # normalize whitespace and remove trailing artifacts
        question_text = re.sub(r'\s+', ' ', question_text).strip()
        if question_text:
            questions.append({'question': question_text, 'answers': cleaned})

    return questions

def _normalize(text):
    if not text:
        return ''
    s = text.strip()
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'^(?:\d+\.\s*)', '', s)  # remove leading numbering like "1. "
    s = s.strip()
    return s

def clean_questions(questions):
    """
    - Normalise texte des questions
    - Déduplique questions (texte égal ou très proche)
    - Déduplique options, filtre items vides/parasites
    """
    seen = {}
    order = []
    for q in questions:
        qtxt = _normalize(q.get('question', ''))
        if not qtxt:
            continue
        key = qtxt.lower()
        opts = q.get('answers', []) or []
        # normalize options
        norm_opts = []
        seen_opts = set()
        for o in opts:
            no = _normalize(o)
            if not no:
                continue
            # filter common junk
            if no.lower() in {'marquer la question', 'retirer la marque', 'effacer mon choix', 'end'}:
                continue
            if len(no) <= 2 and not (no.isupper() and 2 <= len(no) <= 4):
                continue
            if no not in seen_opts:
                seen_opts.add(no)
                norm_opts.append(no)
        # merge into seen if duplicate question
        if key in seen:
            # extend with new unique options preserving order
            for o in norm_opts:
                if o not in seen[key]:
                    seen[key].append(o)
        else:
            seen[key] = norm_opts
            order.append(key)

    cleaned = []
    for k in order:
        cleaned.append({'question': k, 'answers': seen.get(k, [])})
    return cleaned

def download_quiz(session, quiz_url, target_folder):
    try:
        page = session.get(quiz_url, timeout=20)
        soup = BeautifulSoup(page.text, 'html.parser')

        if "Ce test est fermé" in soup.get_text():
            logging.info(f"Quiz fermé: {quiz_url}")
            return None

        # tenter de trouver le formulaire de démarrage (si présent)
        start_form = soup.find('form', method=re.compile(r'post', re.I))
        form_action = urljoin(quiz_url, start_form['action']) if start_form and start_form.get('action') else quiz_url
        form_data = {}
        if start_form:
            for inp in start_form.find_all('input', {'name': True}):
                form_data[inp['name']] = inp.get('value', '')

        # démarrer tentative si possible
        if start_form:
            attempt = session.post(form_action, data=form_data, timeout=20)
        else:
            attempt = page  # pas de start form, utiliser la page telle quelle

        # collecte des questions : on tente d'abord de détecter la pagination "page X sur Y"
        questions = []
        combined_text = (attempt.text or '') + (page.text or '')
        m = re.search(r'page\s*\d+\s*(?:sur|of)\s*(\d+)', combined_text, re.I)
        if m:
            total = int(m.group(1))
            # Moodle indexe souvent les pages de 0..(total-1)
            for p in range(0, total):
                parsed = urlparse(attempt.url)
                qs = dict(parse_qsl(parsed.query))
                qs['page'] = str(p)
                new_query = urlencode(qs)
                page_url = urlunparse(parsed._replace(query=new_query))
                resp = session.get(page_url, timeout=20)
                questions.extend(extract_questions(BeautifulSoup(resp.text, 'html.parser')))
        else:
            # fallback : rechercher tous les "page=" disponibles et les parcourir
            pages = set(int(x) for x in re.findall(r'page=(\d+)', combined_text))
            if pages:
                for p in sorted(pages):
                    parsed = urlparse(attempt.url)
                    qs = dict(parse_qsl(parsed.query))
                    qs['page'] = str(p)
                    page_url = urlunparse(parsed._replace(query=urlencode(qs)))
                    resp = session.get(page_url, timeout=20)
                    questions.extend(extract_questions(BeautifulSoup(resp.text, 'html.parser')))
            else:
                # dernier recours : navigation "next" comme avant
                current = attempt
                visited = set()
                while True:
                    if current.url in visited:
                        break
                    visited.add(current.url)
                    qsoup = BeautifulSoup(current.text, 'html.parser')
                    questions.extend(extract_questions(qsoup))

                    next_input = qsoup.find('input', {'type': 'submit', 'name': re.compile(r'next', re.I)})
                    next_form = next_input.find_parent('form') if next_input else None
                    if not next_form:
                        break

                    next_action = urljoin(current.url, next_form.get('action', current.url))
                    next_data = {}
                    for inp in next_form.find_all('input', {'name': True}):
                        next_data[inp['name']] = inp.get('value', '')

                    try:
                        current = session.post(next_action, data=next_data, timeout=20)
                    except Exception:
                        break

        # nom du fichier
        last_soup = BeautifulSoup((attempt.text if 'attempt' in locals() else page.text), 'html.parser')
        quiz_title = last_soup.find('title').get_text(strip=True) if last_soup.find('title') else urlparse(quiz_url).path.split('/')[-1]
        safe = sanitize_name(quiz_title)
        os.makedirs(target_folder, exist_ok=True)
        filename = f"{safe}.json"
        filepath = os.path.join(target_folder, filename)
        questions = clean_questions(questions)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump({'quiz_title': quiz_title, 'source_url': quiz_url, 'questions': questions}, f, ensure_ascii=False, indent=2)
        logging.info(f"Saved quiz: {filepath}")
        return filepath
    except Exception as e:
        logging.exception(f"Erreur download_quiz {quiz_url}: {e}")
        return None

def main():
    parser = argparse.ArgumentParser(description="Télécharger uniquement les QCM d'une page de cours Moodle.")
    parser.add_argument('--login-url', required=True, help='URL de login Moodle (ex: https://moodle.example.com/login/index.php)')
    parser.add_argument('--course-url', required=True, help='URL du cours Moodle (ex: https://moodle.example.com/course/view.php?id=123)')
    parser.add_argument('--out', default='downloaded_quizzes', help='Dossier de sortie')
    args = parser.parse_args()

    username = input("Nom d'utilisateur: ")
    password = getpass.getpass("Mot de passe: ")

    with requests.Session() as session:
        try:
            login_to_moodle(session, args.login_url, username, password)
            print("✅ Connecté.")
            logging.info("Connexion réussie.")

            resp = session.get(args.course_url, timeout=20)
            soup = BeautifulSoup(resp.text, 'html.parser')
            course_title = soup.title.get_text(strip=True) if soup.title else 'course'
            safe_course = sanitize_name(course_title)
            target_folder = os.path.join(args.out, safe_course)

            quiz_links = find_quiz_links(session, args.course_url)
            if not quiz_links:
                print("⚠️ Aucun QCM trouvé.")
                return

            saved = 0
            for qurl in quiz_links:
                print(f"🔎 Traitement: {qurl}")
                path = download_quiz(session, qurl, target_folder)
                if path:
                    saved += 1
                    print(f"✅ QCM sauvegardé: {path}")
                else:
                    print(f"⚠️ Échec pour: {qurl}")

            print(f"✅ Terminé. QCM sauvegardés: {saved}/{len(quiz_links)}")
        except Exception as e:
            print(f"❌ Erreur: {e}")
            logging.exception("Erreur principale:")

if __name__ == '__main__':
    main()