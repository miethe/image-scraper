import os
import threading
from flask import Flask, request, jsonify, render_template, send_from_directory
from dotenv import load_dotenv
from scraper import scrape_site, logging # Import the logger too

load_dotenv() # Load environment variables from .env file if it exists

app = Flask(__name__)

# Configuration
# Use environment variable or default to './data' inside the container
OUTPUT_DIRECTORY = os.getenv('OUTPUT_DIR', '/app/data')
app.config['OUTPUT_DIR'] = OUTPUT_DIRECTORY
app.config['MAX_PAGES'] = int(os.getenv('MAX_PAGES', '50')) # Max pages from env or default

# Ensure output directory exists on startup
os.makedirs(app.config['OUTPUT_DIR'], exist_ok=True)

# --- Routes ---

@app.route('/', methods=['GET'])
def index():
    """Serves the simple HTML web form."""
    return render_template('index.html')

@app.route('/scrape', methods=['POST'])
def handle_scrape_request():
    """API endpoint to trigger scraping."""
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({"error": "Missing 'url' in JSON payload"}), 400

    url_to_scrape = data['url']
    if not url_to_scrape:
        return jsonify({"error": "'url' cannot be empty"}), 400

    # Basic URL validation (very simple)
    if not url_to_scrape.startswith(('http://', 'https://')):
         if '.' not in url_to_scrape: # Very basic check if it looks like a domain
              return jsonify({"error": "Invalid URL format"}), 400
         url_to_scrape = 'https://' + url_to_scrape # Attempt to fix if missing scheme

    # Run scraping in a background thread to avoid blocking the request
    # For production, consider a proper task queue (Celery, RQ)
    logging.info(f"Received scrape request for: {url_to_scrape}")
    thread = threading.Thread(
        target=run_scrape_background,
        args=(url_to_scrape, app.config['OUTPUT_DIR'], app.config['MAX_PAGES'])
    )
    thread.start()

    return jsonify({"message": "Scraping process started in background.", "url": url_to_scrape}), 202

def run_scrape_background(url, output_dir, max_pages):
    """Wrapper function to run scrape_site and handle results/errors in background."""
    logging.info(f"Background thread started for {url}")
    try:
        # Note: scrape_site already logs extensively
        output_path, count = scrape_site(url, output_dir, follow_pagination=True, max_pages=max_pages)
        logging.info(f"Background thread finished for {url}. Saved {count} images to {output_path}")
    except Exception as e:
        logging.error(f"Error in background scraping thread for {url}: {e}", exc_info=True) # Log traceback

# Optional: Add route to list/browse downloaded files (requires careful security considerations)
# @app.route('/files/')
# @app.route('/files/<path:req_path>')
# def list_files(req_path=''):
#     abs_path = os.path.join(app.config['OUTPUT_DIR'], req_path)
#     ... implementation needed ...

if __name__ == '__main__':
    # Use waitress or gunicorn in production instead of Flask's dev server
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)