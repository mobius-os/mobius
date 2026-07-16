# Image generation

How to generate an image with Codex and get it into the chat. `Read` this before generating an image, and check the `Provider:` line in your injected `<agent_experience>` block first. Möbius does not expose a built-in image-generation path for other providers.

For simple icons or logos, consider an SVG instead — it's crisp, themeable, and reviewable in diffs.

---

## Codex (`$imagegen`)

Codex includes a built-in image generator covered by the plan, with no separate API key needed.

```bash
$imagegen "a serene mountain landscape"
```

The PNG saves under `/data/cli-auth/codex/generated_images/...` and is NOT automatically visible in Möbius chat. Copy it into the chat's media dir, then embed:

```bash
IMG=$(ls -t /data/cli-auth/codex/generated_images/*.png 2>/dev/null | head -1)
mkdir -p /data/chats/$CHAT_ID/media
FNAME="$(basename "$IMG")"
cp "$IMG" /data/chats/$CHAT_ID/media/"$FNAME"
```

Then in your reply:

```markdown
![description](/api/chats/$CHAT_ID/media/<filename>)
```

## Embedding — always show it after creating

Embed the image in chat after creating it (an image you generated but didn't embed is invisible to the partner — same trap as screenshots):

```markdown
![description](/api/chats/$CHAT_ID/media/<filename>)
```

`$CHAT_ID` above is a shell placeholder — it only expands inside a bash command. In the markdown link you write, the path must carry the RESOLVED chat id, never the literal `$CHAT_ID` segment: the renderer extracts the chat id from the path, so an unexpanded `$CHAT_ID` matches no real chat and 404s. Two-part discipline: put the real chat id in the link, and make sure the file physically lives in `/data/chats/<resolved-id>/media/` before you embed it.
