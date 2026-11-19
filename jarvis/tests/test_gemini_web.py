"""Tests unitarios para el puente Gemini Web (Edge + Playwright CDP), enrutamiento de modelos,
explicación de código (portapapeles / pantalla), análisis de repositorios y limpieza TTS.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis import cerebro, config, gemini_web, herramientas


# ---------------------------------------------------------------- 1. Enrutamiento de modelos

def test_elegir_modelo_por_defecto_neo_medio_misma_pestana():
    ruta = gemini_web.elegir_modelo("Explicame cómo funciona asyncio en Python")
    assert ruta.modelo == "Neo"
    assert ruta.pensamiento == "Medio"
    assert ruta.chat_nuevo is False


@pytest.mark.parametrize(
    "pedido",
    [
        "Hacé una investigación profunda sobre PostgreSQL MVCC",
        "Necesito una investigación exacta de la arquitectura de Tryton",
        "Investigá a fondo cómo configurar GNU Health en Debian",
        "Hacé una auditoría profunda de seguridad en este módulo",
    ],
)
def test_elegir_modelo_investigacion_profunda_usa_nivel_alto_y_reutiliza_chat(pedido):
    ruta = gemini_web.elegir_modelo(pedido)
    assert ruta.modelo == "Neo"
    assert ruta.pensamiento == "Alto"
    assert ruta.chat_nuevo is False


@pytest.mark.parametrize(
    "pedido",
    [
        "Investigá esto en un chat nuevo",
        "Empezá de cero y explicame este módulo",
        "Limpiá el chat antes de analizar esto",
        "Nuevo tema: cómo configurar nginx",
    ],
)
def test_elegir_modelo_activa_chat_nuevo_solo_si_se_pide_explicitamente(pedido):
    ruta = gemini_web.elegir_modelo(pedido)
    assert ruta.chat_nuevo is True


def test_alias_argon_4_argon_y_neo_resuelven_siempre_a_neo():
    for alias in ("argon", "Argon", "4 argon", "4 Argon", "neo", "NEO"):
        assert gemini_web.normalizar_nombre_modelo(alias) == "Neo"
        assert gemini_web.Ruta(alias, "Medio", False).modelo == "Neo"

    for frase in ("Usá 4 Argon para revisar esta arquitectura", "usá Argon 4 en este análisis", "usá Neo en medio"):
        ruta_neo = gemini_web.elegir_modelo(frase)
        assert ruta_neo.modelo == "Neo"
        assert ruta_neo.pensamiento == "Medio"
        assert ruta_neo.chat_nuevo is False


# ---------------------------------------------------------------- 2. Limpieza de Markdown para TTS

def test_limpiar_para_voz_elimina_bloques_de_codigo_y_simbolos_markdown():
    markdown = (
        "# Diagnóstico Principal\n\n"
        "Acá tenés la **solución exacta** en `config.py`:\n\n"
        "```python\ndef conectar():\n    return True\n```\n\n"
        "- Paso 1: Abrí [la documentación](https://docs.python.org)\n"
        "- Paso 2: Ejecutá el script\n"
    )
    limpio = gemini_web.limpiar_para_voz(markdown)
    assert "```" not in limpio
    assert "def conectar" not in limpio
    assert "(te dejé el código en la pantalla)" in limpio
    assert "**" not in limpio
    assert "#" not in limpio
    assert "https://docs.python.org" not in limpio
    assert "la documentación" in limpio
    assert "Diagnóstico Principal" in limpio


# ---------------------------------------------------------------- 3. Explicación de código (portapapeles y captura)

def test_explicar_codigo_en_pantalla_usa_portapapeles_nuevo(monkeypatch):
    llamadas = []

    def falso_consultar(mensaje, ruta=None, imagen=None, timeout=0):
        llamadas.append((mensaje, ruta, imagen))
        return "El código define una función suma sin errores."

    monkeypatch.setattr(gemini_web, "consultar", falso_consultar)
    monkeypatch.setattr(herramientas, "_ultimo_portapapeles_usado", None)
    import pyperclip
    monkeypatch.setattr(pyperclip, "paste", lambda: "def suma(a, b):\n    return a + b")

    salida = herramientas.explicar_codigo_en_pantalla("explicame este código")
    assert "[Gemini Neo · pensamiento Medio]" in salida
    assert "función suma" in salida
    assert len(llamadas) == 1
    mensaje, ruta, imagen = llamadas[0]
    assert "def suma(a, b):" in mensaje
    assert ruta.modelo == "Neo"
    assert ruta.pensamiento == "Medio"
    assert ruta.chat_nuevo is False
    assert imagen is None


def test_explicar_codigo_en_pantalla_usa_captura_si_portapapeles_no_cambio(monkeypatch, tmp_path):
    llamadas = []
    captura_falsa = tmp_path / "pantalla.png"
    captura_falsa.write_bytes(b"PNG_DATA")

    def falso_consultar(mensaje, ruta=None, imagen=None, timeout=0):
        llamadas.append((mensaje, ruta, imagen))
        return "Veo en la captura un error de sintaxis en la línea 12."

    monkeypatch.setattr(gemini_web, "consultar", falso_consultar)
    monkeypatch.setattr(herramientas, "_ultimo_portapapeles_usado", "texto viejo")
    import pyperclip
    monkeypatch.setattr(pyperclip, "paste", lambda: "texto viejo")
    monkeypatch.setattr(herramientas, "_capturar_pantalla_png", lambda: captura_falsa)

    salida = herramientas.explicar_codigo_en_pantalla("revisá lo que tengo en pantalla")
    assert "error de sintaxis en la línea 12" in salida
    assert len(llamadas) == 1
    _, ruta, imagen = llamadas[0]
    assert ruta.modelo == "Neo"
    assert ruta.pensamiento == "Medio"
    assert imagen == captura_falsa


# ---------------------------------------------------------------- 4. Análisis de repositorios con Gemini

def test_analizar_repositorio_con_gemini_extrae_archivos_clave(monkeypatch):
    llamadas = []

    class ArbolFalso:
        tree = [
            SimpleNamespace(path="README.md", type="blob"),
            SimpleNamespace(path="requirements.txt", type="blob"),
            SimpleNamespace(path="src/main.py", type="blob"),
            SimpleNamespace(path="node_modules/pkg/index.js", type="blob"),
        ]

    class RepoFalso:
        default_branch = "main"

        def get_git_tree(self, branch, recursive=True):
            return ArbolFalso()

    monkeypatch.setattr(
        herramientas, "_obtener_repo_github", lambda r: (RepoFalso(), "usuario/proyecto", "")
    )
    monkeypatch.setattr(
        herramientas, "gh_leer_archivo", lambda repo, ruta: f"contenido de {ruta}"
    )
    monkeypatch.setattr(
        gemini_web,
        "consultar",
        lambda mensaje, ruta=None, imagen=None, timeout=0: llamadas.append((mensaje, ruta)) or "Arquitectura limpia.",
    )

    resultado = herramientas.analizar_repositorio_con_gemini(
        "https://github.com/usuario/proyecto", "explicame la arquitectura"
    )
    assert "[Gemini Neo · pensamiento Medio]" in resultado
    assert "Arquitectura limpia." in resultado
    mensaje, ruta = llamadas[0]
    assert "README.md" in mensaje
    assert "src/main.py" in mensaje
    assert "node_modules" not in mensaje
    assert ruta.modelo == "Neo"
    assert ruta.pensamiento == "Medio"
    assert ruta.chat_nuevo is False


# ---------------------------------------------------------------- 5. Integración en Cerebro

def test_cerebro_enruta_consultas_y_explicaciones_a_gemini_web_y_limpia_voz(monkeypatch):
    monkeypatch.setattr(
        herramientas,
        "consultar_gemini_pro",
        lambda pedido: "[Gemini Neo · pensamiento Medio]\nUsá **este comando**:\n```bash\npip install pytest\n```",
    )
    c = cerebro.Cerebro(usar_gemini_web=True)
    respuesta_voz = c.pensar("¿Cómo instalo pytest en un entorno virtual?")
    assert "```" not in respuesta_voz
    assert "(te dejé el código en la pantalla)" in respuesta_voz
    assert "pip install pytest" in c.ultima_respuesta_completa


def test_cerebro_mantiene_acciones_de_computadora_en_bucle_de_herramientas(monkeypatch):
    c = cerebro.Cerebro(usar_gemini_web=True)
    assert c._consulta_gemini_web("abrí la calculadora") is None
    assert c._consulta_gemini_web("ejecutá el comando ipconfig en la terminal") is None
    assert c._consulta_gemini_web("explicame este código") is not None
    assert c._consulta_gemini_web("analizá el repositorio torvalds/linux") is not None


# ---------------------------------------------------------------- 6. Selector de modelos por nombre visible

@pytest.mark.parametrize("etiqueta_visible", ["Neo", "4 Hydrogen", "Hydrogen", "3.1 Pro", "4 Argon", "Flash-Lite"])
def test_buscar_boton_modelo_reconoce_nombres_visibles_y_cambia_entre_neo_y_3_1_pro(etiqueta_visible):
    clics = []

    class ElementoFalso:
        def __init__(self, nombre):
            self.nombre = nombre
            self.first = self
            self.last = self

        def count(self):
            return 1

        def is_visible(self):
            return True

        def click(self, timeout=None):
            clics.append(self.nombre)

    classLocVacio = SimpleNamespace(count=lambda: 0)

    class PaginaFalsa:
        keyboard = SimpleNamespace(press=lambda tecla: clics.append(f"key:{tecla}"))

        def get_by_role(self, rol, name=None):
            if rol == "button" and name is not None and hasattr(name, "search") and name.search(etiqueta_visible):
                return ElementoFalso(f"boton:{etiqueta_visible}")
            if rol == "menuitemradio" and name is not None and hasattr(name, "search"):
                if name.search("Neo"):
                    return ElementoFalso("opcion:Neo")
                if name.search("3.1 Pro"):
                    return ElementoFalso("opcion:3.1 Pro")
                if name.search("Medio"):
                    return ElementoFalso("pensamiento:Medio")
                if name.search("Alto"):
                    return ElementoFalso("pensamiento:Alto")
            return classLocVacio

        def locator(self, selector):
            return classLocVacio

    cliente = gemini_web.ClienteGeminiWeb()
    pagina = PaginaFalsa()

    # 1) Selecciona Neo (pensamiento Medio)
    cliente._seleccionar_modelo(pagina, gemini_web.Ruta("Neo", "Medio", chat_nuevo=False))
    assert clics == [f"boton:{etiqueta_visible}", "opcion:Neo", "pensamiento:Medio", "key:Escape"]
    assert cliente._modelo_actual == "Neo"

    # 2) Cambia a 3.1 Pro (pensamiento Alto)
    clics.clear()
    cliente._seleccionar_modelo(pagina, gemini_web.Ruta("3.1 Pro", "Alto", chat_nuevo=True))
    assert clics == [f"boton:{etiqueta_visible}", "opcion:3.1 Pro", "pensamiento:Alto", "key:Escape"]
    assert cliente._modelo_actual == "3.1 Pro"


# ---------------------------------------------------------------- 7. Delegación de investigación profunda e instrucciones de vision.py

def test_investigar_a_fondo_e_investigar_tema_profundo_fuerzan_pensamiento_alto(monkeypatch):
    llamadas = []

    def falso_consultar_web(mensaje, ruta=None, imagen=None, timeout=0):
        llamadas.append((mensaje, ruta))
        return "Documentación oficial desde Gemini Neo en Alto."

    monkeypatch.setattr(gemini_web, "consultar", falso_consultar_web)
    monkeypatch.setattr(
        herramientas,
        "DDGS",
        lambda: (_ for _ in ()).throw(AssertionError("No debe usar DDGS")),
    )

    salida = herramientas.investigar_a_fondo("configurar trytond postgresql")
    assert "[Gemini Neo · pensamiento Alto]" in salida
    assert "Documentación oficial desde Gemini Neo en Alto." in salida
    assert len(llamadas) == 1
    mensaje, ruta = llamadas[0]
    assert mensaje == "configurar trytond postgresql"
    assert ruta.modelo == "Neo"
    assert ruta.pensamiento == "Alto"
    assert ruta.chat_nuevo is False

    llamadas.clear()
    salida_profunda = herramientas.investigar_tema_profundo("arquitectura MVCC de PostgreSQL")
    assert "[Gemini Neo · pensamiento Alto]" in salida_profunda
    assert len(llamadas) == 1
    assert llamadas[0][1].modelo == "Neo"
    assert llamadas[0][1].pensamiento == "Alto"


def test_instrucciones_vision_usan_neo_medio_por_defecto_y_alto_para_investigacion_profunda():
    import vision

    texto = vision.instrucciones(con_video=True, con_herramientas=True)
    assert 'consultar_gemini_pro(consulta, modelo="Neo", pensamiento="Medio")' in texto
    assert 'consultar_gemini_pro(consulta, modelo="Neo", pensamiento="Alto")' in texto
    assert "investigar_tema_profundo(consulta)" in texto


def test_seleccionar_modelo_deshabilitado_por_cuota_cambia_a_flash_lite_sin_timeout():
    clics = []

    class ElementoFalso:
        def __init__(self, nombre, aria_disabled=None, attr_disabled=None):
            self.nombre = nombre
            self.aria_disabled = aria_disabled
            self.attr_disabled = attr_disabled
            self.first = self
            self.last = self

        def count(self):
            return 1

        def is_visible(self):
            return True

        def get_attribute(self, attr):
            if attr == "aria-disabled":
                return self.aria_disabled
            if attr == "disabled":
                return self.attr_disabled
            return None

        def is_disabled(self):
            return self.aria_disabled == "true" or self.attr_disabled is not None

        def click(self, timeout=None):
            if self.is_disabled():
                raise AssertionError(f"No debía hacer clic en opción deshabilitada: {self.nombre}")
            clics.append(self.nombre)

    vacio = SimpleNamespace(count=lambda: 0)

    class PaginaConCuotaAgotada:
        keyboard = SimpleNamespace(press=lambda tecla: clics.append(f"key:{tecla}"))

        def get_by_role(self, rol, name=None):
            if rol == "button" and name is not None and hasattr(name, "search") and name.search("4 Hydrogen"):
                return ElementoFalso("boton:4 Hydrogen")
            if rol == "menuitemradio" and name is not None and hasattr(name, "search"):
                if name.search("3.1 Pro"):
                    return ElementoFalso("opcion:3.1 Pro", aria_disabled="true")
                if name.search("Flash-Lite"):
                    return ElementoFalso("opcion:Flash-Lite")
            return vacio

        def locator(self, selector):
            return vacio

    cliente = gemini_web.ClienteGeminiWeb()
    pagina = PaginaConCuotaAgotada()

    cliente._seleccionar_modelo(pagina, gemini_web.Ruta("3.1 Pro", "Alto", chat_nuevo=True))
    assert clics == ["boton:4 Hydrogen", "opcion:Flash-Lite", "key:Escape"]
    assert cliente._modelo_actual == "Flash-Lite"


def test_consultar_gemini_pro_hace_fallback_limpio_si_web_falla(monkeypatch):
    def fallar_web(*args, **kwargs):
        raise gemini_web.ErrorGeminiWeb("Bloqueo de cuota en navegador")

    monkeypatch.setattr(gemini_web, "consultar", fallar_web)
    monkeypatch.setattr(config, "clave_configurada", lambda: True)
    monkeypatch.setattr(
        herramientas.litellm,
        "completion",
        lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Respuesta de respaldo estándar."))]
        ),
    )

    salida = herramientas.consultar_gemini_pro("Explicame qué es un índice B-Tree")
    assert "Fallback estándar Jarvis" in salida
    assert "Respuesta de respaldo estándar." in salida


def test_consultar_gemini_pro_concatena_contexto_pantalla_antes_de_consulta(monkeypatch):
    enviados = []

    def falso_consultar(mensaje, ruta=None, imagen=None, timeout=0):
        enviados.append((mensaje, ruta))
        return "El error FATAL se corrige iniciando el servicio PostgreSQL."

    monkeypatch.setattr(gemini_web, "consultar", falso_consultar)

    salida = herramientas.consultar_gemini_pro(
        consulta="explicame este error y cómo lo arreglo",
        contexto_pantalla="Programa: DBeaver | Cartel de error: FATAL: password authentication failed for user 'gnuhealth'",
    )
    assert "[Gemini Neo · pensamiento Medio]" in salida
    assert len(enviados) == 1
    mensaje, ruta = enviados[0]
    assert mensaje.startswith("CONTEXTO VISIBLE EN LA PANTALLA DEL USUARIO:\nPrograma: DBeaver")
    assert "CONSULTA DEL USUARIO:\nexplicame este error y cómo lo arreglo" in mensaje
    assert ruta.modelo == "Neo"
    assert ruta.pensamiento == "Medio"


def test_neo_y_alias_argon_soportan_bajo_medio_alto_y_fallback_al_primer_modelo():
    # 1) Niveles Bajo, Medio y Alto para Neo (incluyendo alias Argon / 4 Argon)
    r_bajo = gemini_web.elegir_modelo("Usá Argon 4 en bajo para revisar este bloque")
    assert r_bajo.modelo == "Neo"
    assert r_bajo.pensamiento == "Bajo"

    r_medio = gemini_web.elegir_modelo("Usá Neo en medio para analizar el diseño")
    assert r_medio.modelo == "Neo"
    assert r_medio.pensamiento == "Medio"

    r_alto = gemini_web.elegir_modelo("Usá 4 Argon en alto como juez de arquitectura")
    assert r_alto.modelo == "Neo"
    assert r_alto.pensamiento == "Alto"

    # 2) Fast-Path y transición entre niveles Bajo -> Medio -> Alto
    clics = []
    etiqueta_boton = ["Neo"]

    class ElementoFalso:
        def __init__(self, nombre):
            self.nombre = nombre
            self.first = self
            self.last = self

        def count(self):
            return 1

        def is_visible(self):
            return True

        def inner_text(self):
            return etiqueta_boton[0] if self.nombre.startswith("boton:") else self.nombre

        def click(self, timeout=None):
            clics.append(self.nombre)

    vacio = SimpleNamespace(count=lambda: 0)

    class PaginaTransicion:
        keyboard = SimpleNamespace(press=lambda tecla: clics.append(f"key:{tecla}"))

        def get_by_role(self, rol, name=None):
            if rol == "button" and name is not None and hasattr(name, "search") and name.search(etiqueta_boton[0]):
                return ElementoFalso(f"boton:{etiqueta_boton[0]}")
            if rol == "menuitemradio" and name is not None and hasattr(name, "search"):
                for op in ("Neo", "4 Hydrogen", "3.1 Pro"):
                    if name.search(op):
                        return ElementoFalso(f"opcion:{op}")
                for nivel in ("Bajo", "Medio", "Alto"):
                    if name.search(nivel):
                        return ElementoFalso(f"pensamiento:{nivel}")
            return vacio

        def locator(self, selector):
            return vacio

    cliente = gemini_web.ClienteGeminiWeb()
    pagina = PaginaTransicion()

    # Selecciona Neo en Bajo
    cliente._seleccionar_modelo(pagina, r_bajo)
    assert clics == ["boton:Neo", "opcion:Neo", "pensamiento:Bajo", "key:Escape"]
    assert cliente._ultimo_modelo == "Neo"
    assert cliente._ultimo_pensamiento == "Bajo"

    # Fast-Path: repetir Neo en Bajo con botón "Neo" no vuelve a abrir el menú
    clics.clear()
    cliente._seleccionar_modelo(pagina, r_bajo)
    assert clics == []

    # Si se pide Neo en Medio (por defecto del Hypervisor), cambia la intensidad -> abre el menú
    clics.clear()
    r_defecto = gemini_web.elegir_modelo("Explicame este método")
    assert r_defecto.modelo == "Neo" and r_defecto.pensamiento == "Medio"
    cliente._seleccionar_modelo(pagina, r_defecto)
    assert clics == ["boton:Neo", "opcion:Neo", "pensamiento:Medio", "key:Escape"]
    assert cliente._ultimo_pensamiento == "Medio"

    # Si se pide investigación profunda (Alto), cambia la intensidad a Alto dentro de Neo
    clics.clear()
    r_pro = gemini_web.elegir_modelo("Hacé una investigación profunda sobre este protocolo")
    assert r_pro.modelo == "Neo" and r_pro.pensamiento == "Alto"
    cliente._seleccionar_modelo(pagina, r_pro)
    assert clics == ["boton:Neo", "opcion:Neo", "pensamiento:Alto", "key:Escape"]
    assert cliente._ultimo_modelo == "Neo"
    assert cliente._ultimo_pensamiento == "Alto"

    # 3) Fallback al primer modelo disponible del menú si 'Neo' no figura en el DOM
    clics_fallback = []

    class PaginaSinNeo:
        keyboard = SimpleNamespace(press=lambda tecla: clics_fallback.append(f"key:{tecla}"))

        def get_by_role(self, rol, name=None):
            if rol == "button" and name is not None and hasattr(name, "search") and name.search("Flash-Lite"):
                return ElementoFalso("boton:Flash-Lite")
            if rol == "menuitemradio" and name is None:
                primero = ElementoFalso("opcion:PrimerModeloDOM")
                primero.inner_text = lambda: "Flash-Lite"
                return SimpleNamespace(count=lambda: 1, first=primero)
            if rol == "menuitemradio" and name is not None and hasattr(name, "search") and name.search("Medio"):
                return ElementoFalso("pensamiento:Medio")
            return vacio

        def get_by_text(self, patron):
            return vacio

        def locator(self, selector):
            return vacio

    cliente_fb = gemini_web.ClienteGeminiWeb()
    cliente_fb._seleccionar_modelo(PaginaSinNeo(), gemini_web.Ruta("Neo", "Medio", chat_nuevo=False))
    assert "opcion:PrimerModeloDOM" in clics_fallback
    assert cliente_fb._ultimo_modelo == "Flash-Lite"
    assert gemini_web.INTERVALO_POLLING_RESPUESTA == 0.2
    assert gemini_web.SEGUNDOS_ESTABLE == 0.4


def test_fallback_rota_modelos_ante_503_omite_temperature_en_latest_y_usa_edge_si_api_cae(monkeypatch):
    modelos_intentados = []

    def falsa_completion(**kwargs):
        modelos_intentados.append(kwargs)
        if kwargs["model"] == "gemini/gemini-flash-latest":
            # Verifica que en gemini-flash-latest NO se envía temperature (evita DeprecationWarning)
            assert "temperature" not in kwargs
            raise RuntimeError("503 ServiceUnavailableError: The model is overloaded")
        if kwargs["model"] == "gemini/gemini-2.5-flash":
            assert kwargs.get("temperature") == 0.1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="Respuesta desde gemini-2.5-flash"))]
            )
        raise AssertionError("No debía llegar al tercer modelo si 2.5-flash respondió")

    monkeypatch.setattr(config, "MODELO", "gemini/gemini-flash-latest")
    monkeypatch.setattr(config, "clave_configurada", lambda: True)
    monkeypatch.setattr(herramientas.litellm, "completion", falsa_completion)

    # Simula un TimeoutError vacío () en el enjambre
    monkeypatch.setattr(
        herramientas.enjambre_web,
        "consultar_enjambre_multicerebro",
        lambda **kw: (_ for _ in ()).throw(TimeoutError()),
    )

    salida = herramientas._ejecutar_enjambre_con_respaldo("Analizar arquitectura distribuida")
    assert "[Fallback estándar Jarvis (gemini/gemini-2.5-flash)]" in salida
    assert "Respuesta desde gemini-2.5-flash" in salida
    assert [m["model"] for m in modelos_intentados] == [
        "gemini/gemini-flash-latest",
        "gemini/gemini-2.5-flash",
    ]


def test_vision_pausa_frames_1080p_mientras_corre_herramienta_pesada(monkeypatch):
    import asyncio
    import vision
    from google.genai import types

    v = vision.VisionEnVivo(cliente=None, microfono=None, parlante=None, con_video=True)
    estado_durante_ejecucion = []

    def falso_ejecutar(nombre, argumentos):
        estado_durante_ejecucion.append(v._herramienta_pesada_activa)
        # Intentar encolar video mientras corre la herramienta pesada no debe agregar frames
        v.encolar_video(b"frame-bloqueado")
        estado_durante_ejecucion.append(v.cola_video.empty())
        return "Respuesta del puente"

    monkeypatch.setattr(vision.herramientas, "ejecutar", falso_ejecutar)
    llamada = types.LiveServerToolCall(
        function_calls=[types.FunctionCall(id="1", name="consultar_enjambre_multicerebro", args={"consulta": "hola"})]
    )
    asyncio.run(v.ejecutar_llamadas(llamada))

    assert estado_durante_ejecucion == [True, True]
    assert v._herramienta_pesada_activa is False
    # Al terminar, vuelve a permitir encolar frames normalmente
    v.encolar_video(b"frame-permitido")
    assert not v.cola_video.empty()


def test_precalentar_puente_y_observador_js_y_sin_robar_foco(monkeypatch):
    eventos = []

    class LocatorEntrada:
        def __init__(self, selector, falla_primera_vez=False):
            self.selector = selector
            self.falla_primera_vez = falla_primera_vez
            self.last = self
            self.first = self

        def count(self):
            return 1

        def is_visible(self):
            return True

        def click(self, timeout=None):
            eventos.append(f"click:{self.selector}")

        def fill(self, texto):
            eventos.append(f"fill:{self.selector}")

    class PaginaJS:
        url = "https://gemini.google.com/app"

        def __init__(self):
            self.eval_calls = 0

        def is_closed(self):
            return False

        def bring_to_front(self):
            eventos.append("bring_to_front")

        def get_by_role(self, rol, name=None):
            if rol == "button" and name is not None and hasattr(name, "search") and name.search("Neo"):
                btn = LocatorEntrada("boton:Neo")
                btn.inner_text = lambda: "Neo"
                return btn
            if rol == "menuitemradio" and name is not None and hasattr(name, "search"):
                if name.search("Neo"):
                    return LocatorEntrada("opcion:Neo")
                if name.search("Medio"):
                    return LocatorEntrada("pensamiento:Medio")
            return SimpleNamespace(count=lambda: 0)

        def locator(self, selector):
            if selector == gemini_web.SELECTORES["entrada"][0]:
                return LocatorEntrada(selector)
            if selector == gemini_web.SELECTORES["respuesta"][0]:
                return SimpleNamespace(count=lambda: 0)
            return SimpleNamespace(count=lambda: 0)

        keyboard = SimpleNamespace(press=lambda k: eventos.append(f"key:{k}"))

        def evaluate(self, script, arg):
            self.eval_calls += 1
            assert arg["estableMs"] == int(gemini_web.SEGUNDOS_ESTABLE_JS * 1000)
            return "Respuesta rápida por MutationObserver JS"

    pagina = PaginaJS()
    cliente = gemini_web.ClienteGeminiWeb()
    cliente._navegador = SimpleNamespace(
        is_connected=lambda: True,
        contexts=[SimpleNamespace(pages=[pagina], new_page=lambda: pagina)],
    )
    monkeypatch.setattr(cliente, "_conectar", lambda: None)

    # 1) Precalentar deja listo modelo/pensamiento (Neo + Medio) y caché de entrada sin robar foco
    assert cliente.precalentar() is True
    assert cliente._ultimo_modelo == "Neo"
    assert cliente._ultimo_pensamiento == "Medio"
    assert cliente._selector_entrada_cache == gemini_web.SELECTORES["entrada"][0]
    assert "bring_to_front" not in eventos

    # 2) Preguntar por defecto usa Neo + Medio, observador JS y caché sin llamar bring_to_front()
    eventos.clear()
    resp = cliente.preguntar("Hola rápido")
    assert resp == "Respuesta rápida por MutationObserver JS"
    assert pagina.eval_calls == 1
    assert "bring_to_front" not in eventos
    assert f"fill:{gemini_web.SELECTORES['entrada'][0]}" in eventos

    # 3) precalentar_puente respeta config.GEMINI_WEB_ACTIVO
    enviados_ejecutor = []
    monkeypatch.setattr(config, "GEMINI_WEB_ACTIVO", False)
    monkeypatch.setattr(gemini_web._EJECUTOR, "submit", lambda fn: enviados_ejecutor.append(fn))
    gemini_web.precalentar_puente()
    assert enviados_ejecutor == []

    monkeypatch.setattr(config, "GEMINI_WEB_ACTIVO", True)
    gemini_web.precalentar_puente()
    assert enviados_ejecutor == [gemini_web._precalentar_en_hilo]


def test_anti_throttling_cdp_fast_path_3_1_pro_y_separacion_pensamiento():
    # 1) Flags anti-throttling y timeouts rápidos
    assert "--disable-background-timer-throttling" in gemini_web.FLAGS_ANTI_THROTTLING_EDGE
    assert "--disable-backgrounding-occluded-windows" in gemini_web.FLAGS_ANTI_THROTTLING_EDGE
    assert "--disable-renderer-backgrounding" in gemini_web.FLAGS_ANTI_THROTTLING_EDGE
    assert gemini_web.TIMEOUT_BOTON_MODELO_RAPIDO_MS == 300
    assert gemini_web.TIMEOUT_SELECTOR_RAPIDO_MS == 600
    assert gemini_web.SEGUNDOS_ESTABLE == 0.4
    assert "model-thoughts" in gemini_web.SELECTORES["pensamiento_borrador"]

    # 2) CDP lifecycle active sin robar foco
    comandos_cdp = []

    class SesionCDPFalsa:
        def send(self, metodo, params=None):
            comandos_cdp.append((metodo, params))

    pagina_cdp = SimpleNamespace(
        url="https://gemini.google.com/app",
        is_closed=lambda: False,
        context=SimpleNamespace(new_cdp_session=lambda p: SesionCDPFalsa()),
    )
    cliente = gemini_web.ClienteGeminiWeb()
    cliente._navegador = SimpleNamespace(
        is_connected=lambda: True,
        contexts=[SimpleNamespace(pages=[pagina_cdp], new_page=lambda: pagina_cdp)],
    )
    cliente._pagina_principal()
    assert comandos_cdp == [
        ("Page.enable", None),
        ("Page.setWebLifecycleState", {"state": "active"}),
    ]

    # 3) Fast-Path instantáneo para 3.1 Pro (Alto) cuando ya está cacheado
    cliente._modelo_actual = "3.1 Pro"
    cliente._pensamiento_actual = "Alto"
    clics_menu = []
    boton_31 = SimpleNamespace(
        count=lambda: 1,
        is_visible=lambda: True,
        inner_text=lambda: "3.1 Pro",
        click=lambda **kw: clics_menu.append("menu_abierto"),
    )
    boton_31.last = boton_31
    pagina_fast = SimpleNamespace(
        get_by_role=lambda rol, name=None: boton_31 if rol == "button" else SimpleNamespace(count=lambda: 0),
        locator=lambda sel: SimpleNamespace(count=lambda: 0),
    )
    cliente._seleccionar_modelo(pagina_fast, gemini_web.Ruta("3.1 Pro", "Alto", chat_nuevo=False))
    assert clics_menu == []

    # 4) Recorte de reportes largos conservando inicio y cierre
    reporte_extenso = ("ENCABEZADO\n" * 2000) + ("CONCLUSION\n" * 1000)
    recortado = cliente._recortar_reporte_largo(reporte_extenso, max_chars=2000)
    assert "ENCABEZADO" in recortado and "CONCLUSION" in recortado
    assert "[... reporte extenso resumido ...]" in recortado









