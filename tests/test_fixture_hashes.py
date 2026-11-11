import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "sample_invoices"


def test_all_fixture_pdf_hashes_match_committed_manifest():
    expected = {}
    for line in (ROOT / "data" / "fixtures.sha256").read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, relative_path = line.split(maxsplit=1)
            expected[relative_path.strip().replace("\\", "/")] = digest

    actual_files = {path.name for path in SAMPLES.glob("*.pdf")}
    assert actual_files == {Path(name).name for name in expected}
    for relative_path, digest in expected.items():
        path = ROOT / relative_path
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, relative_path
