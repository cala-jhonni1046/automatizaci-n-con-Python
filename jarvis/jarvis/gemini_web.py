"""Puente con la app web de Gemini (gemini.google.com) en Microsoft Edge vía Playwright + CDP.

Jarvis se conecta al Edge que ya tenés abierto con depuración remota (o lo abre él con un perfil
propio) y usa tu cuenta de Gemini Pro como cerebro principal de razonamiento:

- "4 Hydrogen" (pensamiento Alto): todo por defecto, reutilizando SIEMPRE la misma pestaña para
  mantener el hilo de la charla.
- "3.1 Pro" (pensamiento Alto): solo para "investigación profunda/exacta" o "investigá a fondo",
  en un chat nuevo para no mezclar contextos.
- "4 Argon": nunca por defecto; solo si el pedido lo nombra explícitamente.

IMPORTANTE: la app web no es una API. Los selectores dependen de la interfaz de Gemini y pueden
cambiar; están todos juntos en SELECTORES para ajustarlos en un solo lugar.

Playwright (API síncrona) solo puede usarse desde el hilo que lo creó. Como las herramientas de
Jarvis corren en hilos distintos (asyncio.to_thread), todo el trabajo con el navegador pasa por un
único hilo dedicado (_EJECUTOR).
"""
import logging
import re
import shutil
import socket
import subprocess
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import config

log = logging.getLogger("jarvis")

# ---------------------------------------------------------------- enrutamiento de modelos

MODELO_POR_DEFECTO = "4 Hydrogen"
MODELO_INVESTIGACION = "3.1 Pro"
MODELO_ARGON = "4 Argon"
MODELOS_GRATUITOS_RAPIDOS = ("Flash-Lite", "Flash")
PENSAMIENTO_ALTO = "Alto"
PENSAMIENTO_MEDIO = "Medio"
PENSAMIENTO_BAJO = "Bajo"

_PATRON_ARGON = re.compile(r"\b(?:4\s*argon|argon\s*4|us[aá]\s+argon)\b", re.IGNORECASE)
_PATRON_ARGON_BAJO = re.compile(r"\bbaj[oa]\b", re.IGNORECASE)
_PATRON_ARGON_MEDIO = re.compile(r"\bmedi[oa]\b", re.IGNORECASE)
_PATRON_INVESTIGACION = re.compile(
    r"investigaci[oó]n\s+(profunda|exacta)|investig[aá]\s+a\s+fondo|auditor[ií]a\s+profunda",
    re.IGNORECASE,
)
_PATRON_CHAT_NUEVO = re.compile(
    r"\b(?:en\s+un\s+)?chat\s+nuevo\b|\bnuevo\s+chat\b|\bempez[aá]\s+de\s+cero\b|"
    r"\blimpi[aá]\s+el\s+chat\b|\bnuevo\s+tema\b",
    re.IGNORECASE,
)


def _pensamiento_argon(texto: str) -> str:
    """Devuelve 'Bajo' o 'Medio' para 4 Argon (nunca 'Alto', para ahorrar tokens)."""
    if _PATRON_ARGON_BAJO.search(texto or ""):
        return PENSAMIENTO_BAJO
    if _PATRON_ARGON_MEDIO.search(texto or ""):
        return PENSAMIENTO_MEDIO
    return PENSAMIENTO_MEDIO


@dataclass(frozen=True)
class Ruta:
    modelo: str
    pensamiento: str
    chat_nuevo: bool


def elegir_modelo(pedido: str) -> Ruta:
    """Decide modelo, nivel de pensamiento y si hace falta un chat nuevo según el pedido."""
    texto = pedido or ""
    chat_nuevo = bool(_PATRON_CHAT_NUEVO.search(texto))
    if _PATRON_ARGON.search(texto):
        return Ruta(MODELO_ARGON, _pensamiento_argon(texto), chat_nuevo=chat_nuevo)
    if _PATRON_INVESTIGACION.search(texto):
        return Ruta(MODELO_INVESTIGACION, PENSAMIENTO_ALTO, chat_nuevo=chat_nuevo)
    return Ruta(MODELO_POR_DEFECTO, PENSAMIENTO_ALTO, chat_nuevo=chat_nuevo)


