# llmfailover brand assets

The mark is an **open circuit breaker with a detour**: two terminals on the primary line, the
breaker lever (orange) flipped open, and a green arc carrying the request over it to the far
terminal. That is what the gateway does: a provider's breaker trips and the request still arrives.

| File | Use |
|---|---|
| `logo.svg` / `logo-dark.svg` | Lockup (mark + wordmark) for light / dark backgrounds. README header. |
| `icon.svg` / `icon-dark.svg` | Mark alone, transparent background. |
| `favicon.svg`, `favicon-32.png`, `favicon-16.png` | Browser tab favicon (own ink tile, works on light and dark tabs). |
| `avatar.svg` → `avatar-512.png` | Square, opaque avatar. |
| `social-preview.svg` → `social-preview.png` | Repository social preview (Settings → Social preview), 1280×640, opaque. |

## Colors

| Token | Hex | Contrast | Use |
|---|---|---|---|
| Ink | `#0C1417` | 18.6:1 on `#FFFFFF` | Mark and wordmark on light backgrounds; tiles |
| Snow | `#EEF4F1` | 17.0:1 on `#0D1117` | Mark and wordmark on dark backgrounds |
| Route (light) | `#007A55` | 5.4:1 on `#FFFFFF` | Detour arc and "failover" on light backgrounds |
| Route (dark) | `#34D399` | 9.8:1 on `#0D1117` | Detour arc and "failover" on dark backgrounds |
| Trip (light) | `#C2410C` | 5.2:1 on `#FFFFFF` | Open breaker lever, light backgrounds |
| Trip (dark) | `#FB923C` | 8.4:1 on `#0D1117` | Open breaker lever, dark backgrounds |

The build script prints these ratios and fails below WCAG's 3:1 minimum for graphics.

## Rebuilding

```sh
cargo install resvg --locked --version 0.48.1   # PNG export
uv run assets/brand/tools/build_brand.py
```

The script fetches JetBrains Mono v2.304 from its official release, verifies its SHA-256,
outlines the wordmark (no font needed to render the SVGs), checks colour contrast, rejects any
SVG containing scripts, event handlers, `foreignObject` or external references, and renders the
PNGs with resvg. The font archive is cached in `tools/.cache/` (git-ignored).

## Licensing

The wordmark uses outlines of **JetBrains Mono** (SIL Open Font License 1.1, © The JetBrains
Mono Project Authors), which permits use in artwork such as logos. The font itself is not
redistributed here. The code is MIT; the MIT license grants no trademark rights to the
llmfailover name or logo.
