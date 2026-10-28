"""Shared test utilities."""

_XOR_KEY = b"ACERT"


def xor_crypt(data: bytes) -> bytes:
    """XOR cipher with key ACERT — same call encrypts and decrypts."""
    return bytes(b ^ _XOR_KEY[i % len(_XOR_KEY)] for i, b in enumerate(data))


def jpeg_images(reader):
    """Unique JPEG image streams in a generated report (no source PDF vectors)."""
    images = {}
    for page in reader.pages:
        for ref in page.get('/Resources', {}).get('/XObject', {}).values():
            obj = ref.get_object()
            if obj.get('/Subtype') == '/Image':
                filters = obj.get('/Filter', [])
                if isinstance(filters, str):
                    filters = [filters]
                assert '/DCTDecode' in filters
                images[ref.idnum] = obj
    assert images, 'Supporting pages must be embedded as JPEG bitmaps'
    return list(images.values())