# ---------------------------------------------------------------- síntesis para voz

_FENCE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)


def limpiar_para_voz(texto: str) -> str:
    """Quita el markdown que traba al TTS: bloques de código, títulos, negritas, tablas, enlaces."""
    if not texto:
        return ""
    limpio = _FENCE.sub(" (te dejé el código en la pantalla) ", texto)
    limpio = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", limpio)            # imágenes
    limpio = re.sub(r"\[([^\]]+)\]\((?:[^)]*)\)", r"\1", limpio)          # enlaces -> texto
    limpio = re.sub(r"`([^`]*)`", r"\1", limpio)                           # código en línea
    limpio = re.sub(r"^\s{0,3}#{1,6}\s*", "", limpio, flags=re.MULTILINE)  # títulos
    limpio = re.sub(r"^\s*>\s?", "", limpio, flags=re.MULTILINE)          # citas
    limpio = re.sub(r"^\s*\|?\s*:?-{3,}.*$", "", limpio, flags=re.MULTILINE)  # separadores de tabla
    limpio = re.sub(r"^\s*([-*_])\1{2,}\s*$", "", limpio, flags=re.MULTILINE)  # reglas horizontales
    limpio = limpio.replace("|", ", ")
    limpio = re.sub(r"(\*\*|__)(.+?)\1", r"\2", limpio)                    # negrita
    limpio = re.sub(r"(?<![\w*])[*_](?!\s)(.+?)(?<!\s)[*_](?![\w*])", r"\1", limpio)  # cursiva
    limpio = re.sub(r"^\s*[-*+•]\s+", "", limpio, flags=re.MULTILINE)      # viñetas
    limpio = re.sub(r"^\s*(\d+)[.)]\s+", r"\1. ", limpio, flags=re.MULTILINE)
    limpio = re.sub(r"[*#~]", "", limpio)
    limpio = re.sub(r"[ \t]+", " ", limpio)
    limpio = re.sub(r"\s*\n\s*\n+", "\n", limpio)
    return limpio.strip()


# ---------------------------------------------------------------- selectores de la interfaz

SELECTORES = {
    "entrada": [
        'rich-textarea div.ql-editor[contenteditable="true"]',
        'div[contenteditable="true"][role="textbox"]',
        'div[contenteditable="true"]',
    ],
    "respuesta": ["model-response message-content", "model-response", ".model-response-text"],
    "detener": 'button[aria-label*="Detener" i], button[aria-label*="Stop" i]',
    "nombres_modelo": re.compile(r"Hydrogen|3\.1\s*Pro|4\s*Argon|Argon|Flash-Lite", re.IGNORECASE),
    "menu_modelo": [
        'button:has-text("Hydrogen")',
        'button:has-text("3.1 Pro")',
        'button:has-text("Argon")',
        'button:has-text("Flash-Lite")',
        '[role="button"]:has-text("Hydrogen")',
        '[role="button"]:has-text("3.1 Pro")',
        '[role="button"]:has-text("Argon")',
        '[role="button"]:has-text("Flash-Lite")',
        '[data-test-id="bard-mode-menu-button"]',
        'button[aria-label*="modelo" i]',
        'button[aria-label*="model" i]',
    ],
    "nuevo_chat": re.compile(r"nuevo chat|new chat", re.IGNORECASE),
    "subir": re.compile(r"subir|upload|agregar archivos|add files|añadir", re.IGNORECASE),
    "subir_archivos": re.compile(r"subir archivos|upload files|archivos|files", re.IGNORECASE),
}

SEGUNDOS_MAX_RESPUESTA = 300
SEGUNDOS_ESTABLE = 0.6
INTERVALO_POLLING_RESPUESTA = 0.25


