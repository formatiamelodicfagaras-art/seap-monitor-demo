import json

import anthropic
from loguru import logger

from app.config import get_settings

settings = get_settings()

ANALYSIS_SYSTEM_PROMPT = """
Esti un expert in achizitii publice din Romania (SEAP/SICAP).
Analizeaza documentatia de licitatie si extrage structurat TOATE informatiile esentiale.

IMPORTANT: Extrage cu precizie fiecare sectiune din caietul de sarcini.

Raspunde DOAR cu JSON valid, fara markdown, fara code blocks.
Structura JSON ceruta:
{
  "summary": {
    "description": "descriere completa a ce se cere",
    "authority": "numele complet al autoritatii contractante si partenerilor",
    "estimated_value": "valoare estimata cu moneda",
    "deadline": "termen limita depunere oferta (data si ora exacta)",
    "submission_method": "unde si cum se trimite oferta (email, SEAP, etc.)",
    "offer_validity": "perioada de valabilitate a ofertelor (ex: 30 zile)"
  },
  "technical_requirements": [
    {"item": "denumire produs/serviciu", "specifications": "specificatii tehnice detaliate (dimensiuni, material, culori, etc.)", "quantity": "cantitate", "delivery_term": "termen livrare"}
  ],
  "delivery_conditions": {
    "delivery_term": "termenul maxim de livrare",
    "delivery_location": "locul de livrare",
    "transport_included": "daca transportul e inclus in pret",
    "other_conditions": "alte conditii de livrare"
  },
  "reception_conditions": {
    "method": "cum se face receptia (PV receptie, etc.)",
    "inspection": "ce se verifica la receptie",
    "remedy_term": "termen remediere defecte",
    "other": "alte conditii receptie"
  },
  "payment_conditions": {
    "method": "modalitate plata (OP, factura, etc.)",
    "term": "termen de plata (ex: 30 zile de la factura)",
    "documents_required": "documente necesare pentru plata",
    "split_details": "detalii repartizare plata daca exista mai multi beneficiari"
  },
  "qualification_requirements": [
    {"requirement": "cerinta", "details": "detalii"}
  ],
  "evaluation_criteria": {
    "type": "pret cel mai scazut / cel mai avantajos economic",
    "factors": [{"name": "...", "weight": "..."}],
    "scoring_formula": "formula de calcul punctaj"
  },
  "required_documents": [
    {"document": "denumire document", "details": "detalii, format, termene"}
  ],
  "match_score": {
    "score": 8,
    "reasoning": "motivare scurta - cat de potrivita e pentru o firma de produse promotionale, tiparituri, steaguri, autocolante, cadouri corporate"
  }
}
"""


async def analyze_notice(extracted_text: str, notice_title: str = "") -> dict:
    """Analyze notice text using Claude API and return structured analysis."""
    if not settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY not configured")

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Truncate text if too long (Claude context limit)
    max_chars = 150_000
    if len(extracted_text) > max_chars:
        extracted_text = extracted_text[:max_chars] + "\n\n[... text trunchiat din cauza dimensiunii ...]"

    user_message = f"Titlu licitație: {notice_title}\n\nDocumentație:\n{extracted_text}"

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=ANALYSIS_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        response_text = response.content[0].text.strip()

        # Parse JSON response
        # Try to extract JSON if wrapped in code blocks
        if "```" in response_text:
            import re
            json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", response_text)
            if json_match:
                response_text = json_match.group(1).strip()

        # Try to find JSON object in response
        if not response_text.startswith("{"):
            start = response_text.find("{")
            if start >= 0:
                response_text = response_text[start:]

        # Fix common JSON issues from LLM output
        try:
            analysis = json.loads(response_text)
        except json.JSONDecodeError:
            # Try fixing trailing commas and truncated JSON
            import re
            cleaned = re.sub(r',\s*([}\]])', r'\1', response_text)
            # If JSON is truncated, try to close it
            open_braces = cleaned.count('{') - cleaned.count('}')
            open_brackets = cleaned.count('[') - cleaned.count(']')
            cleaned += ']' * open_brackets + '}' * open_braces
            analysis = json.loads(cleaned)

        tokens_used = response.usage.input_tokens + response.usage.output_tokens

        return {
            "analysis": analysis,
            "model": "claude-sonnet-4-20250514",
            "tokens_used": tokens_used,
        }

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse AI response as JSON: {e}")
        raise ValueError(f"Răspunsul AI nu este JSON valid: {e}")
    except anthropic.APIError as e:
        logger.error(f"Claude API error: {e}")
        raise
