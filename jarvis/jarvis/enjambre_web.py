"""Enjambre Multi-Cerebro en Microsoft Edge (DeepSeek + ChatGPT + OpenRouter + Juez en Gemini Neo).

Reutiliza la conexión CDP en http://127.0.0.1:9222 para:
1. Consultar a DeepSeek (chat.deepseek.com) enfocado en auditoría lógica, casos borde,
   rendimiento SQL y detección de bugs ocultos.
2. Consultar a ChatGPT (chatgpt.com) enfocado en arquitectura limpia, buenas prácticas
   del framework y estructura de código mantenible.
3. Enviar las respuestas a Gemini Neo (en nivel 'Medio' por defecto) como Juez Arquitecto
   Principal para contrastar propuestas, descartar errores y entregar la solución final verificada.
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, gemini_web, openrouter_api, seguridad

log = logging.getLogger("jarvis")

INTERVALO_POLLING_ENJAMBRE = 0.25
INTERVALO_MULTIPLEXADO = 0.3
SEGUNDOS_ESTABLE_ENJAMBRE = 0.6
SEGUNDOS_MAX_ESPECIALISTA = 40.0
TIMEOUT_COMPARTIDO_ESPECIALISTAS = 40.0
TIMEOUT_ENJAMBRE_TOTAL = 180.0

_ESTADO_ENJAMBRE: dict = {
    "estado": "inactivo",  # "inactivo", "en_progreso", "completado", "error"
    "consulta": "",
    "etapa": "",
    "resultado": "",
    "resultados": {},
}
_CACHE_ENTRADA_ESPECIALISTA: dict[str, str] = {}

INSTRUCCION_RESUMEN_EXACTO = (
    "Usá tu máximo razonamiento interno, pero redactá como salida ÚNICAMENTE un resumen técnico "
    "exacto y denso (máximo 250 palabras, en puntos clave y código esencial, sin saludos ni relleno) "
    "con lo que se debe hacer."
)

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
    if "alt" in texto:
        return gemini_web.PENSAMIENTO_ALTO
    return gemini_web.PENSAMIENTO_MEDIO


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


def _primero_visible(
    pagina,
    selectores: list[str],
    timeout_ms: int = 5000,
    clave_cache: str | None = None,
):
    if clave_cache:
        cache = _CACHE_ENTRADA_ESPECIALISTA.get(clave_cache)
        if cache:
            try:
                loc = pagina.locator(cache)
                if loc.count() and loc.last.is_visible():
                    return loc.last
            except Exception:
                pass
            _CACHE_ENTRADA_ESPECIALISTA.pop(clave_cache, None)

    limite = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < limite:
        for selector in selectores:
            try:
                loc = pagina.locator(selector)
                if loc.count() and loc.last.is_visible():
                    if clave_cache:
                        _CACHE_ENTRADA_ESPECIALISTA[clave_cache] = selector
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


def _leer_texto_parcial_dom(pagina, selectores_respuesta: list[str]) -> str:
    """Lee cualquier texto parcial que el especialista ya haya volcado en el DOM ante un timeout."""
    if pagina is None:
        return ""
    for selector in selectores_respuesta:
        try:
            loc = pagina.locator(selector)
            if loc.count():
                texto = (loc.last.inner_text() or "").strip()
                if texto:
                    return texto
        except Exception:
            continue
    return ""


def _esperar_respuesta_especialista(
    pagina,
    selectores_respuesta: list[str],
    selector_detener: str,
    cantidad_previa: int,
    timeout: float = SEGUNDOS_MAX_ESPECIALISTA,
) -> str:
    """Espera con polling de 0.25s (máx 45s) y si supera el timeout devuelve el texto parcial del DOM."""
    if pagina is None:
        return ""
    selector_principal = selectores_respuesta[0]
    limite = time.monotonic() + timeout
    try:
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
        if ultimo.strip():
            return ultimo.strip()
    except Exception as e:
        log.warning("Interrupción esperando respuesta del especialista (%s: %s); leyendo parcial.", type(e).__name__, e)
    return _leer_texto_parcial_dom(pagina, selectores_respuesta)


def _preparar_y_enviar_deepseek(contexto_browser, problema: str) -> tuple[object | None, int]:
    """Abre/prepara la pestaña de DeepSeek, activa DeepThink y dispara el prompt sin esperar respuesta."""
    try:
        pagina = _obtener_o_abrir_pestana(contexto_browser, "deepseek.com", URL_DEEPSEEK)
        if _sesion_invalida(pagina):
            log.warning("DeepSeek no tiene sesión iniciada en Edge; se omite su opinión.")
            return None, 0
        entrada = _primero_visible(
            pagina, SELECTORES_DEEPSEEK["entrada"], timeout_ms=5000, clave_cache="deepseek"
        )
        if entrada is None:
            log.warning("No se encontró el cuadro de texto de DeepSeek.")
            return None, 0
        prompt = (
            "Actuá como Especialista en Auditoría Lógica, Rendimiento SQL y Casos Borde. "
            "Analizá el siguiente problema, detectá bugs ocultos, cuellos de botella o fallos de concurrencia "
            "y proponé la solución técnica exacta.\n"
            f"{INSTRUCCION_RESUMEN_EXACTO}\n\n"
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
        return pagina, previas
    except Exception as e:
        log.warning("Fallo al enviar prompt a DeepSeek (%s: %s)", type(e).__name__, e)
        return None, 0


def _recolectar_respuesta_deepseek(
    pagina, previas: int, timeout: float = SEGUNDOS_MAX_ESPECIALISTA
) -> str:
    if pagina is None:
        return ""
    try:
        return _esperar_respuesta_especialista(
            pagina,
            SELECTORES_DEEPSEEK["respuesta"],
            SELECTORES_DEEPSEEK["detener"],
            previas,
            timeout=timeout,
        )
    except Exception as e:
        log.warning("Fallo al recolectar DeepSeek (%s: %s)", type(e).__name__, e)
        return _leer_texto_parcial_dom(pagina, SELECTORES_DEEPSEEK["respuesta"])


def _preparar_y_enviar_chatgpt(contexto_browser, problema: str) -> tuple[object | None, int]:
    """Abre/prepara la pestaña de ChatGPT, activa Reason/Pensar y dispara el prompt sin esperar respuesta."""
    try:
        pagina = _obtener_o_abrir_pestana(contexto_browser, "chatgpt.com", URL_CHATGPT)
        if _sesion_invalida(pagina):
            log.warning("ChatGPT no tiene sesión iniciada en Edge; se omite su opinión.")
            return None, 0
        entrada = _primero_visible(
            pagina, SELECTORES_CHATGPT["entrada"], timeout_ms=5000, clave_cache="chatgpt"
        )
        if entrada is None:
            log.warning("No se encontró el cuadro de texto de ChatGPT.")
            return None, 0
        prompt = (
            "Actuá como Arquitecto de Software Especialista en Clean Architecture y Buenas Prácticas del Framework. "
            "Analizá el siguiente problema y proponé una estructura de código limpia, desacoplada y mantenible.\n"
            f"{INSTRUCCION_RESUMEN_EXACTO}\n\n"
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
        return pagina, previas
    except Exception as e:
        log.warning("Fallo al enviar prompt a ChatGPT (%s: %s)", type(e).__name__, e)
        return None, 0


def _recolectar_respuesta_chatgpt(
    pagina, previas: int, timeout: float = SEGUNDOS_MAX_ESPECIALISTA
) -> str:
    if pagina is None:
        return ""
    try:
        return _esperar_respuesta_especialista(
            pagina,
            SELECTORES_CHATGPT["respuesta"],
            SELECTORES_CHATGPT["detener"],
            previas,
            timeout=timeout,
        )
    except Exception as e:
        log.warning("Fallo al recolectar ChatGPT (%s: %s)", type(e).__name__, e)
        return _leer_texto_parcial_dom(pagina, SELECTORES_CHATGPT["respuesta"])


def _consultar_deepseek(
    contexto_browser, problema: str, timeout: float = SEGUNDOS_MAX_ESPECIALISTA
) -> str:
    """Consulta a DeepSeek en chat.deepseek.com pidiendo auditoría lógica, casos borde, SQL y bugs ocultos."""
    pagina, previas = _preparar_y_enviar_deepseek(contexto_browser, problema)
    return _recolectar_respuesta_deepseek(pagina, previas, timeout=timeout)


def _consultar_chatgpt(
    contexto_browser, problema: str, timeout: float = SEGUNDOS_MAX_ESPECIALISTA
) -> str:
    """Consulta a ChatGPT en chatgpt.com pidiendo arquitectura limpia, buenas prácticas y mantenibilidad."""
    pagina, previas = _preparar_y_enviar_chatgpt(contexto_browser, problema)
    return _recolectar_respuesta_chatgpt(pagina, previas, timeout=timeout)


def _recolectar_especialistas_multiplexado(
    page_ds,
    page_cg,
    previas_ds: int = 0,
    previas_cg: int = 0,
    timeout_compartido: float = TIMEOUT_COMPARTIDO_ESPECIALISTAS,
) -> tuple[str, str]:
    """Revisa DeepSeek y ChatGPT en un único bucle multiplexado de reloj compartido (40s máx)."""
    inicio = time.monotonic()
    sel_ds = SELECTORES_DEEPSEEK["respuesta"][0]
    sel_cg = SELECTORES_CHATGPT["respuesta"][0]
    det_ds = SELECTORES_DEEPSEEK["detener"]
    det_cg = SELECTORES_CHATGPT["detener"]

    listo_ds = page_ds is None
    listo_cg = page_cg is None
    ultimo_ds, ultimo_cg = "", ""
    estable_ds = inicio
    estable_cg = inicio

    while (time.monotonic() - inicio) < timeout_compartido:
        ahora = time.monotonic()

        if not listo_ds and page_ds is not None:
            try:
                loc_ds = page_ds.locator(sel_ds)
                if loc_ds.count() > previas_ds:
                    txt_ds = loc_ds.last.inner_text() or ""
                    try:
                        gen_ds = page_ds.locator(det_ds).count() > 0
                    except Exception:
                        gen_ds = False
                    if txt_ds != ultimo_ds:
                        ultimo_ds, estable_ds = txt_ds, ahora
                    elif txt_ds.strip() and not gen_ds and (ahora - estable_ds) >= SEGUNDOS_ESTABLE_ENJAMBRE:
                        ultimo_ds = txt_ds.strip()
                        listo_ds = True
            except Exception as e:
                log.warning("Error leyendo DeepSeek en bucle multiplexado (%s: %s)", type(e).__name__, e)
                listo_ds = True

        if not listo_cg and page_cg is not None:
            try:
                loc_cg = page_cg.locator(sel_cg)
                if loc_cg.count() > previas_cg:
                    txt_cg = loc_cg.last.inner_text() or ""
                    try:
                        gen_cg = page_cg.locator(det_cg).count() > 0
                    except Exception:
                        gen_cg = False
                    if txt_cg != ultimo_cg:
                        ultimo_cg, estable_cg = txt_cg, ahora
                    elif txt_cg.strip() and not gen_cg and (ahora - estable_cg) >= SEGUNDOS_ESTABLE_ENJAMBRE:
                        ultimo_cg = txt_cg.strip()
                        listo_cg = True
            except Exception as e:
                log.warning("Error leyendo ChatGPT en bucle multiplexado (%s: %s)", type(e).__name__, e)
                listo_cg = True

        if listo_ds and listo_cg:
            break
        time.sleep(INTERVALO_MULTIPLEXADO)

    final_ds = ultimo_ds.strip() or _leer_texto_parcial_dom(page_ds, SELECTORES_DEEPSEEK["respuesta"])
    final_cg = ultimo_cg.strip() or _leer_texto_parcial_dom(page_cg, SELECTORES_CHATGPT["respuesta"])
    return final_ds, final_cg


_consultar_deepseek_original = _consultar_deepseek
_consultar_chatgpt_original = _consultar_chatgpt


class ClienteEnjambreWeb:
    """Coordina a los especialistas (DeepSeek, ChatGPT y OpenRouter) y al Juez Supremo en Gemini Neo."""

    def __init__(self, cliente_gemini: gemini_web.ClienteGeminiWeb | None = None):
        self._cliente_gemini = cliente_gemini
        self.ultimos_resultados: dict[str, str] = {}

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

    def precalentar_especialistas(self) -> dict[str, bool]:
        """Abre/reutiliza pestañas de DeepSeek y ChatGPT y deja activado el razonamiento máximo."""
        resultados = {"deepseek": False, "chatgpt": False}
        try:
            ctx = self._contexto_browser()
        except Exception as e:
            log.warning("No se pudo conectar para precalentar especialistas (%s: %s)", type(e).__name__, e)
            return resultados
        if ctx is None:
            return resultados

        especialistas = (
            ("deepseek", "deepseek.com", URL_DEEPSEEK, SELECTORES_DEEPSEEK),
            ("chatgpt", "chatgpt.com", URL_CHATGPT, SELECTORES_CHATGPT),
        )
        for nombre, dominio, url, selectores in especialistas:
            try:
                pagina = _obtener_o_abrir_pestana(ctx, dominio, url)
                if _sesion_invalida(pagina):
                    continue
                entrada = _primero_visible(
                    pagina, selectores["entrada"], timeout_ms=3000, clave_cache=nombre
                )
                if entrada is None:
                    continue
                _activar_razonamiento_si_disponible(
                    pagina, selectores["razonamiento_patron"], selectores["razonamiento"]
                )
                resultados[nombre] = True
            except Exception as e:
                log.warning("Precalentamiento de %s omitido (%s: %s)", nombre, type(e).__name__, e)
        return resultados

    def consultar_enjambre(
        self,
        consulta: str,
        contexto_pantalla: str = "",
        modelo_juez: str = gemini_web.MODELO_NEO,
        nivel_juez: str = gemini_web.PENSAMIENTO_MEDIO,
    ) -> str:
        contexto_limpio = (contexto_pantalla or "").strip()
        consulta_limpia = (consulta or "").strip()
        problema = (
            f"CONTEXTO EN PANTALLA:\n{contexto_limpio}\n\nPROBLEMA / CONSULTA:\n{consulta_limpia}"
            if contexto_limpio
            else consulta_limpia
        )

        # Lanza OpenRouter en un hilo en paralelo mientras corren DeepSeek y ChatGPT en Edge
        futuro_openrouter = None
        pool_openrouter = None
        if openrouter_api.openrouter_configurado():
            try:
                pool_openrouter = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="jarvis-openrouter"
                )
                futuro_openrouter = pool_openrouter.submit(
                    openrouter_api.consultar_openrouter, problema
                )
            except Exception as e:
                log.warning(
                    "No se pudo lanzar el hilo de OpenRouter (%s: %s)", type(e).__name__, e
                )

        opinion_deepseek = ""
        opinion_chatgpt = ""
        try:
            ctx = self._contexto_browser()
            if ctx is not None:
                if (
                    _consultar_deepseek is not _consultar_deepseek_original
                    or _consultar_chatgpt is not _consultar_chatgpt_original
                ):
                    opinion_deepseek = _consultar_deepseek(ctx, problema)
                    opinion_chatgpt = _consultar_chatgpt(ctx, problema)
                else:
                    # 1) Dispara el prompt en ambas pestañas de forma consecutiva inmediata
                    # para que DeepSeek y ChatGPT razonen al mismo tiempo en paralelo.
                    _ESTADO_ENJAMBRE["etapa"] = "Enviando prompts en paralelo a DeepSeek y ChatGPT"
                    pagina_ds, previas_ds = _preparar_y_enviar_deepseek(ctx, problema)
                    pagina_gpt, previas_gpt = _preparar_y_enviar_chatgpt(ctx, problema)
                    # 2) Recolecta ambas respuestas bajo UN SOLO reloj compartido de 40s.
                    _ESTADO_ENJAMBRE["etapa"] = "Esperando deliberación paralela de DeepSeek y ChatGPT (máx 40s)"
                    opinion_deepseek, opinion_chatgpt = _recolectar_especialistas_multiplexado(
                        pagina_ds,
                        pagina_gpt,
                        previas_ds=previas_ds,
                        previas_cg=previas_gpt,
                        timeout_compartido=TIMEOUT_COMPARTIDO_ESPECIALISTAS,
                    )
        except Exception as e:
            log.warning(
                "No se pudo completar la fase de especialistas del enjambre (%s: %s)",
                type(e).__name__,
                e,
            )

        opinion_openrouter = ""
        if futuro_openrouter is not None:
            try:
                resp_or = futuro_openrouter.result(timeout=openrouter_api.TIMEOUT_OPENROUTER + 2.0)
                if not openrouter_api.es_error_openrouter(resp_or):
                    opinion_openrouter = (resp_or or "").strip()
            except Exception as e:
                log.warning("OpenRouter no completó a tiempo (%s: %s)", type(e).__name__, e)
            finally:
                if pool_openrouter is not None:
                    pool_openrouter.shutdown(wait=False)

        resultados_enjambre: dict[str, str] = {
            "deepseek": opinion_deepseek,
            "chatgpt": opinion_chatgpt,
        }
        if opinion_openrouter:
            resultados_enjambre["openrouter"] = opinion_openrouter
        self.ultimos_resultados = resultados_enjambre
        _ESTADO_ENJAMBRE["resultados"] = dict(resultados_enjambre)

        bloque_ds = opinion_deepseek or "(DeepSeek no disponible o sin sesión activa en Edge)"
        bloque_gpt = opinion_chatgpt or "(ChatGPT no disponible o sin sesión activa en Edge)"
        bloque_or = (
            f"=== RESUMEN EXACTO 3 (OpenRouter - {openrouter_api.obtener_modelo()}) ===\n"
            f"{opinion_openrouter}\n\n"
            if opinion_openrouter
            else ""
        )

        prompt_sintesis = (
            "Actuá como JUEZ ARQUITECTO PRINCIPAL. Los modelos especialistas del enjambre (con máximo razonamiento activo) "
            "redactaron sus resúmenes técnicos exactos sobre el mismo problema.\n"
            "Analizá todos los resúmenes, cruzá sus puntos fuertes, descartá cualquier contradicción o error "
            "y entregá la RESPUESTA FINAL DEFINITIVA Y VERIFICADA.\n\n"
            f"=== PROBLEMA ORIGINAL ===\n{problema}\n\n"
            f"=== RESUMEN EXACTO 1 (DeepSeek - Lógica, Casos Borde y SQL) ===\n{bloque_ds}\n\n"
            f"=== RESUMEN EXACTO 2 (ChatGPT - Arquitectura Limpia y Framework) ===\n{bloque_gpt}\n\n"
            f"{bloque_or}"
            "Entregá el veredicto técnico directo, el código o comandos exactos corregidos y los pasos numerados (1, 2, 3)."
        )

        nivel_normalizado = _normalizar_nivel_juez(nivel_juez)
        modelo_final = gemini_web.normalizar_nombre_modelo(modelo_juez or gemini_web.MODELO_NEO)
        _ESTADO_ENJAMBRE["etapa"] = f"Sintetizando veredicto con Juez {modelo_final} ({nivel_normalizado})"
        cliente_gemini = self._obtener_cliente_gemini()
        try:
            veredicto = cliente_gemini.preguntar(
                prompt_sintesis,
                modelo=modelo_final,
                pensamiento=nivel_normalizado,
                chat_nuevo=False,
            )
            return seguridad.ocultar_secretos(
                f"[Enjambre Multi-Cerebro · Juez {modelo_final} ({nivel_normalizado})]\n{veredicto}"
            )
        except Exception as e:
            log.warning(
                "Juez %s (%s) no respondió a tiempo (%s: %s); fusionando especialistas.",
                modelo_final,
                nivel_normalizado,
                type(e).__name__,
                e,
            )
            if opinion_deepseek or opinion_chatgpt or opinion_openrouter:
                rescate = (
                    f"[Enjambre Multi-Cerebro · Rescate directo de Especialistas]\n"
                    f"[DEEPSEEK]\n{bloque_ds}\n\n"
                    f"[CHATGPT]\n{bloque_gpt}"
                )
                if opinion_openrouter:
                    rescate += f"\n\n[OPENROUTER ({openrouter_api.obtener_modelo()})]\n{opinion_openrouter}"
                return seguridad.ocultar_secretos(rescate)
            raise


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
    modelo_juez: str = gemini_web.MODELO_NEO,
    nivel_juez: str = gemini_web.PENSAMIENTO_MEDIO,
    timeout: float = TIMEOUT_ENJAMBRE_TOTAL,
) -> str:
    """Ejecuta el Enjambre Multi-Cerebro en el hilo dedicado de Playwright (timeout 120s) y devuelve el veredicto."""
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

