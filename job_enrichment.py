from __future__ import annotations

import re
from typing import Any


_CURRENCY_MARKER = r"(?:US\$|CA\$|\$|£|€|USD|CAD|GBP|EUR)"
_PAY_NUMBER = r"\d[\d,]*(?:\.\d{1,2})?\s*[kK]?"

PAY_CONTEXT_PATTERN = re.compile(
    rf"(?i)\b(?:pay range|salary range|compensation|starting at|up to|salary|pay|wage|hourly rate|annual rate)\b"
    rf"[^.;\n\r]{{0,40}}?(?:{_CURRENCY_MARKER}\s*)?{_PAY_NUMBER}(?:\s*(?:usd|cad|gbp|eur|dollars?))?"
    rf"(?:\s*(?:-|to|–|—)\s*(?:{_CURRENCY_MARKER}\s*)?{_PAY_NUMBER}(?:\s*(?:usd|cad|gbp|eur|dollars?))?)?"
    r"(?:\s*(?:/|per)?\s*(?:hour|hr|year|yr|annually|annual|month|monthly|week|weekly))?"
)

PAY_PATTERN = re.compile(
    rf"(?i)(?:{_CURRENCY_MARKER}\s*{_PAY_NUMBER}|{_PAY_NUMBER}\s*(?:USD|CAD|GBP|EUR|dollars?))"
    rf"(?:\s*(?:-|to|–|—)\s*(?:{_CURRENCY_MARKER}\s*)?{_PAY_NUMBER}(?:\s*(?:usd|cad|gbp|eur|dollars?))?)?"
    r"(?:\s*(?:/|per)?\s*(?:hour|hr|year|yr|annually|annual|month|monthly|week|weekly))?"
)

EXEC_TITLE_PATTERNS = [
    r"\bchief\b",
    r"\bc[- ]?level\b",
    r"\bexecutive vice president\b",
    r"\bevp\b",
    r"\bsenior vice president\b",
    r"\bsvp\b",
    r"\bvice president\b",
    r"\bvp\b",
    r"\bpresident\b",
    r"\bcio\b",
    r"\bcto\b",
    r"\bciso\b",
    r"\bcoo\b",
    r"\bcfo\b",
    r"\bceo\b",
]

MGR_TITLE_PATTERNS = [
    r"\bmanager\b",
    r"\bsupervisor\b",
    r"\bdirector\b",
    r"\bhead of\b",
    r"\bteam lead\b",
    r"\blead manager\b",
    r"\bbranch manager\b",
    r"\boperations manager\b",
    r"\bassistant manager\b",
    r"\bdepartment manager\b",
]

MGR_DESCRIPTION_PATTERNS = [
    r"manages a team",
    r"supervises employees",
    r"direct reports",
    r"performance reviews",
    r"coaching staff",
    r"\b(?:hire|hiring),?\s+(?:coach(?:ing)?|manage(?:ment)?|supervis(?:e|ing))\b",
    r"leads department",
    r"responsible for (?:a )?team",
    r"oversees staff",
    r"people leader",
]

IC_TITLE_WORDS = {
    "specialist",
    "analyst",
    "engineer",
    "administrator",
    "consultant",
    "representative",
    "teller",
    "banker",
    "processor",
    "coordinator",
    "technician",
    "architect",
    "developer",
    "associate",
    "clerk",
    "advisor",
    "assistant",
    "officer",
}


def extract_pay_info(text: str) -> dict[str, Any]:
    value = " ".join(str(text or "").split())
    result: dict[str, Any] = {
        "payMin": None,
        "payMax": None,
        "payText": "",
        "payPeriod": "unknown",
        "payCurrency": "USD",
        "payExtractionSource": "",
        "payPatternMatched": "",
    }
    if not value:
        return result

    context_match = PAY_CONTEXT_PATTERN.search(value)
    match = context_match or PAY_PATTERN.search(value)
    if not match:
        return result

    pay_text = clean_pay_text(match.group(0))
    numbers = parse_pay_numbers(pay_text)
    if not numbers:
        return result
    if not plausible_pay_match(value, match.end(), pay_text, numbers, context_match is not None):
        return result
    result["payMin"] = numbers[0]
    result["payMax"] = numbers[1] if len(numbers) > 1 else None
    result["payText"] = pay_text
    result["payPeriod"] = detect_pay_period(pay_text)
    result["payCurrency"] = detect_currency(pay_text)
    result["payPatternMatched"] = "context" if context_match is not None else "currency"
    return result


