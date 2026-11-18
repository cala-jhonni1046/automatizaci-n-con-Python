"""Tests unitarios para el Enjambre Multi-Cerebro (enjambre_web.py):
DeepSeek + ChatGPT + Juez Supremo en Gemini 4 Argon (niveles Alto y Medio).
"""
from types import SimpleNamespace

import pytest

import vision
from jarvis import enjambre_web, gemini_web, herramientas, orquestador_autonomo


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
    assert "KEY:Enter" in captura_gpt


def test_especialista_sin_sesion_devuelve_vacio_sin_romper():
    pagina_login = SimpleNamespace(
        url="https://chat.deepseek.com/sign_in",
        goto=lambda u, wait_until=None: None,
    )
    ctx = SimpleNamespace(pages=[pagina_login], new_page=lambda: pagina_login)
    assert enjambre_web._consultar_deepseek(ctx, "problema") == ""


# ---------------------------------------------------------------- 2. Juez Supremo en 4 Argon (Alto y Medio)

@pytest.mark.parametrize("nivel_pedido, nivel_esperado", [("Alto", "Alto"), ("Medio", "Medio")])
def test_consultar_enjambre_multicerebro_usa_4_argon_como_juez_en_alto_y_medio(
    monkeypatch, nivel_pedido, nivel_esperado
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
            return "Veredicto del Juez Argon: aplicar índice compuesto y servicio desacoplado."

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

    resultado = herramientas.consultar_enjambre_multicerebro(
        consulta="Optimizar endpoint de facturación",
        contexto_pantalla="IDE: archivo facturacion.py abierto",
        modelo_juez="4 Argon",
        nivel_juez=nivel_pedido,
    )

    assert f"[Enjambre Multi-Cerebro · Juez 4 Argon ({nivel_esperado})]" in resultado
    assert "Veredicto del Juez Argon" in resultado
    assert len(llamadas_juez) == 1
    info = llamadas_juez[0]
    assert info["modelo"] == "4 Argon"
    assert info["pensamiento"] == nivel_esperado
    assert info["chat_nuevo"] is False
    assert "Propuesta DeepSeek" in info["mensaje"]
    assert "Propuesta ChatGPT" in info["mensaje"]
    assert "IDE: archivo facturacion.py abierto" in info["mensaje"]


# ---------------------------------------------------------------- 3. Integración con Orquestador y Vision

def test_orquestador_autonomo_puede_usar_enjambre_con_4_argon_como_juez(monkeypatch, tmp_path):
    repo_dir = tmp_path / "repo-enjambre"
    (repo_dir / ".git").mkdir(parents=True)
    (repo_dir / ".git" / "config").write_text('[remote "origin"]\n\turl = https://github.com/org/repo-enjambre.git\n')

    invocaciones_enjambre = []

    def falso_enjambre(consulta, contexto_pantalla="", modelo_juez="4 Argon", nivel_juez="Alto", timeout=0):
        invocaciones_enjambre.append((modelo_juez, nivel_juez, consulta))
        if len(invocaciones_enjambre) == 1:
            return "PROMPT ENJAMBRE PARA ANTIGRAVITY"
        return "1) Propósito\n2) Integración\n3) Funcionamiento\n4) Auditoría aprobada por Enjambre 4 Argon"

    monkeypatch.setattr(enjambre_web, "consultar_enjambre_multicerebro", falso_enjambre)
    monkeypatch.setattr(orquestador_autonomo, "_buscar_repositorio_local", lambda *a, **k: repo_dir)
    monkeypatch.setattr(orquestador_autonomo, "_preparar_base_datos_y_entorno", lambda r: "Docker OK")
    monkeypatch.setattr(orquestador_autonomo, "_abrir_antigravity_y_enviar_prompt", lambda r, p: "IDE OK")
    monkeypatch.setattr(orquestador_autonomo, "_monitorear_cambios_git", lambda r, **k: "+linea nueva")

    orquestador_autonomo._ejecutar_ciclo_autonomo(
        "https://github.com/org/repo-enjambre/issues/99",
        modelo="4 Argon",
        usar_enjambre=True,
        nivel_juez="Medio",
    )

    assert len(invocaciones_enjambre) == 2
    assert invocaciones_enjambre[0][0] == "4 Argon" and invocaciones_enjambre[0][1] == "Medio"
    assert invocaciones_enjambre[1][0] == "4 Argon" and invocaciones_enjambre[1][1] == "Medio"
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


