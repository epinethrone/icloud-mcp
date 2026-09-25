"""Draw the README artwork (assets/readme/*.svg) in a light and a dark version.

    python dev/readme_art.py

Text uses the reader's system font (San Francisco on Apple devices), so the images stay small and sharp. The app glyphs are
simple generic drawings, not Apple's icons.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "readme"
FONT = "-apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Helvetica Neue', Helvetica, Arial, sans-serif"

THEMES = {
    "light": dict(bg="#fbfbfd", card="#ffffff", line="#e5e5ea", ink="#1d1d1f", soft="#6e6e73", faint="#86868b",
                  glow="#c7d2fe"),
    "dark": dict(bg="#000000", card="#1c1c1e", line="#2c2c2e", ink="#f5f5f7", soft="#a1a1a6", faint="#86868b",
                 glow="#312e81"),
}


def logo(x: float, y: float, size: float) -> str:
    """The project logo, inlined (an <img> SVG cannot load another file)."""
    body = (ROOT / "assets" / "logo.svg").read_text()
    inner = re.search(r"<svg[^>]*>(.*)</svg>", body, re.S).group(1)
    inner = re.sub(r"<title[^>]*>.*?</title>", "", inner, flags=re.S)
    inner = re.sub(r'id="(\w+)"', r'id="logo-\1"', inner).replace("url(#", "url(#logo-")
    return f'<svg x="{x}" y="{y}" width="{size}" height="{size}" viewBox="0 0 512 512">{inner}</svg>'


def svg(w: int, h: int, title: str, body: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
            f'aria-labelledby="t"><title id="t">{title}</title>\n<style>text{{font-family:{FONT}}}</style>\n{body}\n</svg>\n')


# ------------------------------------------------------------------ hero
def hero(t: dict) -> str:
    w, h = 1280, 600
    return svg(w, h, "iCloud MCP. Your iCloud, in Claude.", f"""
<defs>
  <radialGradient id="glow" cx="0.5" cy="0.34" r="0.5">
    <stop offset="0" stop-color="{t['glow']}" stop-opacity="0.9"/>
    <stop offset="1" stop-color="{t['glow']}" stop-opacity="0"/>
  </radialGradient>
  <linearGradient id="word" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#0ea5e9"/><stop offset="0.5" stop-color="#6366f1"/><stop offset="1" stop-color="#a855f7"/>
  </linearGradient>
