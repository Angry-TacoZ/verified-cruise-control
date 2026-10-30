"""Centralized import limits (PR #1). All enforcement reads these constants."""

MAX_PHOTOS_PER_LISTING = 40
MAX_PHOTO_BYTES = 15 * 1024 * 1024  # 15 MiB per photograph
MAX_LISTING_PHOTO_BYTES = 300 * 1024 * 1024  # 300 MiB total per listing
MAX_IMAGE_PIXELS = 40_000_000  # 40 megapixels decoded
ALLOWED_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
MAX_LISTING_TEXT_CHARS = 50_000
MAX_URL_CHARS = 2_048
MAX_CLAIMS = 200
MAX_CLAIM_CHARS = 500
