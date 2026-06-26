import sys
from pathlib import Path

# Make `import campaign_runner` and `import run` work from the repo.
ROOT = Path(__file__).resolve().parents[1]          # experiments/campaign/
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"

def sample_spec_text() -> str:
    return (FIXTURES / "sample_spec.yml").read_text()