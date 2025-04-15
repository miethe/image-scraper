import os
import shutil
import tempfile
import threading
from queue import Queue
import pytest
from unittest import mock
import sys
from scraper import scrape_site

# Patch requests and BeautifulSoup for isolation
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

@pytest.fixture
def temp_output_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d)

def make_mock_response(html=None, content_type='text/html', status_code=200, img_bytes=b'fakeimg', img_content_type='image/png'):
    mock_resp = mock.Mock()
    mock_resp.status_code = status_code
    mock_resp.headers = {'content-type': content_type}
    mock_resp.content = html.encode('utf-8') if html else b''
    mock_resp.raise_for_status = mock.Mock()
    # For image requests, we want to return image bytes
    def content_side_effect():
        return img_bytes
    mock_resp_img = mock.Mock()
    mock_resp_img.status_code = 200
    mock_resp_img.headers = {'content-type': img_content_type}
    mock_resp_img.content = img_bytes
    mock_resp_img.raise_for_status = mock.Mock()
    return mock_resp, mock_resp_img

@mock.patch('scraper.can_fetch', return_value=True)
@mock.patch('scraper.requests.Session')
def test_scrape_site_basic(mock_session_cls, mock_can_fetch, temp_output_dir):
    # HTML with two images and one link to another page
    html1 = """
    <html>
      <body>
        <img src="img1.png"/>
        <img src="img2.jpg"/>
        <a href="/page2">Next</a>
      </body>
    </html>
    """
    html2 = """
    <html>
      <body>
        <img src="img3.gif"/>
      </body>
    </html>
    """
    # Setup mock session.get to return different responses based on URL
    def get_side_effect(url, *args, **kwargs):
        if url.endswith('/page2'):
            resp, img_resp = make_mock_response(html2)
            return resp
        elif url.endswith('img1.png'):
            _, img_resp = make_mock_response(img_bytes=b'img1', img_content_type='image/png')
            return img_resp
        elif url.endswith('img2.jpg'):
            _, img_resp = make_mock_response(img_bytes=b'img2', img_content_type='image/jpeg')
            return img_resp
        elif url.endswith('img3.gif'):
            _, img_resp = make_mock_response(img_bytes=b'img3', img_content_type='image/gif')
            return img_resp
        else:
            resp, img_resp = make_mock_response(html1)
            return resp

    mock_session = mock.Mock()
    mock_session.get.side_effect = get_side_effect
    mock_session.headers = {}
    mock_session_cls.return_value = mock_session

    # Run scraper
    output_dir, img_count = scrape_site(
        start_url='http://test.com',
        output_dir=temp_output_dir,
        follow_pagination=True,
        max_pages=2
    )

    # Check output directory and image files
    files = os.listdir(output_dir)
    assert img_count == 3
    assert any(f.endswith('.png') for f in files)
    assert any(f.endswith('.jpg') for f in files)
    assert any(f.endswith('.gif') for f in files)

@mock.patch('scraper.can_fetch', return_value=True)
@mock.patch('scraper.requests.Session')
def test_scrape_site_image_update_queue(mock_session_cls, mock_can_fetch, temp_output_dir):
    html = """
    <html>
      <body>
        <img src="img1.png"/>
      </body>
    </html>
    """
    def get_side_effect(url, *args, **kwargs):
        if url.endswith('img1.png'):
            _, img_resp = make_mock_response(img_bytes=b'img1', img_content_type='image/png')
            return img_resp
        else:
            resp, img_resp = make_mock_response(html)
            return resp

    mock_session = mock.Mock()
    mock_session.get.side_effect = get_side_effect
    mock_session.headers = {}
    mock_session_cls.return_value = mock_session

    q = Queue()
    output_dir, img_count = scrape_site(
        start_url='http://test.com',
        output_dir=temp_output_dir,
        image_update_queue=q,
        base_image_serve_path='/images',
        follow_pagination=False,
        max_pages=1
    )
    # The queue should get the image serve URL and then None
    serve_urls = []
    while True:
        item = q.get()
        if item is None:
            break
        serve_urls.append(item)
    assert len(serve_urls) == 1
    assert serve_urls[0].startswith('/images/')
    assert img_count == 1

@mock.patch('scraper.can_fetch', return_value=False)
@mock.patch('scraper.requests.Session')
def test_scrape_site_respects_robots_txt(mock_session_cls, mock_can_fetch, temp_output_dir):
    # If can_fetch returns False, no images should be downloaded
    output_dir, img_count = scrape_site(
        start_url='http://test.com',
        output_dir=temp_output_dir,
        follow_pagination=False,
        max_pages=1
    )
    assert img_count == 0
    assert os.path.exists(output_dir)
    assert os.listdir(output_dir) == []

@mock.patch('scraper.can_fetch', return_value=True)
@mock.patch('scraper.requests.Session')
def test_scrape_site_skips_non_html(mock_session_cls, mock_can_fetch, temp_output_dir):
    # Simulate a non-HTML page (e.g., PDF)
    mock_resp, _ = make_mock_response(html=None, content_type='application/pdf')
    mock_session = mock.Mock()
    mock_session.get.return_value = mock_resp
    mock_session.headers = {}
    mock_session_cls.return_value = mock_session

    output_dir, img_count = scrape_site(
        start_url='http://test.com',
        output_dir=temp_output_dir,
        follow_pagination=False,
        max_pages=1
    )
    assert img_count == 0
    assert os.path.exists(output_dir)
    assert os.listdir(output_dir) == []