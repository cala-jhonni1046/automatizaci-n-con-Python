"""Tests unitarios para el Enjambre Multi-Cerebro (enjambre_web.py):
DeepSeek + ChatGPT + Juez Supremo en Gemini 4 Argon (niveles Alto y Medio).
"""
from types import SimpleNamespace

import pytest

import vision
from jarvis import config, enjambre_web, gemini_web, herramientas, openrouter_api, orquestador_autonomo


@pytest.fixture(autouse=True)
def limpiar_estado_enjambre(monkeypatch):
    """Garantiza estado limpio de _ESTADO_ENJAMBRE y caché entre pruebas."""
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    enjambre_web._ESTADO_ENJAMBRE.update({
        "estado": "inactivo",
        "consulta": "",
        "etapa": "",
        "resultado": "",
        "resultados": {},
    })
    enjambre_web._CACHE_ENTRADA_ESPECIALISTA.clear()
    yield
    enjambre_web._ESTADO_ENJAMBRE.update({
        "estado": "inactivo",
        "consulta": "",
        "etapa": "",
        "resultado": "",
        "resultados": {},
    })
    enjambre_web._CACHE_ENTRADA_ESPECIALISTA.clear()


# ---------------------------------------------------------------- 1. Especialistas (DeepSeek y ChatGPT)

def _crear_contexto_especialista(
    url_inicial: str,
    respuesta_modelo: str,
    relleno_capturado: list,
    atributos_razonamiento: dict | None = None,
):
    attrs_razon = atributos_razonamiento if atributos_razonamiento is not None else {"aria-pressed": "false"}

    class BotonRazonamientoFalso:
        def __init__(self):
            self.last = self
            self.first = self

        def count(self):
            return 1

        def is_visible(self):
            return True

        def get_attribute(self, nombre):
            return attrs_razon.get(nombre)

        def is_disabled(self):
            return attrs_razon.get("aria-disabled") == "true" or "disabled" in attrs_razon

        def click(self, timeout=None):
            relleno_capturado.append("CLICK_RAZONAMIENTO")

    class EntradaFalsa:
        last = None

        def __init__(self):
            self.last = self

        def count(self):
            return 1

        def is_visible(self):
            return True

        def click(self):
            pass

        def fill(self, texto):
            relleno_capturado.append(texto)

    class RespuestaLocator:
        def __init__(self, llamadas):
            self.llamadas = llamadas
            self.last = self

        def count(self):
            # Primera llamada (previas) devuelve 0; luego devuelve 1
            self.llamadas[0] += 1
            return 0 if self.llamadas[0] == 1 else 1

        def inner_text(self):
            return respuesta_modelo

    class DetenerLocator:
        def count(self):
            return 0

    class PaginaFalsa:
        def __init__(self):
            self.url = url_inicial
            self.contador_resp = [0]
            self.entrada = EntradaFalsa()
            self.boton_razon = BotonRazonamientoFalso()
            self.keyboard = SimpleNamespace(press=lambda k: relleno_capturado.append(f"KEY:{k}"))

        def goto(self, url, wait_until=None):
            self.url = url

        def get_by_role(self, rol, name=None):
            if rol == "button":
                return self.boton_razon
            return SimpleNamespace(count=lambda: 0)

        def locator(self, selector):
            s_min = selector.lower()
            if "stop" in s_min or "detener" in s_min:
                return DetenerLocator()
            if "markdown" in s_min or "assistant" in s_min:
                return RespuestaLocator(self.contador_resp)
            if any(k in s_min for k in ("deepthink", "pensamiento", "reason", "pensar", "think")):
                return self.boton_razon
            return self.entrada

    pagina = PaginaFalsa()
    return SimpleNamespace(pages=[pagina], new_page=lambda: pagina)


