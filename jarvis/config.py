"""Configuración de Jarvis, leída desde el archivo .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")

# Qué IA usar. Para cambiar de IA, cambiá solo JARVIS_MODELO en el .env:
#   Gemini: gemini/gemini-flash-latest   (necesita GEMINI_API_KEY)
#   Claude: anthropic/claude-sonnet-5-5  (necesita ANTHROPIC_API_KEY)
MODELO = os.getenv("JARVIS_MODELO", "gemini/gemini-flash-latest")

NOMBRE = os.getenv("JARVIS_NOMBRE", "jarvis").lower()
IDIOMA_VOZ = os.getenv("JARVIS_IDIOMA", "es-AR")

# Motor de voz para hablar: "pyttsx3" (sin internet) o "edge" (voz más natural).
MOTOR_VOZ = os.getenv("JARVIS_MOTOR_VOZ", "pyttsx3")
VOZ_EDGE = os.getenv("JARVIS_VOZ_EDGE", "es-AR-TomasNeural")

# Segundos que Jarvis sigue escuchando sin que repitas su nombre.
SEGUNDOS_CONVERSACION = int(os.getenv("JARVIS_SEGUNDOS_CONVERSACION", "30"))

# Máximo de pasos (llamadas a herramientas) por pedido.
MAX_PASOS = int(os.getenv("JARVIS_MAX_PASOS", "8"))

ARCHIVO_CONTACTOS = RAIZ / "contactos.json"
ARCHIVO_LOG = RAIZ / "jarvis.log"


def clave_configurada() -> bool:
    """Revisa que exista la clave de API del proveedor elegido."""
    if MODELO.startswith("gemini/"):
        return bool(os.getenv("GEMINI_API_KEY"))
    if MODELO.startswith("anthropic/"):
        return bool(os.getenv("ANTHROPIC_API_KEY"))
    return True  # otros proveedores: LiteLLM avisará si falta algo
