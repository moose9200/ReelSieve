# Listing Reel — by Braivex

Paste an Airbnb listing URL → a ~30 s cinematic 16:9 reel (intro, trust card, depth-parallax walkthrough, real review card, outro "by Braivex.com") → handed to the **listing host through Airbnb messaging** (pre-filled from your own Airbnb account; you press Send).

## Run
```bash
uv venv .venv --python 3.12
VIRTUAL_ENV=$PWD/.venv uv pip install torch torchvision transformers pillow opencv-python-headless numpy higgsfield-client python-dotenv fastapi "uvicorn[standard]" httpx jinja2 python-multipart playwright
.venv/bin/python -m playwright install chromium
.venv/bin/uvicorn app.server:app --port 8787
```
Open http://localhost:8787 → Settings → add keys (written to `.env.local`, chmod 600, git-ignored, never rendered back).

## Settings
| Setting | Purpose |
|---|---|
| Airbnb account | "Connect Airbnb" opens a Chrome window (Playwright persistent profile in `.listing-reel/airbnb-profile`); log in once. Used only to open the host's contact form pre-filled. |
| Public link | Airbnb messages can't carry video, so the reel is linked. Set `PUBLIC_BASE_URL`, or "Start tunnel" (cloudflared quick tunnel, live while the app runs). |
| Message template | `DEFAULT_MESSAGE`, `{reel_link}` is replaced with the public link. |
| `HF_KEY` | Higgsfield API `key-id:key-secret` — enables "AI camera motion" (Seedance 2.5 image-to-video, billable). |

## Sending to the host (by design, never automatic)
Job page → "Open pre-filled Airbnb message" → a browser window opens `airbnb.co.uk/contact_host/<id>/send_message` with the message filled in → you review and press **Send message**. Or copy the link/message and use "Open contact form in this browser".

## Pipeline (`app/pipeline.py`)
1. `scrape_listing` — public listing HTML: title, city, rating, review count, guests, superhost, category ratings, amenities, photos with room labels.
2. `scrape_reviews` — headless Chromium on `/rooms/<id>/reviews` (client-rendered): name, stars, date, text, % five-star.
3. `build_manifest` — route order (exterior → living → kitchen → bedroom → bath → garden → spa), captions from real facts only, best short 5★ review.
4. Optional `seedance_clips` — Higgsfield `bytedance/seedance-2.5/image-to-video` per scene via official SDK `subscribe`; failed/nsfw/canceled → parallax fallback.
5. `depth.py` (Depth-Anything-V2-Small) + `render_v2.py` (2.5D parallax, letterbox, grade, animated text, xfade) → 1080p + 720p copy.
6. `hostmsg.open_draft` — contact-host form pre-filled via your Airbnb session; `hostmsg.tunnel_*` — public link.

CLI: `.venv/bin/python app/pipeline.py <airbnb_url> jobs/<name> [--email you@x.com] [--ai-motion]`

`main.py` — Seedance 2.5 text-to-video SDK smoke test (`subscribe`, handles Failed/NSFW/Cancelled).
Skill: `~/.claude/skills/property-to-generator` (same renderer, manual/agent workflow).

## Keep it running (macOS)
Installed as a LaunchAgent so it survives Claude sessions and reboots:
`~/Library/LaunchAgents/com.braivex.listing-reel.plist` → http://localhost:8787, logs in `logs/server.log`.
```bash
launchctl kickstart -k gui/$(id -u)/com.braivex.listing-reel   # restart after code changes
launchctl bootout gui/$(id -u)/com.braivex.listing-reel        # stop
```

## QA guards (never repeat a fixed mistake)
`pipeline.lint_manifest` runs on every job before rendering and fails the job on: duplicate captions, a photo used twice, outro sharing the trust/review background, fewer than 6 distinct photos, repeated facts in intro/outro lines, unsanitised review text (leading ", ·" or "Stayed with kids" tags), brand watermark on, missing image files, or a reel outside 26–36 s.
Offline tests on two real listings: `.venv/bin/python -m pytest -q tests` — run before restarting the LaunchAgent.
Fonts: Canva Sans is auto-used if you install it (`~/Library/Fonts/CanvaSans-*.ttf`); otherwise Inter (free, OFL) → Montserrat → Arial.

## Pacing research (19 Sep 2026)
Shots 3–5 s (over 6 s loses momentum); 8–15 photos per reel; order = exterior → entry → living → kitchen/dining → bedrooms → baths → outdoor, end on the best feature + CTA; slideshow reels 60–180 s.
Sources: [StudioBinder](https://www.studiobinder.com/blog/real-estate-video-production/), [Vimeo](https://vimeo.com/blog/post/real-estate-video-marketing), [Reel-E length guide](https://www.reel-e.ai/blog/real-estate-video-length), [Reel-E photos→video](https://www.reel-e.ai/blog/make-real-estate-video-from-photos), [Virtuance slideshows](https://www.virtuance.com/image-slideshow-videos/), [Runway walkthrough](https://runway.com/resources/real-estate-walkthrough-video).
Applied: 4.5 s per photo, every labelled photo up to 14 grouped by room in strict route order, length scales with photo count.

## v3 renderer (default since 19 Sep 2026) — matches the tutorial output
Vertical 9:16 full-bleed, continuous forward camera with zoom-through transitions into the next room, clean bright grade, kinetic captions (one yellow accent word), title/rating/review/CTA overlays on scenes, plus `-16x9.mp4` (portrait over blurred fill). Set `RENDERER=v2` for the older letterboxed card look. Higgsfield Seedance/Kling clips slot in per scene when credits exist.
Host message: "Message host on Airbnb" copies the message and opens the contact form in the browser you're using (already logged in); paste and press Send. The Playwright helper window is optional.

## Prompt-set mode (no Airbnb listing)
`swiss-home/` — 10-image chained prompt set (exterior → living → open-plan → dining/stairs → kitchen → island → living hero → lanai → rear lawn → drone). Images generated in Gemini (free) with each result as the next reference; `prompts.json` holds the prompts. Then `depth.py images depth` + `render_v3.py manifest.json out/…mp4`. Higgsfield had 0 credits/no unlimited allowance, so Gemini was used.
Gemini download quirk: the "Download full-sized image" button only fires when the image is scrolled into view and hovered first.