def test_consultar_deepseek_y_chatgpt_insertan_con_fill_y_leen_respuesta(monkeypatch):
    monkeypatch.setattr(enjambre_web, "SEGUNDOS_ESTABLE_ENJAMBRE", 0.01)
    monkeypatch.setattr(enjambre_web, "INTERVALO_POLLING_ENJAMBRE", 0.01)

    captura_ds = []
    ctx_ds = _crear_contexto_especialista(
        "https://chat.deepseek.com",
        "DeepSeek: falta índice compuesto en (paciente_id, fecha).",
        captura_ds,
    )
    out_ds = enjambre_web._consultar_deepseek(ctx_ds, "Consulta SQL lenta en tabla turnos")
    assert "índice compuesto" in out_ds
    assert any("Auditoría Lógica" in c for c in captura_ds)
    assert any("máximo 250 palabras" in c for c in captura_ds)
    assert "KEY:Enter" in captura_ds

    captura_gpt = []
    ctx_gpt = _crear_contexto_especialista(
        "https://chatgpt.com",
        "ChatGPT: separar en RepositorioTurnos y ServicioTurnos.",
        captura_gpt,
    )
    out_gpt = enjambre_web._consultar_chatgpt(ctx_gpt, "Refactorizar módulo de turnos")
    assert "RepositorioTurnos" in out_gpt
    assert any("Clean Architecture" in c for c in captura_gpt)
    assert any("máximo 250 palabras" in c for c in captura_gpt)
    assert "KEY:Enter" in captura_gpt


def test_especialista_sin_sesion_devuelve_vacio_sin_romper():
    pagina_login = SimpleNamespace(
        url="https://chat.deepseek.com/sign_in",
        goto=lambda u, wait_until=None: None,
    )
    ctx = SimpleNamespace(pages=[pagina_login], new_page=lambda: pagina_login)
    assert enjambre_web._consultar_deepseek(ctx, "problema") == ""


# ---------------------------------------------------------------- 2. Juez Supremo en Neo (Medio por defecto, compatible con alias Argon)

@pytest.mark.parametrize(
    "modelo_pedido, nivel_pedido, nivel_esperado",
    [
        ("Neo", "Medio", "Medio"),
        ("4 Argon", "Medio", "Medio"),
        ("Neo", "Alto", "Alto"),
    ],
)
def test_consultar_enjambre_multicerebro_usa_neo_como_juez_en_medio_por_defecto(
    monkeypatch, modelo_pedido, nivel_pedido, nivel_esperado
):
    llamadas_juez = []

    class ClienteGeminiFalso:
        def _conectar(self):
            pass

        _navegador = SimpleNamespace(contexts=[SimpleNamespace(pages=[])])

        def preguntar(self, mensaje, ruta=None, imagen=None, *, modelo=None, pensamiento=None, chat_nuevo=False):
            llamadas_juez.append({
                "mensaje": mensaje,
                "modelo": modelo,
                "pensamiento": pensamiento,
                "chat_nuevo": chat_nuevo,
            })
            return "Veredicto del Juez Neo: aplicar índice compuesto y servicio desacoplado."

    monkeypatch.setattr(
        enjambre_web,
        "_consultar_deepseek",
        lambda ctx, prob: "Propuesta DeepSeek: optimizar query con EXPLAIN ANALYZE.",
    )
    monkeypatch.setattr(
        enjambre_web,
        "_consultar_chatgpt",
        lambda ctx, prob: "Propuesta ChatGPT: usar patrón Repository.",
    )
    monkeypatch.setattr(
        enjambre_web,
        "_cliente_enjambre",
        enjambre_web.ClienteEnjambreWeb(cliente_gemini=ClienteGeminiFalso()),
    )

    aviso_inicio = herramientas.consultar_enjambre_multicerebro(
        consulta="Optimizar endpoint de facturación",
        contexto_pantalla="IDE: archivo facturacion.py abierto",
        modelo_juez=modelo_pedido,
        nivel_juez=nivel_pedido,
    )
    assert "Enjambre iniciado en segundo plano" in aviso_inicio

    # Espera breve a que el hilo en segundo plano complete la ejecución simulada
    import time as _t
    limite = _t.monotonic() + 3.0
    while _t.monotonic() < limite and enjambre_web._ESTADO_ENJAMBRE.get("estado") == "en_progreso":
        _t.sleep(0.02)

    resultado = herramientas.consultar_enjambre_multicerebro("¿ya terminó el enjambre?")

    assert f"[Enjambre Multi-Cerebro · Juez Neo ({nivel_esperado})]" in resultado
    assert "Veredicto del Juez Neo" in resultado
    # También accesible desde consultar_estado_tarea_autonoma()
    assert "Veredicto del Juez Neo" in herramientas.consultar_estado_tarea_autonoma()
    assert len(llamadas_juez) == 1
    info = llamadas_juez[0]
    assert info["modelo"] == "Neo"
    assert info["pensamiento"] == nivel_esperado
    assert info["chat_nuevo"] is False
    assert "Propuesta DeepSeek" in info["mensaje"]
    assert "Propuesta ChatGPT" in info["mensaje"]
    assert "IDE: archivo facturacion.py abierto" in info["mensaje"]


