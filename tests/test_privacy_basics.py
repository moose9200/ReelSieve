"""Privacy basics that must never regress: no visitor data sent to third-party asset hosts."""
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / 'app'


def test_no_page_loads_fonts_or_assets_from_google():
    offenders = [str(p.relative_to(APP)) for p in list((APP / 'templates').glob('*.html')) + list((APP / 'static').glob('*.css'))
                 if 'fonts.googleapis.com' in p.read_text() or 'fonts.gstatic.com' in p.read_text()]
    assert offenders == []
    assert (APP / 'static' / 'fonts' / 'assistant-latin.woff2').stat().st_size > 10_000
    assert 'assistant-latin.woff2' in (APP / 'static' / 'fonts.css').read_text()
