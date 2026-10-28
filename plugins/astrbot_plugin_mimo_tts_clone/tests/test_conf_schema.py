import json
from pathlib import Path
import unittest


class ConfigSchemaTests(unittest.TestCase):
    def test_list_and_object_fields_define_items(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        missing = [
            key
            for key, value in schema.items()
            if value.get("type") in {"list", "object"} and "items" not in value
        ]

        self.assertEqual(missing, [])