# ---------------------------------------------------------------- 3. Integración con Orquestador y Vision

def test_orquestador_autonomo_puede_usar_enjambre_con_neo_como_juez(monkeypatch, tmp_path):
    repo_dir = tmp_path / "repo-enjambre"
    (repo_dir / ".git").mkdir(parents=True)
    (repo_dir / ".git" / "config").write_text('[remote "origin"]\n\turl = https://github.com/org/repo-enjambre.git\n')

    invocaciones_enjambre = []

    def falso_enjambre(consulta, contexto_pantalla="", modelo_juez="Neo", nivel_juez="Medio", timeout=0):
        invocaciones_enjambre.append((modelo_juez, nivel_juez, consulta))
        if len(invocaciones_enjambre) == 1:
            return "PROMPT ENJAMBRE PARA ANTIGRAVITY"
        return "1) Propósito\n2) Integración\n3) Funcionamiento\n4) Auditoría aprobada por Enjambre Neo"

    monkeypatch.setattr(enjambre_web, "consultar_enjambre_multicerebro", falso_enjambre)
    monkeypatch.setattr(orquestador_autonomo, "_buscar_repositorio_local", lambda *a, **k: repo_dir)
    monkeypatch.setattr(orquestador_autonomo, "_preparar_base_datos_y_entorno", lambda r: "Docker OK")
    monkeypatch.setattr(orquestador_autonomo, "_abrir_antigravity_y_enviar_prompt", lambda r, p: "IDE OK")
    monkeypatch.setattr(orquestador_autonomo, "_monitorear_cambios_git", lambda r, **k: "+linea nueva")

    orquestador_autonomo._ejecutar_ciclo_autonomo(
        "https://github.com/org/repo-enjambre/issues/99",
        modelo="Neo",
        usar_enjambre=True,
        nivel_juez="Medio",
    )

    assert len(invocaciones_enjambre) == 2
    assert invocaciones_enjambre[0][0] == "Neo" and invocaciones_enjambre[0][1] == "Medio"
    assert invocaciones_enjambre[1][0] == "Neo" and invocaciones_enjambre[1][1] == "Medio"
    assert "consultar_enjambre_multicerebro" in vision.NO_BLOQUEANTES


