"""Photos a customer uploads for a 'Your own photos' reel, checked and cleaned in memory before they go
to the customer's own Google Drive. Nothing here touches local disk.

The type comes from the bytes (never the file name or the browser's Content-Type), the photo must really
decode, and it is re-encoded as a fresh JPEG: the camera's rotation is applied first, then EXIF (GPS, camera,
dates), XMP, IPTC, ICC profiles and comments are all left behind.
"""
import io
import re

from PIL import Image, ImageOps

MIN_PHOTOS, MAX_PHOTOS = 6, 40
MAX_BYTES = 15 * 1024 * 1024      # each upload, as sent
MAX_TOTAL = 250 * 1024 * 1024     # all uploads of one reel, as sent
MAX_EDGE = 2560                   # listing photos arrive 1920 wide; this keeps headroom for camera moves
MAX_PIXELS = 60_000_000           # refuse decompression bombs before decoding
MAX_QUOTES = 3
ROOMS = ('exterior', 'living', 'kitchen', 'bedroom', 'bathroom', 'garden', 'spa', 'view', 'other')


class PhotoError(ValueError):
    """A customer-facing reason the upload was refused."""


def _name(name):
    return re.sub(r'[\x00-\x1f]', '', str(name or 'A photo'))[:60]


def kind(data):
    """Pillow format name from the first bytes, or None: JPEG, PNG and WebP only."""
    if data[:3] == b'\xff\xd8\xff':
        return 'JPEG'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'PNG'
    return 'WEBP' if data[:4] == b'RIFF' and data[8:12] == b'WEBP' else None


def check_batch(files):
    """[(name, bytes)] within the count and size limits, else PhotoError. Runs before any decoding."""
    if not MIN_PHOTOS <= len(files) <= MAX_PHOTOS:
        raise PhotoError(f'Choose 6 to 40 photos (you chose {len(files)})')
    for name, data in files:
        if len(data) > MAX_BYTES:
            raise PhotoError(f'{_name(name)} is larger than 15 MB')
    if sum(len(data) for _, data in files) > MAX_TOTAL:
        raise PhotoError('Your photos add up to more than 250 MB. Choose fewer or smaller photos.')


def clean(data, name=''):
    """A metadata-free JPEG of the photo, upright and at most MAX_EDGE on its long side."""
    fmt = kind(data)
    if not fmt:
        raise PhotoError(f'{_name(name)} is not a JPEG, PNG or WebP image')
    try:
        im = Image.open(io.BytesIO(data), formats=[fmt])
        if im.width * im.height > MAX_PIXELS:
            raise PhotoError(f'{_name(name)} has too many pixels. Use a photo under 60 megapixels.')
        im = ImageOps.exif_transpose(im).convert('RGB')
    except PhotoError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise PhotoError(f'{_name(name)} could not be read as an image') from None
    im.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
    out = Image.new('RGB', im.size)  # a fresh image carries no info dict: no EXIF, XMP, ICC or comment
    out.paste(im)
    buf = io.BytesIO()
    out.save(buf, 'JPEG', quality=92, optimize=True)
    return buf.getvalue()


def _one_line(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def details(fields):
    """The typed facts for the reel. Guest quotes keep text and stars only: never a name."""
    title, location = _one_line(fields.get('title')), _one_line(fields.get('location'))
    if not 1 <= len(title) <= 80:
        raise PhotoError('Enter a property title (up to 80 characters)')
    if not 1 <= len(location) <= 80:
        raise PhotoError('Enter the location, for example Whitby, UK (up to 80 characters)')
    raw = fields.get('highlights') or ''
    items = raw if isinstance(raw, list) else re.split(r'[,\n]', str(raw))
    highlights = [h for h in dict.fromkeys(_one_line(x)[:40] for x in items) if h][:8]
    quotes = []
    for q in fields.get('quotes') or []:
        text = _one_line((q or {}).get('text'))
        if not text:
            continue
        if not 20 <= len(text) <= 300:
            raise PhotoError('Each guest quote needs 20 to 300 characters')
        try:
            stars = int(q.get('stars'))
        except (TypeError, ValueError):
            stars = 0
        if not 1 <= stars <= 5:
            raise PhotoError('Give each guest quote 1 to 5 stars')
        quotes.append({'text': text, 'stars': stars})
    if len(quotes) > MAX_QUOTES:
        raise PhotoError('Add up to 3 guest quotes')
    return {'title': title, 'location': location, 'highlights': highlights, 'quotes': quotes}


def room_of(choice, name=''):
    """The room a photo shows: the customer's choice, else guessed from its file name (kitchen-2.jpg)."""
    if choice in ROOMS:
        return choice
    from app.pipeline import classify
    return classify(re.sub(r'[_\W]+', ' ', str(name).rsplit('.', 1)[0]))
