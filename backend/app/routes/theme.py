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

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import models
from app.config import Settings, get_settings
from app.deps import get_current_owner, reject_cross_site
from app.routes.notify import publish_system_event
from app.theme import (
  UnreadableThemeCss, reset_theme_override, set_theme_mode, theme_with_toggle,
)


router = APIRouter(prefix="/api/theme", tags=["theme"])


@router.get("")
def get_theme(
  _: models.Owner = Depends(get_current_owner),
  settings: Settings = Depends(get_settings),
):
  """Returns the effective theme: user's `theme.css` if non-empty,
  otherwise the built-in default. Always returns valid CSS. `toggle` is
  the theme the light/dark switch would save, painted on tap."""
  # Include `mode` so the client paints data-theme/color-scheme from the
  # AUTHORITATIVE persisted mode, not inferMode(bg). Without it, a stale
  # while-revalidate /api/theme body repaints a mode inferred from the (stale)
  # bg, which diverges from — and sticks against — the toggle knob's separate
  # theme-mode source.
  return theme_with_toggle(settings.data_dir)


class ThemeModeBody(BaseModel):
  mode: Literal["dark", "light"]


@router.post("/mode", dependencies=[Depends(reject_cross_site)])
def save_theme_mode(
  body: ThemeModeBody,
  _: models.Owner = Depends(get_current_owner),
  settings: Settings = Depends(get_settings),
):
  """The light/dark switch: rewrites theme.css for `mode` from what is on
  disk now (so an edit the shell has not loaded yet is kept), saves the
  mode, tells open views, and returns the new theme as `GET` does."""
  try:
    changed = set_theme_mode(settings.data_dir, body.mode)
  except UnreadableThemeCss as exc:
    raise HTTPException(409, detail=str(exc)) from None
  if changed:
    publish_system_event({"type": "theme_updated"})
  return theme_with_toggle(settings.data_dir)


@router.post("/reset", dependencies=[Depends(reject_cross_site)])
def reset_theme(
  _: models.Owner = Depends(get_current_owner),
  settings: Settings = Depends(get_settings),
):
  """Moves `/data/shared/theme.css` aside so DEFAULT_THEME paints
  again, preserving the previous theme as
  `theme.css.reset-bak-<unix-ts>`.

  This is the JSON endpoint used by the shell's `?reset-theme=1`
  URL-parameter reset flow.

  Idempotent: with no override present, returns
  `{"reset": false, "reason": "no override"}`.
  """
  return reset_theme_override(settings.data_dir)