</defs>
<rect width="{w}" height="{h}" rx="36" fill="{t['bg']}"/>
<ellipse cx="640" cy="200" rx="420" ry="240" fill="url(#glow)"/>
{logo(572, 64, 136)}
<text x="640" y="296" text-anchor="middle" font-size="30" font-weight="600" fill="{t['soft']}" letter-spacing="0.5">iCloud MCP</text>
<text x="640" y="392" text-anchor="middle" font-size="84" font-weight="700" fill="{t['ink']}" letter-spacing="-2.5">Your iCloud. <tspan fill="url(#word)">In Claude.</tspan></text>
<text x="640" y="456" text-anchor="middle" font-size="27" fill="{t['soft']}">Mail, Calendar, Contacts, Reminders, Notes, iCloud Drive, Maps and Messages.</text>
<text x="640" y="494" text-anchor="middle" font-size="27" fill="{t['soft']}">Private by design. It asks before anything leaves.</text>
<text x="640" y="556" text-anchor="middle" font-size="19" fill="{t['faint']}" letter-spacing="1.5">88 TOOLS  ·  ONE CONNECTOR  ·  RUNS ON YOUR OWN MACHINE</text>
""")


# ------------------------------------------------------------------ app grid
def glyph(kind: str, cx: float, cy: float) -> str:
    """A white line drawing centred on (cx, cy), about 44 px across."""
    s = f'transform="translate({cx - 22},{cy - 22})" fill="none" stroke="#fff" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"'
    return {
        "mail": f'<g {s}><rect x="2" y="8" width="40" height="28" rx="5"/><path d="M4 11 L22 25 L40 11"/></g>',
        "calendar": f'<g {s}><rect x="4" y="7" width="36" height="32" rx="6"/><path d="M4 16 H40 M14 3 V10 M30 3 V10"/>'
                    f'<circle cx="15" cy="25" r="1.6" fill="#fff"/><circle cx="22" cy="25" r="1.6" fill="#fff"/>'
                    f'<circle cx="29" cy="25" r="1.6" fill="#fff"/><circle cx="15" cy="32" r="1.6" fill="#fff"/></g>',
        "contacts": f'<g {s}><circle cx="22" cy="15" r="8"/><path d="M7 39 C8 29 15 25 22 25 C29 25 36 29 37 39"/></g>',
        "reminders": f'<g {s}><circle cx="9" cy="11" r="3.5"/><circle cx="9" cy="22" r="3.5"/><circle cx="9" cy="33" r="3.5"/>'
                     f'<path d="M18 11 H40 M18 22 H40 M18 33 H34"/></g>',
        "notes": f'<g {s}><rect x="6" y="3" width="32" height="38" rx="6"/><path d="M13 14 H31 M13 22 H31 M13 30 H24"/></g>',
        "drive": f'<g {s}><path d="M3 13 C3 10 5 8 8 8 H17 L21 12 H36 C39 12 41 14 41 17 V33 C41 36 39 38 36 38 H8 C5 38 3 36 3 33 Z"/></g>',
        "maps": f'<g {s}><path d="M22 41 C22 41 8 27 8 17 A14 14 0 0 1 36 17 C36 27 22 41 22 41 Z"/><circle cx="22" cy="17" r="5"/></g>',
        "messages": f'<g {s}><path d="M22 6 C33 6 41 12.5 41 20.5 C41 28.5 33 35 22 35 C19.5 35 17 34.6 14.8 33.9 L6 38 L8.5 30.5 C5 27.8 3 24.3 3 20.5 C3 12.5 11 6 22 6 Z"/></g>',
        "shortcuts": f'<g {s}><rect x="4" y="12" width="28" height="28" rx="7"/><path d="M12 4 H34 C37.3 4 40 6.7 40 10 V32"/></g>',
    }[kind]


APPS = [
    ("mail", "Mail", 25, "#0a84ff", "#5ac8fa", ["Search every folder, read whole", "threads, reply and file away."]),
    ("calendar", "Calendar", 12, "#ff3b30", "#ff6961", ["Find free time, plan, move", "and answer invitations."]),
    ("contacts", "Contacts", 11, "#8e8e93", "#aeaeb2", ["Find anyone, even misspelled.", "Addresses, phones, emails."]),
    ("reminders", "Reminders", 10, "#ff9500", "#ffb340", ["Repeating, with alerts,", "live through EventKit."]),
    ("notes", "Notes", 9, "#ffcc00", "#ffd60a", ["Read, edit and organise", "into folders."]),
    ("drive", "iCloud Drive", 10, "#32ade6", "#64d2ff", ["Search inside documents,", "send, write and tidy files."]),
    ("maps", "Maps", 2, "#00a89d", "#40d3c9", ["Travel times by bike, car,", "foot or transit. Find places."]),
    ("messages", "Messages", 4, "#28b44b", "#5de07a", ["Read and search your chats.", "Sending waits for your OK."]),
    ("shortcuts", "Shortcuts", 2, "#7d4ce0", "#c56cf0", ["Run only the shortcuts", "you allow, listed twice."]),
]


def apps(t: dict) -> str:
    w, h = 1280, 1036
    cw, ch, gap, x0, y0 = 392, 272, 24, 28, 132
    tiles = []
    for i, (kind, name, count, c1, c2, lines) in enumerate(APPS):
        x, y = x0 + (i % 3) * (cw + gap), y0 + (i // 3) * (ch + gap)
        mac = i >= 3
        tiles.append(f"""
<g>
  <rect x="{x}" y="{y}" width="{cw}" height="{ch}" rx="28" fill="{t['card']}" stroke="{t['line']}"/>
  <defs><linearGradient id="g{i}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{c2}"/><stop offset="1" stop-color="{c1}"/></linearGradient></defs>
  <rect x="{x + 32}" y="{y + 32}" width="72" height="72" rx="18" fill="url(#g{i})"/>
  {glyph(kind, x + 68, y + 68)}
  <text x="{x + cw - 32}" y="{y + 64}" text-anchor="end" font-size="44" font-weight="700" fill="{t['ink']}" letter-spacing="-1">{count}</text>
  <text x="{x + cw - 32}" y="{y + 92}" text-anchor="end" font-size="16" fill="{t['faint']}">tools</text>
  <text x="{x + 32}" y="{y + 158}" font-size="30" font-weight="700" fill="{t['ink']}" letter-spacing="-0.5">{name}</text>
  <text x="{x + 32}" y="{y + 196}" font-size="20" fill="{t['soft']}">{lines[0]}</text>
  <text x="{x + 32}" y="{y + 224}" font-size="20" fill="{t['soft']}">{lines[1]}</text>
  {f'<text x="{x + 32}" y="{y + 254}" font-size="14" font-weight="600" fill="{t["faint"]}" letter-spacing="1.2">WITH THE MAC HELPER</text>' if mac else ''}
