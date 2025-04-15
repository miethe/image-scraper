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
    # Common path segments indicating resizing (more aggressive)
    path_resize_patterns = [
        re.compile(r'_\d+x\d+(\.[a-zA-Z]+)$'), # _300x200.jpg
        re.compile(r'/thumb/', re.IGNORECASE),    # /thumb/image.png
        re.compile(r'/thumbnail/', re.IGNORECASE), # /thumbnail/image.png
        re.compile(r'/preview/', re.IGNORECASE),  # /preview/image.jpg
        re.compile(r'@[1-9]\.[0-9]+x', re.IGNORECASE) # Ali CDN @1.5x
    ]

    try:
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)

        # Remove query parameters
        original_query_count = len(query_params)
        query_params = {k: v for k, v in query_params.items() if k.lower() not in params_to_remove}

        # Remove path segments (use with caution - might break URLs)
        # Commented out by default - enable if needed and test carefully
        # new_path = parsed.path
        # for pattern in path_resize_patterns:
        #     new_path = pattern.sub(r'\1', new_path) # Try removing size suffixes like _WxH.ext
        #     new_path = re.sub(pattern, '/', new_path) # Try removing path segments like /thumb/

        # Reconstruct URL
        new_query = urlencode(query_params, doseq=True)
        # Use original path unless path modification is enabled
        # new_path = new_path if new_path != parsed.path else parsed.path # Check if path changed
        new_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

        if new_url != url:
             logging.debug(f"Cleaned image URL: {url} -> {new_url}")
        return new_url

    except Exception as e:
        logging.warning(f"Error cleaning URL {url}: {e}")
        return url # Return original if cleaning fails

# --- Image Hashing ---
def get_image_hash(content):
    """Generates a SHA256 hash for the image content."""
    return hashlib.sha256(content).hexdigest()

# --- Main Scraping Function ---
def scrape_site(start_url, output_dir, image_update_queue: Queue = None, base_image_serve_path="/images", follow_pagination=True, max_pages=MAX_PAGES_TO_CRAWL):
    """
    Scrapes a website starting from start_url for images.

    Args:
        start_url (str): The URL to start scraping from.
        output_dir (str): The base directory to save images to.
        image_update_queue (Queue, optional): Queue to put accessible image URLs for SSE.
        base_image_serve_path (str): The base HTTP path where images will be served from.
        follow_pagination (bool): Whether to follow links likely indicating pagination.
        max_pages (int): Maximum number of pages to crawl.
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

    urls_to_visit = deque([start_url])
    visited_urls = set([start_url])
    found_image_srcs = set() # Track image source URLs to avoid re-downloading based on URL
    downloaded_image_hashes = set() # Track hashes to avoid saving duplicates with different URLs
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    pages_crawled = 0
    total_images_downloaded = 0 # Keep track of count

    try:
        while urls_to_visit and pages_crawled < max_pages:
            current_url = urls_to_visit.popleft()
            # --- Skip if already visited (needed with potential queue additions) ---
            if current_url in visited_urls and current_url != start_url: # Avoid re-processing
                 logging.debug(f"Skipping already visited URL: {current_url}")
                 continue
            visited_urls.add(current_url) # Add here instead of when queueing
            
            pages_crawled += 1
            logging.info(f"[{pages_crawled}/{max_pages}] Crawling: {current_url}")

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
                        img_response = session.get(cleaned_img_url, timeout=DOWNLOAD_TIMEOUT, stream=True)
                        img_response.raise_for_status()

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
                    if abs_link not in visited_urls and abs_link not in urls_to_visit: # Check both lists
                        if len(urls_to_visit) + len(visited_urls) < max_pages:
                            # Simple check: assume internal links are worth visiting for products/illustrations
                            # Refine this logic based on observed patterns (e.g., '/product/', '/gallery/')
                            is_potentially_interesting = True # Default to true for internal links

                            # Check pagination heuristic (as before)
                            link_text = link.get_text(strip=True).lower()
                            link_rel = link.get('rel', [])
                            link_class = link.get('class', [])
                            is_pagination = ('next' in link_text or '>' in link_text or '>>' in link_text or 'next' in link_rel or any('page' in c.lower() or 'pagin' in c.lower() or 'next' in c.lower() for c in link_class))

                            if follow_pagination and is_pagination:
                                logging.debug(f"Queueing pagination link: {abs_link}")
                                urls_to_visit.appendleft(abs_link) # Prioritize pagination
                            elif is_potentially_interesting:
                                logging.debug(f"Queueing internal link: {abs_link}")
                                urls_to_visit.append(abs_link) # Add other links to the end
                        
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