# ZENITH — Logo assets

The logo is the full **ZENITH.** wordmark — used everywhere, including the
favicon and app icon (no separate icon mark).

**Spec**
- Font: **Inter 700**
- Tracking: **0.16em**
- Text: `#E6E8EB` on dark · `#11141A` on light
- Full stop: `#00C896` (signal green) · `#00A87D` on light

## Files
| File | Use |
|---|---|
| `wordmark.css` | Canonical CSS for the wordmark — drop into any page |
| `logo-snippets.html` | Copy-paste HTML for nav / topbar / footer + the `<head>` tags |
| `favicon.svg` | Scalable favicon (Inter with system fallback) |
| `favicon-16/32/48/64.png` | PNG favicons (dark square) |
| `favicon-180.png` · `apple-touch-icon.png` | iOS home-screen icon |
| `favicon-192.png` · `favicon-512.png` | PWA / Android icons (manifest) |
| `zenith-wordmark-dark-bg.png` | Transparent wordmark, light text — for dark backgrounds (email, decks) |
| `zenith-wordmark-light-bg.png` | Transparent wordmark, dark text — for light backgrounds |

## Quick start
1. Load Inter + `wordmark.css` in `<head>` (see `logo-snippets.html`).
2. Add the favicon/app-icon `<link>` tags.
3. Place `<a class="zenith-wordmark" style="font-size:18px">ZENITH<span class="dot">.</span></a>`
   wherever the logo goes; change `font-size` per placement.

> The PNGs are rendered in real Inter, so they're correct even where the
> webfont isn't loaded. The SVG favicon falls back to a system sans if Inter
> isn't available in the favicon context.
