"""Enjambre Multi-Cerebro en Microsoft Edge (DeepSeek + ChatGPT + Juez en Gemini 4 Argon).

Reutiliza la conexión CDP en http://127.0.0.1:9222 para:
1. Consultar a DeepSeek (chat.deepseek.com) enfocado en auditoría lógica, casos borde,
   rendimiento SQL y detección de bugs ocultos.
2. Consultar a ChatGPT (chatgpt.com) enfocado en arquitectura limpia, buenas prácticas
   del framework y estructura de código mantenible.
3. Enviar ambas respuestas a Gemini 4 Argon (en nivel 'Alto' o 'Medio') como Juez Arquitecto
   Principal para contrastar propuestas, descartar errores y entregar la solución final verificada.
"""
import logging
import re
import time

from . import config, gemini_web, seguridad

log = logging.getLogger("jarvis")

INTERVALO_POLLING_ENJAMBRE = 0.25
SEGUNDOS_ESTABLE_ENJAMBRE = 0.6
SEGUNDOS_MAX_ESPECIALISTA = 120.0

URL_DEEPSEEK = "https://chat.deepseek.com"
URL_CHATGPT = "https://chatgpt.com"

SELECTORES_DEEPSEEK = {
    "entrada": [
        "textarea#chat-input",
        'textarea[placeholder*="DeepSeek" i]',
        "textarea",
        'div[contenteditable="true"]',
    ],
    "razonamiento_patron": re.compile(r"DeepThink|Pensamiento\s+profundo|R1", re.IGNORECASE),
    "razonamiento": [
        'button:has-text("DeepThink")',
        'button:has-text("Pensamiento profundo")',
        '[role="button"]:has-text("DeepThink")',
        '[role="button"]:has-text("Pensamiento profundo")',
    ],
    "respuesta": [
        ".ds-markdown",
        ".markdown-body",
        '[class*="ds-markdown"]',
    ],
    "detener": '[aria-label*="Stop" i], [aria-label*="Detener" i], [role="button"][aria-label*="stop" i]',
}

SELECTORES_CHATGPT = {
    "entrada": [
        "#prompt-textarea",
        'div[contenteditable="true"]#prompt-textarea',
        "textarea",
        'div[contenteditable="true"]',
    ],
    "razonamiento_patron": re.compile(r"\b(?:Reason|Pensar|Think|Razona(?:r)?)\b", re.IGNORECASE),
    "razonamiento": [
        'button[aria-label*="Reason" i]',
        'button[aria-label*="Pensar" i]',
        'button[aria-label*="Think" i]',
        'button:has-text("Reason")',
        'button:has-text("Pensar")',
        'button:has-text("Think")',
    ],
    "respuesta": [
        '[data-message-author-role="assistant"] .markdown',
        '[data-message-author-role="assistant"]',
        ".markdown",
    ],
    "detener": (
        'button[data-testid="stop-button"], '
        'button[aria-label*="Stop" i], '
        'button[aria-label*="Detener" i]'
    ),
}


def _normalizar_nivel_juez(nivel: str) -> str:
    texto = (nivel or "").strip().lower()
    if "baj" in texto:
        return gemini_web.PENSAMIENTO_BAJO
    if "med" in texto:
        return gemini_web.PENSAMIENTO_MEDIO
    return gemini_web.PENSAMIENTO_ALTO


def _obtener_o_abrir_pestana(contexto_browser, dominio: str, url_destino: str):
    """Reutiliza una pestaña existente que coincida con el dominio o abre una nueva en el contexto."""
    existentes = [p for p in getattr(contexto_browser, "pages", []) if dominio in (getattr(p, "url", "") or "")]
    pagina = existentes[0] if existentes else contexto_browser.new_page()
    url_actual = getattr(pagina, "url", "") or ""
    if dominio not in url_actual:
        pagina.goto(url_destino, wait_until="domcontentloaded")
    return pagina


def _sesion_invalida(pagina) -> bool:
    url = (getattr(pagina, "url", "") or "").lower()
    return any(p in url for p in ("login", "signin", "sign_in", "auth", "accounts.google.com"))


