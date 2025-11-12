import logging
import os
import json
import re

def generate_h5p_summary(h5p_extracted, out_folder):
    """Generate a summary of H5P activities in Markdown and JSON formats."""
    md_lines = ["# Résumé des activités H5P\n"]
    summary = []
    for h5p in h5p_extracted:
        md_lines.append(f"## {h5p['title']}")
        md_lines.append(f"- URL: {h5p['url']}")
        md_lines.append(f"- Fichier: {h5p['file']}")
        md_lines.append(f"- Type: {h5p['type']}")
        if h5p['type'] == 'html':
            md_lines.append(f"- Interactions: {len(h5p.get('interactions', []))}")
        md_lines.append("")
        summary.append({k: h5p[k] for k in ['title', 'url', 'file', 'folder', 'type', 'text', 'interactions']})

    # Write Markdown summary
    md_path = os.path.join(out_folder, 'H5P_SUMMARY.md')
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(md_lines))

    # Write JSON summary
    json_path = os.path.join(out_folder, 'H5P_SUMMARY.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"📝 Résumé H5P généré: {md_path} et {json_path}")
    logging.info(f"H5P summary generated: {md_path} and {json_path}")

def extract_h5p_text(h5p_downloaded):
    """Extract text and interactions from downloaded H5P activities."""
    extracted = []
    for h5p in h5p_downloaded:
        if h5p['type'] == 'html':
            try:
                with open(h5p['file'], 'r', encoding='utf-8') as f:
                    content = f.read()
                # Extract interactions or other relevant data from the HTML content
                interactions = []  # Placeholder for actual extraction logic
                extracted.append({
                    'title': h5p['title'],
                    'url': h5p['url'],
                    'file': h5p['file'],
                    'folder': h5p['folder'],
                    'type': h5p['type'],
                    'text': content,
                    'interactions': interactions
                })
            except Exception as e:
                print(f"⚠️ Impossible d'extraire le texte de {h5p['file']}: {e}")
                logging.warning(f"Failed to extract text from {h5p['file']}: {e}", exc_info=True)
        else:
            extracted.append(h5p)
    return extracted

def download_h5p_activities(session, h5p_links):
    """Download H5P activities and save them locally."""
    downloaded = []
    for h5p in h5p_links:
        url = h5p['url']
        title = h5p['title']
        folder = h5p['folder']
        try:
            response = session.get(url, timeout=20)
            response.raise_for_status()

            # Save the H5P content
            safe_title = re.sub(r'[^\\w\-_.]', '_', title)
            filename = f"H5P_{safe_title}.html"
            filepath = os.path.join(folder, filename)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(response.text)

            downloaded.append({
                'title': title,
                'url': url,
                'file': filepath,
                'folder': folder,
                'type': 'html'
            })

            print(f"✅ Activité H5P sauvegardée: {filename}")
            logging.info(f"H5P activity saved: {filename}")
        except Exception as e:
            print(f"⚠️ Échec du téléchargement de l'activité H5P {url}: {e}")
            logging.warning(f"Failed to download H5P activity {url}: {e}", exc_info=True)
    return downloaded