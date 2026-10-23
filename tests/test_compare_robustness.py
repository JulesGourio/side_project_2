"""XML attributes, LibreOffice discovery memory and the cut-.doc notice."""

import pytest

from server.services.compare import conversion, soffice
from server.services.compare.processors import text_diff
from server.services.compare.processors.xml import XMLProcessor, _extract_xml


def test_xml_attributes_are_extracted_in_a_stable_order():
    lines = _extract_xml(b'<limit max="85" min="-40"/>')
    assert [ln.rsplit(' [Item', 1)[0] for ln in lines] == ['limit@max=85', 'limit@min=-40']


def test_a_changed_xml_attribute_is_reported_in_the_diff():
    old = b'<spec><limit min="-40" max="85"/></spec>'
    new = b'<spec><limit min="-40" max="90"/></spec>'
    result = XMLProcessor(method='standard').build_messages(old, 'a.xml', new, 'b.xml')
    text = result.messages[-1]['content'][0]['text']
    assert 'limit@max=85' in text and 'limit@max=90' in text


def test_identical_xml_attributes_give_no_change():
    xml = b'<spec><limit min="-40" max="85"/></spec>'
    result = XMLProcessor(method='standard').build_messages(xml, 'a.xml', xml, 'b.xml')
    assert 'No significant text changes' in result.messages[-1]['content'][0]['text']


@pytest.fixture
def fake_discovery(monkeypatch):
    """A clock the test moves, a probe that always fails and the counter of its calls."""
    clock = {'now': 1000.0}
    calls = []
    monkeypatch.setattr(soffice, '_clock', lambda: clock['now'])
    monkeypatch.setattr(soffice, '_discovered_path', None)
    monkeypatch.setattr(soffice, '_discovery_failed_at', None)
    monkeypatch.setattr(soffice, '_discovery_error', None)
    monkeypatch.delenv('SOFFICE_PATH', raising=False)
    monkeypatch.delenv('SOFFICE_ARCHIVE_VOLUME_PATH', raising=False)

    def well_known():
        calls.append(clock['now'])
        return []

    monkeypatch.setattr(soffice, '_well_known_paths', well_known)
    monkeypatch.setattr(soffice, '_extract_root', lambda: soffice.Path('/nonexistent-soffice-root'))
    return clock, calls


def test_a_failed_discovery_is_not_probed_again_for_five_minutes(fake_discovery):
    clock, calls = fake_discovery
    with pytest.raises(soffice.SofficeUnavailable):
        soffice.find_soffice()
    clock['now'] += 299
    with pytest.raises(soffice.SofficeUnavailable):
        soffice.find_soffice()
    assert len(calls) == 1
    clock['now'] += 2
    with pytest.raises(soffice.SofficeUnavailable):
        soffice.find_soffice()
    assert len(calls) == 2


def test_a_cut_doc_note_becomes_a_warning_and_is_not_diffed():
    note = conversion.DOC_TRUNCATED_NOTE
    assert text_diff._without_doc_note(f'a\nb\n{note}') == ('a\nb', True)
    assert text_diff._without_doc_note('a\nb') == ('a\nb', False)