def test_deepseek_y_chatgpt_activan_razonamiento_antes_de_fill_y_respetan_estado_activo_o_cuota(monkeypatch):
    monkeypatch.setattr(enjambre_web, "SEGUNDOS_ESTABLE_ENJAMBRE", 0.01)
    monkeypatch.setattr(enjambre_web, "INTERVALO_POLLING_ENJAMBRE", 0.01)

    # 1) DeepSeek con DeepThink apagado -> hace clic antes de fill()
    eventos_ds_inactivo = []
    ctx_ds_off = _crear_contexto_especialista(
        "https://chat.deepseek.com",
        "Respuesta con DeepThink",
        eventos_ds_inactivo,
        atributos_razonamiento={"aria-pressed": "false", "class": "ds-button"},
    )
    enjambre_web._consultar_deepseek(ctx_ds_off, "Auditar query")
    assert eventos_ds_inactivo[0] == "CLICK_RAZONAMIENTO"

    # 2) DeepSeek con DeepThink ya activo (class active) -> no vuelve a hacer clic
    eventos_ds_activo = []
    ctx_ds_on = _crear_contexto_especialista(
        "https://chat.deepseek.com",
        "Respuesta con DeepThink ya activo",
        eventos_ds_activo,
        atributos_razonamiento={"class": "ds-button ds-button--active"},
    )
    enjambre_web._consultar_deepseek(ctx_ds_on, "Auditar query")
    assert "CLICK_RAZONAMIENTO" not in eventos_ds_activo

    # 3) ChatGPT con botón Reason/Pensar apagado y habilitado -> lo activa antes de fill()
    eventos_gpt_inactivo = []
    ctx_gpt_off = _crear_contexto_especialista(
        "https://chatgpt.com",
        "Respuesta con Reason",
        eventos_gpt_inactivo,
        atributos_razonamiento={"aria-pressed": "false"},
    )
    enjambre_web._consultar_chatgpt(ctx_gpt_off, "Diseñar arquitectura")
    assert eventos_gpt_inactivo[0] == "CLICK_RAZONAMIENTO"

    # 4) ChatGPT con botón Reason deshabilitado por cuota gratuita -> continúa sin hacer clic ni lanzar error
    eventos_gpt_cuota = []
    ctx_gpt_disabled = _crear_contexto_especialista(
        "https://chatgpt.com",
        "Respuesta estándar sin cuota de Reason",
        eventos_gpt_cuota,
        atributos_razonamiento={"aria-disabled": "true", "disabled": ""},
    )
    salida_gpt = enjambre_web._consultar_chatgpt(ctx_gpt_disabled, "Diseñar arquitectura")
    assert "CLICK_RAZONAMIENTO" not in eventos_gpt_cuota
    assert salida_gpt == "Respuesta estándar sin cuota de Reason"


def test_envio_paralelo_y_bucle_multiplexado_de_reloj_compartido(monkeypatch):
    orden_eventos = []

    def falso_prep_ds(ctx, prob):
        orden_eventos.append("ENVIAR_DEEPSEEK")
        return SimpleNamespace(), 0

    def falso_prep_gpt(ctx, prob):
        orden_eventos.append("ENVIAR_CHATGPT")
        return SimpleNamespace(), 0

    def falso_multiplexado(page_ds, page_cg, previas_ds=0, previas_cg=0, timeout_compartido=40.0):
        orden_eventos.append(f"MULTIPLEXADO:{timeout_compartido}")
        return (
            "Texto parcial recuperado de DeepSeek tras 40s",
            "Respuesta completa de ChatGPT",
        )

    monkeypatch.setattr(enjambre_web, "_preparar_y_enviar_deepseek", falso_prep_ds)
    monkeypatch.setattr(enjambre_web, "_preparar_y_enviar_chatgpt", falso_prep_gpt)
    monkeypatch.setattr(enjambre_web, "_recolectar_especialistas_multiplexado", falso_multiplexado)

    class ClienteGeminiJuez:
        _navegador = SimpleNamespace(contexts=[SimpleNamespace(pages=[])])

        def _conectar(self):
            pass

        def preguntar(self, mensaje, ruta=None, imagen=None, *, modelo=None, pensamiento=None, chat_nuevo=False):
            orden_eventos.append("JUEZ_ARGON")
            assert "Texto parcial recuperado de DeepSeek tras 40s" in mensaje
            assert "Respuesta completa de ChatGPT" in mensaje
            return "Solución sintetizada con texto parcial."

    cliente = enjambre_web.ClienteEnjambreWeb(cliente_gemini=ClienteGeminiJuez())
    salida = cliente.consultar_enjambre("Resolver deadlock en base de datos")

    # Verifica que ambos prompts se envían ANTES del único bucle multiplexado de 40s
    assert orden_eventos == [
        "ENVIAR_DEEPSEEK",
        "ENVIAR_CHATGPT",
        "MULTIPLEXADO:40.0",
        "JUEZ_ARGON",
    ]
    assert "Solución sintetizada con texto parcial." in salida