class ErrorGeminiWeb(Exception):
    """Fallo al usar la app web de Gemini (Edge cerrado, sin sesión, interfaz cambiada...)."""


# ---------------------------------------------------------------- Edge con depuración remota

RUTAS_EDGE = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def _puerto_abierto(url_cdp: str) -> bool:
    partes = urllib.parse.urlparse(url_cdp)
    try:
        with socket.create_connection((partes.hostname or "127.0.0.1", partes.port or 9222), timeout=0.5):
            return True
    except OSError:
        return False


def _ruta_edge() -> str:
    encontrado = shutil.which("msedge")
    if encontrado:
        return encontrado
    for ruta in RUTAS_EDGE:
        if Path(ruta).is_file():
            return ruta
    raise ErrorGeminiWeb("No encontré msedge.exe. Instalá Microsoft Edge o agregalo al PATH.")


def lanzar_edge_si_hace_falta(url_cdp: str | None = None, segundos: float = 20.0) -> None:
    """Abre Edge con --remote-debugging-port y el perfil de Jarvis si el puerto CDP no responde."""
    url_cdp = url_cdp or config.GEMINI_WEB_CDP
    if _puerto_abierto(url_cdp):
        return
    puerto = urllib.parse.urlparse(url_cdp).port or 9222
    comando = [
        _ruta_edge(),
        f"--remote-debugging-port={puerto}",
        f"--user-data-dir={config.EDGE_PERFIL_JARVIS}",
        config.GEMINI_WEB_URL,
    ]
    log.info("Abriendo Edge para Gemini web: %s", comando)
    subprocess.Popen(comando)
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if _puerto_abierto(url_cdp):
            return
        time.sleep(0.5)
    raise ErrorGeminiWeb(
        f"Edge no abrió el puerto de depuración {puerto}. Si ya tenías Edge abierto con ese perfil, "
        "cerralo y dejá que Jarvis lo abra.")


# ---------------------------------------------------------------- cliente

