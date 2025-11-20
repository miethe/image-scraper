import argparse
import os
from dotenv import load_dotenv
from scraper import scrape_site, logging # Import the logger too

load_dotenv()

def main():
    parser = argparse.ArgumentParser(description="Scrape images from a website.")
    parser.add_argument("url", help="The starting URL to scrape.")
    parser.add_argument(
        "-o", "--output",
        default=os.getenv('OUTPUT_DIR', 'data'),
        help="Directory to save images (defaults to 'data' or OUTPUT_DIR env var)."
    )
    parser.add_argument(
        "-p", "--pagination",
        action="store_true",
        default=True, # Defaulting to True as per requirement
        help="Follow pagination links (default: True)."
    )
    parser.add_argument(
        "--no-pagination",
        action="store_false",
        dest="pagination",
        help="Do not follow pagination links."
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=int(os.getenv('MAX_PAGES', '50')),
        help="Maximum number of pages to crawl (default: 50 or MAX_PAGES env var)."
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_const",
        dest="loglevel",
        const=logging.DEBUG,
        default=logging.INFO,
        help="Increase output verbosity to DEBUG level."
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        default=False,
        help="Use browser automation for JavaScript-heavy sites with interactive galleries (slower but more comprehensive)."
    )

    args = parser.parse_args()

    # Update logging level if verbose flag is set
    logging.getLogger().setLevel(args.loglevel)

    logging.info(f"Starting CLI scrape for: {args.url}")
    logging.info(f"Output directory: {args.output}")
    logging.info(f"Follow pagination: {args.pagination}")
    logging.info(f"Max pages: {args.max_pages}")
    logging.info(f"Browser mode: {args.browser}")

    try:
        output_path, count = scrape_site(
            start_url=args.url,
            output_dir=args.output,
            follow_pagination=args.pagination,
            max_pages=args.max_pages,
            use_browser=args.browser
        )
        logging.info(f"CLI scrape finished. Saved {count} images to {output_path}")
    except Exception as e:
        logging.error(f"CLI scrape failed: {e}", exc_info=True) # Log traceback
        exit(1)

if __name__ == "__main__":
    main()