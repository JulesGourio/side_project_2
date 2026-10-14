"""Checks which serving endpoints accept an image: one chat request per (endpoint, image) on a folder of images.

Each request carries the image as a data URL with the question. One line is logged per (endpoint, image): ok or fail,
the HTTP status, the seconds, and the first 120 characters of the answer (or the error). The run ends with the count of
ok and fail per endpoint. Exit code 0 when every request was ok, 1 otherwise.

USAGE
-----
    python utils/evaluation/check_vision_endpoints.py --profile DEV --images /path/to/images
    python utils/evaluation/check_vision_endpoints.py --profile UAT --images /path/to/images \
        --endpoints databricks-gpt-5-6-luna,databricks-gpt-6-luna --question "What is in this image?"

The profile gives the workspace host and the token. Read-only apart from the requests themselves (a few tokens each).
"""
import argparse
import base64
import logging
import mimetypes
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import httpx

logger = logging.getLogger('check_vision_endpoints')

DEFAULT_ENDPOINTS = 'databricks-gpt-5-6-luna,databricks-gpt-6-luna,databricks-claude-sonnet-4-6'
DEFAULT_QUESTION = 'Describe this image in one sentence.'
IMAGE_SUFFIXES = ('.png', '.jpg', '.jpeg')
# Reasoning models spend output tokens before the first visible word: a one-sentence answer still needs room.
MAX_TOKENS = 2000
ANSWER_CHARS = 120


@dataclass
class CheckResult:
    endpoint: str
    image: str
    ok: bool
    status: Optional[int]
    seconds: float
    text: str   # the answer when ok, the reason when not


def image_files(folder: Path) -> List[Path]:
    """The png / jpg / jpeg files of `folder`, by name."""
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)


def data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or 'image/jpeg'
    return f'data:{mime};base64,' + base64.b64encode(path.read_bytes()).decode()


def build_payload(question: str, image_url: str) -> dict:
    """Chat request with the image in the OpenAI image format, as the description step sends it."""
    return {
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': question},
            {'type': 'image_url', 'image_url': {'url': image_url}},
        ]}],
        'max_tokens': MAX_TOKENS,
    }


def _answer_text(body: dict) -> str:
    content = body['choices'][0]['message']['content']
    if isinstance(content, list):
        content = ''.join(p.get('text', '') if isinstance(p, dict) else str(p) for p in content)
    return (content or '').strip()


def check_one(http: httpx.Client, host: str, token: str, endpoint: str, image: Path, question: str,
              image_url: Optional[str] = None) -> CheckResult:
    """One request. ok when the endpoint answered with text; otherwise the reason is kept in `text`."""
    url = image_url if image_url is not None else data_url(image)
    started = time.monotonic()
    try:
        resp = http.post(f'{host}/serving-endpoints/{endpoint}/invocations', json=build_payload(question, url),
                         headers={'Authorization': f'Bearer {token}'})
    except httpx.HTTPError as exc:
        return CheckResult(endpoint, image.name, False, None, time.monotonic() - started,
                           f'{type(exc).__name__}: {str(exc)[:ANSWER_CHARS]}')
    seconds = time.monotonic() - started
    if resp.status_code != 200:
        return CheckResult(endpoint, image.name, False, resp.status_code, seconds, resp.text[:ANSWER_CHARS])
    try:
        text = _answer_text(resp.json())
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        return CheckResult(endpoint, image.name, False, resp.status_code, seconds, f'unreadable answer: {exc}')
    if not text:
        return CheckResult(endpoint, image.name, False, resp.status_code, seconds, 'empty answer')
    return CheckResult(endpoint, image.name, True, resp.status_code, seconds, text)


def summarize(results: Sequence[CheckResult]) -> Dict[str, Tuple[int, int]]:
    """endpoint -> (ok, fail), in the order the endpoints first appear."""
    counts: Dict[str, List[int]] = {}
    for r in results:
        counts.setdefault(r.endpoint, [0, 0])[0 if r.ok else 1] += 1
    return {endpoint: (ok, fail) for endpoint, (ok, fail) in counts.items()}


def _log_result(r: CheckResult) -> None:
    detail = r.text[:ANSWER_CHARS].replace('\n', ' ')
    status = 'ok' if r.ok else 'fail'
    code = r.status if r.status is not None else '-'
    logger.info(f'{r.endpoint} | {r.image} | {status} | HTTP {code} | {r.seconds:.1f} s | {detail}')


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--profile', default='', help='Databricks CLI profile (DEV, UAT…); empty = default auth')
    parser.add_argument('--images', required=True, help='folder of png / jpg / jpeg images')
    parser.add_argument('--endpoints', default=DEFAULT_ENDPOINTS, help='comma-separated serving endpoints')
    parser.add_argument('--question', default=DEFAULT_QUESTION, help='question sent with each image')
    parser.add_argument('--timeout', type=float, default=120.0, help='seconds per request')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    folder = Path(args.images)
    if not folder.is_dir():
        logger.error(f'not a folder: {folder}')
        return 2
    images = image_files(folder)
    endpoints = [e.strip() for e in args.endpoints.split(',') if e.strip()]
    if not images or not endpoints:
        logger.error(f'nothing to check: {len(images)} image(s) in {folder}, endpoints {endpoints}')
        return 2

    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient(profile=args.profile or None)
    host = w.config.host.rstrip('/')
    token = w.config.authenticate()['Authorization'].split(' ', 1)[1]
    logger.info(f'{len(images)} image(s) x {len(endpoints)} endpoint(s) on {host}')

    results: List[CheckResult] = []
    with httpx.Client(timeout=args.timeout) as http:
        for endpoint in endpoints:
            for image in images:
                result = check_one(http, host, token, endpoint, image, args.question)
                _log_result(result)
                results.append(result)

    logger.info('summary per endpoint (ok / fail):')
    for endpoint, (ok, fail) in summarize(results).items():
        logger.info(f'  {endpoint}: {ok} ok / {fail} fail')
    return 0 if all(r.ok for r in results) else 1


if __name__ == '__main__':
    sys.exit(main())
