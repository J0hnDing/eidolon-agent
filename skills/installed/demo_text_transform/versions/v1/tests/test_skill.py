import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("demo_text_transform_skill", ROOT / "skill.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_transform_normalizes_and_uppercases_text():
    assert MODULE.transform({"text": "  Hello   from Eidolon  "}) == {
        "transformed_text": "HELLO FROM EIDOLON",
        "character_count": 18,
    }
