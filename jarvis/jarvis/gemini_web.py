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

MODELO_NEO = "Neo"
MODELO_POR_DEFECTO = MODELO_NEO
MODELO_INVESTIGACION = MODELO_NEO
MODELO_ARGON = MODELO_NEO  # alias de compatibilidad: "Argon" / "4 Argon" -> "Neo"
MODELOS_GRATUITOS_RAPIDOS = ("Flash-Lite", "Flash")
PENSAMIENTO_ALTO = "Alto"
PENSAMIENTO_MEDIO = "Medio"
PENSAMIENTO_BAJO = "Bajo"
PENSAMIENTO_POR_DEFECTO = PENSAMIENTO_MEDIO

_PATRON_NEO = re.compile(
    r"\b(?:4\s*argon|argon\s*4|argon|4\s*neo|neo\s*4|neo|us[aá]\s+(?:argon|neo))\b",
    re.IGNORECASE,
)
_PATRON_ARGON = _PATRON_NEO
_PATRON_ARGON_ALTO = re.compile(r"\balt[oa]\b", re.IGNORECASE)
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


def normalizar_nombre_modelo(modelo: str | None) -> str:
    """Normaliza alias de modelo: 'argon', '4 argon' y 'neo' resuelven siempre a 'Neo'."""
    texto = (modelo or "").strip()
    if not texto:
        return MODELO_POR_DEFECTO
    minuscula = re.sub(r"\s+", " ", texto.lower())
    if minuscula in ("argon", "4 argon", "argon 4", "neo", "4 neo", "neo 4") or "argon" in minuscula or "neo" in minuscula:
        return MODELO_NEO
    return texto


def _pensamiento_argon(texto: str, por_defecto: str = PENSAMIENTO_MEDIO) -> str:
    """Devuelve 'Alto', 'Bajo' o 'Medio' para Neo (o alias Argon) según el pedido (o por_defecto='Medio')."""
    if _PATRON_INVESTIGACION.search(texto or "") or _PATRON_ARGON_ALTO.search(texto or ""):
        return PENSAMIENTO_ALTO
    if _PATRON_ARGON_BAJO.search(texto or ""):
        return PENSAMIENTO_BAJO
    if _PATRON_ARGON_MEDIO.search(texto or ""):
        return PENSAMIENTO_MEDIO
    return por_defecto


_pensamiento_neo = _pensamiento_argon


@dataclass(frozen=True)
class Ruta:
    modelo: str
    pensamiento: str
    chat_nuevo: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "modelo", normalizar_nombre_modelo(self.modelo))


