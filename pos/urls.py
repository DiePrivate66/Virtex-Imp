"""Compatibility facade for POS URLs.

The canonical routing table now lives in ``pos.presentation.urls`` so the
presentation layer owns HTTP entry points explicitly.
"""

from .presentation.urls import urlpatterns
