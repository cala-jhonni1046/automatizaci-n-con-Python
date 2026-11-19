"""Cliente de OpenRouter (https://openrouter.ai/api/v1) como especialista adicional del Enjambre.

Lee OPENROUTER_API_KEY y OPENROUTER_MODEL (por defecto 'openrouter/free') desde config / .env.
Maneja timeouts (25.0s) y errores HTTP 401, 402 y 429 devolviendo un mensaje controlado sin lanzar
excepciones para no frenar nunca al resto del enjambre.
"""
import logging
import os

from . import config, seguridad

try:
    import openai
except ImportError:  # pragma: no cover
    openai = None  # type: ignore[assignment]

log = logging.getLogger("jarvis")

BASE_URL_OPENROUTER = "https://openrouter.ai/api/v1"
TIMEOUT_OPENROUTER = 25.0
PREFIJO_ERROR_OPENROUTER = "Error OpenRouter"

INSTRUCCION_OPENROUTER = (
    "Sos un Ingeniero Principal del Enjambre Multi-Cerebro de Jarvis. "
    "Usá tu máximo razonamiento interno, pero redactá como salida ÚNICAMENTE un resumen técnico "
    "exacto y denso (máximo 250 palabras, en puntos clave y código esencial, sin saludos ni relleno) "
    "con la solución verificada."
)


def obtener_api_key() -> str:
    """Devuelve la clave OPENROUTER_API_KEY activa desde config o variables de entorno."""
    clave = config.OPENROUTER_API_KEY or os.getenv("OPENROUTER_API_KEY") or ""
    return clave.strip()


def obtener_modelo() -> str:
    """Devuelve el modelo de OpenRouter configurado (por defecto 'openrouter/free')."""
    modelo = config.OPENROUTER_MODEL or os.getenv("OPENROUTER_MODEL") or "openrouter/free"
    return modelo.strip() or "openrouter/free"


def openrouter_configurado() -> bool:
    """True si existe una OPENROUTER_API_KEY configurada."""
    return bool(obtener_api_key())


def es_error_openrouter(texto: str) -> bool:
    """True si la respuesta está vacía o es un error controlado de OpenRouter."""
    limpio = (texto or "").strip()
    return not limpio or limpio.startswith(PREFIJO_ERROR_OPENROUTER)


def _crear_cliente(api_key: str, timeout: float = TIMEOUT_OPENROUTER):
    if openai is None:
        raise RuntimeError("La librería 'openai' no está instalada.")
    return openai.OpenAI(
        base_url=BASE_URL_OPENROUTER,
        api_key=api_key,
        timeout=timeout,
    )


def _clasificar_error_openrouter(error: BaseException, timeout: float) -> str:
    codigo = getattr(error, "status_code", None)
    texto = str(error)
    nombre = type(error).__name__

    if isinstance(error, TimeoutError) or "timeout" in nombre.lower() or "timed out" in texto.lower():
        return f"{PREFIJO_ERROR_OPENROUTER} (timeout): la consulta superó los {timeout:.1f}s."
    if codigo == 401 or "401" in texto:
        return f"{PREFIJO_ERROR_OPENROUTER} (401): OPENROUTER_API_KEY inválida o no autorizada."
    if codigo == 402 or "402" in texto:
        return f"{PREFIJO_ERROR_OPENROUTER} (402): créditos o saldo insuficiente en OpenRouter."
    if codigo == 429 or "429" in texto:
        return f"{PREFIJO_ERROR_OPENROUTER} (429): límite de cuota o rate limit alcanzado en OpenRouter."
    return f"{PREFIJO_ERROR_OPENROUTER} ({nombre}): {seguridad.ocultar_secretos(texto)}"


def consultar_openrouter(
    problema: str,
    modelo: str | None = None,
    timeout: float = TIMEOUT_OPENROUTER,
) -> str:
    """Consulta a OpenRouter y devuelve el resumen técnico o un error controlado (nunca lanza excepción)."""
    api_key = obtener_api_key()
    if not api_key:
        return f"{PREFIJO_ERROR_OPENROUTER} (sin clave): OPENROUTER_API_KEY no está configurada."

    problema_limpio = (problema or "").strip()
    if not problema_limpio:
        return f"{PREFIJO_ERROR_OPENROUTER} (vacío): no se indicó consulta."

    modelo_elegido = (modelo or "").strip() or obtener_modelo()
    try:
        cliente = _crear_cliente(api_key=api_key, timeout=timeout)
        respuesta = cliente.chat.completions.create(
            model=modelo_elegido,
            messages=[
                {"role": "system", "content": INSTRUCCION_OPENROUTER},
                {"role": "user", "content": problema_limpio},
            ],
            timeout=timeout,
        )
        contenido = ""
        if getattr(respuesta, "choices", None):
            mensaje = getattr(respuesta.choices[0], "message", None)
            contenido = (getattr(mensaje, "content", "") or "").strip()
        if not contenido:
            return f"{PREFIJO_ERROR_OPENROUTER} (sin contenido): el modelo {modelo_elegido} devolvió vacío."
        return seguridad.ocultar_secretos(contenido)
    except Exception as e:
        controlado = _clasificar_error_openrouter(e, timeout=timeout)
        log.warning("OpenRouter omitido en esta consulta: %s", controlado)
        return controlado

