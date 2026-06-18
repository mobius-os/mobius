# Image generation

How to generate an image and get it into the chat — the method depends on which provider is running. `Read` this before generating an image; check the `Provider:` line in your injected `<agent_experience>` block first.

For simple icons or logos, consider an SVG instead — it's crisp, themeable, and reviewable in diffs.

---

## Codex (built-in `$imagegen` — default)

Codex includes a free built-in image generator covered by the plan — **use this by default**, no API key needed. Only fall back to Gemini if the partner explicitly asks.

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

---

## Claude (Gemini API)

Claude has no built-in image generation — use the Gemini endpoint. If the response is 503, tell the partner that no Gemini API key is configured (they can add one in Settings).

```bash
curl -s -X POST "$API_BASE_URL/api/chats/$CHAT_ID/generate-image" \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a serene mountain landscape", "aspect_ratio": "1:1"}'
```

Returns `{ "url": "/api/chats/{id}/media/{filename}", "model": "..." }`. Aspect ratios: `"1:1"` (default), `"16:9"`, `"9:16"`, `"4:3"`, `"3:4"`.

---

## Embedding — always show it after creating

Either way, embed the image in chat after creating it (an image you generated but didn't embed is invisible to the partner — same trap as screenshots):

```markdown
![description](/api/chats/$CHAT_ID/media/<filename>)
```

`$CHAT_ID` above is a shell placeholder — it only expands inside a bash command. In the markdown link you write, the path must carry the RESOLVED chat id, never the literal `$CHAT_ID` segment: the renderer extracts the chat id from the path, so an unexpanded `$CHAT_ID` matches no real chat and 404s. Two-part discipline: put the real chat id in the link, and make sure the file physically lives in `/data/chats/<resolved-id>/media/` before you embed it.
