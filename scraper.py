import os
import requests
import time
import re
import hashlib
from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode, quote
from urllib.robotparser import RobotFileParser
from bs4 import BeautifulSoup
from collections import deque
import logging
from queue import Queue
import threading
from interactive_scraper import scrape_interactive_images

# --- Configuration ---
# Consider moving these to env variables or a config file
REQUEST_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 ImageScraperBot/1.0 (+https://your-contact-info.com)" # Be a good bot!
DELAY_BETWEEN_REQUESTS = 1 # Seconds
MAX_PAGES_TO_CRAWL = 50 # Limit crawl scope
# Filter common/small icons (adjust regex/size as needed)
COMMON_ICON_PATTERNS = re.compile(r'/icons?/|/social/|/nav/|favicon|logo|spinner|loader|rating|cart|search|user|account|menu|arrow|/flags/', re.IGNORECASE)
MIN_IMAGE_DIMENSION = 64 # Minimum width/height for non-SVG if detectable (heuristic)

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Scrape Control Class ---
class ScrapeControl:
    """Thread-safe control for scraping operations (pause/stop)."""
    def __init__(self):
        self._lock = threading.Lock()
        self._paused = False
        self._stopped = False

    def pause(self):
        with self._lock:
            self._paused = True
            logging.info("Scraping paused")

    def resume(self):
        with self._lock:
            self._paused = False
            logging.info("Scraping resumed")

    def stop(self):
        with self._lock:
            self._stopped = True
            self._paused = False
            logging.info("Scraping stopped")

    def is_paused(self):
        with self._lock:
            return self._paused

    def is_stopped(self):
        with self._lock:
            return self._stopped

    def wait_if_paused(self):
        """Blocks while paused, returns True if stopped."""
        while self.is_paused():
            time.sleep(0.5)
        return self.is_stopped()

# --- Robots.txt Handling ---
robot_parsers = {} # Cache parsers per domain

def can_fetch(url):
    parsed_url = urlparse(url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    robots_url = urljoin(base_url, "/robots.txt")

    if base_url not in robot_parsers:
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            logging.info(f"Fetching robots.txt from {robots_url}")
            parser.read()
            robot_parsers[base_url] = parser
            time.sleep(DELAY_BETWEEN_REQUESTS) # Delay after robots.txt fetch
        except Exception as e:
            logging.warning(f"Could not fetch or parse robots.txt at {robots_url}: {e}")
            # Be cautious: Assume disallowed if robots.txt fails? Or allow? Let's allow but warn.
            robot_parsers[base_url] = None # Mark as failed
            return True # Proceed with caution

    parser = robot_parsers.get(base_url)
    if parser:
        return parser.can_fetch(USER_AGENT, url)
    else:
        # If fetching robots.txt failed previously, proceed but log it
        logging.warning(f"Proceeding without robots.txt check for {url} (fetch previously failed)")
        return True


# --- URL Cleaning for High-Res Images ---
def clean_image_url(url):
    """Attempts to remove common resizing/quality parameters from image URLs."""
    # Common query parameters to remove
    params_to_remove = [
        'width', 'height', 'w', 'h', 'size', 'quality', 'q', 'fit', 'crop',
        'maxWidth', 'maxHeight', 'scale', 'res', 'resize', 'fm', 'format', 'auto', 'dpr'
    ]

    try:
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)

        # Remove query parameters
        query_params = {k: v for k, v in query_params.items() if k.lower() not in params_to_remove}

        # Reconstruct URL
        new_query = urlencode(query_params, doseq=True)
        new_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

        if new_url != url:
             logging.debug(f"Cleaned image URL: {url} -> {new_url}")
        return new_url

    except Exception as e:
        logging.warning(f"Error cleaning URL {url}: {e}")
        return url # Return original if cleaning fails

def try_get_high_res_image(session, url, timeout=DOWNLOAD_TIMEOUT):
    """Attempts to fetch a higher resolution version of an image with fallback."""
    # Try the cleaned URL first
    cleaned_url = clean_image_url(url)

    # List of variations to try (from most likely to work to least)
    urls_to_try = [cleaned_url]

    # If URL has common resize patterns in path, try variations
    parsed = urlparse(cleaned_url)
    path = parsed.path

    # Try removing size suffixes like _300x200.jpg -> .jpg
    size_pattern = re.compile(r'_\d+x\d+(\.[a-zA-Z]+)$')
    if size_pattern.search(path):
        new_path = size_pattern.sub(r'\1', path)
        new_url = urlunparse((parsed.scheme, parsed.netloc, new_path, parsed.params, parsed.query, parsed.fragment))
        if new_url not in urls_to_try:
            urls_to_try.append(new_url)

    # Try common high-res query parameters
    if '?' in cleaned_url or '&' in cleaned_url:
        # Some CDNs use explicit quality/size parameters
        high_res_variants = []
        base_url = cleaned_url.split('?')[0] if '?' in cleaned_url else cleaned_url

        # Try common high-res patterns
        for variant in [
            f"{base_url}?quality=100",
            f"{base_url}?w=2048",
            f"{base_url}?size=large",
        ]:
            if variant not in urls_to_try:
                high_res_variants.append(variant)

        urls_to_try.extend(high_res_variants)

    # Add original URL as final fallback
    if url not in urls_to_try:
        urls_to_try.append(url)

    # Try each URL variation
    for attempt_url in urls_to_try:
        try:
            logging.debug(f"Attempting to fetch: {attempt_url}")
            response = session.get(attempt_url, timeout=timeout, stream=True)
            response.raise_for_status()

            # Success! Return this version
            if attempt_url != url:
                logging.info(f"Successfully fetched higher-res variant: {attempt_url}")
            return response, attempt_url

        except requests.exceptions.RequestException as e:
            logging.debug(f"Failed to fetch {attempt_url}: {e}")
            continue

    # All attempts failed
    return None, None

# --- Image Hashing ---
def get_image_hash(content):
    """Generates a SHA256 hash for the image content."""
    return hashlib.sha256(content).hexdigest()

# --- Main Scraping Function ---
def scrape_site(start_url, output_dir, image_update_queue: Queue = None, base_image_serve_path="/images", follow_pagination=True, max_pages=MAX_PAGES_TO_CRAWL, depth=1, control: ScrapeControl = None, use_browser=False):
    """
    Scrapes a website starting from start_url for images.

    Args:
        start_url (str): The URL to start scraping from.
        output_dir (str): The base directory to save images to.
        image_update_queue (Queue, optional): Queue to put accessible image URLs for SSE.
        base_image_serve_path (str): The base HTTP path where images will be served from.
        follow_pagination (bool): Whether to follow links likely indicating pagination.
        max_pages (int): Maximum number of pages to crawl.
        depth (int): Maximum link depth to follow (0=start page only, 1=start+direct links, etc.)
        control (ScrapeControl, optional): Control object for pause/stop functionality.
        use_browser (bool): Whether to use browser automation for JavaScript-heavy sites and interactive galleries.
    """
    if not start_url.startswith(('http://', 'https://')):
        start_url = 'https://' + start_url
        logging.info(f"Prepended https:// to URL: {start_url}")

    parsed_start_url = urlparse(start_url)
    base_domain = parsed_start_url.netloc
    # Subdirectory within output_dir for this specific domain
    domain_output_path = os.path.join(output_dir, base_domain)
    os.makedirs(domain_output_path, exist_ok=True)
    logging.info(f"Saving images to filesystem path: {domain_output_path}")
    logging.info(f"Crawl depth limit: {depth}")

    # Track URLs with their depth: (url, current_depth)
    urls_to_visit = deque([(start_url, 0)])
    visited_urls = set([start_url])
    found_image_srcs = set() # Track image source URLs to avoid re-downloading based on URL
    downloaded_image_hashes = set() # Track hashes to avoid saving duplicates with different URLs
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    pages_crawled = 0
    total_images_downloaded = 0 # Keep track of count

    # Use browser automation if requested (for JavaScript-heavy sites)
    if use_browser:
        logging.info(f"Using browser automation to scrape interactive content from: {start_url}")
        try:
            interactive_images = scrape_interactive_images(start_url, headless=True)
            logging.info(f"Browser automation found {len(interactive_images)} images")

            # Process and download the images found via browser
            for img_url in interactive_images:
                if control and control.wait_if_paused():
                    logging.info("Scraping stopped by user during browser automation")
                    break

                if img_url in found_image_srcs:
                    continue
                found_image_srcs.add(img_url)

                # Download and save the image
                img_content, final_url = try_get_high_res_image(img_url, session)
                if img_content:
                    img_hash = get_image_hash(img_content)
                    if img_hash not in downloaded_image_hashes:
                        downloaded_image_hashes.add(img_hash)

                        # Determine filename
                        parsed_img_url = urlparse(final_url if final_url else img_url)
                        img_filename = os.path.basename(parsed_img_url.path)
                        if not img_filename or '.' not in img_filename:
                            img_filename = f"image_{img_hash[:8]}.jpg"

                        # Save image
                        img_filepath = os.path.join(domain_output_path, img_filename)
                        counter = 1
                        base_name, ext = os.path.splitext(img_filename)
                        while os.path.exists(img_filepath):
                            img_filepath = os.path.join(domain_output_path, f"{base_name}_{counter}{ext}")
                            counter += 1

                        with open(img_filepath, 'wb') as img_file:
                            img_file.write(img_content)

                        total_images_downloaded += 1
                        logging.info(f"[Browser] Downloaded image {total_images_downloaded}: {img_filename}")

                        # Queue for SSE if available
                        if image_update_queue:
                            served_url = f"{base_image_serve_path}/{base_domain}/{os.path.basename(img_filepath)}"
                            image_update_queue.put(served_url)

        except Exception as e:
            logging.error(f"Error during browser automation: {e}")
            logging.info("Continuing with standard scraping...")

    try:
        while urls_to_visit and pages_crawled < max_pages:
            # Check control state
            if control:
                if control.wait_if_paused():  # Returns True if stopped
                    logging.info("Scraping stopped by user")
                    break

            current_url, current_depth = urls_to_visit.popleft()

            # Skip if already visited (needed with potential queue additions)
            if current_url in visited_urls and current_url != start_url:
                logging.debug(f"Skipping already visited URL: {current_url}")
                continue
            visited_urls.add(current_url)

            pages_crawled += 1
            logging.info(f"[{pages_crawled}/{max_pages}] Crawling (depth {current_depth}): {current_url}")

            # Respect robots.txt
            if not can_fetch(current_url):
                logging.warning(f"Skipping {current_url} due to robots.txt")
                continue

            try:
                response = session.get(current_url, timeout=REQUEST_TIMEOUT)
                response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
                time.sleep(DELAY_BETWEEN_REQUESTS) # Be polite

                # Check content type to avoid parsing non-HTML pages
                content_type = response.headers.get('content-type', '').lower()
                if 'html' not in content_type:
                    logging.info(f"Skipping non-HTML page: {current_url} (Content-Type: {content_type})")
                    continue

                soup = BeautifulSoup(response.content, 'lxml') # Use lxml for speed

                # --- Find Images ---
                images_found_on_page = 0
                for img_tag in soup.find_all(['img', 'source']):
                    src = None
                    if img_tag.name == 'img':
                        # Also check data-src, data-srcset etc. for lazy loaded images
                        src = img_tag.get('src') or img_tag.get('data-src') or img_tag.get('data-lazy-src')
                        # Basic handling of srcset: try to pick the largest if obvious, otherwise fallback to src
                        srcset = img_tag.get('srcset') or img_tag.get('data-srcset')
                        if srcset and not src:
                            # Very basic srcset parsing: grab the last URL listed, hoping it's the largest
                            try:
                                src = srcset.strip().split(',')[-1].strip().split(' ')[0]
                            except:
                                pass # Stick with original src if parsing fails
                    elif img_tag.name == 'source' and img_tag.has_attr('srcset'):
                        # Primarily for <picture><source srcset="..."><img src="..."></picture>
                        # Try to get the source's srcset, prefer webp/avif if specified? More complex.
                        # Let's keep it simple and rely on the fallback <img> tag mostly.
                        # But if we find a source with a single srcset, maybe use it.
                        srcset = img_tag.get('srcset').strip()
                        if ' ' not in srcset and ',' not in srcset: # Simple case: <source srcset="image.webp">
                            src = srcset

                    if not src or src.startswith('data:image'): # Skip inline base64 images
                        continue

                    # Make URL absolute
                    abs_img_url = urljoin(current_url, src)
                    # Basic filtering for common/small icons by URL pattern
                    if COMMON_ICON_PATTERNS.search(abs_img_url):
                        logging.debug(f"Skipping likely icon by URL pattern: {abs_img_url}")
                        continue

                    # Clean URL for potentially higher resolution (especially non-SVGs)
                    is_svg = abs_img_url.lower().endswith('.svg') or 'svg' in abs_img_url.lower() # Basic check
                    cleaned_img_url = abs_img_url if is_svg else clean_image_url(abs_img_url)

                    if cleaned_img_url in found_image_srcs:
                        continue # Already processed this image URL

                    found_image_srcs.add(cleaned_img_url)

                    # --- Download Image ---
                    try:
                        logging.debug(f"Attempting to download: {cleaned_img_url}")
                        # Try to get high-res version with fallback
                        img_response, fetched_url = try_get_high_res_image(session, cleaned_img_url)

                        if img_response is None:
                            logging.warning(f"Failed to download image (all attempts): {cleaned_img_url}")
                            continue

                        # Get filename and sanitize
                        img_filename_base = os.path.basename(urlparse(cleaned_img_url).path)
                        if not img_filename_base or '.' not in img_filename_base:
                            content_type = img_response.headers.get('content-type', '')
                            ext = '.jpg'; # ... (your extension logic) ...
                            if 'svg' in content_type: ext = '.svg'
                            elif 'png' in content_type: ext = '.png'
                            elif 'webp' in content_type: ext = '.webp'
                            elif 'gif' in content_type: ext = '.gif'
                            img_filename_base = f"image_{len(found_image_srcs)}{ext}"
                        img_filename_sanitized = re.sub(r'[\\/*?:"<>|]', "_", img_filename_base)
                        
                        # Ensure filename is unique within the domain directory
                        counter = 0
                        img_filename = img_filename_sanitized
                        img_save_path = os.path.join(domain_output_path, img_filename)
                        while os.path.exists(img_save_path):
                            counter += 1
                            name, ext = os.path.splitext(img_filename_sanitized)
                            img_filename = f"{name}_{counter}{ext}"
                            img_save_path = os.path.join(domain_output_path, img_filename)

                        # Check hash before saving fully to disk
                        img_content = img_response.content # Read content (can be memory intensive for large images)
                        img_hash = get_image_hash(img_content)

                        if img_hash in downloaded_image_hashes:
                            logging.info(f"Skipping duplicate image (hash matched): {cleaned_img_url}")
                            continue

                        # Basic size check (heuristic, requires decoding for non-SVG)
                        # Disabled by default as it requires image libraries like Pillow
                        # if not is_svg:
                        #    try:
                        #        from PIL import Image
                        #        from io import BytesIO
                        #        img_pil = Image.open(BytesIO(img_content))
                        #        width, height = img_pil.size
                        #        if width < MIN_IMAGE_DIMENSION or height < MIN_IMAGE_DIMENSION:
                        #            logging.info(f"Skipping small image ({width}x{height}): {cleaned_img_url}")
                        #            continue
                        #    except ImportError:
                        #        if 'warned_pillow' not in globals():
                        #             logging.warning("Pillow library not found. Cannot perform image dimension checks. `pip install Pillow`")
                        #             globals()['warned_pillow'] = True
                        #    except Exception as e_pil:
                        #         logging.warning(f"Could not check dimensions for {cleaned_img_url}: {e_pil}")

                        # Save the image
                        with open(img_save_path, 'wb') as f:
                            f.write(img_content)

                        downloaded_image_hashes.add(img_hash)
                        images_found_on_page += 1
                        total_images_downloaded += 1
                        logging.info(f"Successfully downloaded: {cleaned_img_url} -> {img_save_path}")
                        
                        # Put the web-accessible path onto the queue if it exists
                        if image_update_queue:
                            # Construct the relative path for serving: domain/filename
                            # Quote the filename part for safe URL usage
                            relative_image_path = f"{base_domain}/{quote(img_filename)}"
                            serve_url = f"{base_image_serve_path}/{relative_image_path}"
                            logging.debug(f"Adding image to update queue: {serve_url}")
                            image_update_queue.put(serve_url)

                    except requests.exceptions.RequestException as e_img:
                        logging.warning(f"Failed to download image {cleaned_img_url}: {e_img}")
                    except Exception as e_save:
                        logging.error(f"Failed to save image {cleaned_img_url} to {img_save_path}: {e_save}")

                logging.info(f"Found and processed {images_found_on_page} new images on {current_url}")

                # --- Find Links (Internal & Pagination) ---
                # Only follow links if we haven't reached depth limit
                if current_depth < depth:
                    next_depth = current_depth + 1
                    page_links = set()
                    for link in soup.find_all('a', href=True):
                        href = link.get('href')
                        if not href: continue
                        abs_link = urljoin(current_url, href)
                        parsed_link = urlparse(abs_link)

                        # Stay on the same domain
                        if parsed_link.netloc != base_domain:
                            continue

                        # Clean fragment identifiers
                        abs_link = urlunparse((parsed_link.scheme, parsed_link.netloc, parsed_link.path, parsed_link.params, parsed_link.query, ''))

                        # Basic check: is it a potential file download we don't want to crawl?
                        if re.search(r'\.(pdf|zip|docx?|xlsx?|pptx?|exe|dmg|pkg|gz|rar)$', parsed_link.path, re.IGNORECASE):
                            continue

                        # Add to queue if new and allowed
                        if abs_link not in visited_urls:
                            # Check if URL is already in queue
                            already_queued = any(url == abs_link for url, _ in urls_to_visit)
                            if not already_queued and len(urls_to_visit) + len(visited_urls) < max_pages:
                                # Simple check: assume internal links are worth visiting for products/illustrations
                                is_potentially_interesting = True # Default to true for internal links

                                # Check pagination heuristic (as before)
                                link_text = link.get_text(strip=True).lower()
                                link_rel = link.get('rel', [])
                                link_class = link.get('class', [])
                                is_pagination = ('next' in link_text or '>' in link_text or '>>' in link_text or 'next' in link_rel or any('page' in c.lower() or 'pagin' in c.lower() or 'next' in c.lower() for c in link_class))

                                if follow_pagination and is_pagination:
                                    logging.debug(f"Queueing pagination link at depth {next_depth}: {abs_link}")
                                    urls_to_visit.appendleft((abs_link, next_depth)) # Prioritize pagination
                                elif is_potentially_interesting:
                                    logging.debug(f"Queueing internal link at depth {next_depth}: {abs_link}")
                                    urls_to_visit.append((abs_link, next_depth)) # Add other links to the end
                else:
                    logging.debug(f"Depth limit ({depth}) reached, not following links from {current_url}")
                        
                    # Also consider adding other internal links (optional - can broaden scope significantly)
                    # else:
                    #    if len(urls_to_visit) + len(visited_urls) < max_pages:
                    #       logging.debug(f"Found internal link: {abs_link}")
                    #       visited_urls.add(abs_link)
                    #       urls_to_visit.append(abs_link) # Add to back

            except requests.exceptions.RequestException as e:
                logging.error(f"Failed to fetch page {current_url}: {e}")
            except Exception as e:
                logging.error(f"An error occurred while processing {current_url}: {e}")
                
    finally: # Ensure the end signal is always sent
        if image_update_queue:
            logging.info("Scraping loop finished or max pages reached. Sending end signal to queue.")
            image_update_queue.put(None) # Signal end of stream

    logging.info(f"Scraping finished. Crawled {pages_crawled} pages. Found {total_images_downloaded} unique images.")
    # Return the *domain specific* path and count
    return domain_output_path, total_images_downloaded

# --- Example Usage (for testing scraper.py directly) ---
if __name__ == "__main__":
    test_url = "https://www.shopify.com/stock-photos" # Example URL with images
    test_output = "test_scrape_output"
    logging.info(f"--- Starting Direct Scraper Test ---")
    scrape_site(test_url, test_output, follow_pagination=True, max_pages=5)
    logging.info(f"--- Direct Scraper Test Finished ---")
    # Remember to clean up the test_scrape_output directory afterwards