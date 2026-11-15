"""Reglas de seguridad: qué comandos corren solos y cuáles necesitan tu confirmación."""
import re
from typing import Callable

# Comandos que solo leen información: se ejecutan sin preguntar.
# "time" y "date" no están: en Windows cambian la hora y la fecha, y sin argumentos se
# quedan esperando que escribas una. Para saber la fecha está la herramienta fecha_y_hora().
COMANDOS_SEGUROS = (
    "dir", "ls", "pwd", "cd", "echo", "whoami", "hostname",
    "git status", "git log", "git diff", "git branch", "git remote -v",
    "python --version", "python3 --version", "pip list", "pip show",
    "node --version", "npm --version", "ipconfig", "ifconfig", "ping",
    "type ", "cat ", "where ", "which ", "tasklist", "systeminfo",
    # Lectura de sistema, estado de servicios, puertos y consultas SQL:
    "systemctl status", "systemctl is-active", "systemctl is-enabled", "systemctl list-units",
    "service ", "journalctl", "ps", "top -b", "uptime", "free", "df", "du", "uname", "lsblk",
    "ss", "netstat", "lsof", "ufw status", "ip addr", "ip route", "ip a", "ip r",
    "sc query", "get-service", "get-process", "get-nettcpconnection",
    "psql", "select ", "show ", "explain ", "describe ",
    "grep ", "find ", "head ", "tail ", "wc ", "stat ", "file ",
)

# Comandos estrictamente destructivos que siempre se bloquean / requieren advertencia crítica.
PATRONES_DESTRUCTIVOS = [
    r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+|--recursive|--force)+",
    r"\brm\s+-rf\b",
    r"\bdrop\s+(table|database|schema|index|view|role|user)\b",
    r"\bdrop\b",
    r"\bmkfs\b",
    r"\bdd\b",
    r"\bformat\b",
    r"\bdiskpart\b",
    r"\btruncate\s+table\b",
]

# Patrones claramente peligrosos: se confirman con advertencia fuerte.
# Se evalúan siempre y le ganan a la lista de seguros.
PATRONES_PELIGROSOS = [
    r"\brm\b", r"\bdel\b", r"\berase\b", r"\brmdir\b", r"\brd\b",
    r"remove-item", r"\bformat\b", r"\bmkfs", r"\bdd\b", r"\bshutdown\b",
    r"\breboot\b", r"\brestart-computer\b", r"\bstop-computer\b",
    r"\bdrop\b", r"\btruncate\s+table\b",
    r"\bsudo\b", r"\bdoas\b", r"\bchmod\b", r"\bchown\b", r"reg\s+delete",
    r"git\s+push\s+.*(--force|-f\b)", r"git\s+reset\s+--hard", r"git\s+clean",
    r"\|\s*(sh|bash|powershell|iex)\b", r"invoke-expression", r"\bdiskpart\b",
    # Comandos de la lista de seguros usados para modificar algo.
    r"\bbranch\b.*(?<!\S)(-[a-z]*d|--delete)",  # borra ramas (va en minúscula: -d y -D)
    r"--output\b", r"(?<!\S)-o(\s*\S)",           # escriben la salida en un archivo
    r"/(release|renew)",                          # ipconfig: corta o renueva la red
    r"\bdate\s+(-s|--set)\b",                     # cambia la fecha del sistema
    # Archivos y tokens con secretos: su contenido terminaría en el proveedor de IA o en logs.
    r"\.ssh\b", r"\.env\b", r"credential", r"credencial", r"\.pem\b",
    r"\bid_(rsa|dsa|ecdsa|ed25519)",
    r"\bgithub_token\b", r"ghp_[a-z0-9_]*",
    # Variables de entorno (%CLAVE% en Windows, $CLAVE o ${CLAVE} en Linux): pueden tener
    # claves de API, como GEMINI_API_KEY o GITHUB_TOKEN, que terminarían en el proveedor de IA.
    r"%[a-z_][a-z0-9_]*%", r"\$\{?[a-z_]",
]

CREDENCIALES_PROTEGIDAS = ("GITHUB_TOKEN", "ghp_", "VM_SSH_PASSWORD")

# Patrones de rutas y nombres de archivos sensibles cuya lectura local está bloqueada.
PATRONES_ARCHIVOS_SENSIBLES = (
    r"(^|[\\/])\.env($|[.\\/])",
    r"\.env$",
    r"(^|[\\/])\.ssh([\\/]|$)",
    r"(^|[\\/])id_(rsa|dsa|ecdsa|ed25519)",
    r"\.(pem|key|pfx|p12)$",
    r"credential",
    r"credencial",
    r"(^|[\\/])\.(netrc|npmrc|pypirc)$",
)


def es_archivo_sensible(ruta: str) -> bool:
    """True si la ruta apunta a un archivo sensible (especialmente .env, claves privadas o credenciales)."""
    texto = str(ruta or "").strip().lower().replace("\\", "/")
    if not texto:
        return False
    nombre = texto.rsplit("/", 1)[-1]
    if nombre == ".env" or nombre.startswith(".env.") or nombre.endswith(".env"):
        return True
    return any(re.search(patron, texto) for patron in PATRONES_ARCHIVOS_SENSIBLES)


def ocultar_secretos(texto: str) -> str:
    """Enmascara tokens de GitHub (ghp_...), GITHUB_TOKEN o VM_SSH_PASSWORD para consola y logs."""
    limpio = re.sub(r"\bghp_[A-Za-z0-9_]+", "ghp_***", texto)
    limpio = re.sub(r"(?i)(github_token\s*[=:]\s*)(\S+)", r"\1***", limpio)
    limpio = re.sub(r"(?i)(vm_ssh_password\s*[=:]\s*)(\S+)", r"\1***", limpio)
    return limpio


def es_destructivo(comando: str) -> bool:
    """True si el comando contiene operaciones destructivas (rm -rf, DROP, mkfs, dd, format)."""
    c = (comando or "").lower()
    return any(re.search(p, c) for p in PATRONES_DESTRUCTIVOS)

# Caracteres que permiten encadenar otro comando: anulan la lista de seguros.
# Con shell=True un salto de línea separa comandos igual que ";".
ENCADENADORES = re.compile(r"[;&|`><\r\n]|\$\(")

# Comandos que ejecutan a otro: lo que importa es lo que viene después, así que
# nunca se dan por seguros por el prefijo.
ENVOLVENTES = {"time", "nohup", "xargs", "env", "sudo", "doas", "watch", "timeout"}


def es_seguro(comando: str) -> bool:
    """True si el comando solo lee, no encadena nada raro y no es peligroso."""
    c = comando.strip().lower()
    if es_peligroso(c) or ENCADENADORES.search(c):
        return False
    palabras = c.split()
    if not palabras or palabras[0] in ENVOLVENTES:
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