def test_recolectar_especialistas_multiplexado_corta_al_estabilizar_ambos(monkeypatch):
    monkeypatch.setattr(enjambre_web, "INTERVALO_MULTIPLEXADO", 0.01)
    monkeypatch.setattr(enjambre_web, "SEGUNDOS_ESTABLE_ENJAMBRE", 0.02)

    class PaginaSimulada:
        def __init__(self, texto_fijo):
            self.texto_fijo = texto_fijo

        def locator(self, selector):
            if "stop" in selector.lower() or "detener" in selector.lower():
                return SimpleNamespace(count=lambda: 0)
            loc = SimpleNamespace()
            loc.count = lambda: 1
            loc.last = SimpleNamespace(inner_text=lambda: self.texto_fijo)
            return loc

    ds, cg = enjambre_web._recolectar_especialistas_multiplexado(
        PaginaSimulada("Resumen DS"),
        PaginaSimulada("Resumen CG"),
        previas_ds=0,
        previas_cg=0,
        timeout_compartido=1.0,
    )
    assert ds == "Resumen DS"
    assert cg == "Resumen CG"


def test_rescate_sin_perdida_cuando_juez_argon_da_timeout(monkeypatch):
    monkeypatch.setattr(
        enjambre_web,
        "_consultar_deepseek",
        lambda ctx, prob: "Resumen exacto DeepSeek: usar SELECT FOR UPDATE SKIP LOCKED.",
    )
    monkeypatch.setattr(
        enjambre_web,
        "_consultar_chatgpt",
        lambda ctx, prob: "Resumen exacto ChatGPT: encapsular en UnitOfWork.",
    )

    class ClienteGeminiConFallo:
        _navegador = SimpleNamespace(contexts=[SimpleNamespace(pages=[])])

        def _conectar(self):
            pass

        def preguntar(self, *args, **kwargs):
            raise gemini_web.ErrorGeminiWeb("Gemini no respondió a tiempo.")

    cliente = enjambre_web.ClienteEnjambreWeb(cliente_gemini=ClienteGeminiConFallo())
    rescate = cliente.consultar_enjambre("Evitar condición de carrera en cola de pagos")
    assert "[Enjambre Multi-Cerebro · Rescate directo de Especialistas]" in rescate
    assert "SELECT FOR UPDATE SKIP LOCKED" in rescate
    assert "UnitOfWork" in rescate


def test_esperar_respuesta_especialista_devuelve_parcial_del_dom_si_expira_timeout(monkeypatch):
    monkeypatch.setattr(enjambre_web, "INTERVALO_POLLING_ENJAMBRE", 0.01)

    class LocatorParcial:
        def __init__(self):
            self.last = self

        def count(self):
            return 1

        def inner_text(self):
            return "Análisis parcial en curso antes del corte..."

    class PaginaLenta:
        def locator(self, selector):
            if "stop" in selector.lower() or "detener" in selector.lower():
                # Simula que sigue generando hasta agotar el timeout
                return SimpleNamespace(count=lambda: 1)
            return LocatorParcial()

    parcial = enjambre_web._esperar_respuesta_especialista(
        PaginaLenta(),
        enjambre_web.SELECTORES_DEEPSEEK["respuesta"],
        enjambre_web.SELECTORES_DEEPSEEK["detener"],
        cantidad_previa=0,
        timeout=0.03,
    )
    assert parcial == "Análisis parcial en curso antes del corte..."


