"""Acciones que Jarvis puede hacer en tu computadora.

Para agregar una acción nueva: escribí una función con @herramienta, tipos en los
parámetros y un docstring. La primera línea describe la acción; cada línea
"parametro: descripción" explica un parámetro. La IA la va a poder usar sola.
"""
import ctypes
import datetime
import inspect
import json
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import time
import urllib.parse
import webbrowser
from pathlib import Path

from . import config
from .seguridad import confirmar, es_peligroso, es_seguro

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
    log.info("Herramienta %s %s", nombre, argumentos)
    try:
        return str(funcion(**argumentos))
    except TypeError as e:
        return f"Error en los parámetros: {e}"
    except Exception as e:  # la IA recibe el error y puede intentar otra cosa
        log.exception("Falló %s", nombre)
        return f"Error al ejecutar {nombre}: {e}"


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