</g>""")
    return svg(w, h, "Nine apps, one connector: Mail 25 tools, Calendar 12, Contacts 11, Reminders 10, Notes 9, iCloud Drive 10, "
                     "Maps 2, Messages 4, Shortcuts 2.", f"""
<rect width="{w}" height="{h}" rx="36" fill="{t['bg']}"/>
<text x="640" y="72" text-anchor="middle" font-size="46" font-weight="700" fill="{t['ink']}" letter-spacing="-1.2">Nine apps. One connector.</text>
<text x="640" y="108" text-anchor="middle" font-size="21" fill="{t['soft']}">Plus a health check that tests every service in one call.</text>
{''.join(tiles)}
""")


# ------------------------------------------------------------------ how it works
def node(t: dict, x: float, y: float, w: float, h: float, title: str, sub: str, accent: str | None = None, sub2: str = "") -> str:
    stroke = accent or t["line"]
    width = 2.5 if accent else 1
    top = h / 2 - (14 if sub2 else 4)
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="22" fill="{t["card"]}" stroke="{stroke}" stroke-width="{width}"/>'
            f'<text x="{x + w / 2}" y="{y + top}" text-anchor="middle" font-size="24" font-weight="700" fill="{t["ink"]}">{title}</text>'
            f'<text x="{x + w / 2}" y="{y + top + 28}" text-anchor="middle" font-size="18" fill="{t["soft"]}">{sub}</text>'
            + (f'<text x="{x + w / 2}" y="{y + top + 52}" text-anchor="middle" font-size="18" fill="{t["soft"]}">{sub2}</text>' if sub2 else ""))


def arrow(t: dict, x1, y1, x2, y2, label: str, lx: float, ly: float, anchor: str = "middle") -> str:
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{t["faint"]}" stroke-width="2" marker-end="url(#head)"/>'
            f'<text x="{lx}" y="{ly}" text-anchor="{anchor}" font-size="18" fill="{t["faint"]}">{label}</text>')


def how(t: dict) -> str:
    w, h = 1280, 520
    return svg(w, h, "How it works: Claude connects to your server over OAuth and MCP; the server talks to iCloud Mail, Calendar and "
                     "Contacts; an optional Mac helper connects out to the server for Reminders, Notes, iCloud Drive, Maps and "
                     "Messages; an optional menu bar app controls the server through a private admin port.", f"""
<defs><marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
  <path d="M0 0 L10 5 L0 10 z" fill="{t['faint']}"/></marker></defs>
<rect width="{w}" height="{h}" rx="36" fill="{t['bg']}"/>
<text x="640" y="68" text-anchor="middle" font-size="40" font-weight="700" fill="{t['ink']}" letter-spacing="-1">How it works.</text>
{node(t, 60, 150, 280, 110, "Claude", "or any MCP client")}
{node(t, 500, 150, 280, 110, "icloud-mcp", "on your server or your Mac", "#6366f1")}
{node(t, 940, 150, 280, 110, "iCloud", "Mail · Calendar · Contacts")}
{node(t, 500, 360, 280, 110, "Mac helper", "optional, connects out")}
{node(t, 940, 360, 280, 110, "On your Mac", "Reminders · Notes · Drive", sub2="Maps · Messages")}
{node(t, 60, 360, 280, 110, "Menu bar app", "optional, on the same Mac")}
{arrow(t, 342, 205, 496, 205, "OAuth + MCP", 420, 190)}
{arrow(t, 782, 205, 936, 205, "IMAP · SMTP · DAV", 860, 190)}
{arrow(t, 640, 358, 640, 264, "pinned TLS, outbound only", 656, 316, "start")}
{arrow(t, 782, 415, 936, 415, "EventKit · MapKit", 860, 400)}
{arrow(t, 342, 390, 540, 266, "admin, this Mac only", 462, 300, "end")}
""")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, draw in (("hero", hero), ("apps", apps), ("how", how)):
        for theme, colours in THEMES.items():
            (OUT / f"{name}-{theme}.svg").write_text(draw(colours))
    print(sorted(p.name for p in OUT.iterdir()))
