"""The owner's pages (sign-in and outbox) follow the system's light or dark mode, with no colour left hard-coded outside the theme."""
import re

from icloud_mcp import approvals, auth, dashboard


def check(css):
    assert "color-scheme:light dark" in css and "@media (prefers-color-scheme:dark)" in css
    rules = css[css.index("\n", css.index("prefers-color-scheme")):]           # everything after the two theme blocks
    colours = re.findall(r"#[0-9a-fA-F]{3,6}\b", rules.replace("color:#fff", ""))  # white button text reads on both
    assert not colours, colours


def test_sign_in_page_follows_the_system_theme():
    css = auth._PAGE.split("<style>")[1].split("</style>")[0]
    check(css)
    assert '<meta name="color-scheme" content="light dark">' in auth._PAGE


def test_outbox_page_follows_the_system_theme():
    check(approvals._CSS)
    assert '<meta name="color-scheme" content="light dark">' in approvals._page("t", "x").body.decode()


def test_dashboard_follows_the_system_theme():
    css = dashboard._CSS
    assert "color-scheme:light dark" in css and "@media (prefers-color-scheme:dark)" in css
    rules = css[css.index("}}", css.index("prefers-color-scheme")) + 2:]           # everything after the dark block
    colours = re.findall(r"#[0-9a-fA-F]{3,6}\b", rules.replace("color:#fff", ""))
    assert not colours, colours


def test_the_sign_in_card_is_centred_on_wide_screens():
    css = dashboard._CSS
    auth_rule = re.search(r"\.auth\{([^}]*)\}", css).group(1)
    assert "max-width:52rem" in re.search(r"(?:^|\n)main\{([^}]*)\}", css).group(1)   # the page column is capped ...
    assert "max-width:none" in auth_rule and "justify-content:center" in auth_rule   # ... the sign-in screen is not
