"""Theme endpoint.

Exposes the effective theme (user override OR built-in default).
The frontend's `useTheme` hook fetches this rather than reading
`/api/storage/shared/theme.css` directly, so:

  - Reverting the theme is just `DELETE /api/storage/shared/theme.css`.
    The platform falls back to defaults automatically (single source
    of truth lives in `theme.py:DEFAULT_THEME`).
  - The frontend never needs to know what defaults look like, so
    server-side defaults can change without a coordinated frontend
    update.
"""

from fastapi import APIRouter, Depends

from app import models
from app.config import Settings, get_settings
from app.deps import get_current_owner
from app.theme import get_theme_css, get_bg_color


router = APIRouter(prefix="/api/theme", tags=["theme"])


@router.get("")
def get_theme(
  _: models.Owner = Depends(get_current_owner),
  settings: Settings = Depends(get_settings),
):
  """Returns the effective theme: user's `theme.css` if non-empty,
  otherwise the built-in default. Always returns valid CSS."""
  return {
    "css": get_theme_css(settings.data_dir),
    "bg": get_bg_color(settings.data_dir),
  }
