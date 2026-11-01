"""List Vultr Serverless Inference models and whether any accepts image input.

Usage: python scripts/list_models.py [--json]
Reads VULTR_INFERENCE_API_KEY (and optional VULTR_INFERENCE_BASE_URL) from .env or the environment.
One GET /v1/models request; no tokens are spent.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eig_agent.models import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(as_json="--json" in sys.argv[1:]))