def _primero_visible(pagina, selectores: list[str], timeout_ms: int = 5000):
    limite = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < limite:
        for selector in selectores:
            try:
                loc = pagina.locator(selector)
                if loc.count() and loc.last.is_visible():
                    return loc.last
            except Exception:
                continue
        time.sleep(INTERVALO_POLLING_ENJAMBRE)
    return None


def _boton_razonamiento_activo(boton) -> bool:
    """True si el botón de razonamiento (DeepThink / Reason / Pensar) ya está activado en el DOM."""
    try:
        if hasattr(boton, "get_attribute"):
            for attr in ("aria-pressed", "aria-checked", "data-is-selected"):
                val = (boton.get_attribute(attr) or "").strip().lower()
                if val == "true":
                    return True
            estado = (boton.get_attribute("data-state") or "").strip().lower()
            if estado in ("on", "active", "checked", "open"):
                return True
            clases = (boton.get_attribute("class") or "").strip().lower()
            if any(token in clases for token in ("active", "selected", "checked", "pressed")):
                return True
    except Exception:
        pass
    return False


def _boton_deshabilitado(boton) -> bool:
    """True si el botón está deshabilitado (por ejemplo, por límite de cuota gratuita)."""
    try:
        if hasattr(boton, "get_attribute"):
            if (boton.get_attribute("aria-disabled") or "").strip().lower() == "true":
                return True
            if boton.get_attribute("disabled") is not None:
                return True
        if hasattr(boton, "is_disabled") and callable(boton.is_disabled) and boton.is_disabled():
            return True
    except Exception:
        pass
    return False


def _activar_razonamiento_si_disponible(pagina, patron_nombre, selectores_css: list[str]) -> bool:
    """Busca el botón de razonamiento y lo activa si está visible, habilitado y aún no activo."""
    candidato = None
    try:
        if hasattr(pagina, "get_by_role"):
            por_rol = pagina.get_by_role("button", name=patron_nombre)
            if por_rol.count() and por_rol.last.is_visible():
                candidato = por_rol.last
    except Exception:
        candidato = None

    if candidato is None:
        for selector in selectores_css:
            try:
                loc = pagina.locator(selector)
                if loc.count() and loc.last.is_visible():
                    candidato = loc.last
                    break
            except Exception:
                continue

    if candidato is None:
        return False
    if _boton_deshabilitado(candidato) or _boton_razonamiento_activo(candidato):
        return False
    try:
        try:
            candidato.click(timeout=2000)
        except TypeError:
            candidato.click()
        return True
    except Exception:
        return False


def _esperar_respuesta_especialista(
    pagina,
    selectores_respuesta: list[str],
    selector_detener: str,
    cantidad_previa: int,
    timeout: float = SEGUNDOS_MAX_ESPECIALISTA,
) -> str:
    """Espera con polling de 0.25s y corta apenas el texto se mantiene estable por 0.6s sin generación activa."""
    selector_principal = selectores_respuesta[0]
    limite = time.monotonic() + timeout
    while time.monotonic() < limite and pagina.locator(selector_principal).count() <= cantidad_previa:
        time.sleep(INTERVALO_POLLING_ENJAMBRE)

    ultimo = ""
    estable_desde = time.monotonic()
    while time.monotonic() < limite:
        try:
            generando = pagina.locator(selector_detener).count() > 0
        except Exception:
            generando = False
        loc = pagina.locator(selector_principal)
        texto = loc.last.inner_text() if loc.count() else ""
        if texto != ultimo:
            ultimo = texto
            estable_desde = time.monotonic()
        elif texto and not generando and (time.monotonic() - estable_desde) >= SEGUNDOS_ESTABLE_ENJAMBRE:
            return texto.strip()
        time.sleep(INTERVALO_POLLING_ENJAMBRE)
    return ultimo.strip()


