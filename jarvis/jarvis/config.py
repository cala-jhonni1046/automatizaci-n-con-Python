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

# Token opcional de GitHub para las herramientas de análisis de repositorios (PyGithub).
GITHUB_TOKEN: str | None = os.getenv("GITHUB_TOKEN") or None

# Configuración SSH de la máquina virtual (por defecto GNU Health en red local).
VM_SSH_HOST: str = os.getenv("VM_SSH_HOST", "192.168.100.24")
VM_SSH_PORT: int = int(os.getenv("VM_SSH_PORT", "22"))
VM_SSH_USER: str = os.getenv("VM_SSH_USER", "gnuhealth")
VM_SSH_PASSWORD: str | None = os.getenv("VM_SSH_PASSWORD") or None

# Gemini web (gemini.google.com) en Microsoft Edge vía Playwright + CDP como cerebro principal.
# Con JARVIS_GEMINI_WEB=false se usa solo la API (JARVIS_MODELO) como antes.
GEMINI_WEB_ACTIVO: bool = os.getenv("JARVIS_GEMINI_WEB", "true").strip().lower() in ("1", "true", "si", "sí", "yes")
GEMINI_WEB_URL: str = os.getenv("JARVIS_GEMINI_WEB_URL", "https://gemini.google.com/app")
GEMINI_WEB_CDP: str = os.getenv("JARVIS_EDGE_CDP", "http://127.0.0.1:9222")
EDGE_PERFIL_JARVIS: str = os.getenv("JARVIS_EDGE_PERFIL", r"C:\EdgeJarvisProfile")

ARCHIVO_CONTACTOS = RAIZ / "contactos.json"
ARCHIVO_LOG = RAIZ / "jarvis.log"


def clave_configurada() -> bool:
    """Revisa que exista la clave de API del proveedor elegido."""
    if MODELO.startswith("gemini/"):
        return bool(os.getenv("GEMINI_API_KEY"))
    if MODELO.startswith("anthropic/"):
        return bool(os.getenv("ANTHROPIC_API_KEY"))
    return True  # otros proveedores: LiteLLM avisará si falta algo
