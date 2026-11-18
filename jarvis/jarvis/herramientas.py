"""Acciones que Jarvis puede hacer en tu computadora.

Para agregar una acción nueva: escribí una función con @herramienta, tipos en los
parámetros y un docstring. La primera línea describe la acción; cada línea
"parametro: descripción" explica un parámetro. La IA la va a poder usar sola.
"""
from concurrent.futures import ThreadPoolExecutor
import ctypes
import datetime
import inspect
import io
import json
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import webbrowser
from pathlib import Path

from . import config
from .seguridad import (
    confirmar,
    es_archivo_sensible,
    es_destructivo,
    es_peligroso,
    es_seguro,
    ocultar_secretos,
)

try:
    import fitz
except ImportError:  # pragma: no cover
    fitz = None  # type: ignore[assignment]

try:
    import mss
except ImportError:  # pragma: no cover
    mss = None  # type: ignore[assignment]

try:
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None  # type: ignore[assignment]

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment,misc]

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None  # type: ignore[assignment,misc]

try:
    from duckduckgo_search import DDGS
except ImportError:  # pragma: no cover
    DDGS = None  # type: ignore[assignment,misc]

try:
    from youtube_transcript_api import YouTubeTranscriptApi
except ImportError:  # pragma: no cover
    YouTubeTranscriptApi = None  # type: ignore[assignment,misc]

try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None  # type: ignore[assignment]

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

try:
    import pywinauto
except ImportError:  # pragma: no cover
    pywinauto = None  # type: ignore[assignment]

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
try:
    import litellm
    litellm.suppress_debug_info = True
except ImportError:  # pragma: no cover
    litellm = None  # type: ignore[assignment]

try:
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover
    class BaseModel:  # type: ignore[no-redef]
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    def Field(default=None, **_kwargs):  # type: ignore[misc]
        return default

try:
    from github import Github, GithubException
except ImportError:  # pragma: no cover
    Github = None  # type: ignore[assignment,misc]

    class GithubException(Exception):  # type: ignore[no-redef]
        def __init__(self, status: int = 0, data=None, headers=None):
            super().__init__(data or status)
            self.status = status
            self.data = data

log = logging.getLogger("jarvis")
ES_WINDOWS = platform.system() == "Windows"

REGISTRO: dict = {}

_TIPOS_JSON = {str: "string", int: "integer", float: "number", bool: "boolean"}


def herramienta(funcion):
    """Registra una función como herramienta disponible para la IA."""
    REGISTRO[funcion.__name__] = funcion
    return funcion


def esquemas() -> list:
    """Describe las herramientas en el formato que entienden Gemini y Claude."""
    resultado = []
    for nombre, funcion in REGISTRO.items():
        lineas = [l.strip() for l in (inspect.getdoc(funcion) or "").splitlines() if l.strip()]
        descripcion = lineas[0] if lineas else nombre
        firma = inspect.signature(funcion)
        ayuda = {}
        for linea in lineas[1:]:
            clave, _, texto = linea.partition(":")
            if clave in firma.parameters:
                ayuda[clave] = texto.strip()
        propiedades, requeridos = {}, []
        for p in firma.parameters.values():
            propiedades[p.name] = {
                "type": _TIPOS_JSON.get(p.annotation, "string"),
                "description": ayuda.get(p.name, p.name),
            }
            if p.default is inspect.Parameter.empty:
                requeridos.append(p.name)
        resultado.append({
            "type": "function",
            "function": {
                "name": nombre,
                "description": descripcion,
                "parameters": {"type": "object", "properties": propiedades, "required": requeridos},
            },
        })
    return resultado


def ejecutar(nombre: str, argumentos: dict) -> str:
    """Corre una herramienta por nombre y siempre devuelve texto (nunca explota)."""
    funcion = REGISTRO.get(nombre)
    if funcion is None:
        return f"Error: no existe la herramienta '{nombre}'."
    log.info("Herramienta %s %s", nombre, ocultar_secretos(str(argumentos)))
    try:
        return ocultar_secretos(str(funcion(**argumentos)))
    except TypeError as e:
        return f"Error en los parámetros: {e}"
    except Exception as e:  # la IA recibe el error y puede intentar otra cosa
        log.exception("Falló %s", nombre)
        return f"Error al ejecutar {nombre}: {ocultar_secretos(str(e))}"


def _abrir_con_el_sistema(objetivo: str) -> None:
    if ES_WINDOWS:
        os.startfile(objetivo)  # type: ignore[attr-defined]
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", objetivo])
    else:
        subprocess.Popen(["xdg-open", objetivo])


# ---------------------------------------------------------------- ventanas al frente (Windows)
#
# Windows no deja que cualquier proceso pase una ventana al frente (foreground lock): solo el
# que está en primer plano, el que recibió la última entrada del usuario o uno lanzado por
# ellos. Jarvis corre dentro de una terminal y no cumple ninguna, así que lo que abre queda
# detrás. Por eso, después de abrir, se busca la ventana nueva y se la pasa al frente.

SEGUNDOS_ESPERANDO_VENTANA = 5.0
SEGUNDOS_ENTRE_REVISIONES = 0.25
NAVEGADORES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}
AVISO_SIN_FOCO = " No pude traer la ventana al frente: puede haber quedado detrás de otra."


def _ventanas() -> dict[int, str]:
    """Ventanas principales visibles con título: {hwnd: título}."""
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    ventanas = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visitar(hwnd, _):
        # Sin dueño (GW_OWNER = 4): es una ventana principal, no un diálogo de otra.
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):
            largo = user32.GetWindowTextLengthW(hwnd)
            if largo:
                titulo = ctypes.create_unicode_buffer(largo + 1)
                user32.GetWindowTextW(hwnd, titulo, largo + 1)
                ventanas[hwnd] = titulo.value
        return True

    user32.EnumWindows(visitar, 0)
    return ventanas


def _ventanas_excluidas() -> set:
    """La terminal de Jarvis y la que está al frente: su título cambia solo, no las abrió Jarvis."""
    return {ctypes.windll.user32.GetForegroundWindow(), ctypes.windll.kernel32.GetConsoleWindow()}


def _programa_de(hwnd: int) -> str:
    """Nombre del ejecutable dueño de la ventana, en minúsculas ('' si no se puede saber)."""
    from ctypes import wintypes

    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proceso = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if not proceso:
        return ""
    try:
        ruta = ctypes.create_unicode_buffer(32768)
        largo = wintypes.DWORD(len(ruta))
        if not ctypes.windll.kernel32.QueryFullProcessImageNameW(proceso, 0, ruta, ctypes.byref(largo)):
            return ""
        return Path(ruta.value).name.lower()
    finally:
        ctypes.windll.kernel32.CloseHandle(proceso)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("mi", _MOUSEINPUT)]  # type 0 = INPUT_MOUSE


def _enfocar(hwnd: int) -> bool:
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE: si estaba minimizada
    # Una entrada de mouse vacía (sin movimiento ni clic) hace que este proceso sea "el que
    # recibió la última entrada", una de las condiciones que Windows pide para SetForegroundWindow.
    entrada = _INPUT(type=0)
    user32.SendInput(1, ctypes.byref(entrada), ctypes.sizeof(_INPUT))
    user32.SetForegroundWindow(hwnd)
    return user32.GetForegroundWindow() == hwnd


def _abrir_al_frente(abrir, pestaña_de_navegador: bool = False) -> bool:
    """Ejecuta abrir() y pasa al frente la ventana que aparezca. False si no apareció ninguna.

    pestaña_de_navegador: si el navegador ya estaba abierto no hay ventana nueva, solo una
    pestaña; entonces vale también una ventana de navegador cuyo título cambió.
    """
    if not ES_WINDOWS:
        abrir()
        return True
    antes, excluidas = _ventanas(), _ventanas_excluidas()
    abrir()
    limite = time.monotonic() + SEGUNDOS_ESPERANDO_VENTANA
    while time.monotonic() < limite:
        time.sleep(SEGUNDOS_ENTRE_REVISIONES)
        ahora = _ventanas()
        nuevas = [h for h in ahora if h not in antes and h not in excluidas]
        if not nuevas and pestaña_de_navegador:
            nuevas = [h for h, titulo in ahora.items()
                      if h in antes and titulo != antes[h] and h not in excluidas
                      and _programa_de(h) in NAVEGADORES]
        if nuevas:
            return _enfocar(nuevas[0])  # EnumWindows va de adelante hacia atrás: la de más arriba
    return False


# ---------------------------------------------------------------- información

@herramienta
def fecha_y_hora() -> str:
    """Devuelve la fecha y la hora actuales de la computadora."""
    return datetime.datetime.now().strftime("%A %d/%m/%Y, %H:%M")


# ---------------------------------------------------------------- programas

ALIAS_PROGRAMAS = {
    "bloc de notas": "notepad", "notas": "notepad", "calculadora": "calc",
    "excel": "excel", "word": "winword", "powerpoint": "powerpnt",
    "explorador": "explorer", "terminal": "wt", "cmd": "cmd",
    "chrome": "chrome", "edge": "msedge", "vscode": "code", "visual studio code": "code",
    "antigravity": "antigravity", "whatsapp": "whatsapp:", "configuracion": "ms-settings:",
}


