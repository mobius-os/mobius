# Game Project

Use this guidance in a Game Studio Project. Edit `scene.game.json` inside
`PROJECT_ROOT`; title, colors, step size and the initial target are editable.
Build its Game Creation and open that result to play. Never overwrite other
projects. The Project app supplies its custom player in `player.html` and
`build.py`; those are provider source, not the project's scene data.

To create a different game engine, extend this Project app's builder and starter
files, not the Möbius shell. Keep the emitted HTML player and assets local and
self-contained. Existing projects retain source; reinstalling a provider must
not recreate their scene from the starter template.