def _consultar_deepseek(contexto_browser, problema: str) -> str:
    """Consulta a DeepSeek en chat.deepseek.com pidiendo auditoría lógica, casos borde, SQL y bugs ocultos."""
    try:
        pagina = _obtener_o_abrir_pestana(contexto_browser, "deepseek.com", URL_DEEPSEEK)
        if _sesion_invalida(pagina):
            log.warning("DeepSeek no tiene sesión iniciada en Edge; se omite su opinión.")
            return ""
        entrada = _primero_visible(pagina, SELECTORES_DEEPSEEK["entrada"], timeout_ms=5000)
        if entrada is None:
            log.warning("No se encontró el cuadro de texto de DeepSeek.")
            return ""
        prompt = (
            "Actuá como Especialista en Auditoría Lógica, Rendimiento SQL y Casos Borde. "
            "Analizá el siguiente problema, detectá bugs ocultos, cuellos de botella o fallos de concurrencia "
            "y proponé la solución técnica exacta:\n\n"
            f"{problema}"
        )
        selector_resp = SELECTORES_DEEPSEEK["respuesta"][0]
        previas = pagina.locator(selector_resp).count()
        _activar_razonamiento_si_disponible(
            pagina,
            SELECTORES_DEEPSEEK["razonamiento_patron"],
            SELECTORES_DEEPSEEK["razonamiento"],
        )
        entrada.click()
        entrada.fill(prompt)
        pagina.keyboard.press("Enter")
        return _esperar_respuesta_especialista(
            pagina,
            SELECTORES_DEEPSEEK["respuesta"],
            SELECTORES_DEEPSEEK["detener"],
            previas,
        )
    except Exception as e:
        log.warning("Fallo no bloqueante al consultar DeepSeek: %s", e)
        return ""


def _consultar_chatgpt(contexto_browser, problema: str) -> str:
    """Consulta a ChatGPT en chatgpt.com pidiendo arquitectura limpia, buenas prácticas y mantenibilidad."""
    try:
        pagina = _obtener_o_abrir_pestana(contexto_browser, "chatgpt.com", URL_CHATGPT)
        if _sesion_invalida(pagina):
            log.warning("ChatGPT no tiene sesión iniciada en Edge; se omite su opinión.")
            return ""
        entrada = _primero_visible(pagina, SELECTORES_CHATGPT["entrada"], timeout_ms=5000)
        if entrada is None:
            log.warning("No se encontró el cuadro de texto de ChatGPT.")
            return ""
        prompt = (
            "Actuá como Arquitecto de Software Especialista en Clean Architecture y Buenas Prácticas del Framework. "
            "Analizá el siguiente problema y proponé una estructura de código limpia, desacoplada y mantenible:\n\n"
            f"{problema}"
        )
        selector_resp = SELECTORES_CHATGPT["respuesta"][0]
        previas = pagina.locator(selector_resp).count()
        _activar_razonamiento_si_disponible(
            pagina,
            SELECTORES_CHATGPT["razonamiento_patron"],
            SELECTORES_CHATGPT["razonamiento"],
        )
        entrada.click()
        entrada.fill(prompt)
        pagina.keyboard.press("Enter")
        return _esperar_respuesta_especialista(
            pagina,
            SELECTORES_CHATGPT["respuesta"],
            SELECTORES_CHATGPT["detener"],
            previas,
        )
    except Exception as e:
        log.warning("Fallo no bloqueante al consultar ChatGPT: %s", e)
        return ""