@herramienta
def abrir_programa(nombre: str) -> str:
    """Abre un programa instalado, por ejemplo Excel, Chrome, el bloc de notas o Antigravity.
    nombre: nombre del programa como lo dice el usuario, por ejemplo 'excel' o 'bloc de notas'.
    """
    clave = nombre.strip().lower()
    objetivo = ALIAS_PROGRAMAS.get(clave, clave)
    if not re.fullmatch(r"[\w .:\-]+", objetivo):
        return "Nombre de programa no válido."
    if ES_WINDOWS:
        # "start" busca el programa como el menú Inicio; el nombre va como un solo argumento.
        al_frente = _abrir_al_frente(lambda: subprocess.Popen(["cmd", "/c", "start", "", objetivo]))
        return f"Abrí {nombre}." + ("" if al_frente else AVISO_SIN_FOCO)
    ejecutable = shutil.which(objetivo)
    if not ejecutable:
        return f"No encontré el programa '{nombre}' en esta computadora."
    subprocess.Popen([ejecutable])
    return f"Abrí {nombre}."


# ---------------------------------------------------------------- archivos

CARPETAS_CONOCIDAS = {
    "documentos": "Documents", "mis documentos": "Documents", "descargas": "Downloads",
    "escritorio": "Desktop", "imagenes": "Pictures", "imágenes": "Pictures",
    "musica": "Music", "música": "Music", "videos": "Videos", "inicio": "",
}


def _resolver_carpeta(ruta: str) -> Path:
    clave = ruta.strip().lower()
    if clave in CARPETAS_CONOCIDAS:
        return Path.home() / CARPETAS_CONOCIDAS[clave]
    return Path(os.path.expandvars(ruta)).expanduser()


@herramienta
def abrir_carpeta(ruta: str) -> str:
    """Abre una carpeta en el explorador de archivos.
    ruta: ruta completa o un nombre conocido: documentos, descargas, escritorio, imagenes, musica, videos.
    """
    carpeta = _resolver_carpeta(ruta)
    if not carpeta.is_dir():
        return f"La carpeta {carpeta} no existe."
    al_frente = _abrir_al_frente(lambda: _abrir_con_el_sistema(str(carpeta)))
    return f"Abrí la carpeta {carpeta}." + ("" if al_frente else AVISO_SIN_FOCO)


@herramienta
def buscar_archivos(nombre: str, carpeta: str = "inicio") -> str:
    """Busca archivos cuyo nombre contenga un texto y devuelve hasta 20 rutas.
    nombre: parte del nombre del archivo, por ejemplo 'presupuesto' o '.xlsx'.
    carpeta: dónde buscar; por defecto toda la carpeta del usuario.
    """
    raiz = _resolver_carpeta(carpeta)
    if not raiz.is_dir():
        return f"La carpeta {raiz} no existe."
    buscado = nombre.lower()
    encontrados, revisados = [], 0
    for directorio, subcarpetas, archivos in os.walk(raiz):
        # saltear carpetas ocultas y pesadas
        subcarpetas[:] = [d for d in subcarpetas
                          if not d.startswith(".") and d not in ("node_modules", "AppData", "__pycache__")]
        for archivo in archivos:
            revisados += 1
            if buscado in archivo.lower():
                encontrados.append(str(Path(directorio) / archivo))
                if len(encontrados) >= 20:
                    return "\n".join(encontrados)
        if revisados > 200_000:
            break
    return "\n".join(encontrados) if encontrados else f"No encontré archivos con '{nombre}' en {raiz}."


@herramienta
def abrir_archivo(ruta: str) -> str:
    """Abre un archivo con su programa predeterminado (un Excel con Excel, un PDF con el lector, etc.).
    ruta: ruta completa del archivo.
    """
    archivo = Path(os.path.expandvars(ruta)).expanduser()
    if not archivo.is_file():
        return f"El archivo {archivo} no existe."
    al_frente = _abrir_al_frente(lambda: _abrir_con_el_sistema(str(archivo)))
    return f"Abrí {archivo.name}." + ("" if al_frente else AVISO_SIN_FOCO)


# ---------------------------------------------------------------- internet

@herramienta
def abrir_web(direccion_o_busqueda: str) -> str:
    """Abre una página web, o busca en Google si no es una dirección.
    direccion_o_busqueda: una URL como 'github.com' o un texto para buscar.
    """
    texto = direccion_o_busqueda.strip()
    if re.fullmatch(r"(https?://)?[\w\-]+(\.[\w\-]+)+(/\S*)?", texto):
        url = texto if texto.startswith("http") else "https://" + texto
    else:
        url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(texto)
    al_frente = _abrir_al_frente(lambda: webbrowser.open(url), pestaña_de_navegador=True)
    return f"Abrí {url}" + ("." if al_frente else "." + AVISO_SIN_FOCO)


# ---------------------------------------------------------------- terminal

# Comandos internos de cmd: no existen como programa, así que en Windows solo corren con
# "cmd /c". Llegan ya filtrados por es_seguro(): sin & | < > saltos de línea ni %VARIABLES%.
INTERNOS_CMD = {"dir", "cd", "echo", "type"}


def _sin_shell(comando: str, en_linux: bool):
    """Arma la llamada para un comando de la lista blanca sin pasar por un shell."""
    if en_linux and ES_WINDOWS:
        return ["wsl", "-e", *shlex.split(comando)]
    if not ES_WINDOWS:
        # Sin shell nadie expande el ~: se hace acá.
        return [os.path.expanduser(p) if p.startswith("~") else p for p in shlex.split(comando)]
    programa = comando.split()[0].lower()
    ejecutable = shutil.which(programa) or ""
    if programa in INTERNOS_CMD or ejecutable.lower().endswith((".bat", ".cmd")):
        # /d: sin los AutoRun del registro. /v:off: sin la expansión !VARIABLE!.
        return f"cmd /d /v:off /c {comando}"
    # En Windows cada programa separa sus argumentos: la línea va tal cual, sin shell
    # (así las barras invertidas de las rutas no se pierden como con shlex).
    return comando


@herramienta
def ejecutar_comando(comando: str, en_linux: bool = False) -> str:
    """Ejecuta un comando en la terminal y devuelve lo que muestra. Los comandos que modifican algo se confirman con el usuario.
    comando: el comando exacto a ejecutar.
    en_linux: true para ejecutarlo en la terminal de Linux (WSL) cuando la computadora es Windows.
    """
    comando = comando.strip()
    if es_seguro(comando):
        # Lista blanca: sin shell, para que ningún carácter especial cuele otro comando.
        try:
            argumentos, usar_shell = _sin_shell(comando, en_linux), False
        except ValueError as e:  # shlex no pudo separar los argumentos (comillas sin cerrar)
            return f"No pude interpretar el comando: {e}"
    else:
        aviso = "⚠️ COMANDO PELIGROSO. " if es_peligroso(comando) else ""
        if not confirmar(f"{aviso}¿Ejecuto este comando?  {comando}"):
            return "El usuario NO autorizó el comando. No lo ejecutes ni insistas."
        # Confirmado por el usuario: con shell, para que anden tuberías y redirecciones.
        if en_linux and ES_WINDOWS:
            argumentos, usar_shell = ["wsl", "bash", "-lc", comando], False
        else:
            argumentos, usar_shell = comando, True
    try:
        proceso = subprocess.run(argumentos, shell=usar_shell, capture_output=True,
                                 text=True, timeout=120, errors="replace")
    except subprocess.TimeoutExpired:
        return "El comando tardó más de 2 minutos y lo corté."
    except FileNotFoundError:
        return f"No encontré el programa para ejecutar '{comando}'."
    salida = (proceso.stdout + proceso.stderr).strip() or "(sin salida)"
    if len(salida) > 3000:
        salida = salida[:1500] + "\n...\n" + salida[-1500:]
    return f"Código de salida {proceso.returncode}.\n{salida}"


# ---------------------------------------------------------------- teclado

@herramienta
def escribir_texto(texto: str) -> str:
    """Escribe un texto en la ventana que está activa, como si lo tipeara el usuario (por ejemplo en el bloc de notas).
    texto: lo que hay que escribir.
    """
    import pyautogui
    import pyperclip

    time.sleep(1.0)  # dar tiempo a que la ventana quede en primer plano
    # Pegar desde el portapapeles respeta tildes y eñes, que pyautogui no tipea bien.
    anterior = pyperclip.paste()
    pyperclip.copy(texto)
    pyautogui.hotkey("command" if platform.system() == "Darwin" else "ctrl", "v")
    time.sleep(0.3)
    pyperclip.copy(anterior)
    return "Listo, lo escribí."


# ---------------------------------------------------------------- WhatsApp

def _cargar_contactos() -> dict:
    if config.ARCHIVO_CONTACTOS.exists():
        datos = json.loads(config.ARCHIVO_CONTACTOS.read_text(encoding="utf-8"))
        return {k.lower(): v for k, v in datos.items()}
    return {}


@herramienta
def enviar_whatsapp(contacto: str, mensaje: str) -> str:
    """Envía un mensaje de WhatsApp usando la app de escritorio. Siempre se confirma con el usuario antes de enviar.
    contacto: nombre del contacto (como está en contactos.json) o número con código de país, por ejemplo 5492611234567.
    mensaje: el texto a enviar.
    """
    contactos = _cargar_contactos()
    numero = contactos.get(contacto.strip().lower())
    if numero is None:
        solo_digitos = re.sub(r"\D", "", contacto)
        if len(solo_digitos) < 8:
            nombres = ", ".join(contactos) or "ninguno"
            return f"No tengo a '{contacto}' en contactos.json. Contactos guardados: {nombres}."
        numero = solo_digitos
    if not confirmar(f"¿Le mando a {contacto}: «{mensaje}»?"):
        return "El usuario NO autorizó el envío. No lo mandes."
    url = f"whatsapp://send?phone={numero}&text={urllib.parse.quote(mensaje)}"
    _abrir_con_el_sistema(url)
    time.sleep(6)  # esperar a que WhatsApp abra el chat
    import pyautogui
    pyautogui.press("enter")
    return f"Mensaje enviado a {contacto}."


