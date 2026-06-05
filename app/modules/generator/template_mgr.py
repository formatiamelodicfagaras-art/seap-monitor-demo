import json

import anthropic
from loguru import logger

from app.config import get_settings
from app.modules.analyzer.extractor import extract_text

settings = get_settings()

TEMPLATE_ANALYSIS_PROMPT = """
Analizează acest document model (template) dintr-o licitație publică anterioară.
Identifică toate câmpurile variabile care se schimbă de la o licitație la alta.

Returnează JSON cu structura:
{
  "document_type": "formular administrativ / propunere tehnică / propunere financiară / altul",
  "description": "Descriere scurtă a documentului",
  "variable_fields": [
    {
      "field_name": "Numele câmpului",
      "current_value": "Valoarea din document",
      "field_type": "company_data / authority_data / contract_data / technical_data / financial_data / date",
      "description": "Ce reprezintă acest câmp"
    }
  ],
  "fixed_sections": [
    "Lista secțiunilor care rămân neschimbate"
  ]
}

Răspunde DOAR cu JSON valid, fără markdown.
"""


async def analyze_template(file_path: str, file_type: str) -> dict | None:
    """Use AI to analyze a template document and identify variable fields."""
    if not settings.anthropic_api_key:
        logger.warning("ANTHROPIC_API_KEY not set, skipping template analysis")
        return None

    text = extract_text(file_path, file_type)
    if not text or text.startswith("["):
        return None

    # Truncate if needed
    if len(text) > 100_000:
        text = text[:100_000] + "\n[... trunchiat ...]"

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=TEMPLATE_ANALYSIS_PROMPT,
            messages=[{"role": "user", "content": text}],
        )

        response_text = response.content[0].text.strip()
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            response_text = "\n".join(lines[1:-1])

        return json.loads(response_text)

    except Exception as e:
        logger.error(f"Template analysis failed: {e}")
        return None