def test_precalentar_especialistas_abre_pestanas_activa_razonamiento_y_guarda_cache():
    clics_ds = []
    clics_gpt = []
    ctx_ds = _crear_contexto_especialista(
        "https://chat.deepseek.com", "ok", clics_ds, atributos_razonamiento={"aria-pressed": "false"}
    )
    ctx_gpt = _crear_contexto_especialista(
        "https://chatgpt.com", "ok", clics_gpt, atributos_razonamiento={"aria-pressed": "false"}
    )
    # Combinamos ambas páginas en un solo contexto del navegador
    pagina_ds = ctx_ds.pages[0]
    pagina_gpt = ctx_gpt.pages[0]

    class ClienteGeminiStub:
        _navegador = SimpleNamespace(
            contexts=[SimpleNamespace(pages=[pagina_ds, pagina_gpt], new_page=lambda: pagina_ds)]
        )

        def _conectar(self):
            pass

    enjambre = enjambre_web.ClienteEnjambreWeb(cliente_gemini=ClienteGeminiStub())
    estado = enjambre.precalentar_especialistas()

    assert estado == {"deepseek": True, "chatgpt": True}
    assert clics_ds == ["CLICK_RAZONAMIENTO"]
    assert clics_gpt == ["CLICK_RAZONAMIENTO"]
    assert "deepseek" in enjambre_web._CACHE_ENTRADA_ESPECIALISTA
    assert "chatgpt" in enjambre_web._CACHE_ENTRADA_ESPECIALISTA


# ---------------------------------------------------------------- 4. Integración de OpenRouter en el Enjambre

def test_openrouter_api_maneja_timeout_y_errores_401_402_429_sin_lanzar(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-test-123")
    monkeypatch.setattr(config, "OPENROUTER_MODEL", "openrouter/free")

    class ErrorHTTP(Exception):
        def __init__(self, status_code: int, msg: str):
            super().__init__(msg)
            self.status_code = status_code

    for codigo in (401, 402, 429):
        def crear_cliente_error(*args, _c=codigo, **kwargs):
            class _Completions:
                @staticmethod
                def create(**kw):
                    raise ErrorHTTP(_c, f"HTTP {_c} error")

            return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))

        monkeypatch.setattr(openrouter_api, "_crear_cliente", crear_cliente_error)
        resp = openrouter_api.consultar_openrouter("Analizar query")
        assert openrouter_api.es_error_openrouter(resp) is True
        assert f"({codigo})" in resp

    def crear_cliente_timeout(*args, **kwargs):
        class _Completions:
            @staticmethod
            def create(**kw):
                raise TimeoutError("Request timed out after 25.0s")

        return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))

    monkeypatch.setattr(openrouter_api, "_crear_cliente", crear_cliente_timeout)
    resp_timeout = openrouter_api.consultar_openrouter("Analizar query", timeout=25.0)
    assert openrouter_api.es_error_openrouter(resp_timeout) is True
    assert "(timeout)" in resp_timeout
    assert "25.0s" in resp_timeout


