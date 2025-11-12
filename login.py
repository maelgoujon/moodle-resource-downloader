import logging
from bs4 import BeautifulSoup
import requests

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