def elegir_modelo(pedido: str) -> Ruta:
    """Decide modelo, nivel de pensamiento y si hace falta un chat nuevo según el pedido.

    - Por defecto (Hypervisor): modelo='Neo', pensamiento='Medio'.
    - Investigación profunda: pensamiento='Alto'.
    - Si el usuario especifica explícitamente 'bajo', 'medio' o 'alto' con Neo/Argon, se respeta.
    """
    texto = pedido or ""
    chat_nuevo = bool(_PATRON_CHAT_NUEVO.search(texto))
    if _PATRON_INVESTIGACION.search(texto):
        return Ruta(MODELO_INVESTIGACION, PENSAMIENTO_ALTO, chat_nuevo=chat_nuevo)
    if _PATRON_NEO.search(texto):
        return Ruta(MODELO_NEO, _pensamiento_neo(texto, por_defecto=PENSAMIENTO_MEDIO), chat_nuevo=chat_nuevo)
    return Ruta(MODELO_POR_DEFECTO, PENSAMIENTO_POR_DEFECTO, chat_nuevo=chat_nuevo)


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
    "pensamiento_borrador": [
        "model-thoughts",
        "thinking-process",
        ".model-thoughts",
        '[data-test-id*="thought" i]',
    ],
    "respuesta": ["model-response message-content", "model-response", ".model-response-text"],
    "pie_respuesta_final": (
        "model-response message-actions button, "
        'model-response button[aria-label*="Copiar" i], '
        'model-response button[aria-label*="Copy" i], '
        'model-response button[aria-label*="Me gusta" i], '
        'model-response button[aria-label*="Good response" i], '
        'model-response button[aria-label*="Regenerar" i]'
    ),
    "iniciar_investigacion": re.compile(r"iniciar\s+investigaci[oó]n|start\s+research", re.IGNORECASE),
    "detener": 'button[aria-label*="Detener" i], button[aria-label*="Stop" i]',
    "nombres_modelo": re.compile(r"Neo|Hydrogen|3\.1\s*Pro|4\s*Argon|Argon|Flash-Lite|Flash", re.IGNORECASE),
    "menu_modelo": [
        'button:has-text("Neo")',
        'button:has-text("Hydrogen")',
        'button:has-text("3.1 Pro")',
        'button:has-text("Argon")',
        'button:has-text("Flash-Lite")',
        '[role="button"]:has-text("Neo")',
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
SEGUNDOS_ESTABLE = 0.4
SEGUNDOS_ESTABLE_JS = 0.4
INTERVALO_POLLING_RESPUESTA = 0.2
SEGUNDOS_ESPERA_PRECALENTAR = 4.0
TIMEOUT_SELECTOR_RAPIDO_MS = 600
TIMEOUT_BOTON_MODELO_RAPIDO_MS = 300
MAX_CARACTERES_REPORTE_LARGO = 24_000

FLAGS_ANTI_THROTTLING_EDGE = (
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
)


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
        *FLAGS_ANTI_THROTTLING_EDGE,
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
        self._selector_entrada_cache: str | None = None
        self._cdp_activo_en_pagina: bool = False

    @property
    def _modelo_actual(self) -> str | None:
        return self._ultimo_modelo

    @_modelo_actual.setter
    def _modelo_actual(self, valor: str | None) -> None:
        self._ultimo_modelo = normalizar_nombre_modelo(valor) if valor is not None else None
        if valor is None:
            self._ultimo_pensamiento = None
        elif self._ultimo_pensamiento is None:
            self._ultimo_pensamiento = PENSAMIENTO_POR_DEFECTO

    @property
    def _pensamiento_actual(self) -> str | None:
        return self._ultimo_pensamiento

    @_pensamiento_actual.setter
    def _pensamiento_actual(self, valor: str | None) -> None:
        self._ultimo_pensamiento = valor

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
        self._selector_entrada_cache = None
        self._cdp_activo_en_pagina = False

    def _desactivar_throttling_cdp(self, pagina) -> None:
        """Envía Page.enable y Page.setWebLifecycleState='active' por CDP para evitar throttling en fondo."""
        if self._cdp_activo_en_pagina or pagina is None:
            return
        try:
            ctx = getattr(pagina, "context", None)
            if ctx is not None and hasattr(ctx, "new_cdp_session"):
                sesion_cdp = ctx.new_cdp_session(pagina)
                sesion_cdp.send("Page.enable")
                sesion_cdp.send("Page.setWebLifecycleState", {"state": "active"})
                self._cdp_activo_en_pagina = True
        except Exception as e:
            log.debug("No se pudo fijar WebLifecycleState por CDP (%s: %s)", type(e).__name__, e)

    def _pagina_principal(self):
        if self._pagina is not None and not self._pagina.is_closed():
            if "gemini.google.com" not in (self._pagina.url or ""):
                self._pagina.goto(config.GEMINI_WEB_URL, wait_until="domcontentloaded")
            self._verificar_sesion(self._pagina)
            self._desactivar_throttling_cdp(self._pagina)
            return self._pagina
        contexto = self._navegador.contexts[0] if self._navegador.contexts else self._navegador.new_context()
        existentes = [p for p in contexto.pages if "gemini.google.com" in (p.url or "")]
        self._pagina = existentes[0] if existentes else contexto.new_page()
        self._cdp_activo_en_pagina = False
        if "gemini.google.com" not in (self._pagina.url or ""):
            self._pagina.goto(config.GEMINI_WEB_URL, wait_until="domcontentloaded")
        self._verificar_sesion(self._pagina)
        self._desactivar_throttling_cdp(self._pagina)
        return self._pagina

    @staticmethod
    def _verificar_sesion(pagina) -> None:
        if "accounts.google.com" in (pagina.url or ""):
            raise ErrorGeminiWeb(
                f"Gemini pide iniciar sesión. Entrá con tu cuenta en la ventana de Edge del perfil "
                f"{config.EDGE_PERFIL_JARVIS} y volvé a pedírmelo.")

    @staticmethod
    def _primero_visible(pagina, selectores: list[str], timeout_ms: int = TIMEOUT_SELECTOR_RAPIDO_MS):
        limite = time.monotonic() + timeout_ms / 1000
        while True:
            for selector in selectores:
                try:
                    loc = pagina.locator(selector)
                    if loc.count() > 0 and loc.last.is_visible():
                        return loc.last
                except Exception:
                    continue
            if time.monotonic() >= limite:
                break
            time.sleep(0.05)
        return None

    def _buscar_entrada(self, pagina, timeout_ms: int = TIMEOUT_SELECTOR_RAPIDO_MS):
        """Busca el cuadro de texto probando primero el selector cacheado con verificación instantánea."""
        cache = self._selector_entrada_cache
        if cache:
            try:
                loc = pagina.locator(cache)
                if loc.count() > 0 and loc.last.is_visible():
                    return loc.last
            except Exception:
                pass
            self._selector_entrada_cache = None

        limite = time.monotonic() + timeout_ms / 1000
        while True:
            for selector in SELECTORES["entrada"]:
                try:
                    loc = pagina.locator(selector)
                    if loc.count() > 0 and loc.last.is_visible():
                        self._selector_entrada_cache = selector
                        return loc.last
                except Exception:
                    continue
            if time.monotonic() >= limite:
                break
            time.sleep(0.05)
        return None

    # -- acciones de interfaz
    def _nuevo_chat(self, pagina) -> None:
        for rol in ("link", "button"):
            boton = pagina.get_by_role(rol, name=SELECTORES["nuevo_chat"])
            try:
                if boton.count() > 0:
                    boton.first.click(timeout=TIMEOUT_SELECTOR_RAPIDO_MS)
                    pagina.wait_for_timeout(300)
                    self._modelo_actual = None
                    return
            except Exception:
                continue
        pagina.goto(config.GEMINI_WEB_URL, wait_until="domcontentloaded")
        self._modelo_actual = None

    def _buscar_boton_modelo(self, pagina, timeout_ms: int = TIMEOUT_BOTON_MODELO_RAPIDO_MS):
        """Ubica el botón selector de modelo en <= 300 ms con chequeos instantáneos count() > 0."""
        patron = SELECTORES["nombres_modelo"]
        limite = time.monotonic() + timeout_ms / 1000
        while True:
            # 1) Búsqueda por rol button + nombre accesible (Hydrogen, 3.1 Pro, Argon, Flash-Lite)
            if hasattr(pagina, "get_by_role"):
                try:
                    por_rol = pagina.get_by_role("button", name=patron)
                    if por_rol.count() > 0 and por_rol.last.is_visible():
                        return por_rol.last
                except Exception:
                    pass
            # 2) Búsqueda por filtro de texto visible dentro de button / [role='button']
            try:
                base = pagina.locator("button, [role='button']")
                if hasattr(base, "filter"):
                    filtrado = base.filter(has_text=patron)
                    if filtrado.count() > 0 and filtrado.last.is_visible():
                        return filtrado.last
            except Exception:
                pass
            # 3) Selectores CSS / :has-text directos configurados en SELECTORES["menu_modelo"]
            for selector in SELECTORES["menu_modelo"]:
                try:
                    loc = pagina.locator(selector)
                    if loc.count() > 0 and loc.last.is_visible():
                        return loc.last
                except Exception:
                    continue
            if time.monotonic() >= limite:
                break
            time.sleep(0.05)
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
        """Busca y clica una opción del menú con timeout rápido (600 ms)."""
        patron = re.compile(re.escape(texto), re.IGNORECASE)
        vio_deshabilitada = False
        for rol in ("menuitemradio", "menuitem", "option", "radio", "button"):
            opcion = pagina.get_by_role(rol, name=patron)
            try:
                if opcion.count() > 0:
                    objetivo = opcion.last if rol == "button" else opcion.first
                    if self._elemento_deshabilitado(objetivo):
                        vio_deshabilitada = True
                        continue
                    objetivo.click(timeout=TIMEOUT_SELECTOR_RAPIDO_MS)
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
            objetivo.click(timeout=TIMEOUT_SELECTOR_RAPIDO_MS)
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
        objetivo = normalizar_nombre_modelo(modelo_objetivo).lower()
        texto_min = texto.lower()
        if objetivo in texto_min:
            return True
        if objetivo == "neo" and "argon" in texto_min:
            return True
        partes = [p for p in objetivo.split() if not p.isdigit()]
        return bool(partes) and all(p in texto_min for p in partes)

    def _seleccionar_primer_modelo_disponible(self, pagina) -> str | None:
        """Selecciona el primer modelo habilitado disponible en el menú desplegable como fallback."""
        for rol in ("menuitemradio", "menuitem", "option"):
            try:
                opciones = pagina.get_by_role(rol)
                if hasattr(opciones, "count") and opciones.count() > 0:
                    primero = opciones.first
                    if not self._elemento_deshabilitado(primero):
                        primero.click(timeout=TIMEOUT_SELECTOR_RAPIDO_MS)
                        for attr in ("inner_text", "text_content"):
                            fn = getattr(primero, attr, None)
                            if callable(fn):
                                try:
                                    txt = (fn() or "").strip()
                                    if txt:
                                        return txt.splitlines()[0].strip()
                                except Exception:
                                    pass
                        return str(getattr(primero, "nombre", "") or MODELO_POR_DEFECTO)
            except Exception:
                continue
        for candidato in (*MODELOS_GRATUITOS_RAPIDOS, "4 Hydrogen", "3.1 Pro", "Argon"):
            if self._intentar_opcion(pagina, candidato) == "seleccionada":
                return candidato
        return None

    def _seleccionar_modelo(self, pagina, ruta: Ruta) -> None:
        modelo_objetivo = normalizar_nombre_modelo(ruta.modelo)
        # Fast-Path instantáneo (<= 300 ms): si _modelo_actual y _pensamiento_actual ya coinciden
        # y el botón visible confirma el modelo (o no requiere abrir menú), salta todos los menús.
        menu = self._buscar_boton_modelo(pagina, timeout_ms=TIMEOUT_BOTON_MODELO_RAPIDO_MS)
        if (
            self._modelo_actual == modelo_objetivo
            and self._pensamiento_actual == ruta.pensamiento
            and (menu is None or self._boton_coincide_con_modelo(menu, modelo_objetivo))
        ):
            return
        if menu is None:
            log.warning("No encontré el selector de modelo de Gemini; sigo con el modelo actual.")
            return
        menu.click()
        estado = self._intentar_opcion(pagina, modelo_objetivo)
        if estado == "no_encontrada" and modelo_objetivo == MODELO_NEO:
            # Coincidencia parcial / compatibilidad si el DOM aún rotula la opción como Argon
            estado = self._intentar_opcion(pagina, "Argon")
        if estado == "deshabilitada":
            log.warning(
                "El modelo '%s' está deshabilitado por límite de cuota; cambio al modelo rápido gratuito.",
                modelo_objetivo,
            )
            modelo_respaldo = None
            for rapido in MODELOS_GRATUITOS_RAPIDOS:
                if self._intentar_opcion(pagina, rapido) == "seleccionada":
                    modelo_respaldo = rapido
                    break
            if modelo_respaldo is None:
                modelo_respaldo = self._seleccionar_primer_modelo_disponible(pagina)
            pagina.keyboard.press("Escape")
            self._ultimo_modelo = modelo_respaldo or MODELOS_GRATUITOS_RAPIDOS[0]
            self._ultimo_pensamiento = None
            return
        if estado != "seleccionada":
            modelo_fallback = self._seleccionar_primer_modelo_disponible(pagina)
            log.warning(
                "No encontré el modelo '%s' en el menú de Gemini; usando fallback '%s'.",
                modelo_objetivo,
                modelo_fallback or "actual",
            )
            if not self._seleccionar_opcion(pagina, ruta.pensamiento):
                log.info("No encontré el nivel de pensamiento '%s'; queda el del modelo.", ruta.pensamiento)
            pagina.keyboard.press("Escape")
            self._ultimo_modelo = normalizar_nombre_modelo(modelo_fallback) if modelo_fallback else modelo_objetivo
            self._ultimo_pensamiento = ruta.pensamiento
            return
        # Ajusta explícitamente el nivel de pensamiento requerido (Medio por defecto para Neo; Alto para investigación profunda).
        if not self._seleccionar_opcion(pagina, ruta.pensamiento):
            log.info("No encontré el nivel de pensamiento '%s'; queda el del modelo.", ruta.pensamiento)
        pagina.keyboard.press("Escape")
        self._ultimo_modelo = modelo_objetivo
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

    _SCRIPT_ESPERAR_RESPUESTA = """
    ({ respSelectors, thoughtSelectors, footerSelector, stopSelector, previas, estableMs, timeoutMs }) => new Promise((resolve) => {
        const limpiarPensamiento = (nodo) => {
            try {
                const clon = nodo.cloneNode(true);
                for (const tSel of (thoughtSelectors || [])) {
                    clon.querySelectorAll(tSel).forEach((el) => el.remove());
                }
                return (clon.innerText || clon.textContent || "").trim();
            } catch (_) {
                return (nodo.innerText || nodo.textContent || "").trim();
            }
        };
        const clicarIniciarInvestigacion = () => {
            try {
                const botones = document.querySelectorAll('button, [role="button"]');
                for (const b of botones) {
                    const t = (b.innerText || b.textContent || "").trim();
                    if (/iniciar\\s+investigaci[oó]n|start\\s+research/i.test(t)) {
                        b.click();
                    }
                }
            } catch (_) {}
        };
        const leerTexto = () => {
            for (const sel of respSelectors) {
                try {
                    const lista = document.querySelectorAll(sel);
                    if (lista.length > previas || (sel !== respSelectors[0] && lista.length > 0)) {
                        const ultimo = lista[lista.length - 1];
                        const txt = limpiarPensamiento(ultimo);
                        if (txt) return txt;
                    }
                } catch (_) {}
            }
            return "";
        };
        const estaGenerando = () => {
            try {
                return Boolean(document.querySelector(stopSelector));
            } catch (_) {
                return false;
            }
        };
        const tienePieFinal = () => {
            if (!footerSelector) return false;
            try {
                const respuestas = document.querySelectorAll("model-response");
                if (respuestas.length <= previas) return false;
                const ultima = respuestas[respuestas.length - 1];
                return Boolean(
                    ultima.querySelector(
                        'message-actions button, button[aria-label*="Copiar" i], button[aria-label*="Copy" i], button[aria-label*="Me gusta" i], button[aria-label*="Good response" i]'
                    )
                );
            } catch (_) {
                return false;
            }
        };

        let ultimoTexto = "";
        let timerEstable = null;
        let observer = null;
        let timerGlobal = null;
        let finalizado = false;

        const cerrar = (valor) => {
            if (finalizado) return;
            finalizado = true;
            if (observer) observer.disconnect();
            if (timerEstable) clearTimeout(timerEstable);
            if (timerGlobal) clearTimeout(timerGlobal);
            resolve(valor);
        };

        const revisar = () => {
            clicarIniciarInvestigacion();
            const txt = leerTexto();
            const gen = estaGenerando();
            if (txt !== ultimoTexto) {
                ultimoTexto = txt;
                if (timerEstable) {
                    clearTimeout(timerEstable);
                    timerEstable = null;
                }
            }
            if (txt && !gen) {
                const espera = tienePieFinal() ? Math.min(estableMs, 200) : estableMs;
                if (!timerEstable) {
                    timerEstable = setTimeout(() => {
                        const finalTxt = leerTexto() || ultimoTexto;
                        if (!estaGenerando() && finalTxt) {
                            cerrar(finalTxt);
                        } else {
                            timerEstable = null;
                        }
                    }, espera);
                }
            } else if (timerEstable) {
                clearTimeout(timerEstable);
                timerEstable = null;
            }
        };

        const raiz = document.body || document.documentElement;
        if (!raiz) {
            cerrar(null);
            return;
        }
        observer = new MutationObserver(revisar);
        observer.observe(raiz, { childList: true, subtree: true, characterData: true, attributes: true });
        timerGlobal = setTimeout(() => cerrar(leerTexto() || ultimoTexto || null), timeoutMs);
        revisar();
    })
    """

    @staticmethod
    def _recortar_reporte_largo(texto: str, max_chars: int = MAX_CARACTERES_REPORTE_LARGO) -> str:
        """Si es un reporte muy extenso, prioriza los bloques superiores y conclusiones."""
        limpio = (texto or "").strip()
        if len(limpio) <= max_chars:
            return limpio
        mitad_superior = int(max_chars * 0.75)
        mitad_inferior = max_chars - mitad_superior
        return (
            limpio[:mitad_superior].rstrip()
            + "\n\n[... reporte extenso resumido ...]\n\n"
            + limpio[-mitad_inferior:].lstrip()
        )

    def _auto_clicar_iniciar_investigacion(self, pagina) -> None:
        """Si Gemini 3.1 Pro muestra el botón 'Iniciar investigación' / 'Start research', lo pulsa al instante."""
        try:
            if hasattr(pagina, "get_by_role"):
                btn = pagina.get_by_role("button", name=SELECTORES["iniciar_investigacion"])
                if btn.count() > 0 and btn.last.is_visible():
                    btn.last.click(timeout=TIMEOUT_SELECTOR_RAPIDO_MS)
        except Exception:
            pass

    def _esperar_respuesta_js(
        self, pagina, cantidad_previa: int, timeout: float = SEGUNDOS_MAX_RESPUESTA
    ) -> str | None:
        """Usa MutationObserver nativo dentro de Edge (ignorando model-thoughts) sin viajes CDP de polling."""
        fn_evaluate = getattr(pagina, "evaluate", None)
        if not callable(fn_evaluate):
            return None
        try:
            resultado = fn_evaluate(
                self._SCRIPT_ESPERAR_RESPUESTA,
                {
                    "respSelectors": SELECTORES["respuesta"],
                    "thoughtSelectors": SELECTORES["pensamiento_borrador"],
                    "footerSelector": SELECTORES["pie_respuesta_final"],
                    "stopSelector": SELECTORES["detener"],
                    "previas": cantidad_previa,
                    "estableMs": int(SEGUNDOS_ESTABLE_JS * 1000),
                    "timeoutMs": int(timeout * 1000),
                },
            )
            if isinstance(resultado, str) and resultado.strip():
                return self._recortar_reporte_largo(resultado.strip())
        except Exception as e:
            log.debug("Observador JS no disponible, uso polling en Python (%s: %s)", type(e).__name__, e)
        return None

    def _esperar_respuesta(
        self, pagina, cantidad_previa: int, timeout: float = SEGUNDOS_MAX_RESPUESTA
    ) -> str:
        resultado_js = self._esperar_respuesta_js(pagina, cantidad_previa, timeout=timeout)
        if resultado_js:
            return resultado_js

        selector = SELECTORES["respuesta"][0]
        selector_pie = SELECTORES["pie_respuesta_final"]
        limite = time.monotonic() + timeout
        while time.monotonic() < limite and pagina.locator(selector).count() <= cantidad_previa:
            self._auto_clicar_iniciar_investigacion(pagina)
            time.sleep(INTERVALO_POLLING_RESPUESTA)
        ultimo, estable_desde = "", time.monotonic()
        while time.monotonic() < limite:
            self._auto_clicar_iniciar_investigacion(pagina)
            generando = pagina.locator(SELECTORES["detener"]).count() > 0
            loc = pagina.locator(selector)
            texto = loc.last.inner_text() if loc.count() > 0 else ""
            try:
                hay_pie_final = pagina.locator(selector_pie).count() > cantidad_previa
            except Exception:
                hay_pie_final = False
            if texto != ultimo:
                ultimo, estable_desde = texto, time.monotonic()
            elif texto and not generando and (
                (time.monotonic() - estable_desde) >= SEGUNDOS_ESTABLE
                or (hay_pie_final and (time.monotonic() - estable_desde) >= 0.2)
            ):
                return self._recortar_reporte_largo(texto.strip())
            time.sleep(INTERVALO_POLLING_RESPUESTA)
        if ultimo.strip():
            return self._recortar_reporte_largo(ultimo.strip())
        # Rescate de cualquier texto parcial visible en selectores alternativos del DOM
        for sel_alt in SELECTORES["respuesta"]:
            try:
                loc_alt = pagina.locator(sel_alt)
                if loc_alt.count() > 0:
                    parcial = (loc_alt.last.inner_text() or "").strip()
                    if parcial:
                        return self._recortar_reporte_largo(parcial)
            except Exception:
                continue
        raise ErrorGeminiWeb("Gemini no respondió a tiempo.")

    # -- API
    def precalentar(self, ruta: Ruta | None = None) -> bool:
        """Conecta con Edge y deja lista la pestaña de Gemini y el modelo por defecto en segundo plano."""
        try:
            self._conectar()
            pagina = self._pagina_principal()
            ruta_objetivo = ruta or Ruta(MODELO_POR_DEFECTO, PENSAMIENTO_POR_DEFECTO, chat_nuevo=False)
            self._seleccionar_modelo(pagina, ruta_objetivo)
            self._cerrar_banners_cuota(pagina)
            self._buscar_entrada(pagina, timeout_ms=1500)
            return True
        except Exception as e:
            log.warning("Precalentamiento de Gemini web omitido (%s: %s)", type(e).__name__, e)
            return False

    def preguntar(
        self,
        mensaje: str,
        ruta: Ruta | None = None,
        imagen: Path | None = None,
        *,
        modelo: str | None = None,
        pensamiento: str | None = None,
        chat_nuevo: bool = False,
    ) -> str:
        if ruta is None:
            ruta = Ruta(
                normalizar_nombre_modelo(modelo or MODELO_POR_DEFECTO),
                pensamiento or PENSAMIENTO_POR_DEFECTO,
                chat_nuevo=chat_nuevo,
            )
        self._conectar()
        pagina = self._pagina_principal()
        if ruta.chat_nuevo:
            self._nuevo_chat(pagina)
        self._seleccionar_modelo(pagina, ruta)
        self._cerrar_banners_cuota(pagina)
        if imagen is not None:
            self._adjuntar_imagen(pagina, imagen)
        entrada = self._buscar_entrada(pagina)
        if entrada is None:
            # Rescate: solo trae la pestaña al frente si no respondió en segundo plano.
            try:
                pagina.bring_to_front()
            except Exception:
                pass
            pagina.keyboard.press("Escape")
            entrada = self._buscar_entrada(pagina, timeout_ms=2000)
        if entrada is None:
            raise ErrorGeminiWeb("No encontré el cuadro de texto de Gemini (¿cambió la interfaz?).")
        previas = pagina.locator(SELECTORES["respuesta"][0]).count()
        try:
            entrada.click()
            entrada.fill(mensaje)
        except Exception:
            # No reenviar si el mensaje ya entró y generó respuesta nueva en el chat
            if pagina.locator(SELECTORES["respuesta"][0]).count() <= previas:
                try:
                    pagina.bring_to_front()
                except Exception:
                    pass
                self._selector_entrada_cache = None
                entrada = self._buscar_entrada(pagina, timeout_ms=TIMEOUT_SELECTOR_RAPIDO_MS) or entrada
                entrada.click()
                entrada.fill(mensaje)
        pagina.keyboard.press("Enter")
        return self._esperar_respuesta(pagina, previas)


_EJECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-gemini-web")
_cliente: ClienteGeminiWeb | None = None


def _precalentar_en_hilo() -> bool:
    global _cliente
    time.sleep(SEGUNDOS_ESPERA_PRECALENTAR)
    if _cliente is None:
        _cliente = ClienteGeminiWeb()
    return _cliente.precalentar()


def precalentar_puente() -> None:
    """Encola el precalentamiento de Gemini web en Edge sin bloquear al invocador."""
    if not config.GEMINI_WEB_ACTIVO:
        return
    try:
        _EJECUTOR.submit(_precalentar_en_hilo)
    except Exception as e:
        log.warning("No se pudo iniciar precalentamiento de Gemini web (%s: %s)", type(e).__name__, e)


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