def test_enjambre_incluye_openrouter_en_paralelo_cuando_hay_api_key(monkeypatch):
    import threading

    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setattr(config, "OPENROUTER_MODEL", "openrouter/free")

    hilos_usados = {}

    def falso_openrouter(problema, modelo=None, timeout=25.0):
        hilos_usados["openrouter_thread"] = threading.current_thread().name
        return "Propuesta OpenRouter: agregar índice parcial y circuit breaker."

    monkeypatch.setattr(openrouter_api, "consultar_openrouter", falso_openrouter)
    monkeypatch.setattr(
        enjambre_web,
        "_consultar_deepseek",
        lambda ctx, prob: "Propuesta DeepSeek: validar locks en transacción.",
    )
    monkeypatch.setattr(
        enjambre_web,
        "_consultar_chatgpt",
        lambda ctx, prob: "Propuesta ChatGPT: separar capa de aplicación.",
    )

    prompts_juez = []

    class ClienteGeminiJuezOR:
        _navegador = SimpleNamespace(contexts=[SimpleNamespace(pages=[])])

        def _conectar(self):
            pass

        def preguntar(self, mensaje, ruta=None, imagen=None, *, modelo=None, pensamiento=None, chat_nuevo=False):
            prompts_juez.append(mensaje)
            return "Veredicto final combinando DeepSeek, ChatGPT y OpenRouter."

    cliente = enjambre_web.ClienteEnjambreWeb(cliente_gemini=ClienteGeminiJuezOR())
    salida = cliente.consultar_enjambre("Mejorar resiliencia del servicio de pagos")

    assert "Veredicto final combinando DeepSeek, ChatGPT y OpenRouter." in salida
    assert "openrouter" in cliente.ultimos_resultados
    assert cliente.ultimos_resultados["openrouter"] == "Propuesta OpenRouter: agregar índice parcial y circuit breaker."
    assert enjambre_web._ESTADO_ENJAMBRE["resultados"]["openrouter"] == (
        "Propuesta OpenRouter: agregar índice parcial y circuit breaker."
    )
    assert len(prompts_juez) == 1
    assert "=== RESUMEN EXACTO 3 (OpenRouter - openrouter/free) ===" in prompts_juez[0]
    assert "Propuesta OpenRouter: agregar índice parcial y circuit breaker." in prompts_juez[0]
    assert "jarvis-openrouter" in hilos_usados.get("openrouter_thread", "")


def test_enjambre_omite_openrouter_limpiamente_si_falta_api_key_o_devuelve_429(monkeypatch):
    monkeypatch.setattr(
        enjambre_web,
        "_consultar_deepseek",
        lambda ctx, prob: "Propuesta DeepSeek: usar paginación por cursor.",
    )
    monkeypatch.setattr(
        enjambre_web,
        "_consultar_chatgpt",
        lambda ctx, prob: "Propuesta ChatGPT: DTO inmutable.",
    )

    prompts_juez = []

    class ClienteGeminiJuezSinOR:
        _navegador = SimpleNamespace(contexts=[SimpleNamespace(pages=[])])

        def _conectar(self):
            pass

        def preguntar(self, mensaje, ruta=None, imagen=None, *, modelo=None, pensamiento=None, chat_nuevo=False):
            prompts_juez.append(mensaje)
            return "Veredicto con 2 cerebros."

    cliente = enjambre_web.ClienteEnjambreWeb(cliente_gemini=ClienteGeminiJuezSinOR())

    # Caso 1: sin OPENROUTER_API_KEY
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", None)
    salida_sin_key = cliente.consultar_enjambre("Optimizar listado")
    assert "Veredicto con 2 cerebros." in salida_sin_key
    assert "openrouter" not in cliente.ultimos_resultados
    assert "RESUMEN EXACTO 3" not in prompts_juez[-1]

    # Caso 2: con OPENROUTER_API_KEY pero OpenRouter devuelve error 429
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-rate-limited")
    monkeypatch.setattr(
        openrouter_api,
        "consultar_openrouter",
        lambda prob, **kw: "Error OpenRouter (429): límite de cuota o rate limit alcanzado en OpenRouter.",
    )
    salida_429 = cliente.consultar_enjambre("Optimizar listado con 429")
    assert "Veredicto con 2 cerebros." in salida_429
    assert "openrouter" not in cliente.ultimos_resultados
    assert "RESUMEN EXACTO 3" not in prompts_juez[-1]