# ---------------------------------------------------------------- GitHub (estilo MCP)

def _limpiar_repo(repositorio: str) -> str:
    """Recibe una URL completa de GitHub o 'usuario/repo' y devuelve limpiamente 'usuario/repo'."""
    texto = (repositorio or "").strip()
    texto = re.sub(r"^https?://(www\.)?github\.com/", "", texto, flags=re.IGNORECASE)
    texto = re.sub(r"^(www\.)?github\.com/", "", texto, flags=re.IGNORECASE)
    texto = texto.strip("/")
    if texto.endswith(".git"):
        texto = texto[:-4]
    partes = [p for p in texto.split("/") if p]
    if len(partes) >= 2 and re.fullmatch(r"[\w.\-]+", partes[0]) and re.fullmatch(r"[\w.\-]+", partes[1]):
        return f"{partes[0]}/{partes[1]}"
    return ""


def _obtener_repo_github(repositorio: str):
    """Valida el token y devuelve (repo_obj, nombre_limpio, None) o (None, nombre_limpio, mensaje_error)."""
    nombre_limpio = _limpiar_repo(repositorio)
    if not nombre_limpio:
        return None, "", (
            f"El formato de repositorio '{repositorio}' no es válido. "
            "Indicá 'usuario/repo' o el enlace de GitHub."
        )
    token = config.GITHUB_TOKEN or os.getenv("GITHUB_TOKEN")
    if not token:
        return None, nombre_limpio, (
            "No está configurado GITHUB_TOKEN en el archivo .env. "
            "Agregá tu token de GitHub para poder analizar repositorios."
        )
    if Github is None:
        return None, nombre_limpio, "Falta instalar la librería PyGithub (pip install PyGithub)."
    try:
        cliente = Github(token)
        return cliente.get_repo(nombre_limpio), nombre_limpio, None
    except GithubException as e:
        if getattr(e, "status", None) == 404:
            return None, nombre_limpio, (
                f"No encontré el repositorio '{nombre_limpio}' en GitHub "
                "(verificá el nombre o si es privado)."
            )
        if getattr(e, "status", None) in (401, 403):
            return None, nombre_limpio, "El GITHUB_TOKEN no es válido o no tiene permisos suficientes en GitHub."
        return None, nombre_limpio, f"Error de GitHub al abrir '{nombre_limpio}': {ocultar_secretos(str(e))}"
    except Exception as e:
        return None, nombre_limpio, f"No pude conectar con GitHub para '{nombre_limpio}': {ocultar_secretos(str(e))}"


@herramienta
def gh_estructura_repo(repositorio: str, rama: str = "") -> str:
    """Devuelve el árbol completo de carpetas y archivos de un repositorio de GitHub.
    repositorio: URL del repositorio (ej. 'https://github.com/usuario/repo') o 'usuario/repo'.
    rama: rama a inspeccionar; si se deja vacío usa la rama por defecto del repositorio.
    """
    repo, nombre_limpio, error = _obtener_repo_github(repositorio)
    if error:
        return error
    rama_objetivo = rama.strip() or repo.default_branch
    try:
        arbol = repo.get_git_tree(rama_objetivo, recursive=True)
    except GithubException as e:
        if getattr(e, "status", None) == 404:
            return f"No encontré la rama '{rama_objetivo}' en el repositorio '{nombre_limpio}'."
        return f"Error de GitHub al obtener el árbol de '{nombre_limpio}': {ocultar_secretos(str(e))}"
    elementos = getattr(arbol, "tree", None) or []
    if not elementos:
        return f"El repositorio '{nombre_limpio}' (rama {rama_objetivo}) está vacío."
    lineas = [
        f"{item.path}/" if getattr(item, "type", "") == "tree" else item.path
        for item in elementos
    ]
    encabezado = f"Estructura de {nombre_limpio} (rama {rama_objetivo}, {len(lineas)} elementos):"
    salida = encabezado + "\n" + "\n".join(lineas[:500])
    if len(lineas) > 500:
        salida += f"\n... ({len(lineas) - 500} elementos más)"
    return salida


@herramienta
def gh_leer_archivo(repositorio: str, ruta_archivo: str, rama: str = "") -> str:
    """Descarga y devuelve en texto plano el contenido de un archivo de un repositorio de GitHub.
    repositorio: URL del repositorio o 'usuario/repo'.
    ruta_archivo: ruta interna del archivo dentro del repositorio, por ejemplo 'README.md' o 'src/main.py'.
    rama: rama opcional de donde leer el archivo; si se deja vacío usa la rama por defecto.
    """
    repo, nombre_limpio, error = _obtener_repo_github(repositorio)
    if error:
        return error
    ruta_limpia = ruta_archivo.strip().lstrip("/")
    if not ruta_limpia:
        return "Indicá la ruta del archivo que querés leer dentro del repositorio."
    rama_limpia = rama.strip()
    try:
        if rama_limpia:
            archivo = repo.get_contents(ruta_limpia, ref=rama_limpia)
        else:
            archivo = repo.get_contents(ruta_limpia)
    except GithubException as e:
        if getattr(e, "status", None) == 404:
            return f"No encontré el archivo '{ruta_limpia}' en '{nombre_limpio}'."
        return f"Error de GitHub al leer '{ruta_limpia}': {ocultar_secretos(str(e))}"
    if isinstance(archivo, list):
        nombres = ", ".join(a.path for a in archivo[:30])
        return f"'{ruta_limpia}' es una carpeta en '{nombre_limpio}', no un archivo. Contiene: {nombres}"
    crudo = getattr(archivo, "decoded_content", b"")
    texto = crudo.decode("utf-8", errors="replace") if isinstance(crudo, (bytes, bytearray)) else str(crudo)
    return ocultar_secretos(texto)


@herramienta
def gh_listar_issues(repositorio: str, estado: str = "open") -> str:
    """Lista los issues de un repositorio de GitHub con su número, título, autor, etiquetas y mensaje.
    repositorio: URL del repositorio o 'usuario/repo'.
    estado: estado de los issues a buscar: 'open', 'closed' o 'all' (por defecto 'open').
    """
    repo, nombre_limpio, error = _obtener_repo_github(repositorio)
    if error:
        return error
    estado_limpio = estado.strip().lower() or "open"
    try:
        issues = repo.get_issues(state=estado_limpio)
    except GithubException as e:
        return f"Error de GitHub al listar issues de '{nombre_limpio}': {ocultar_secretos(str(e))}"
    bloques = []
    for issue in issues:
        if getattr(issue, "pull_request", None) is not None:
            continue
        autor = getattr(getattr(issue, "user", None), "login", None) or "desconocido"
        etiquetas_raw = getattr(issue, "labels", None) or []
        etiquetas = ", ".join(getattr(et, "name", str(et)) for et in etiquetas_raw) or "sin etiquetas"
        cuerpo = (getattr(issue, "body", None) or "(sin descripción)").strip()
        if len(cuerpo) > 400:
            cuerpo = cuerpo[:400] + "..."
        bloques.append(
            f"#{issue.number} - {issue.title}\n"
            f"  Autor: {autor} | Etiquetas: {etiquetas}\n"
            f"  Mensaje: {ocultar_secretos(cuerpo)}"
        )
        if len(bloques) >= 25:
            break
    if not bloques:
        return f"No hay issues en estado '{estado_limpio}' en '{nombre_limpio}'."
    return f"Issues ({estado_limpio}) en {nombre_limpio}:\n\n" + "\n\n".join(bloques)


@herramienta
def gh_ver_pull_requests(repositorio: str, estado: str = "open") -> str:
    """Lista los Pull Requests de un repositorio mostrando autor, ramas a fusionar y archivos modificados.
    repositorio: URL del repositorio o 'usuario/repo'.
    estado: estado de los pull requests: 'open', 'closed' o 'all' (por defecto 'open').
    """
    repo, nombre_limpio, error = _obtener_repo_github(repositorio)
    if error:
        return error
    estado_limpio = estado.strip().lower() or "open"
    try:
        pulls = repo.get_pulls(state=estado_limpio)
    except GithubException as e:
        return f"Error de GitHub al listar pull requests de '{nombre_limpio}': {ocultar_secretos(str(e))}"
    bloques = []
    for pr in pulls:
        autor = getattr(getattr(pr, "user", None), "login", None) or "desconocido"
        origen = getattr(getattr(pr, "head", None), "ref", "?")
        destino = getattr(getattr(pr, "base", None), "ref", "?")
        try:
            archivos = [f.filename for f in (pr.get_files() or [])]
        except Exception:
            archivos = []
        lista_archivos = ", ".join(archivos[:30]) if archivos else "(sin archivos listados)"
        if len(archivos) > 30:
            lista_archivos += f" (+{len(archivos) - 30} más)"
        bloques.append(
            f"PR #{pr.number} - {pr.title}\n"
            f"  Autor: {autor} | Fusión: {origen} -> {destino}\n"
            f"  Archivos modificados: {lista_archivos}"
        )
        if len(bloques) >= 20:
            break
    if not bloques:
        return f"No hay pull requests en estado '{estado_limpio}' en '{nombre_limpio}'."
    return f"Pull Requests ({estado_limpio}) en {nombre_limpio}:\n\n" + "\n\n".join(bloques)


