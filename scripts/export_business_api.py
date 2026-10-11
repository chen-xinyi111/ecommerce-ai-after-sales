import argparse
import json
from pathlib import Path

from service import app

parser = argparse.ArgumentParser()
parser.add_argument("--output", default="docs/api/openapi.json")
output = Path(parser.parse_args().output)
output.parent.mkdir(parents=True, exist_ok=True)
document = app.openapi()
document["paths"] = {
    key: value for key, value in document["paths"].items() if key.startswith("/api/v1/")
}
output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Exported {len(document['paths'])} business paths to {output}")
