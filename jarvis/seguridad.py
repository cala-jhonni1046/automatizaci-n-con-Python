"""Reglas de seguridad: qué comandos corren solos y cuáles necesitan tu confirmación."""
import re
from typing import Callable

# Comandos que solo leen información: se ejecutan sin preguntar.
COMANDOS_SEGUROS = (
    "dir", "ls", "pwd", "cd", "echo", "whoami", "date", "time", "hostname",
    "git status", "git log", "git diff", "git branch", "git remote -v",
    "python --version", "python3 --version", "pip list", "pip show",
    "node --version", "npm --version", "ipconfig", "ifconfig", "ping",
    "type ", "cat ", "where ", "which ", "tasklist", "systeminfo",
)

# Patrones claramente peligrosos: se confirman con advertencia fuerte.
PATRONES_PELIGROSOS = [
    r"\brm\b", r"\bdel\b", r"\berase\b", r"\brmdir\b", r"\brd\b",
    r"remove-item", r"\bformat\b", r"\bmkfs", r"\bdd\b", r"\bshutdown\b",
    r"\breboot\b", r"\brestart-computer\b", r"\bstop-computer\b",
    r"\bsudo\b", r"\bchmod\b", r"\bchown\b", r"reg\s+delete",
    r"git\s+push\s+.*(--force|-f\b)", r"git\s+reset\s+--hard", r"git\s+clean",
    r"\|\s*(sh|bash|powershell|iex)\b", r"invoke-expression", r"\bdiskpart\b",
]

# Caracteres que permiten encadenar otro comando: anulan la lista de seguros.
ENCADENADORES = re.compile(r"[;&|`><]|\$\(")


def es_seguro(comando: str) -> bool:
    """True si el comando solo lee y no encadena nada raro."""
    c = comando.strip().lower()
    if ENCADENADORES.search(c):
        return False
    for seguro in COMANDOS_SEGUROS:
        base = seguro.strip()
        if c == base or c.startswith(base + " "):
            return True
    return False


def es_peligroso(comando: str) -> bool:
    c = comando.lower()
    return any(re.search(p, c) for p in PATRONES_PELIGROSOS)


# La interfaz (texto o voz) reemplaza esta función al arrancar.
_confirmar: Callable[[str], bool] = lambda pregunta: False


def registrar_confirmador(funcion: Callable[[str], bool]) -> None:
    global _confirmar
    _confirmar = funcion


def confirmar(pregunta: str) -> bool:
    """Le pregunta al usuario. Si no hay interfaz, responde que no."""
    return _confirmar(pregunta)
