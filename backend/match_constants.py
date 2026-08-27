import hashlib
import json
from typing import Any


MATCH_ALGORITHM_VERSION = "2"


def job_match_fingerprint(job: dict[str, Any]) -> str:
    content = {
        key: job.get(key) or ""
        for key in ("title", "description", "descriptionSnippet", "roleType", "location", "workType")
    }
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
