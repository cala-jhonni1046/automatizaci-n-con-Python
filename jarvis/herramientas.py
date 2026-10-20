"""Acciones que Jarvis puede hacer en tu computadora.

Para agregar una acción nueva: escribí una función con @herramienta, tipos en los
parámetros y un docstring. La primera línea describe la acción; cada línea
"parametro: descripción" explica un parámetro. La IA la va a poder usar sola.
"""
import datetime
import inspect
import json
import logging
import os
import platform
import re
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
        subprocess.Popen(["cmd", "/c", "start", "", objetivo])
        return f"Abrí {nombre}."
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
    _abrir_con_el_sistema(str(carpeta))
    return f"Abrí la carpeta {carpeta}."


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
    _abrir_con_el_sistema(str(archivo))
    return f"Abrí {archivo.name}."


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
    webbrowser.open(url)
    return f"Abrí {url}"


# ---------------------------------------------------------------- terminal

@herramienta
def ejecutar_comando(comando: str, en_linux: bool = False) -> str:
    """Ejecuta un comando en la terminal y devuelve lo que muestra. Los comandos que modifican algo se confirman con el usuario.
    comando: el comando exacto a ejecutar.
    en_linux: true para ejecutarlo en la terminal de Linux (WSL) cuando la computadora es Windows.
    """
    if not es_seguro(comando):
        aviso = "⚠️ COMANDO PELIGROSO. " if es_peligroso(comando) else ""
        if not confirmar(f"{aviso}¿Ejecuto este comando?  {comando}"):
            return "El usuario NO autorizó el comando. No lo ejecutes ni insistas."
    if en_linux and ES_WINDOWS:
        argumentos, usar_shell = ["wsl", "bash", "-lc", comando], False
    else:
        argumentos, usar_shell = comando, True
    try:
        proceso = subprocess.run(argumentos, shell=usar_shell, capture_output=True,
                                 text=True, timeout=120, errors="replace")
    except subprocess.TimeoutExpired:
        return "El comando tardó más de 2 minutos y lo corté."
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