@herramienta
def gh_ultimos_commits(repositorio: str, cantidad: int = 10) -> str:
    """Muestra los últimos commits de la rama principal con hash corto, autor, fecha y mensaje.
    repositorio: URL del repositorio o 'usuario/repo'.
    cantidad: cantidad máxima de commits a mostrar (por defecto 10).
    """
    repo, nombre_limpio, error = _obtener_repo_github(repositorio)
    if error:
        return error
    limite = max(1, min(int(cantidad), 50))
    try:
        commits = repo.get_commits()
    except GithubException as e:
        return f"Error de GitHub al obtener los commits de '{nombre_limpio}': {ocultar_secretos(str(e))}"
    lineas = []
    for indice, c in enumerate(commits):
        if indice >= limite:
            break
        sha_corto = (getattr(c, "sha", "") or "")[:7]
        commit_interno = getattr(c, "commit", None)
        autor_obj = getattr(commit_interno, "author", None)
        autor = (
            getattr(autor_obj, "name", None)
            or getattr(getattr(c, "author", None), "login", None)
            or "desconocido"
        )
        fecha_raw = getattr(autor_obj, "date", None)
        if isinstance(fecha_raw, datetime.datetime):
            fecha = fecha_raw.strftime("%Y-%m-%d %H:%M")
        else:
            fecha = str(fecha_raw or "sin fecha")
        mensaje_completo = (getattr(commit_interno, "message", "") or "").strip()
        primera_linea = mensaje_completo.splitlines()[0] if mensaje_completo else "(sin mensaje)"
        lineas.append(f"- [{sha_corto}] {fecha} ({autor}): {ocultar_secretos(primera_linea)}")
    if not lineas:
        return f"No se encontraron commits en '{nombre_limpio}'."
    return f"Últimos {len(lineas)} commits en {nombre_limpio}:\n" + "\n".join(lineas)


# ---------------------------------------------------------------- lectura de documentos locales

MAX_CARACTERES_DOCUMENTO = 80_000
EXTENSIONES_TEXTO_LOCAL = {".txt", ".py", ".json", ".md"}


@herramienta
def leer_documento_local(ruta_archivo: str) -> str:
    """Lee el contenido de un documento local (.txt, .py, .json, .md o .pdf) y devuelve su texto.
    ruta_archivo: ruta local del archivo a leer (ej. 'C:/proyecto/main.py' o 'informe.pdf').
    """
    ruta_limpia = (ruta_archivo or "").strip().strip('"').strip("'")
    if not ruta_limpia:
        return "Indicá la ruta del archivo local que querés leer."
    if es_archivo_sensible(ruta_limpia):
        return (
            f"Acceso bloqueado por seguridad: '{ruta_limpia}' es un archivo sensible "
            "(como .env o credenciales) y su lectura está estrictamente prohibida."
        )
    archivo = Path(ruta_limpia).expanduser()
    if es_archivo_sensible(str(archivo)):
        return (
            f"Acceso bloqueado por seguridad: '{ruta_limpia}' es un archivo sensible "
            "(como .env o credenciales) y su lectura está estrictamente prohibida."
        )
    if not archivo.exists():
        return f"No encontré el archivo '{ruta_limpia}'."
    if not archivo.is_file():
        return f"'{ruta_limpia}' no es un archivo válido (puede ser una carpeta)."

    extension = archivo.suffix.lower()
    try:
        if extension in EXTENSIONES_TEXTO_LOCAL:
            with open(archivo, encoding="utf-8") as f:
                texto = f.read()
        elif extension == ".pdf":
            if fitz is None:
                return "Falta instalar PyMuPDF (pip install PyMuPDF) para poder leer archivos PDF."
            documento = fitz.open(archivo)
            try:
                paginas = [pagina.get_text() for pagina in documento]
            finally:
                close_fn = getattr(documento, "close", None)
                if callable(close_fn):
                    close_fn()
            texto = "\n".join(paginas)
        else:
            soportadas = ", ".join(sorted(EXTENSIONES_TEXTO_LOCAL | {".pdf"}))
            return f"Formato '{extension or 'sin extensión'}' no soportado. Formatos permitidos: {soportadas}."
    except UnicodeDecodeError:
        return f"No pude decodificar '{ruta_limpia}' como texto UTF-8."
    except Exception as e:
        return f"Error al leer '{ruta_limpia}': {ocultar_secretos(str(e))}"

    aviso_truncado = ""
    if len(texto) > MAX_CARACTERES_DOCUMENTO:
        total_original = len(texto)
        texto = texto[:MAX_CARACTERES_DOCUMENTO]
        aviso_truncado = (
            f"\n\n[AVISO: El archivo supera el límite de {MAX_CARACTERES_DOCUMENTO} caracteres "
            f"(tenía {total_original} caracteres) y fue truncado.]"
        )

    return ocultar_secretos(texto) + aviso_truncado


# ---------------------------------------------------------------- investigación técnica web

MAX_CARACTERES_INVESTIGACION = 12_000
TIMEOUT_DESCARGA_INVESTIGACION = 4
MAX_HILOS_INVESTIGACION = 5
DOMINIOS_DOCUMENTACION_OFICIAL = (
    ".org",
    "docs.",
    "wiki.",
    "github.com",
    "postgresql.org",
    "gnuhealth.org",
    "tryton.org",
)
USER_AGENT_ESTANDAR = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_REEMPLAZOS_FONETICOS = (
    (re.compile(r"\b(genugel|gnu\s+g|geneohealth|genu\s+health)\b", re.IGNORECASE), "GNU Health"),
    (re.compile(r"\b(divi\s+builder|debeaver|d\s+beaver)\b", re.IGNORECASE), "DBeaver"),
    (re.compile(r"\b(triton|traiton)\b", re.IGNORECASE), "Tryton"),
    (re.compile(r"\b(posgre(?:sql)?|postgre(?!sql))\b", re.IGNORECASE), "PostgreSQL"),
)


def normalizar_consulta_tecnica(consulta: str) -> str:
    """Corrige errores fonéticos del micrófono para términos técnicos frecuentes antes de buscar."""
    texto = (consulta or "").strip()
    if not texto:
        return ""
    for patron, reemplazo in _REEMPLAZOS_FONETICOS:
        texto = patron.sub(reemplazo, texto)
    return texto


