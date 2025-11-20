"""
Interactive Gallery Scraper using Playwright
Handles JavaScript-heavy sites with dynamically loaded images,
particularly Wix Pro Gallery and similar interactive gallery systems.
"""

import logging
import time
import re
from urllib.parse import urljoin, urlparse
from typing import Set, List, Dict, Optional


# --- Configuration ---
DEFAULT_TIMEOUT = 30000  # 30 seconds for page loads
CLICK_TIMEOUT = 5000     # 5 seconds for click actions
WAIT_AFTER_CLICK = 2000  # 2 seconds to wait after clicking for content to load


def scrape_interactive_images(url: str, headless: bool = True) -> Set[str]:
    """
    Scrape images from JavaScript-heavy pages with interactive galleries.

    Args:
        url: The URL to scrape
        headless: Whether to run browser in headless mode

    Returns:
        Set of image URLs found (including high-res versions from interactive galleries)
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        logging.error("Playwright not installed. Run: pip install playwright && playwright install")
        return set()

    image_urls = set()

    try:
        with sync_playwright() as p:
            # Launch browser
            logging.info(f"Launching browser for interactive scraping: {url}")
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={'width': 1920, 'height': 1080}
            )
            page = context.new_page()

            # Navigate to the page
            try:
                page.goto(url, wait_until='networkidle', timeout=DEFAULT_TIMEOUT)
            except PlaywrightTimeout:
                logging.warning(f"Page load timeout for {url}, continuing with partial content")

            # Wait a bit for any lazy-loaded content
            page.wait_for_timeout(2000)

            # First, collect all static images
            static_images = _extract_static_images(page, url)
            image_urls.update(static_images)
            logging.info(f"Found {len(static_images)} static images")

            # Detect and handle interactive galleries
            gallery_images = _handle_interactive_galleries(page, url)
            image_urls.update(gallery_images)
            logging.info(f"Found {len(gallery_images)} images from interactive galleries")

            browser.close()

    except Exception as e:
        logging.error(f"Error during interactive scraping of {url}: {e}")

    return image_urls


def _extract_static_images(page, base_url: str) -> Set[str]:
    """Extract all static images from the current page state."""
    image_urls = set()

    # Get all img elements
    img_elements = page.query_selector_all('img')
    for img in img_elements:
        # Try various attributes
        for attr in ['src', 'data-src', 'data-lazy-src', 'data-original']:
            url = img.get_attribute(attr)
            if url:
                full_url = urljoin(base_url, url)
                if _is_valid_image_url(full_url):
                    image_urls.add(full_url)

        # Handle srcset
        srcset = img.get_attribute('srcset') or img.get_attribute('data-srcset')
        if srcset:
            urls = _parse_srcset(srcset, base_url)
            image_urls.update(urls)

    # Get source elements (in picture tags)
    source_elements = page.query_selector_all('source')
    for source in source_elements:
        srcset = source.get_attribute('srcset')
        if srcset:
            urls = _parse_srcset(srcset, base_url)
            image_urls.update(urls)

    return image_urls


def _handle_interactive_galleries(page, base_url: str) -> Set[str]:
    """
    Detect and handle interactive galleries (Wix Pro Gallery, etc.).
    Clicks on gallery items to reveal high-res images.
    """
    image_urls = set()

    # Detect Wix Pro Gallery
    wix_images = _handle_wix_pro_gallery(page, base_url)
    if wix_images:
        logging.info(f"Detected Wix Pro Gallery, found {len(wix_images)} images")
        image_urls.update(wix_images)

    # Detect other common gallery patterns
    other_images = _handle_generic_galleries(page, base_url)
    if other_images:
        logging.info(f"Found {len(other_images)} images from generic galleries")
        image_urls.update(other_images)

    return image_urls


def _handle_wix_pro_gallery(page, base_url: str) -> Set[str]:
    """
    Handle Wix Pro Gallery specifically.
    Detects gallery items, clicks them, and extracts high-res URLs from popups.
    """
    image_urls = set()

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        # Find all clickable gallery items
        # Wix Pro Gallery typically uses data-hook="gallery-item-container" or similar
        gallery_selectors = [
            '[data-hook*="gallery-item"]',
            '[data-hook*="item-container"]',
            '[class*="gallery-item"]',
            '[class*="pro-gallery-item"]',
        ]

        gallery_items = []
        for selector in gallery_selectors:
            items = page.query_selector_all(selector)
            if items:
                gallery_items.extend(items)
                logging.info(f"Found {len(items)} gallery items with selector: {selector}")
                break

        if not gallery_items:
            return image_urls

        logging.info(f"Processing {len(gallery_items)} gallery items...")

        # Click each gallery item to reveal high-res images
        for i, item in enumerate(gallery_items[:50]):  # Limit to first 50 to avoid excessive time
            try:
                # Scroll item into view
                item.scroll_into_view_if_needed()
                page.wait_for_timeout(500)

                # Click the item
                item.click(timeout=CLICK_TIMEOUT)

                # Wait for popup to appear
                page.wait_for_timeout(WAIT_AFTER_CLICK)

                # Look for the pro-gallery-popup
                popup = page.query_selector('[data-comp-type="pro-gallery-popup"]')
                if popup:
                    # Extract images from group-view elements
                    group_views = popup.query_selector_all('[data-hook="group-view"]')

                    for group in group_views:
                        # Look for img tags
                        imgs = group.query_selector_all('img')
                        for img in imgs:
                            for attr in ['src', 'data-src', 'srcset', 'data-srcset']:
                                value = img.get_attribute(attr)
                                if value:
                                    if 'srcset' in attr:
                                        urls = _parse_srcset(value, base_url)
                                        image_urls.update(urls)
                                    else:
                                        full_url = urljoin(base_url, value)
                                        if _is_valid_image_url(full_url):
                                            image_urls.add(full_url)

                        # Also check for image URLs in data attributes
                        for attr in group.evaluate('(el) => Array.from(el.attributes).map(a => a.name)'):
                            if 'data-' in attr:
                                value = group.get_attribute(attr)
                                if value and _looks_like_image_url(value):
                                    full_url = urljoin(base_url, value)
                                    if _is_valid_image_url(full_url):
                                        image_urls.add(full_url)

                    # Close the popup (look for close button)
                    close_button = popup.query_selector('[data-hook*="close"]') or \
                                   popup.query_selector('[aria-label*="close" i]') or \
                                   popup.query_selector('button[class*="close"]')

                    if close_button:
                        close_button.click(timeout=CLICK_TIMEOUT)
                    else:
                        # Try pressing Escape key
                        page.keyboard.press('Escape')

                    page.wait_for_timeout(500)

                if (i + 1) % 10 == 0:
                    logging.info(f"Processed {i + 1}/{len(gallery_items)} gallery items")

            except PlaywrightTimeout:
                logging.warning(f"Timeout clicking gallery item {i + 1}")
                continue
            except Exception as e:
                logging.warning(f"Error processing gallery item {i + 1}: {e}")
                continue

    except Exception as e:
        logging.error(f"Error handling Wix Pro Gallery: {e}")

    return image_urls


def _handle_generic_galleries(page, base_url: str) -> Set[str]:
    """
    Handle generic lightbox/modal galleries.
    Looks for clickable images that open larger versions.
    """
    image_urls = set()

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        # Find images that are likely clickable (in links, have click handlers, etc.)
        clickable_selectors = [
            'a img',
            '[onclick] img',
            '[data-lightbox] img',
            '[data-fancybox] img',
            '[class*="thumbnail"] img',
            '[class*="thumb"] img',
        ]

        clickable_images = []
        for selector in clickable_selectors:
            imgs = page.query_selector_all(selector)
            if imgs:
                clickable_images.extend(imgs[:30])  # Limit to avoid excessive processing
                break

        if not clickable_images:
            return image_urls

        logging.info(f"Found {len(clickable_images)} potentially clickable images")

        for i, img in enumerate(clickable_images):
            try:
                # Get the parent link if it exists
                parent = img.evaluate('(el) => el.closest("a")')
                if parent:
                    href = img.evaluate('(el) => el.closest("a").href')
                    if href and _is_valid_image_url(href):
                        image_urls.add(href)

                # Try clicking to see if a modal appears
                img.scroll_into_view_if_needed()
                page.wait_for_timeout(300)

                # Store current image count
                before_click_count = len(image_urls)

                try:
                    img.click(timeout=CLICK_TIMEOUT)
                    page.wait_for_timeout(1500)

                    # Look for modal/lightbox
                    modal_selectors = [
                        '[class*="modal"]',
                        '[class*="lightbox"]',
                        '[role="dialog"]',
                        '[class*="popup"]',
                    ]

                    for modal_sel in modal_selectors:
                        modal = page.query_selector(modal_sel)
                        if modal and modal.is_visible():
                            # Extract images from modal
                            modal_imgs = modal.query_selector_all('img')
                            for modal_img in modal_imgs:
                                for attr in ['src', 'data-src', 'srcset', 'data-srcset']:
                                    value = modal_img.get_attribute(attr)
                                    if value:
                                        if 'srcset' in attr:
                                            urls = _parse_srcset(value, base_url)
                                            image_urls.update(urls)
                                        else:
                                            full_url = urljoin(base_url, value)
                                            if _is_valid_image_url(full_url):
                                                image_urls.add(full_url)

                            # Close modal
                            close_btn = modal.query_selector('[class*="close"]') or \
                                       modal.query_selector('[aria-label*="close" i]')
                            if close_btn:
                                close_btn.click(timeout=CLICK_TIMEOUT)
                            else:
                                page.keyboard.press('Escape')

                            page.wait_for_timeout(300)
                            break

                    # If we found new images, this was successful
                    if len(image_urls) > before_click_count:
                        logging.info(f"Found {len(image_urls) - before_click_count} new images from modal")

                except PlaywrightTimeout:
                    pass  # Image not clickable or no modal appeared

            except Exception as e:
                logging.debug(f"Error processing clickable image {i + 1}: {e}")
                continue

    except Exception as e:
        logging.error(f"Error handling generic galleries: {e}")

    return image_urls


def _parse_srcset(srcset: str, base_url: str) -> Set[str]:
    """Parse srcset attribute and return all image URLs."""
    urls = set()

    # srcset format: "url1 1x, url2 2x" or "url1 100w, url2 200w"
    parts = srcset.split(',')
    for part in parts:
        url = part.strip().split()[0]
        if url:
            full_url = urljoin(base_url, url)
            if _is_valid_image_url(full_url):
                urls.add(full_url)

    return urls


def _is_valid_image_url(url: str) -> bool:
    """Check if URL looks like a valid image URL."""
    if not url or url.startswith('data:'):
        return False

    # Check for image extensions or image in path
    parsed = urlparse(url)
    path = parsed.path.lower()

    # Common image extensions
    image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.ico')
    if any(path.endswith(ext) for ext in image_extensions):
        return True

    # Check if URL contains image-related patterns
    if any(pattern in url.lower() for pattern in ['/image', '/img', '/photo', '/pic', '/media']):
        return True

    # Wix-specific patterns
    if 'wixmp.com' in url or 'wixstatic.com' in url:
        return True

    return False


def _looks_like_image_url(value: str) -> bool:
    """Check if a string value looks like it might be an image URL."""
    if not value or len(value) < 10:
        return False

    # Check for URL patterns
    if value.startswith(('http://', 'https://', '//', '/')):
        return _is_valid_image_url(value)

    return False


def get_high_res_variants(image_url: str) -> List[str]:
    """
    Generate possible high-resolution variants of an image URL.
    Useful for Wix and similar CDNs that use URL patterns for different sizes.
    """
    variants = [image_url]

    parsed = urlparse(image_url)

    # Wix-specific patterns
    if 'wix' in parsed.netloc.lower():
        # Try removing size parameters
        url_without_params = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        variants.append(url_without_params)

        # Try adding high-res parameters
        variants.append(f"{url_without_params}?w=2048")
        variants.append(f"{url_without_params}?w=4096")
        variants.append(f"{url_without_params}?quality=100")

        # Try replacing size patterns in path
        path = parsed.path
        # Pattern like /v1/fill/w_200,h_300/ -> /v1/fill/w_2048,h_2048/
        if '/fill/' in path:
            high_res_path = re.sub(r'/w_\d+,h_\d+/', '/w_2048,h_2048/', path)
            variants.append(f"{parsed.scheme}://{parsed.netloc}{high_res_path}")

    return variants
