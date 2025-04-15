import os
import threading
import time
import queue
from flask import Flask, request, jsonify, render_template, send_from_directory, Response, stream_with_context
from dotenv import load_dotenv
from scraper import scrape_site, logging # Import the logger too
from urllib.parse import unquote # To decode filenames from URL

load_dotenv() # Load environment variables from .env file if it exists

app = Flask(__name__)

# Configuration
# Use environment variable or default to './data' inside the container
OUTPUT_DIRECTORY = os.getenv('OUTPUT_DIR', '/app/data')
IMAGE_SERVE_PATH = "/images" # Base URL path for serving images
app.config['OUTPUT_DIR'] = OUTPUT_DIRECTORY
app.config['MAX_PAGES'] = int(os.getenv('MAX_PAGES', '50')) # Max pages from env or default

# Ensure output directory exists on startup
os.makedirs(app.config['OUTPUT_DIR'], exist_ok=True)

# Thread-safe queue for SSE updates
image_update_queue = queue.Queue()

# --- Routes ---

@app.route('/', methods=['GET'])
def index():
    """Serves the simple HTML web form."""
    return render_template('index.html')

@app.route('/scrape', methods=['POST'])
def handle_scrape_request():
    """API endpoint to trigger scraping."""
    # --- Clear the queue for a new request ---
    while not image_update_queue.empty():
        try:
            image_update_queue.get_nowait()
        except queue.Empty:
            break
    logging.info("Cleared image update queue for new request.")
    
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
        args=(url_to_scrape, app.config['OUTPUT_DIR'], image_update_queue, IMAGE_SERVE_PATH, app.config['MAX_PAGES']) # Pass queue and serve path
    )
    thread.start()

    return jsonify({"message": "Scraping process started. Updates will stream.", "url": url_to_scrape}), 202 # Status 202 Accepted

def run_scrape_background(url, output_dir, update_queue, base_serve_path, max_pages):
    """Wrapper function to run scrape_site and handle results/errors in background."""
    logging.info(f"Background thread started for {url}")
    try:
        # Note: scrape_site already logs extensively
        # Pass the queue and the base path for serving images
        # Use keyword arguments for clarity, especially for those with defaults
        output_path, count = scrape_site(
            start_url=url,
            output_dir=output_dir,
            image_update_queue=update_queue,
            base_image_serve_path=base_serve_path,
            follow_pagination=True, # Explicitly True as intended
            max_pages=max_pages
        )
        logging.info(f"Background thread finished for {url}. Saved {count} images to {output_path}")
    except Exception as e:
        logging.error(f"Error in background scraping thread for {url}: {e}", exc_info=True) # Log traceback
        # Ensure the end signal is sent even if the scraper crashes badly
        if update_queue:
            update_queue.put(None) # Signal end on error too

@app.route('/images/<path:domain_and_filename>')
def serve_image(domain_and_filename):
    """Serves images from the data directory."""
    # Security: Basic path cleaning, send_from_directory handles traversal
    safe_path = os.path.normpath(domain_and_filename)
    if safe_path.startswith("..") or os.path.isabs(safe_path):
        return "Forbidden", 403

    # Unquote the filename part in case it contains URL-encoded characters
    # It expects "domain/filename.jpg"
    parts = safe_path.split('/', 1)
    if len(parts) == 2:
        domain, filename = parts
        filename = unquote(filename) # Decode filename
        directory = os.path.join(app.config['OUTPUT_DIR'], domain)
        logging.debug(f"Attempting to serve image: Directory='{directory}', Filename='{filename}'")
        try:
            # send_from_directory is safer for serving files
            return send_from_directory(directory, filename)
        except FileNotFoundError:
             logging.warning(f"Image not found: {os.path.join(directory, filename)}")
             return "Image not found", 404
        except Exception as e:
            logging.error(f"Error serving image {safe_path}: {e}")
            return "Error serving image", 500
    else:
        logging.warning(f"Invalid image path format: {safe_path}")
        return "Invalid image path", 400
    
@app.route('/stream')
def stream_image_updates():
    """Server-Sent Events endpoint to push image URLs."""
    def event_stream():
        logging.info("SSE client connected. Starting stream.")
        try:
            while True:
                # Wait for a new image URL from the queue
                img_url = image_update_queue.get() # This blocks until an item is available

                if img_url is None: # Check for the end signal
                    logging.info("End signal received in queue. Sending end-stream event.")
                    yield "event: end-stream\ndata: finished\n\n"
                    break # Exit the loop to close the stream

                logging.debug(f"SSE sending image URL: {img_url}")
                # Format as Server-Sent Event message: data: <message>\n\n
                yield f"data: {img_url}\n\n"
                # Optional: Add a small delay if needed, but queue.get() handles waiting
                # time.sleep(0.1)
        except GeneratorExit:
            # This happens if the client disconnects
            logging.info("SSE client disconnected.")
        except Exception as e:
            logging.error(f"Error in SSE stream generator: {e}", exc_info=True)
        finally:
            # Clean up or final message if needed
            logging.info("SSE stream generator finished.")

    # Return Response with correct mimetype and the generator
    return Response(stream_with_context(event_stream()), mimetype='text/event-stream')

# Optional: Add route to list/browse downloaded files (requires careful security considerations)
# @app.route('/files/')
# @app.route('/files/<path:req_path>')
# def list_files(req_path=''):
#     abs_path = os.path.join(app.config['OUTPUT_DIR'], req_path)
#     ... implementation needed ...

if __name__ == '__main__':
    # Use waitress or gunicorn in production instead of Flask's dev server
    port = int(os.environ.get('PORT', 5000))
    # Use threaded=True for Flask dev server to handle concurrent requests (SSE + API)
    app.run(debug=False, host='0.0.0.0', port=port, threaded=True)