def extract_json_ld_pay_info(base_salary: Any) -> dict[str, Any]:
    result = {
        "payMin": None,
        "payMax": None,
        "payText": "",
        "payPeriod": "unknown",
        "payCurrency": "USD",
        "payExtractionSource": "json_ld",
        "payPatternMatched": "baseSalary",
    }
    if not isinstance(base_salary, dict):
        return result
    currency = str(base_salary.get("currency") or "USD")
    value = base_salary.get("value")
    unit = ""
    if isinstance(value, dict):
        unit = str(value.get("unitText") or "")
        min_value = value.get("minValue")
        max_value = value.get("maxValue")
        single_value = value.get("value")
        if min_value is not None:
            result["payMin"] = parse_number(min_value)
        if max_value is not None:
            result["payMax"] = parse_number(max_value)
        elif single_value is not None:
            result["payMin"] = parse_number(single_value)
        result["payPeriod"] = detect_pay_period(unit)
    elif value is not None:
        result["payMin"] = parse_number(value)
    result["payCurrency"] = currency
    if result["payMin"] is not None and result["payMax"] is not None:
        result["payText"] = f"{currency} {result['payMin']} - {result['payMax']} {unit}".strip()
    elif result["payMin"] is not None:
        result["payText"] = f"{currency} {result['payMin']} {unit}".strip()
    return result


def classify_role_type(title: str, description: str) -> dict[str, str]:
    lowered_title = str(title or "").lower()
    lowered_description = str(description or "").lower()
    if not lowered_title.strip():
        return {"roleType": "UNKNOWN", "roleTypeReason": "Title missing or unclear."}

    for pattern in EXEC_TITLE_PATTERNS:
        if re.search(pattern, lowered_title):
            return {"roleType": "EXEC", "roleTypeReason": f"Matched executive title pattern '{pattern}'."}

    for pattern in MGR_TITLE_PATTERNS:
        if re.search(pattern, lowered_title):
            return {"roleType": "MGR", "roleTypeReason": f"Matched manager title pattern '{pattern}'."}

    if "lead" in lowered_title:
        for pattern in MGR_DESCRIPTION_PATTERNS:
            if re.search(pattern, lowered_description):
                return {"roleType": "MGR", "roleTypeReason": f"Lead title plus people-management signal '{pattern}'."}

    for pattern in MGR_DESCRIPTION_PATTERNS:
        if re.search(pattern, lowered_description):
            return {"roleType": "MGR", "roleTypeReason": f"Matched people-management description signal '{pattern}'."}

    words = set(re.findall(r"[a-z0-9]+", lowered_title))
    if words & IC_TITLE_WORDS:
        return {"roleType": "IC", "roleTypeReason": "Matched individual contributor title keyword."}
    if len(words) >= 2:
        return {"roleType": "IC", "roleTypeReason": "No executive or people-management signals found."}
    return {"roleType": "UNKNOWN", "roleTypeReason": "Not enough title signal to classify."}


def parse_pay_numbers(pay_text: str) -> list[int]:
    values = []
    for match in re.finditer(r"(?<![\d.])\$?\s?(\d[\d,]*(?:\.\d{1,2})?)\s*(k)?", pay_text, flags=re.IGNORECASE):
        number = float(match.group(1).replace(",", ""))
        if match.group(2):
            number *= 1000
        values.append(int(round(number)))
    if len(values) >= 2:
        return [min(values), max(values)]
    return values


def parse_number(value: Any) -> int | None:
    try:
        return int(round(float(str(value).replace(",", ""))))
    except Exception:
        return None


def detect_pay_period(text: str) -> str:
    lowered = str(text or "").lower()
    if any(term in lowered for term in ["hour", "/hr", " hr", "per hr"]):
        return "hourly"
    if any(term in lowered for term in ["annual", "annually", "year", "/yr"]):
        return "annual"
    if "month" in lowered:
        return "monthly"
    if "week" in lowered:
        return "weekly"
    numbers = parse_pay_numbers(str(text or ""))
    if numbers and max(numbers) <= 300:
        return "hourly"
    if numbers and max(numbers) >= 1000:
        return "annual"
    return "unknown"


def detect_currency(text: str) -> str:
    value = str(text or "").upper()
    if "CAD" in value or "CA$" in value:
        return "CAD"
    if "GBP" in value or "£" in value:
        return "GBP"
    if "EUR" in value or "€" in value:
        return "EUR"
    return "USD"


def plausible_pay_match(
    source_text: str,
    match_end: int,
    pay_text: str,
    numbers: list[int],
    context_match: bool,
) -> bool:
    """Reject common benefit/experience numbers that merely occur near pay language."""

    explicit_currency = re.search(_CURRENCY_MARKER, pay_text, flags=re.IGNORECASE) is not None
    period = detect_pay_period(pay_text)
    maximum = max(numbers)
    suffix = source_text[match_end:match_end + 24]
    if not explicit_currency and re.match(r"\s*\(\s*k\s*\)", suffix, flags=re.IGNORECASE):
        return False
    if not explicit_currency and re.search(r"\b401\s*k\b", pay_text, flags=re.IGNORECASE):
        return False
    if not context_match:
        return True
    if period == "annual" and maximum < 10_000:
        return False
    if period == "monthly" and maximum < 500:
        return False
    if period == "weekly" and maximum < 100:
        return False
    if period == "hourly" and not 5 <= maximum <= 500:
        return False
    if not explicit_currency and period == "unknown" and maximum < 1_000:
        return False
    return True


def clean_pay_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" .;,")
