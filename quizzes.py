import logging
import json
import re
import os
from urllib.parse import urljoin
from bs4 import BeautifulSoup

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
        safe_quiz_title = re.sub(r'[^\\w\-_.]', '_', quiz_title)
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
                    f.write(quiz_soup.prettify())
                print(f"📄 Page HTML du quiz fermé sauvegardée: {html_path}")
                logging.info(f"Quiz fermé HTML saved: {html_path}")
            except Exception as e:
                print(f"⚠️ Impossible de sauvegarder la page HTML du quiz fermé: {e}")
                logging.warning(f"Impossible de sauvegarder la page HTML du quiz fermé: {e}", exc_info=True)
            return None

        # Tenter de trouver une tentative existante (relecture)
        review_link = quiz_soup.find('a', href=re.compile(r'review\\.php\\?attempt='))
        if review_link:
            print(f"ℹ️ Tentative de relecture trouvée pour {safe_quiz_title}")
            review_url = urljoin(quiz_url, review_link['href'])
            attempt_page = session.get(review_url, timeout=20)
            logging.info(f"Using existing review attempt for {safe_quiz_title}")
        else:
            # Sinon, démarrer une nouvelle tentative
            start_form = quiz_soup.find('form', {'method': 'post'}, action=re.compile(r'attempt\\.php'))
            if not start_form:
                print(f"⚠️ Pas de formulaire de démarrage de quiz trouvé sur {quiz_url}")
                logging.warning(f"Pas de formulaire de démarrage de quiz trouvé sur {quiz_url}")
                # Sauvegarder la page HTML du quiz pour référence
                html_path = os.path.join(target_folder, f"{safe_quiz_title}_INACCESSIBLE.html")
                try:
                    with open(html_path, 'w', encoding='utf-8') as f:
                        f.write(quiz_soup.prettify())
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
                page_response = session.get(page_url, timeout=20)
                page_soup = BeautifulSoup(page_response.text, 'html.parser')
                questions.extend(extract_questions(page_soup))
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