class ClienteEnjambreWeb:
    """Coordina a los especialistas (DeepSeek y ChatGPT) y al Juez Supremo en Gemini 4 Argon."""

    def __init__(self, cliente_gemini: gemini_web.ClienteGeminiWeb | None = None):
        self._cliente_gemini = cliente_gemini

    def _obtener_cliente_gemini(self) -> gemini_web.ClienteGeminiWeb:
        if self._cliente_gemini is not None:
            return self._cliente_gemini
        if gemini_web._cliente is None:
            gemini_web._cliente = gemini_web.ClienteGeminiWeb()
        return gemini_web._cliente

    def _contexto_browser(self):
        cliente = self._obtener_cliente_gemini()
        cliente._conectar()
        nav = cliente._navegador
        if nav is None:
            return None
        return nav.contexts[0] if getattr(nav, "contexts", None) else nav.new_context()

    def consultar_enjambre(
        self,
        consulta: str,
        contexto_pantalla: str = "",
        modelo_juez: str = gemini_web.MODELO_ARGON,
        nivel_juez: str = gemini_web.PENSAMIENTO_ALTO,
    ) -> str:
        contexto_limpio = (contexto_pantalla or "").strip()
        consulta_limpia = (consulta or "").strip()
        problema = (
            f"CONTEXTO EN PANTALLA:\n{contexto_limpio}\n\nPROBLEMA / CONSULTA:\n{consulta_limpia}"
            if contexto_limpio
            else consulta_limpia
        )

        opinion_deepseek = ""
        opinion_chatgpt = ""
        try:
            ctx = self._contexto_browser()
            if ctx is not None:
                opinion_deepseek = _consultar_deepseek(ctx, problema)
                opinion_chatgpt = _consultar_chatgpt(ctx, problema)
        except Exception as e:
            log.warning("No se pudo abrir contexto de especialistas del enjambre: %s", e)

        bloque_ds = opinion_deepseek or "(DeepSeek no disponible o sin sesión activa en Edge)"
        bloque_gpt = opinion_chatgpt or "(ChatGPT no disponible o sin sesión activa en Edge)"

        prompt_sintesis = (
            "Actuá como JUEZ ARQUITECTO PRINCIPAL. Dos modelos especialistas analizaron el mismo problema técnico.\n"
            "Tu tarea es contrastar críticamente ambas propuestas, descartar cualquier error lógico, alucinación "
            "o mala práctica, y redactar la SOLUCIÓN FINAL DEFINITIVA Y VERIFICADA.\n\n"
            f"=== PROBLEMA ORIGINAL ===\n{problema}\n\n"
            f"=== PROPUESTA ESPECIALISTA 1 (DeepSeek - Lógica, Casos Borde y SQL) ===\n{bloque_ds}\n\n"
            f"=== PROPUESTA ESPECIALISTA 2 (ChatGPT - Arquitectura Limpia y Framework) ===\n{bloque_gpt}\n\n"
            "Entregá el veredicto técnico directo, el código o comandos exactos corregidos y los pasos numerados (1, 2, 3)."
        )

        nivel_normalizado = _normalizar_nivel_juez(nivel_juez)
        modelo_final = (modelo_juez or gemini_web.MODELO_ARGON).strip() or gemini_web.MODELO_ARGON
        cliente_gemini = self._obtener_cliente_gemini()
        veredicto = cliente_gemini.preguntar(
            prompt_sintesis,
            modelo=modelo_final,
            pensamiento=nivel_normalizado,
            chat_nuevo=False,
        )
        return seguridad.ocultar_secretos(
            f"[Enjambre Multi-Cerebro · Juez {modelo_final} ({nivel_normalizado})]\n{veredicto}"
        )


_cliente_enjambre: ClienteEnjambreWeb | None = None


def _ejecutar_enjambre_en_hilo(
    consulta: str,
    contexto_pantalla: str,
    modelo_juez: str,
    nivel_juez: str,
) -> str:
    global _cliente_enjambre
    if _cliente_enjambre is None:
        _cliente_enjambre = ClienteEnjambreWeb()
    return _cliente_enjambre.consultar_enjambre(
        consulta=consulta,
        contexto_pantalla=contexto_pantalla,
        modelo_juez=modelo_juez,
        nivel_juez=nivel_juez,
    )


def consultar_enjambre_multicerebro(
    consulta: str,
    contexto_pantalla: str = "",
    modelo_juez: str = gemini_web.MODELO_ARGON,
    nivel_juez: str = gemini_web.PENSAMIENTO_ALTO,
    timeout: float = gemini_web.SEGUNDOS_MAX_RESPUESTA + 120,
) -> str:
    """Ejecuta el Enjambre Multi-Cerebro en el hilo dedicado de Playwright y devuelve el veredicto del Juez."""
    consulta_limpia = (consulta or "").strip()
    if not consulta_limpia:
        return "Indicá la consulta o problema técnico para someter al Enjambre Multi-Cerebro."
    return gemini_web._EJECUTOR.submit(
        _ejecutar_enjambre_en_hilo,
        consulta_limpia,
        contexto_pantalla,
        modelo_juez,
        nivel_juez,
    ).result(timeout=timeout)

