import logging

from . import gemini, groq_fallback

log = logging.getLogger(__name__)


def generate_listing(images: list[bytes], note: str, edit_instruction: str | None = None,
                      previous: dict | None = None, model_stats: dict | None = None) -> dict:
    try:
        return gemini.generate_listing(images, note, edit_instruction, previous, model_stats)
    except Exception as e:
        log.warning("Gemini failed (%s), falling back to Groq", e)
        try:
            return groq_fallback.generate_listing(images, note, edit_instruction, previous, model_stats)
        except Exception as fallback_error:
            log.error("Groq fallback also failed: %s", fallback_error)
            raise RuntimeError(
                f"Both Gemini and Groq failed. Gemini: {e}. Groq: {fallback_error}"
            ) from fallback_error
