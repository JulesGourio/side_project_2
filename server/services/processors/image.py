"""Image processor (JPG, PNG, GIF, WebP, BMP, TIFF) — three processing methods.

All three methods send both images to the LLM for visual comparison.
They differ only in the system prompt, which shapes the LLM output format.

standard:    focused Markdown bullet-list output.
structured:  strict JSON array output (for Excel export).
comparative: external system prompt from app config.
"""

import base64
import io
import logging
from typing import Any, Dict, List

from .base import BaseProcessor, ProcessMetadata, ProcessResult
from ._diff_engines import SYSTEM_PROMPT_STANDARD, SYSTEM_PROMPT_STRUCTURED

logger = logging.getLogger(__name__)

_SUPPORTED_MIME = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.bmp': 'image/jpeg',   # normalised to jpeg
    '.tiff': 'image/jpeg',  # normalised to jpeg
    '.tif': 'image/jpeg',
}

_IMG_MAX_PX = 1600
_IMG_JPEG_QUALITY = 85


def _normalise_image(img_bytes: bytes, ext: str) -> tuple[bytes, str]:
    """Return (jpeg_bytes, 'image/jpeg') or original bytes + correct MIME."""
    from PIL import Image
    mime = _SUPPORTED_MIME.get(ext.lower(), 'image/jpeg')
    if ext.lower() in ('.bmp', '.tiff', '.tif') or mime not in ('image/jpeg', 'image/png', 'image/gif', 'image/webp'):
        img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
        w, h = img.size
        scale = min(1.0, _IMG_MAX_PX / max(w, h, 1))
        if scale < 1.0:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=_IMG_JPEG_QUALITY, optimize=True)
        return buf.getvalue(), 'image/jpeg'
    return img_bytes, mime


class ImageProcessor(BaseProcessor):

    def __init__(self, method: str = 'standard') -> None:
        self.method = method

    def build_messages(
        self,
        old_bytes: bytes,
        old_name: str,
        new_bytes: bytes,
        new_name: str,
        system_prompt: str = '',
    ) -> ProcessResult:
        if self.method == 'structured':
            sp = SYSTEM_PROMPT_STRUCTURED
        elif self.method == 'comparative':
            sp = system_prompt
        else:
            sp = SYSTEM_PROMPT_STANDARD

        logger.info(f'ImageProcessor [{self.method}]: visual comparison — {old_name} → {new_name}')

        ext = ('.' + old_name.rsplit('.', 1)[-1]) if '.' in old_name else '.jpg'
        old_img, old_mime = _normalise_image(old_bytes, ext)
        new_img, new_mime = _normalise_image(
            new_bytes,
            ('.' + new_name.rsplit('.', 1)[-1]) if '.' in new_name else '.jpg',
        )

        old_b64 = base64.b64encode(old_img).decode('ascii')
        new_b64 = base64.b64encode(new_img).decode('ascii')

        content_blocks: List[Dict[str, Any]] = [
            {'type': 'text', 'text': f'--- OLD VERSION ({old_name}) ---'},
            {'type': 'image_url', 'image_url': {'url': f'data:{old_mime};base64,{old_b64}'}},
            {'type': 'text', 'text': f'--- NEW VERSION ({new_name}) ---'},
            {'type': 'image_url', 'image_url': {'url': f'data:{new_mime};base64,{new_b64}'}},
        ]

        messages: List[Dict[str, Any]] = []
        if sp:
            messages.append({'role': 'system', 'content': sp})
        messages.append({'role': 'user', 'content': content_blocks})

        from ._diff_engines import _thumbnail_b64
        return ProcessResult(
            messages=messages,
            metadata=ProcessMetadata(file_type='image', method=self.method, old_name=old_name, new_name=new_name),
            image_pairs=[{
                'status': 'modified',
                'old_page': None,
                'new_page': None,
                'old_b64': _thumbnail_b64(old_b64),
                'new_b64': _thumbnail_b64(new_b64),
                'index': 0,
            }],
        )