class ClienteGeminiWeb:
    """Maneja la pestaña de Gemini. Usar SIEMPRE desde el hilo de _EJECUTOR (ver consultar())."""

    def __init__(self):
        self._pw = None
        self._navegador = None
        self._pagina = None          # pestaña principal reutilizable (hilo de la charla)
        self._ultimo_modelo: str | None = None
        self._ultimo_pensamiento: str | None = None

    @property
    def _modelo_actual(self) -> str | None:
        return self._ultimo_modelo

    @_modelo_actual.setter
    def _modelo_actual(self, valor: str | None) -> None:
        self._ultimo_modelo = valor
        if valor is None:
            self._ultimo_pensamiento = None

    # -- conexión
    def _conectar(self) -> None:
        if self._navegador is not None and self._navegador.is_connected():
            return
        from playwright.sync_api import sync_playwright

        lanzar_edge_si_hace_falta()
        if self._pw is None:
            self._pw = sync_playwright().start()
        self._navegador = self._pw.chromium.connect_over_cdp(config.GEMINI_WEB_CDP)
        self._pagina = None
        self._ultimo_modelo = None
        self._ultimo_pensamiento = None

    def _pagina_principal(self):
        if self._pagina is not None and not self._pagina.is_closed():
            if "gemini.google.com" not in (self._pagina.url or ""):
                self._pagina.goto(config.GEMINI_WEB_URL, wait_until="domcontentloaded")
            self._verificar_sesion(self._pagina)
            return self._pagina
        contexto = self._navegador.contexts[0] if self._navegador.contexts else self._navegador.new_context()
        existentes = [p for p in contexto.pages if "gemini.google.com" in (p.url or "")]
        self._pagina = existentes[0] if existentes else contexto.new_page()
        if "gemini.google.com" not in (self._pagina.url or ""):
            self._pagina.goto(config.GEMINI_WEB_URL, wait_until="domcontentloaded")
        self._verificar_sesion(self._pagina)
        return self._pagina

    @staticmethod
    def _verificar_sesion(pagina) -> None:
        if "accounts.google.com" in (pagina.url or ""):
            raise ErrorGeminiWeb(
                f"Gemini pide iniciar sesión. Entrá con tu cuenta en la ventana de Edge del perfil "
                f"{config.EDGE_PERFIL_JARVIS} y volvé a pedírmelo.")

    @staticmethod
    def _primero_visible(pagina, selectores: list[str], timeout_ms: int = 8000):
        limite = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < limite:
            for selector in selectores:
                loc = pagina.locator(selector)
                try:
                    if loc.count() and loc.last.is_visible():
                        return loc.last
                except Exception:
                    continue
            time.sleep(0.25)
        return None

    # -- acciones de interfaz
    def _nuevo_chat(self, pagina) -> None:
        for rol in ("link", "button"):
            boton = pagina.get_by_role(rol, name=SELECTORES["nuevo_chat"])
            try:
                if boton.count():
                    boton.first.click(timeout=3000)
                    pagina.wait_for_timeout(800)
                    self._modelo_actual = None
                    return
            except Exception:
                continue
        pagina.goto(config.GEMINI_WEB_URL, wait_until="domcontentloaded")
        self._modelo_actual = None

    def _buscar_boton_modelo(self, pagina, timeout_ms: int = 5000):
        """Ubica el botón selector de modelo (abajo a la derecha del cuadro de texto) por nombre visible."""
        patron = SELECTORES["nombres_modelo"]
        limite = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < limite:
            # 1) Búsqueda por rol button + nombre accesible (Hydrogen, 3.1 Pro, Argon, Flash-Lite)
            if hasattr(pagina, "get_by_role"):
                try:
                    por_rol = pagina.get_by_role("button", name=patron)
                    if por_rol.count() and por_rol.last.is_visible():
                        return por_rol.last
                except Exception:
                    pass
            # 2) Búsqueda por filtro de texto visible dentro de button / [role='button']
            try:
                base = pagina.locator("button, [role='button']")
                if hasattr(base, "filter"):
                    filtrado = base.filter(has_text=patron)
                    if filtrado.count() and filtrado.last.is_visible():
                        return filtrado.last
            except Exception:
                pass
            # 3) Selectores CSS / :has-text directos configurados en SELECTORES["menu_modelo"]
            for selector in SELECTORES["menu_modelo"]:
                try:
                    loc = pagina.locator(selector)
                    if loc.count() and loc.last.is_visible():
                        return loc.last
                except Exception:
                    continue
            time.sleep(0.25)
        return None

    @staticmethod
    def _elemento_deshabilitado(elemento) -> bool:
        """True si la opción del menú está deshabilitada por límite de cuota (aria-disabled o disabled)."""
        try:
            if hasattr(elemento, "get_attribute"):
                aria = (elemento.get_attribute("aria-disabled") or "").strip().lower()
                if aria == "true":
                    return True
                if elemento.get_attribute("disabled") is not None:
                    return True
            if hasattr(elemento, "is_disabled") and callable(elemento.is_disabled):
                if elemento.is_disabled():
                    return True
        except Exception:
            pass
        return False

    def _intentar_opcion(self, pagina, texto: str) -> str:
        """Busca y clica una opción del menú. Devuelve 'seleccionada', 'deshabilitada' o 'no_encontrada'."""
        patron = re.compile(re.escape(texto), re.IGNORECASE)
        vio_deshabilitada = False
        for rol in ("menuitemradio", "menuitem", "option", "radio", "button"):
            opcion = pagina.get_by_role(rol, name=patron)
            try:
                if opcion.count():
                    objetivo = opcion.last if rol == "button" else opcion.first
                    if self._elemento_deshabilitado(objetivo):
                        vio_deshabilitada = True
                        continue
                    objetivo.click(timeout=3000)
                    return "seleccionada"
            except Exception:
                continue
        if vio_deshabilitada:
            return "deshabilitada"
        try:
            por_texto = pagina.get_by_text(patron)
            if hasattr(por_texto, "count") and not por_texto.count():
                return "no_encontrada"
            objetivo = por_texto.last
            if self._elemento_deshabilitado(objetivo):
                return "deshabilitada"
            objetivo.click(timeout=2000)
            return "seleccionada"
        except Exception:
            return "no_encontrada"

    def _seleccionar_opcion(self, pagina, texto: str) -> bool:
        return self._intentar_opcion(pagina, texto) == "seleccionada"

    @staticmethod
    def _boton_coincide_con_modelo(menu, modelo_objetivo: str) -> bool:
        """Verifica si el texto visible del botón selector coincide con el modelo objetivo."""
        texto = ""
        for attr in ("inner_text", "text_content"):
            fn = getattr(menu, attr, None)
            if callable(fn):
                try:
                    texto = (fn() or "").strip()
                    if texto:
                        break
                except Exception:
                    pass
        if not texto:
            texto = str(getattr(menu, "nombre", "") or "")
        if not texto:
            return True
        objetivo = (modelo_objetivo or "").strip().lower()
        texto_min = texto.lower()
        if objetivo in texto_min:
            return True
        partes = [p for p in objetivo.split() if not p.isdigit()]
        return bool(partes) and all(p in texto_min for p in partes)

    def _seleccionar_modelo(self, pagina, ruta: Ruta) -> None:
        menu = self._buscar_boton_modelo(pagina, timeout_ms=5000)
        if menu is None:
            log.warning("No encontré el selector de modelo de Gemini; sigo con el modelo actual.")
            return
        # Fast-Path seguro: solo salta el menú si el botón visible coincide con el modelo objetivo
        # Y además tanto _ultimo_modelo como _ultimo_pensamiento coinciden exactamente.
        if (
            self._ultimo_modelo == ruta.modelo
            and self._ultimo_pensamiento == ruta.pensamiento
            and self._boton_coincide_con_modelo(menu, ruta.modelo)
        ):
            return
        menu.click()
        estado = self._intentar_opcion(pagina, ruta.modelo)
        if estado == "deshabilitada":
            log.warning(
                "El modelo '%s' está deshabilitado por límite de cuota; cambio al modelo rápido gratuito.",
                ruta.modelo,
            )
            modelo_respaldo = None
            for rapido in MODELOS_GRATUITOS_RAPIDOS:
                if self._intentar_opcion(pagina, rapido) == "seleccionada":
                    modelo_respaldo = rapido
                    break
            pagina.keyboard.press("Escape")
            self._ultimo_modelo = modelo_respaldo or MODELOS_GRATUITOS_RAPIDOS[0]
            self._ultimo_pensamiento = None
            return
        if estado != "seleccionada":
            pagina.keyboard.press("Escape")
            if ruta.modelo == MODELO_ARGON:
                raise ErrorGeminiWeb("No encontré '4 Argon' en el menú de modelos de Gemini.")
            log.warning("No encontré el modelo %s en el menú de Gemini.", ruta.modelo)
            return
        # Ajusta explícitamente el nivel de pensamiento requerido (Alto para Hydrogen/3.1 Pro; Bajo/Medio para Argon).
        if not self._seleccionar_opcion(pagina, ruta.pensamiento):
            log.info("No encontré el nivel de pensamiento '%s'; queda el del modelo.", ruta.pensamiento)
        pagina.keyboard.press("Escape")
        self._ultimo_modelo = ruta.modelo
        self._ultimo_pensamiento = ruta.pensamiento

    @staticmethod
    def _cerrar_banners_cuota(pagina) -> None:
        """Cierra banners o modales de límite de cuota antes de escribir en el cuadro de texto."""
        try:
            for selector in (
                '[role="dialog"] button[aria-label*="Cerrar" i]',
                '[role="dialog"] button[aria-label*="Close" i]',
                '[role="alertdialog"] button',
            ):
                loc = pagina.locator(selector)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=1500)
                    pagina.keyboard.press("Escape")
                    return
        except Exception:
            pass

    def _adjuntar_imagen(self, pagina, ruta_imagen: Path) -> None:
        entrada = pagina.locator('input[type="file"]')
        if entrada.count():
            entrada.first.set_input_files(str(ruta_imagen))
            return
        boton = pagina.get_by_role("button", name=SELECTORES["subir"])
        if not boton.count():
            raise ErrorGeminiWeb("No encontré el botón para subir archivos en Gemini.")
        boton.first.click()
        with pagina.expect_file_chooser(timeout=5000) as selector:
            if not self._seleccionar_opcion(pagina, "Subir archivos"):
                self._seleccionar_opcion(pagina, "Upload files")
        selector.value.set_files(str(ruta_imagen))

    def _esperar_respuesta(self, pagina, cantidad_previa: int) -> str:
        selector = SELECTORES["respuesta"][0]
        limite = time.monotonic() + SEGUNDOS_MAX_RESPUESTA
        while time.monotonic() < limite and pagina.locator(selector).count() <= cantidad_previa:
            time.sleep(INTERVALO_POLLING_RESPUESTA)
        ultimo, estable_desde = "", time.monotonic()
        while time.monotonic() < limite:
            generando = pagina.locator(SELECTORES["detener"]).count() > 0
            loc = pagina.locator(selector)
            texto = loc.last.inner_text() if loc.count() else ""
            if texto != ultimo:
                ultimo, estable_desde = texto, time.monotonic()
            elif texto and not generando and time.monotonic() - estable_desde >= SEGUNDOS_ESTABLE:
                return texto.strip()
            time.sleep(INTERVALO_POLLING_RESPUESTA)
        if ultimo:
            return ultimo.strip()
        raise ErrorGeminiWeb("Gemini no respondió a tiempo.")

    # -- API
    def preguntar(self, mensaje: str, ruta: Ruta, imagen: Path | None = None) -> str:
        self._conectar()
        pagina = self._pagina_principal()
        pagina.bring_to_front()
        if ruta.chat_nuevo:
            self._nuevo_chat(pagina)
        self._seleccionar_modelo(pagina, ruta)
        self._cerrar_banners_cuota(pagina)
        if imagen is not None:
            self._adjuntar_imagen(pagina, imagen)
        entrada = self._primero_visible(pagina, SELECTORES["entrada"])
        if entrada is None:
            pagina.keyboard.press("Escape")
            entrada = self._primero_visible(pagina, SELECTORES["entrada"], timeout_ms=2000)
        if entrada is None:
            raise ErrorGeminiWeb("No encontré el cuadro de texto de Gemini (¿cambió la interfaz?).")
        previas = pagina.locator(SELECTORES["respuesta"][0]).count()
        entrada.click()
        entrada.fill(mensaje)
        pagina.keyboard.press("Enter")
        return self._esperar_respuesta(pagina, previas)


_EJECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-gemini-web")
_cliente: ClienteGeminiWeb | None = None


def _preguntar_en_hilo(mensaje: str, ruta: Ruta, imagen: Path | None) -> str:
    global _cliente
    if _cliente is None:
        _cliente = ClienteGeminiWeb()
    try:
        return _cliente.preguntar(mensaje, ruta, imagen)
    except ErrorGeminiWeb:
        raise
    except Exception:
        # Conexión rota (Edge cerrado, pestaña perdida): un reintento con conexión nueva.
        _cliente = ClienteGeminiWeb()
        return _cliente.preguntar(mensaje, ruta, imagen)


def consultar(mensaje: str, ruta: Ruta | None = None, imagen: Path | None = None,
              timeout: float = SEGUNDOS_MAX_RESPUESTA + 60) -> str:
    """Manda el mensaje a Gemini web y devuelve la respuesta completa (markdown).

    Lanza ErrorGeminiWeb si no puede usar la app web.
    """
    ruta = ruta or elegir_modelo(mensaje)
    return _EJECUTOR.submit(_preguntar_en_hilo, mensaje, ruta, imagen).result(timeout=timeout)

