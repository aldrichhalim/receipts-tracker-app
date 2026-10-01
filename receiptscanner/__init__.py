"""Narmada: receipt tracker and expense report generator.

OpenCV preprocessing + local Tesseract OCR + SQLite bookkeeping.
"""

# APP_NAME names on-disk locations (config and data directories); changing it
# would orphan existing users' files. APP_TITLE is only what the user sees.
APP_NAME = "ReceiptScanner"
APP_TITLE = "Narmada"
__version__ = "1.2.0"
