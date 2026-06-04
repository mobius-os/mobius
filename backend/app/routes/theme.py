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
from app.deps import get_current_owner, reject_cross_site
from app.theme import get_theme_css, get_bg_color, reset_theme_override


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


@router.post("/reset", dependencies=[Depends(reject_cross_site)])
def reset_theme(
  _: models.Owner = Depends(get_current_owner),
  settings: Settings = Depends(get_settings),
):
  """Moves `/data/shared/theme.css` aside so DEFAULT_THEME paints
  again, preserving the previous theme as
  `theme.css.reset-bak-<unix-ts>`.

  This is the JSON endpoint used by the shell's `?reset-theme=1`
  URL-parameter recovery flow. The recovery page (`/recover`)
  performs the same rename inline so it can stay independent of
  the regular API import chain.

  Idempotent: with no override present, returns
  `{"reset": false, "reason": "no override"}`.
  """
  return reset_theme_override(settings.data_dir)