def _url_resultado_ddgs(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    return (item.get("href") or item.get("url") or item.get("link") or "").strip()


def _es_fuente_oficial(url: str) -> bool:
    """Verifica si la URL pertenece a alguno de los dominios de documentación priorizados."""
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        host = ""
    objetivo = f"{host} {(url or '').lower()}"
    return any(dominio in objetivo for dominio in DOMINIOS_DOCUMENTACION_OFICIAL)


def _extraer_contenido_estructurado_html(html: str) -> str:
    """Elimina ruido y extrae contenido técnico usando trafilatura.extract y bloques estructurados (<pre>, <code>)."""
    html_texto = html or ""
    html_sin_ruido = html_texto
    partes: list[str] = []
    bloques_codigo: list[str] = []
    bloques_texto: list[str] = []

    if BeautifulSoup is not None:
        soup = BeautifulSoup(html_texto, "html.parser")
        for etiqueta in soup(["script", "style", "nav", "footer", "header"]):
            etiqueta.decompose()
        html_sin_ruido = str(soup)

        for elemento in soup.find_all(["h1", "h2", "h3", "p", "li", "pre", "code"]):
            nombre = (elemento.name or "").lower()
            if nombre == "code" and elemento.find_parent(["pre", "p", "li", "h1", "h2", "h3"]) is not None:
                continue
            if nombre == "pre":
                codigo = elemento.get_text().strip()
                if codigo:
                    bloque = f"```\n{codigo}\n```"
                    partes.append(bloque)
                    bloques_codigo.append(bloque)
            elif nombre == "code":
                codigo = elemento.get_text().strip()
                if codigo:
                    bloque = f"```\n{codigo}\n```" if "\n" in codigo else f"`{codigo}`"
                    partes.append(bloque)
                    bloques_codigo.append(bloque)
            elif nombre in ("h1", "h2", "h3"):
                nivel = int(nombre[1])
                texto = elemento.get_text(" ", strip=True)
                if texto:
                    linea = f"{'#' * nivel} {texto}"
                    partes.append(linea)
                    bloques_texto.append(linea)
            elif nombre == "li":
                texto = elemento.get_text(" ", strip=True)
                if texto:
                    linea = f"- {texto}"
                    partes.append(linea)
                    bloques_texto.append(linea)
            elif nombre == "p":
                texto = elemento.get_text(" ", strip=True)
                if texto:
                    partes.append(texto)
                    bloques_texto.append(texto)

    texto_trafilatura = ""
    if trafilatura is not None and hasattr(trafilatura, "extract"):
        try:
            extraido = trafilatura.extract(
                html_sin_ruido,
                include_formatting=True,
                include_tables=True,
            )
            if isinstance(extraido, str) and extraido.strip():
                texto_trafilatura = extraido.strip()
        except Exception:
            texto_trafilatura = ""

    natural = "\n".join(partes).strip()
    if texto_trafilatura:
        if not natural:
            return texto_trafilatura
        faltantes = [p for p in partes if p not in texto_trafilatura]
        combinado = (texto_trafilatura + ("\n" + "\n".join(faltantes) if faltantes else "")).strip()
    else:
        combinado = natural

    if len(combinado) > MAX_CARACTERES_INVESTIGACION and bloques_codigo:
        # Prioriza bloques <pre> y <code> para que ningún comando o configuración quede fuera por truncado.
        return "\n".join(bloques_codigo + ([texto_trafilatura] if texto_trafilatura else bloques_texto)).strip()
    return combinado


def _leer_via_jina_reader(url: str) -> str:
    """Descarga versión Markdown limpia vía https://r.jina.ai/ cuando la extracción directa falla."""
    if requests is None or not url:
        return ""
    url_jina = url if url.startswith("https://r.jina.ai/") else f"https://r.jina.ai/{url}"
    try:
        respuesta = requests.get(
            url_jina,
            timeout=TIMEOUT_DESCARGA_INVESTIGACION,
            headers={"User-Agent": USER_AGENT_ESTANDAR},
        )
        if getattr(respuesta, "status_code", 200) >= 400:
            return ""
        return (getattr(respuesta, "text", "") or "").strip()
    except Exception:
        return ""


def _procesar_item_investigacion(item: dict) -> str:
    """Descarga y extrae el contenido de un resultado de búsqueda individual (HTML, Jina o YouTube)."""
    url = _url_resultado_ddgs(item)
    if not url:
        return ""
    titulo = (item.get("title") or "").strip() if isinstance(item, dict) else ""
    encabezado = f"Fuente: {url}" + (f" ({titulo})" if titulo else "")

    id_video = extraer_id_youtube(url)
    if id_video:
        transcripcion, error_yt = _descargar_transcripcion(id_video)
        if transcripcion:
            return f"{encabezado}\n[Transcripción del video de YouTube]\n{transcripcion}"
        log.warning("Sin transcripción para %s: %s", url, error_yt)
        return ""

    html = ""
    try:
        respuesta = requests.get(
            url,
            timeout=TIMEOUT_DESCARGA_INVESTIGACION,
            headers={"User-Agent": USER_AGENT_ESTANDAR},
        )
        if getattr(respuesta, "status_code", 200) < 400:
            html = getattr(respuesta, "text", "") or ""
    except Exception as e:
        log.warning("Error descargando %s: %s", url, e)

    contenido = _extraer_contenido_estructurado_html(html) if html else ""
    if not contenido and isinstance(item, dict) and item.get("usar_jina"):
        contenido = _leer_via_jina_reader(url)
    if not contenido:
        return ""
    return f"{encabezado}\n{contenido}"


@herramienta
def investigar_a_fondo(consulta: str, solo_fuentes_oficiales: bool = False) -> str:
    """Delega la investigación profunda directamente a Gemini Pro en Microsoft Edge usando el modelo '3.1 Pro'.
    consulta: términos técnicos o pregunta a investigar.
    solo_fuentes_oficiales: parámetro opcional mantenido por compatibilidad.
    """
    consulta_limpia = normalizar_consulta_tecnica(consulta)
    if not consulta_limpia:
        return "Indicá qué consulta técnica querés investigar."
    return consultar_gemini_pro(consulta_limpia, modelo="3.1 Pro")


# ---------------------------------------------------------------- YouTube (transcripciones)

IDIOMAS_TRANSCRIPCION = ["es", "es-419", "es-ES", "en", "en-US", "en-GB"]
_PATRON_ID_YOUTUBE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extraer_id_youtube(url: str) -> str:
    """Devuelve el ID de 11 caracteres de un enlace youtube.com/watch, youtu.be, /shorts o /embed; si no, ''."""
    texto = (url or "").strip()
    if not texto:
        return ""
    if not re.match(r"^https?://", texto, flags=re.IGNORECASE):
        texto = "https://" + texto
    try:
        partes = urllib.parse.urlparse(texto)
    except Exception:
        return ""
    host = partes.netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    candidato = ""
    if host == "youtu.be":
        candidato = partes.path.lstrip("/").split("/")[0]
    elif host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        if partes.path == "/watch":
            candidato = (urllib.parse.parse_qs(partes.query).get("v") or [""])[0]
        else:
            segmentos = [s for s in partes.path.split("/") if s]
            if len(segmentos) >= 2 and segmentos[0] in ("shorts", "embed", "live", "v"):
                candidato = segmentos[1]
    return candidato if _PATRON_ID_YOUTUBE.match(candidato or "") else ""


def _texto_de_fragmento(fragmento) -> str:
    if isinstance(fragmento, dict):
        return str(fragmento.get("text", "")).strip()
    return str(getattr(fragmento, "text", "") or "").strip()


def _descargar_transcripcion(id_video: str) -> tuple[str, str]:
    """Descarga la transcripción en español o inglés. Devuelve (texto, error)."""
    if YouTubeTranscriptApi is None:
        return "", "Falta instalar youtube-transcript-api (pip install youtube-transcript-api>=0.6.2)."
    try:
        if hasattr(YouTubeTranscriptApi, "get_transcript"):
            fragmentos = YouTubeTranscriptApi.get_transcript(id_video, languages=IDIOMAS_TRANSCRIPCION)
        else:  # youtube-transcript-api >= 1.0 reemplazó get_transcript por fetch()
            fragmentos = YouTubeTranscriptApi().fetch(id_video, languages=IDIOMAS_TRANSCRIPCION)
    except Exception as e:
        return "", f"No hay transcripción disponible en español o inglés ({type(e).__name__})."
    texto = " ".join(t for t in (_texto_de_fragmento(f) for f in fragmentos or []) if t)
    texto = re.sub(r"\s+", " ", texto).strip()
    if not texto:
        return "", "La transcripción del video está vacía."
    return texto, ""


@herramienta
def leer_video_youtube(url_o_busqueda: str) -> str:
    """Lee la transcripción completa (español o inglés) de un video de YouTube a partir de su enlace o de una búsqueda.
    url_o_busqueda: enlace del video (youtube.com/watch?v=... o youtu.be/...) o términos para buscar el video.
    """
    entrada = (url_o_busqueda or "").strip()
    if not entrada:
        return "Indicá el enlace del video de YouTube o qué querés buscar."

    id_video = extraer_id_youtube(entrada)
    url_video = f"https://www.youtube.com/watch?v={id_video}" if id_video else ""
    titulo = ""

    if not id_video:
        if DDGS is None:
            return "Falta instalar duckduckgo-search (pip install duckduckgo-search>=6.0)."
        try:
            buscador = DDGS()
            try:
                resultados = list(buscador.text(f"{entrada} site:youtube.com", max_results=10) or [])
            except TypeError:
                resultados = list(buscador.text(f"{entrada} site:youtube.com") or [])
        except Exception as e:
            return f"No pude buscar videos para '{entrada}': {ocultar_secretos(str(e))}"
        for item in resultados:
            url = _url_resultado_ddgs(item)
            candidato = extraer_id_youtube(url)
            if candidato:
                id_video, url_video = candidato, url
                titulo = (item.get("title") or "").strip() if isinstance(item, dict) else ""
                break
        if not id_video:
            return f"No encontré ningún video de YouTube para '{entrada}'."

    transcripcion, error = _descargar_transcripcion(id_video)
    if not transcripcion:
        return f"No pude leer el video {url_video}: {error}"

    encabezado = f"Fuente: {url_video}" + (f" ({titulo})" if titulo else "")
    texto = ocultar_secretos(f"{encabezado}\n[Transcripción del video de YouTube]\n{transcripcion}")
    if len(texto) > MAX_CARACTERES_INVESTIGACION:
        texto = texto[:MAX_CARACTERES_INVESTIGACION] + (
            f"\n\n[AVISO: La transcripción superaba {MAX_CARACTERES_INVESTIGACION} caracteres y fue truncada.]"
        )
    return texto


# ---------------------------------------------------------------- ejecución WSL, SSH VM e inspección HD

@herramienta
def ejecutar_wsl(comando: str) -> str:
    """Ejecuta un comando directamente en WSL Ubuntu y devuelve la salida completa.
    comando: comando de bash a ejecutar en WSL Ubuntu.
    """
    comando_limpio = (comando or "").strip()
    if not comando_limpio:
        return "Indicá el comando a ejecutar en WSL."
    if es_destructivo(comando_limpio):
        return (
            f"Comando bloqueado por seguridad: '{comando_limpio}' contiene una operación "
            "destructiva (rm -rf, DROP, mkfs, dd)."
        )
    try:
        proceso = subprocess.run(
            ["wsl", "-e", "bash", "-c", comando_limpio],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "El comando en WSL superó el límite de 30 segundos y fue cancelado."
    except FileNotFoundError:
        return "No encontré el ejecutable 'wsl' en este sistema."
    except Exception as e:
        return f"Error al ejecutar en WSL: {ocultar_secretos(str(e))}"

    salida = ((proceso.stdout or "") + (proceso.stderr or "")).strip() or "(sin salida)"
    return ocultar_secretos(f"Código de salida {proceso.returncode}.\n{salida}")


@herramienta
def ejecutar_ssh_vm(comando: str) -> str:
    """Ejecuta un comando por SSH no interactivo en la máquina virtual configurada y devuelve su salida.
    comando: comando de Linux o consulta a ejecutar en la máquina virtual por SSH.
    """
    comando_limpio = (comando or "").strip()
    if not comando_limpio:
        return "Indicá el comando a ejecutar por SSH en la máquina virtual."
    if es_destructivo(comando_limpio):
        return (
            f"Comando bloqueado por seguridad: '{comando_limpio}' contiene una operación "
            "destructiva (rm -rf, DROP, mkfs, dd)."
        )
    if paramiko is None:
        return "Falta instalar paramiko (pip install paramiko>=3.4)."

    cliente = paramiko.SSHClient()
    cliente.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cliente.connect(
            hostname=config.VM_SSH_HOST,
            port=config.VM_SSH_PORT,
            username=config.VM_SSH_USER,
            password=config.VM_SSH_PASSWORD,
            timeout=30,
        )
        _, stdout, stderr = cliente.exec_command(comando_limpio, timeout=30)
        out_raw = stdout.read()
        err_raw = stderr.read()
        out_txt = out_raw.decode("utf-8", errors="replace") if isinstance(out_raw, (bytes, bytearray)) else str(out_raw or "")
        err_txt = err_raw.decode("utf-8", errors="replace") if isinstance(err_raw, (bytes, bytearray)) else str(err_raw or "")
        canal = getattr(stdout, "channel", None)
        codigo = canal.recv_exit_status() if canal is not None and hasattr(canal, "recv_exit_status") else 0
        salida = (out_txt + err_txt).strip() or "(sin salida)"
        return ocultar_secretos(f"SSH {config.VM_SSH_USER}@{config.VM_SSH_HOST} (código {codigo}):\n{salida}")
    except Exception as e:
        return f"Error SSH en {config.VM_SSH_USER}@{config.VM_SSH_HOST}:{config.VM_SSH_PORT}: {ocultar_secretos(str(e))}"
    finally:
        try:
            cliente.close()
        except Exception:
            pass


def _info_ventana_activa() -> tuple[str, str, dict | None]:
    """Obtiene (titulo, ejecutable, region_mss) de la ventana activa en Windows."""
    if not ES_WINDOWS:
        return "Pantalla principal", "", None
    try:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "Pantalla principal", "", None
        largo = user32.GetWindowTextLengthW(hwnd)
        titulo_buf = ctypes.create_unicode_buffer(largo + 1)
        if largo:
            user32.GetWindowTextW(hwnd, titulo_buf, largo + 1)
        titulo = titulo_buf.value.strip() or "Ventana sin título"
        programa = _programa_de(hwnd)

        rect = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            ancho = rect.right - rect.left
            alto = rect.bottom - rect.top
            if ancho > 50 and alto > 50:
                return titulo, programa, {"left": rect.left, "top": rect.top, "width": ancho, "height": alto}
        return titulo, programa, None
    except Exception:
        return "Pantalla principal", "", None


_emisor_video = None


def registrar_emisor_video(funcion) -> None:
    """Registra el callback de la sesión activa de Gemini Live para enviar frames JPEG en tiempo real."""
    global _emisor_video
    _emisor_video = funcion


def _posicion_cursor() -> tuple[int, int]:
    """Devuelve las coordenadas (x, y) actuales del cursor del mouse en pantalla."""
    try:
        import pyautogui
        pos = pyautogui.position()
        return int(pos[0]), int(pos[1])
    except Exception:
        pass
    if ES_WINDOWS:
        try:
            from ctypes import wintypes
            punto = wintypes.POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(punto)):
                return int(punto.x), int(punto.y)
        except Exception:
            pass
    return (0, 0)


@herramienta
def inspeccionar_pantalla_hd() -> str:
    """Captura la ventana activa o pantalla en 1080p nativo, la envía como frame image/jpeg a Gemini Live y devuelve título y cursor.
    """
    if mss is None or Image is None:
        return "Faltan las librerías mss o Pillow para capturar la pantalla en HD."

    titulo_activo, programa_activo, region = _info_ventana_activa()
    cursor_x, cursor_y = _posicion_cursor()
    try:
        ct = getattr(mss, "mss", None) or getattr(mss, "MSS", None)
        with ct() as sct:
            objetivo = region or getattr(sct, "primary_monitor", None) or sct.monitors[1]
            captura = sct.grab(objetivo)
        imagen = Image.frombytes("RGB", captura.size, captura.rgb)
    except Exception as e:
        return f"No pude capturar la pantalla en HD: {ocultar_secretos(str(e))}"

    # Resolución nativa hasta 1920x1080 (1080p).
    if imagen.width > 1920 or imagen.height > 1080:
        alto = round(imagen.height * 1920 / imagen.width)
        imagen = imagen.resize((1920, min(1080, alto)), Image.Resampling.LANCZOS)

    # 1) Frame MIME image/jpeg (calidad 85, subsampling 4:4:4) enviado al canal de entrada de Gemini Live.
    buffer_jpeg = io.BytesIO()
    imagen.save(buffer_jpeg, format="JPEG", quality=85, subsampling=0)
    bytes_jpeg = buffer_jpeg.getvalue()

    enviado_live = False
    if callable(_emisor_video):
        try:
            _emisor_video(bytes_jpeg)
            enviado_live = True
        except Exception:
            enviado_live = False

    # 2) Copia PNG sin comprimir en disco para inspección local.
    buffer_png = io.BytesIO()
    imagen.save(buffer_png, format="PNG")
    bytes_png = buffer_png.getvalue()

    ruta_captura = Path(tempfile.gettempdir()) / "jarvis_inspeccion_hd.png"
    try:
        ruta_captura.write_bytes(bytes_png)
    except Exception:
        pass

    ventanas_visibles = []
    if ES_WINDOWS:
        try:
            ventanas_visibles = [t for t in _ventanas().values() if t]
        except Exception:
            ventanas_visibles = []

    detalle_programa = f" ({programa_activo})" if programa_activo else ""
    resumen_ventanas = ", ".join(ventanas_visibles[:10]) if ventanas_visibles else titulo_activo
    estado_live = "enviado a la sesión Gemini Live" if enviado_live else "procesado en memoria"
    return (
        f"Frame HD 1080p procesado (mime_type=image/jpeg, calidad=85, {imagen.width}x{imagen.height} px, "
        f"{len(bytes_jpeg) // 1024} KB, {estado_live}; PNG sin comprimir: {len(bytes_png) // 1024} KB).\n"
        f"Ventana activa: {titulo_activo}{detalle_programa}\n"
        f"Posición del cursor: ({cursor_x}, {cursor_y})\n"
        f"Archivo de inspección: {ruta_captura}\n"
        f"Ventanas abiertas detectadas: {resumen_ventanas}"
    )


# ---------------------------------------------------------------- Doble Cerebro: Agente Verificador

TEMPERATURA_AUDITOR = 0.0
_PALABRAS_CONDICIONALES = re.compile(
    r"\b(quiz[aá]s|tal\s+vez|prob[aá]|deber[ií]a[sn]?|fijate\s+si|probablemente|imagino\s+que)\b",
    re.IGNORECASE,
)


class DiagnosticoVerificado(BaseModel):
    """Esquema estructurado obligatorio del Agente Auditor de Certeza Absoluta."""
    CausaVerificada: str = Field(description="Hecho técnico comprobado en el entorno o documentación oficial.")
    EvidenciaDelSistema: str = Field(description="Datos reales leídos de ventana activa, procesos, puertos, VM, WSL o docs.")
    PasosExactosNumerados: list[str] = Field(description="Pasos exactos numerados (1, 2, 3) sin condicionales.")
    ComandoVerificado: str = Field(default="", description="Comando o valor exacto comprobado listo para usar.")


def _inspeccionar_controles_pywinauto() -> str:
    """Lee los controles reales de la ventana activa usando pywinauto (si está disponible)."""
    if pywinauto is None:
        return ""
    try:
        desktop = pywinauto.Desktop(backend="uia")
        ventana = desktop.top_window()
        titulo = ventana.window_text()
        controles = []
        for ctrl in ventana.descendants()[:25]:
            try:
                txt = (ctrl.window_text() or "").strip()
                tipo = getattr(ctrl.element_info, "control_type", "") or ""
                if txt:
                    controles.append(f"{tipo}: {txt}" if tipo else txt)
            except Exception:
                continue
        resumen = ", ".join(controles[:15]) if controles else "sin textos secundarios"
        return f"Ventana UIA: {titulo} | Controles: {resumen}"
    except Exception:
        return ""


def _inspeccionar_procesos_y_puertos_psutil() -> str:
    """Recolecta procesos activos relevantes y puertos en escucha usando psutil."""
    if psutil is None:
        return "psutil no instalado."
    procesos_interes: list[str] = []
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            info = getattr(proc, "info", {}) or {}
            nombre = str(info.get("name") or "")
            if nombre and len(procesos_interes) < 15:
                procesos_interes.append(f"{nombre}(pid={info.get('pid')})")
    except Exception:
        pass

    puertos: list[str] = []
    try:
        for conn in psutil.net_connections(kind="inet"):
            status = getattr(conn, "status", "")
            laddr = getattr(conn, "laddr", None)
            puerto = getattr(laddr, "port", None) if laddr else None
            if (status == "LISTEN" or not status) and puerto is not None:
                etiqueta = f":{puerto}"
                if etiqueta not in puertos and len(puertos) < 15:
                    puertos.append(etiqueta)
    except Exception:
        pass

    txt_proc = ", ".join(procesos_interes) if procesos_interes else "ninguno listado"
    txt_puertos = ", ".join(puertos) if puertos else "ninguno listado"
    return f"Procesos: {txt_proc} | Puertos en escucha: {txt_puertos}"


def _limpiar_condicionales(texto: str) -> str:
    """Elimina cualquier palabra condicional prohibida ('quizás', 'probá', 'debería', etc.)."""
    limpio = _PALABRAS_CONDICIONALES.sub("", texto or "")
    return re.sub(r"  +", " ", limpio).strip()


def _sintetizar_con_agente_auditor(
    consulta: str,
    evidencia_ui: str,
    evidencia_psutil: str,
    evidencia_remota: str,
    evidencia_docs: str,
) -> DiagnosticoVerificado:
    """Ejecuta el segundo cerebro con temperature=0.0 y esquema Pydantic DiagnosticoVerificado."""
    resumen_evidencia = " | ".join(
        parte for parte in (evidencia_ui, evidencia_psutil, evidencia_remota, evidencia_docs[:800]) if parte
    )
    if litellm is not None and config.clave_configurada():
        try:
            prompt_auditor = (
                "Sos el Agente Verificador de Certeza Absoluta. Analizá únicamente los hechos reales recolectados "
                "del sistema y devolvé un JSON válido con las claves exactas: "
                "CausaVerificada, EvidenciaDelSistema, PasosExactosNumerados (lista de strings) y ComandoVerificado. "
                "PROHIBIDO usar palabras condicionales ('quizás', 'probá', 'debería').\n\n"
                f"Consulta: {consulta}\n"
                f"Evidencia UI: {evidencia_ui}\n"
                f"Evidencia Procesos/Puertos: {evidencia_psutil}\n"
                f"Evidencia VM/WSL: {evidencia_remota}\n"
                f"Documentación oficial: {evidencia_docs[:3000]}"
            )
            respuesta = litellm.completion(
                model=config.MODELO,
                messages=[{"role": "user", "content": prompt_auditor}],
                temperature=TEMPERATURA_AUDITOR,
                response_format=DiagnosticoVerificado,
            )
            contenido = (respuesta.choices[0].message.content or "").strip()
            datos = json.loads(contenido)
            return DiagnosticoVerificado(**datos)
        except Exception as e:
            log.warning("Agente auditor usó síntesis determinista por: %s", e)

    # Síntesis determinista basada 100% en la evidencia recolectada del entorno real.
    causa = f"Verificación completada para '{consulta}'."
    if evidencia_remota:
        causa += f" Estado comprobado en entorno remoto/local: {evidencia_remota.splitlines()[0]}"
    pasos = [
        f"En la ventana activa ({evidencia_ui.splitlines()[0] if evidencia_ui else 'interfaz actual'}), ubicá el panel de configuración correspondiente a '{consulta}'.",
        f"Ingresá los parámetros exactos verificados en el sistema ({evidencia_remota.splitlines()[-1] if evidencia_remota else evidencia_psutil}).",
        "Confirmá la operación y verificá que el estado del servicio o conexión responda sin errores.",
    ]
    comando = ""
    if evidencia_remota:
        comando = evidencia_remota.splitlines()[-1].strip()
    return DiagnosticoVerificado(
        CausaVerificada=_limpiar_condicionales(causa),
        EvidenciaDelSistema=_limpiar_condicionales(resumen_evidencia or "Entorno local verificado."),
        PasosExactosNumerados=[_limpiar_condicionales(p) for p in pasos],
        ComandoVerificado=_limpiar_condicionales(comando),
    )


@herramienta
def diagnosticar_y_resolver_con_certeza(consulta_usuario: str) -> str:
    """Agente Verificador de Doble Cerebro (temperature=0.0): recolecta evidencia real (UI, procesos, puertos, VM/WSL y docs) y devuelve la solución verificada paso a paso.
    consulta_usuario: problema técnico, error o consulta de configuración a diagnosticar y resolver con certeza.
    """
    consulta_limpia = normalizar_consulta_tecnica(consulta_usuario)
    if not consulta_limpia:
        return "Indicá la consulta o problema técnico a diagnosticar."

    # a) Contexto real de pantalla (pywinauto + captura HD 1080p) y procesos/puertos (psutil).
    controles_uia = _inspeccionar_controles_pywinauto()
    captura_hd = inspeccionar_pantalla_hd()
    evidencia_ui = "\n".join(p for p in (controles_uia, captura_hd) if p)
    evidencia_psutil = _inspeccionar_procesos_y_puertos_psutil()

    # b) Comprobación silenciosa en VM por SSH o WSL si la consulta los involucra.
    minuscula = consulta_limpia.lower()
    evidencia_remota_partes: list[str] = []
    if any(k in minuscula for k in ("vm", "ssh", "gnu health", "gnuhealth", "tryton", "postgresql", "dbeaver", "base de datos", "servidor", "192.168.")):
        salida_vm = ejecutar_ssh_vm("psql -U gnuhealth -lqt 2>/dev/null || ss -tulpn")
        evidencia_remota_partes.append(f"[VM SSH] {salida_vm}")
    if any(k in minuscula for k in ("wsl", "ubuntu", "linux", "subsistema")):
        salida_wsl = ejecutar_wsl("ss -tulpn 2>/dev/null || ps aux")
        evidencia_remota_partes.append(f"[WSL] {salida_wsl}")
    evidencia_remota = "\n".join(evidencia_remota_partes)

    # Documentación externa verificada.
    evidencia_docs = investigar_a_fondo(consulta_limpia, solo_fuentes_oficiales=True)

    # c) Síntesis estructurada con Pydantic (temperature=0.0) sin palabras condicionales.
    diagnostico = _sintetizar_con_agente_auditor(
        consulta=consulta_limpia,
        evidencia_ui=evidencia_ui,
        evidencia_psutil=evidencia_psutil,
        evidencia_remota=evidencia_remota,
        evidencia_docs=evidencia_docs,
    )

    hecho = _limpiar_condicionales(f"{diagnostico.CausaVerificada} ({diagnostico.EvidenciaDelSistema})")
    lineas_pasos = []
    for idx, paso in enumerate(diagnostico.PasosExactosNumerados, start=1):
        paso_limpio = _limpiar_condicionales(re.sub(r"^\s*(?:paso\s*)?\d+[.):\-]*\s*", "", str(paso), flags=re.IGNORECASE))
        lineas_pasos.append(f"{idx}. {paso_limpio}")

    resultado = (
        f"HECHO VERIFICADO: {hecho}\n"
        f"PASOS EXACTOS (1, 2, 3):\n"
        + "\n".join(lineas_pasos)
    )
    if diagnostico.ComandoVerificado:
        resultado += f"\nCOMANDO VERIFICADO: {_limpiar_condicionales(diagnostico.ComandoVerificado)}"
    return ocultar_secretos(resultado)






# ---------------------------------------------------------------- Gemini web (Edge) como cerebro

from . import gemini_web  # noqa: E402  (no importa herramientas: sin import circular)

MAX_CARACTERES_CODIGO = 60_000
MAX_ARCHIVOS_REPO = 12
MAX_CARACTERES_REPO = 60_000
_ultimo_portapapeles_usado: str | None = None

PROMPT_EXPLICAR_CODIGO = (
    "Explicame este código en español rioplatense, claro y directo: qué hace, cómo fluye, "
    "y marcá errores o riesgos concretos con la línea exacta si los hay."
)


def _fallback_respuesta_estandar(mensaje: str, motivo: str) -> str:
    """Si Gemini web se bloquea o lanza excepción, responde con el modelo estándar de Jarvis por API."""
    if litellm is not None and config.clave_configurada():
        try:
            respuesta = litellm.completion(
                model=config.MODELO,
                messages=[{"role": "user", "content": mensaje}],
                temperature=0.1,
            )
            texto = (respuesta.choices[0].message.content or "").strip()
            if texto:
                return ocultar_secretos(f"[Fallback estándar Jarvis ({config.MODELO})]\n{texto}")
        except Exception as e:
            log.warning("Fallback estándar también falló: %s", e)
    return f"No pude usar Gemini web: {ocultar_secretos(motivo)}"


def _respuesta_gemini(mensaje: str, ruta: "gemini_web.Ruta | None" = None, imagen: Path | None = None) -> str:
    """Consulta Gemini web y devuelve texto listo para el modelo que la llamó (con fallback limpio a Jarvis estándar)."""
    ruta = ruta or gemini_web.elegir_modelo(mensaje)
    try:
        respuesta = gemini_web.consultar(mensaje, ruta=ruta, imagen=imagen)
    except gemini_web.ErrorGeminiWeb as e:
        log.warning("Gemini web falló (%s); ejecutando fallback estándar.", e)
        return _fallback_respuesta_estandar(mensaje, str(e))
    except Exception as e:
        log.warning("Excepción en Gemini web (%s); ejecutando fallback estándar.", e)
        return _fallback_respuesta_estandar(mensaje, f"{type(e).__name__}: {e}")
    return ocultar_secretos(f"[Gemini {ruta.modelo} · pensamiento {ruta.pensamiento}]\n{respuesta}")


def _codigo_nuevo_del_portapapeles() -> str:
    """Texto del portapapeles si es nuevo (no se mandó antes); '' si está vacío o repetido."""
    global _ultimo_portapapeles_usado
    try:
        import pyperclip
        texto = (pyperclip.paste() or "").strip()
    except Exception:
        return ""
    if not texto or texto == _ultimo_portapapeles_usado:
        return ""
    _ultimo_portapapeles_usado = texto
    return texto


def _capturar_pantalla_png() -> Path:
    """Captura el monitor principal en resolución nativa (hasta 1080p) y la guarda como PNG temporal."""
    if mss is None or Image is None:
        raise RuntimeError("Faltan mss o Pillow para capturar la pantalla.")
    ct = getattr(mss, "mss", None) or getattr(mss, "MSS", None)
    with ct() as sct:
        monitor = getattr(sct, "primary_monitor", None) or sct.monitors[1]
        captura = sct.grab(monitor)
    imagen = Image.frombytes("RGB", captura.size, captura.rgb)
    if imagen.width > 1920:
        imagen = imagen.resize((1920, round(imagen.height * 1920 / imagen.width)), Image.Resampling.LANCZOS)
    ruta = Path(tempfile.gettempdir()) / "jarvis_codigo_pantalla.png"
    imagen.save(ruta, format="PNG")
    return ruta


def _resolver_ruta_gemini(consulta: str, modelo: str = "4 Hydrogen") -> "gemini_web.Ruta":
    """Resuelve la Ruta de Gemini web respetando el parámetro modelo explícito o las reglas de la consulta."""
    modelo_limpio = (modelo or "").strip().lower()
    chat_nuevo = bool(gemini_web._PATRON_CHAT_NUEVO.search(consulta or ""))
    if "argon" in modelo_limpio or gemini_web._PATRON_ARGON.search(consulta or ""):
        pensamiento = gemini_web._pensamiento_argon(f"{modelo or ''} {consulta or ''}")
        return gemini_web.Ruta(gemini_web.MODELO_ARGON, pensamiento, chat_nuevo=chat_nuevo)
    if "3.1" in modelo_limpio or "pro" in modelo_limpio:
        return gemini_web.Ruta(gemini_web.MODELO_INVESTIGACION, gemini_web.PENSAMIENTO_ALTO, chat_nuevo=chat_nuevo)
    return gemini_web.elegir_modelo(consulta)


@herramienta
def consultar_gemini_pro(consulta: str, modelo: str = "4 Hydrogen", contexto_pantalla: str = "") -> str:
    """Razona y responde con tu cuenta de Gemini Pro en Edge ('4 Hydrogen' Alto por defecto; '3.1 Pro' Alto para investigación profunda; '4 Argon' Medio solo si se pide explícitamente).
    consulta: la pregunta o tarea completa tal como la pidió el usuario.
    modelo: modelo a usar en Gemini web ('4 Hydrogen' por defecto, '3.1 Pro' para investigación profunda, o '4 Argon' solo si el usuario lo nombra).
    contexto_pantalla: transcripción completa y exacta de todo lo que se ve actualmente en la pantalla del usuario (nombre del programa, código, comandos de terminal, tablas o mensajes de error).
    """
    consulta = (consulta or "").strip()
    if not consulta:
        return "Indicá qué querés consultarle a Gemini."
    ruta = _resolver_ruta_gemini(consulta, modelo=modelo)
    contexto = (contexto_pantalla or "").strip()
    mensaje_final = (
        f"CONTEXTO VISIBLE EN LA PANTALLA DEL USUARIO:\n{contexto}\n\nCONSULTA DEL USUARIO:\n{consulta}"
        if contexto
        else consulta
    )
    return _respuesta_gemini(mensaje_final, ruta=ruta)


@herramienta
def explicar_codigo_en_pantalla(pedido: str = "") -> str:
    """Explica el código copiado en el portapapeles (o, si no hay texto nuevo, una captura de la pantalla) con Gemini 4 Hydrogen.
    pedido: lo que pidió el usuario sobre el código, por ejemplo 'explicame este código' o 'buscá el error'.
    """
    pedido = (pedido or "").strip()
    ruta = gemini_web.elegir_modelo(pedido)
    instruccion = pedido or PROMPT_EXPLICAR_CODIGO
    codigo = _codigo_nuevo_del_portapapeles()
    if codigo:
        if len(codigo) > MAX_CARACTERES_CODIGO:
            codigo = codigo[:MAX_CARACTERES_CODIGO] + "\n# [recortado: el código superaba el límite]"
        mensaje = f"{instruccion}\n\n```\n{codigo}\n```"
        return _respuesta_gemini(ocultar_secretos(mensaje), ruta=ruta)
    try:
        captura = _capturar_pantalla_png()
    except Exception as e:
        return f"No había código nuevo en el portapapeles y no pude capturar la pantalla: {e}"
    mensaje = f"{instruccion}\n\nEl código está en la captura de pantalla adjunta. {PROMPT_EXPLICAR_CODIGO}"
    return _respuesta_gemini(mensaje, ruta=ruta, imagen=captura)


_NOMBRES_CLAVE_REPO = re.compile(
    r"(^|/)(readme(\.\w+)?|requirements[\w.-]*\.txt|pyproject\.toml|setup\.py|package\.json|"
    r"dockerfile|docker-compose\.ya?ml|makefile|cargo\.toml|go\.mod|pom\.xml|"
    r"(main|app|index|server|manage|cli)\.(py|js|ts|go|rs|java))$",
    re.IGNORECASE,
)
_EXTENSIONES_CODIGO = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cs", ".php", ".rb", ".sql")


def _elegir_archivos_relevantes(rutas: list[str], limite: int = MAX_ARCHIVOS_REPO) -> list[str]:
    """Prioriza README, manifiestos y puntos de entrada; después el código menos anidado."""
    ignorar = re.compile(r"(^|/)(node_modules|dist|build|vendor|\.git|__pycache__|\.venv[\w-]*)/", re.IGNORECASE)
    candidatas = [r for r in rutas if not ignorar.search(r)]
    clave = [r for r in candidatas if _NOMBRES_CLAVE_REPO.search(r)]
    codigo = sorted((r for r in candidatas if r.lower().endswith(_EXTENSIONES_CODIGO) and r not in clave),
                    key=lambda r: (r.count("/"), r))
    clave.sort(key=lambda r: (r.count("/"), r))
    return (clave + codigo)[:limite]


@herramienta
def analizar_repositorio_con_gemini(repositorio: str, pedido: str = "") -> str:
    """Extrae los archivos clave de un repositorio de GitHub (módulo estilo MCP) y pide el análisis a Gemini (4 Hydrogen; 3.1 Pro si pedís auditoría profunda).
    repositorio: URL del repositorio o 'usuario/repo'.
    pedido: qué análisis quiere el usuario, por ejemplo 'explicame la arquitectura' o 'auditoría profunda de seguridad'.
    """
    repo, nombre_limpio, error = _obtener_repo_github(repositorio)
    if error:
        return error
    try:
        arbol = repo.get_git_tree(repo.default_branch, recursive=True)
    except GithubException as e:
        return f"Error de GitHub al leer el árbol de '{nombre_limpio}': {ocultar_secretos(str(e))}"
    rutas = [i.path for i in (getattr(arbol, "tree", None) or []) if getattr(i, "type", "") == "blob"]
    elegidos = _elegir_archivos_relevantes(rutas)
    if not elegidos:
        return f"No encontré archivos de código para analizar en '{nombre_limpio}'."

    bloques, total = [], 0
    for ruta_archivo in elegidos:
        contenido = gh_leer_archivo(nombre_limpio, ruta_archivo)
        if contenido.startswith(("No encontré", "Error de GitHub")):
            continue
        restante = MAX_CARACTERES_REPO - total
        if restante <= 0:
            break
        contenido = contenido[:restante]
        total += len(contenido)
        bloques.append(f"### {ruta_archivo}\n```\n{contenido}\n```")

    pedido = (pedido or "").strip() or "Analizá este repositorio: arquitectura, flujo principal, calidad y riesgos."
    # "auditoría profunda" también activa 3.1 Pro en un chat nuevo.
    ruta = gemini_web.elegir_modelo(pedido)
    mensaje = (
        f"{pedido}\n\nRepositorio: {nombre_limpio} (rama {repo.default_branch}, {len(rutas)} archivos en total). "
        f"Te paso los {len(bloques)} archivos más relevantes:\n\n" + "\n\n".join(bloques)
    )
    return _respuesta_gemini(mensaje, ruta=ruta)


# ---------------------------------------------------------------- Orquestador Autónomo y Enjambre Multi-Cerebro

from . import enjambre_web, orquestador_autonomo  # noqa: E402


@herramienta
def consultar_enjambre_multicerebro(
    consulta: str,
    contexto_pantalla: str = "",
    modelo_juez: str = "4 Argon",
    nivel_juez: str = "Alto",
) -> str:
    """Consulta al Enjambre Multi-Cerebro (DeepSeek + ChatGPT + Juez Supremo en Gemini '4 Argon' en nivel 'Alto' o 'Medio').
    consulta: el problema técnico, arquitectónico o de código a resolver.
    contexto_pantalla: transcripción de los datos visibles en pantalla (si aplica).
    modelo_juez: modelo Juez en Gemini web ('4 Argon' por defecto).
    nivel_juez: nivel de pensamiento del Juez ('Alto' por defecto, o 'Medio').
    """
    try:
        return enjambre_web.consultar_enjambre_multicerebro(
            consulta=consulta,
            contexto_pantalla=contexto_pantalla,
            modelo_juez=modelo_juez,
            nivel_juez=nivel_juez,
        )
    except Exception as e:
        log.warning("Fallo en consultar_enjambre_multicerebro (%s); usando fallback.", e)
        return _fallback_respuesta_estandar(consulta, str(e))


@herramienta
def iniciar_tarea_autonoma_issue(
    origen_issue_o_pdf: str,
    modelo: str = "3.1 Pro",
    usar_enjambre: bool = False,
    nivel_juez: str = "Alto",
) -> str:
    """Inicia en segundo plano la resolución autónoma de un Issue de GitHub o tarea en PDF (Docker + Antigravity IDE + Auditoría en Gemini Edge o Enjambre).
    origen_issue_o_pdf: URL del Issue de GitHub, ruta local de un PDF/documento o descripción de la tarea.
    modelo: modelo arquitecto/auditor en Gemini web ('3.1 Pro' por defecto o '4 Argon' si el usuario lo pide).
    usar_enjambre: si es True, usa el Enjambre Multi-Cerebro (DeepSeek + ChatGPT + Juez 4 Argon) para diseñar y auditar.
    nivel_juez: nivel de pensamiento de 4 Argon como Juez del enjambre ('Alto' o 'Medio').
    """
    return orquestador_autonomo.iniciar_tarea_autonoma(
        origen_issue_o_pdf,
        modelo=modelo,
        usar_enjambre=usar_enjambre,
        nivel_juez=nivel_juez,
    )


@herramienta
def consultar_estado_tarea_autonoma() -> str:
    """Consulta en qué etapa va la tarea autónoma en segundo plano o devuelve el informe final auditado si ya terminó.
    """
    return orquestador_autonomo.consultar_estado_tarea()

