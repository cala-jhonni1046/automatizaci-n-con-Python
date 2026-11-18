"""Tests unitarios para el puente Gemini Web (Edge + Playwright CDP), enrutamiento de modelos,
explicación de código (portapapeles / pantalla), análisis de repositorios y limpieza TTS.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis import cerebro, config, gemini_web, herramientas


# ---------------------------------------------------------------- 1. Enrutamiento de modelos

def test_elegir_modelo_por_defecto_4_hydrogen_misma_pestana():
    ruta = gemini_web.elegir_modelo("Explicame cómo funciona asyncio en Python")
    assert ruta.modelo == "4 Hydrogen"
    assert ruta.pensamiento == "Alto"
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
def test_elegir_modelo_investigacion_profunda_usa_3_1_pro_y_reutiliza_chat(pedido):
    ruta = gemini_web.elegir_modelo(pedido)
    assert ruta.modelo == "3.1 Pro"
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


def test_elegir_modelo_4_argon_solo_cuando_se_menciona_explicitamente():
    ruta_normal = gemini_web.elegir_modelo("Resolvé este problema complejo de concurrencia")
    assert ruta_normal.modelo != "4 Argon"

    for frase in ("Usá 4 Argon para revisar esta arquitectura", "usá Argon 4 en este análisis"):
        ruta_argon = gemini_web.elegir_modelo(frase)
        assert ruta_argon.modelo == "4 Argon"
        assert ruta_argon.pensamiento == "Medio"
        assert ruta_argon.chat_nuevo is False


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
    assert "[Gemini 4 Hydrogen · pensamiento Alto]" in salida
    assert "función suma" in salida
    assert len(llamadas) == 1
    mensaje, ruta, imagen = llamadas[0]
    assert "def suma(a, b):" in mensaje
    assert ruta.modelo == "4 Hydrogen"
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
    assert ruta.modelo == "4 Hydrogen"
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
    assert "[Gemini 4 Hydrogen · pensamiento Alto]" in resultado
    assert "Arquitectura limpia." in resultado
    mensaje, ruta = llamadas[0]
    assert "README.md" in mensaje
    assert "src/main.py" in mensaje
    assert "node_modules" not in mensaje
    assert ruta.modelo == "4 Hydrogen"
    assert ruta.chat_nuevo is False


# ---------------------------------------------------------------- 5. Integración en Cerebro

def test_cerebro_enruta_consultas_y_explicaciones_a_gemini_web_y_limpia_voz(monkeypatch):
    monkeypatch.setattr(
        herramientas,
        "consultar_gemini_pro",
        lambda pedido: "[Gemini 4 Hydrogen · pensamiento Alto]\nUsá **este comando**:\n```bash\npip install pytest\n```",
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

@pytest.mark.parametrize("etiqueta_visible", ["4 Hydrogen", "Hydrogen", "3.1 Pro", "4 Argon", "Flash-Lite"])
def test_buscar_boton_modelo_reconoce_nombres_visibles_y_cambia_entre_hydrogen_y_3_1_pro(etiqueta_visible):
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
                if name.search("4 Hydrogen"):
                    return ElementoFalso("opcion:4 Hydrogen")
                if name.search("3.1 Pro"):
                    return ElementoFalso("opcion:3.1 Pro")
                if name.search("Alto"):
                    return ElementoFalso("pensamiento:Alto")
            return classLocVacio

        def locator(self, selector):
            return classLocVacio

    cliente = gemini_web.ClienteGeminiWeb()
    pagina = PaginaFalsa()

    # 1) Selecciona 4 Hydrogen (pensamiento Alto)
    cliente._seleccionar_modelo(pagina, gemini_web.Ruta("4 Hydrogen", "Alto", chat_nuevo=False))
    assert clics == [f"boton:{etiqueta_visible}", "opcion:4 Hydrogen", "pensamiento:Alto", "key:Escape"]
    assert cliente._modelo_actual == "4 Hydrogen"

    # 2) Cambia a 3.1 Pro (pensamiento Alto)
    clics.clear()
    cliente._seleccionar_modelo(pagina, gemini_web.Ruta("3.1 Pro", "Alto", chat_nuevo=True))
    assert clics == [f"boton:{etiqueta_visible}", "opcion:3.1 Pro", "pensamiento:Alto", "key:Escape"]
    assert cliente._modelo_actual == "3.1 Pro"


# ---------------------------------------------------------------- 7. Delegación de investigar_a_fondo e instrucciones de vision.py

def test_investigar_a_fondo_delega_directamente_a_consultar_gemini_pro_3_1_pro(monkeypatch):
    llamadas = []

    def falso_consultar_web(mensaje, ruta=None, imagen=None, timeout=0):
        llamadas.append((mensaje, ruta))
        return "Documentación oficial desde Gemini 3.1 Pro."

    monkeypatch.setattr(gemini_web, "consultar", falso_consultar_web)
    # Incluso si DDGS fallara si se llamase, no se debe invocar nunca:
    monkeypatch.setattr(
        herramientas,
        "DDGS",
        lambda: (_ for _ in ()).throw(AssertionError("No debe usar DDGS")),
    )

    salida = herramientas.investigar_a_fondo("configurar trytond postgresql")
    assert "[Gemini 3.1 Pro · pensamiento Alto]" in salida
    assert "Documentación oficial desde Gemini 3.1 Pro." in salida
    assert len(llamadas) == 1
    mensaje, ruta = llamadas[0]
    assert mensaje == "configurar trytond postgresql"
    assert ruta.modelo == "3.1 Pro"
    assert ruta.pensamiento == "Alto"
    assert ruta.chat_nuevo is False


def test_instrucciones_vision_usan_consultar_gemini_pro_y_prohiben_herramientas_antiguas():
    import vision

    texto = vision.instrucciones(con_video=True, con_herramientas=True)
    assert 'consultar_gemini_pro(consulta, modelo="4 Hydrogen")' in texto
    assert 'consultar_gemini_pro(consulta, modelo="3.1 Pro")' in texto
    assert 'consultar_gemini_pro(consulta, modelo="4 Argon")' in texto
    assert "PROHIBIDO usar investigar_a_fondo o diagnosticar_y_resolver_con_certeza" in texto


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
        modelo="4 Hydrogen",
        contexto_pantalla="Programa: DBeaver | Cartel de error: FATAL: password authentication failed for user 'gnuhealth'",
    )
    assert "[Gemini 4 Hydrogen · pensamiento Alto]" in salida
    assert len(enviados) == 1
    mensaje, ruta = enviados[0]
    assert mensaje.startswith("CONTEXTO VISIBLE EN LA PANTALLA DEL USUARIO:\nPrograma: DBeaver")
    assert "CONSULTA DEL USUARIO:\nexplicame este error y cómo lo arreglo" in mensaje
    assert ruta.modelo == "4 Hydrogen"
    assert ruta.pensamiento == "Alto"


def test_argon_soporta_bajo_y_medio_y_fast_path_restaura_alto_al_volver():
    # 1) Niveles Bajo, Medio y Alto para 4 Argon
    r_bajo = gemini_web.elegir_modelo("Usá Argon 4 en bajo para revisar este bloque")
    assert r_bajo.modelo == "4 Argon"
    assert r_bajo.pensamiento == "Bajo"

    r_medio = gemini_web.elegir_modelo("Usá Argon 4 en medio para analizar el diseño")
    assert r_medio.modelo == "4 Argon"
    assert r_medio.pensamiento == "Medio"

    r_alto = gemini_web.elegir_modelo("Usá Argon 4 en alto como juez de arquitectura")
    assert r_alto.modelo == "4 Argon"
    assert r_alto.pensamiento == "Alto"

    # 2) Fast-Path y restauración de intensidad al pasar de Argon (Bajo/Medio) a Hydrogen/3.1 Pro (Alto)
    clics = []
    etiqueta_boton = ["4 Argon"]

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
                for op in ("4 Argon", "4 Hydrogen", "3.1 Pro"):
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

    # Selecciona 4 Argon en Bajo
    cliente._seleccionar_modelo(pagina, r_bajo)
    assert clics == ["boton:4 Argon", "opcion:4 Argon", "pensamiento:Bajo", "key:Escape"]
    assert cliente._ultimo_modelo == "4 Argon"
    assert cliente._ultimo_pensamiento == "Bajo"

    # Fast-Path: repetir 4 Argon en Bajo con botón "4 Argon" no vuelve a abrir el menú
    clics.clear()
    cliente._seleccionar_modelo(pagina, r_bajo)
    assert clics == []

    # Si se pide 4 Argon en Medio, aunque el modelo sea el mismo, cambia la intensidad -> abre el menú
    clics.clear()
    cliente._seleccionar_modelo(pagina, r_medio)
    assert clics == ["boton:4 Argon", "opcion:4 Argon", "pensamiento:Medio", "key:Escape"]
    assert cliente._ultimo_pensamiento == "Medio"

    # Si se pide 4 Argon en Alto (como Juez), cambia la intensidad a Alto dentro de 4 Argon
    clics.clear()
    cliente._seleccionar_modelo(pagina, r_alto)
    assert clics == ["boton:4 Argon", "opcion:4 Argon", "pensamiento:Alto", "key:Escape"]
    assert cliente._ultimo_pensamiento == "Alto"

    # Al volver a 4 Hydrogen (Alto), abre el menú y restaura explícitamente pensamiento Alto
    clics.clear()
    r_hydrogen = gemini_web.elegir_modelo("Explicame este método")
    cliente._seleccionar_modelo(pagina, r_hydrogen)
    assert clics == ["boton:4 Argon", "opcion:4 Hydrogen", "pensamiento:Alto", "key:Escape"]
    assert cliente._ultimo_modelo == "4 Hydrogen"
    assert cliente._ultimo_pensamiento == "Alto"

    # Al pasar a 3.1 Pro (Alto), abre el menú y fija 3.1 Pro en Alto
    etiqueta_boton[0] = "4 Hydrogen"
    clics.clear()
    r_pro = gemini_web.elegir_modelo("Hacé una investigación profunda sobre este protocolo")
    cliente._seleccionar_modelo(pagina, r_pro)
    assert clics == ["boton:4 Hydrogen", "opcion:3.1 Pro", "pensamiento:Alto", "key:Escape"]
    assert cliente._ultimo_modelo == "3.1 Pro"
    assert cliente._ultimo_pensamiento == "Alto"
    assert gemini_web.INTERVALO_POLLING_RESPUESTA == 0.25
    assert gemini_web.SEGUNDOS_ESTABLE == 0.6






