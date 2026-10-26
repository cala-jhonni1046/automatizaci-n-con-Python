"""Herramientas en vision.py, sin micrófono, pantalla ni conexión a Gemini.  Correr con:  python -m pytest"""
import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from google import genai
from google.genai import types

import vision
from jarvis import herramientas, seguridad


def nueva_vision(con_herramientas=True):
    return vision.VisionEnVivo(cliente=None, microfono=None, parlante=None, con_video=True,
                               con_herramientas=con_herramientas)


def llamada(*funciones):
    return types.LiveServerToolCall(function_calls=[
        types.FunctionCall(id=id_, name=nombre, args=args) for id_, nombre, args in funciones])


# ---------------------------------------------------------------- conversión de formatos

def test_convierte_todas_las_herramientas():
    [tool] = vision.a_herramientas_genai(herramientas.esquemas())
    nombres = [d.name for d in tool.function_declarations]
    assert nombres == list(herramientas.REGISTRO)
    for declaracion, esquema in zip(tool.function_declarations, herramientas.esquemas()):
        assert declaracion.description == esquema["function"]["description"]


def test_convierte_tipos_descripciones_y_requeridos():
    [tool] = vision.a_herramientas_genai(herramientas.esquemas())
    comando = next(d for d in tool.function_declarations if d.name == "ejecutar_comando")
    assert comando.parameters.type == types.Type.OBJECT
    assert comando.parameters.properties["comando"].type == types.Type.STRING
    assert comando.parameters.properties["en_linux"].type == types.Type.BOOLEAN
    assert comando.parameters.properties["comando"].description == "el comando exacto a ejecutar."
    assert comando.parameters.required == ["comando"]


def test_sin_parametros_se_declara_solo_el_nombre():
    [tool] = vision.a_herramientas_genai(herramientas.esquemas())
    fecha = next(d for d in tool.function_declarations if d.name == "fecha_y_hora")
    assert fecha.parameters is None


def test_convierte_numeros():
    esquema = {"type": "function", "function": {
        "name": "sumar", "description": "Suma.",
        "parameters": {"type": "object", "required": ["a"], "properties": {
            "a": {"type": "integer", "description": "a"}, "b": {"type": "number", "description": "b"}}}}}
    [tool] = vision.a_herramientas_genai([esquema])
    propiedades = tool.function_declarations[0].parameters.properties
    assert propiedades["a"].type == types.Type.INTEGER
    assert propiedades["b"].type == types.Type.NUMBER


def test_esquemas_para_litellm_no_cambian():
    # El modo texto (cerebro.py) sigue recibiendo el formato de OpenAI.
    vision.a_herramientas_genai(herramientas.esquemas())
    for esquema in herramientas.esquemas():
        assert esquema["type"] == "function"
        assert esquema["function"]["parameters"]["type"] == "object"


def test_configuracion_con_y_sin_herramientas():
    con = nueva_vision().configuracion()
    assert con.tools and con.tools[0].function_declarations
    assert "NUNCA digas que hiciste algo" in con.system_instruction
    sin = nueva_vision(con_herramientas=False).configuracion()
    assert sin.tools is None
    assert "herramientas" not in sin.system_instruction


# ---------------------------------------------------------------- llamadas a función

def test_ejecuta_cada_llamada_en_otro_hilo_y_arma_las_respuestas(monkeypatch):
    principal = threading.get_ident()
    vistas = []

    def falso_ejecutar(nombre, argumentos):
        vistas.append((nombre, argumentos, threading.get_ident() != principal))
        return f"hecho {nombre}"

    monkeypatch.setattr(vision.herramientas, "ejecutar", falso_ejecutar)
    v = nueva_vision()
    respuestas = asyncio.run(v.ejecutar_llamadas(llamada(
        ("id-1", "abrir_web", {"direccion_o_busqueda": "github.com"}),
        ("id-2", "fecha_y_hora", None))))
    assert vistas == [("abrir_web", {"direccion_o_busqueda": "github.com"}, True),
                      ("fecha_y_hora", {}, True)]
    assert [(r.id, r.name, r.response) for r in respuestas] == [
        ("id-1", "abrir_web", {"result": "hecho abrir_web"}),
        ("id-2", "fecha_y_hora", {"result": "hecho fecha_y_hora"})]


def test_herramienta_real_y_errores_vuelven_como_texto():
    v = nueva_vision()
    respuestas = asyncio.run(v.ejecutar_llamadas(llamada(
        ("a", "fecha_y_hora", {}), ("b", "borrar_todo", {}), ("c", "abrir_programa", {"otro": "x"}))))
    resultados = [r.response["result"] for r in respuestas]
    assert "/" in resultados[0]               # "sábado 24/10/2026, 12:00"
    assert "no existe" in resultados[1]
    assert "parámetros" in resultados[2]


def test_manda_la_respuesta_por_la_sesion(monkeypatch):
    monkeypatch.setattr(vision.herramientas, "ejecutar", lambda nombre, argumentos: "ok")

    class SesionFalsa:
        enviado = None

        async def send_tool_response(self, *, function_responses):
            self.enviado = function_responses

    sesion, v = SesionFalsa(), nueva_vision()
    v.herramientas_en_curso = 1  # lo suma recibir() al crear la tarea
    asyncio.run(v.usar_herramientas(sesion, llamada(("id-9", "fecha_y_hora", {}))))
    assert [(r.id, r.response) for r in sesion.enviado] == [("id-9", {"result": "ok"})]
    assert v.herramientas_en_curso == 0


# ---------------------------------------------------------------- confirmaciones

def confirmar_con(v, respuesta, pregunta="¿Ejecuto este comando?  rm -rf ~/x"):
    """Simula una herramienta que pide permiso desde su hilo y al usuario que contesta."""
    async def escenario():
        v.loop = asyncio.get_running_loop()
        hilo = asyncio.create_task(asyncio.to_thread(v.confirmar_por_terminal, pregunta))
        while v.respuesta_confirmacion is None:
            await asyncio.sleep(0.01)
        assert v.confirmando
        try:
            await v.atender_linea(respuesta)
        except vision.Salir:
            pass
        return await hilo

    return asyncio.run(asyncio.wait_for(escenario(), timeout=5))


@pytest.mark.parametrize("respuesta, esperado", [("sí", True), ("dale", True), ("no", False),
                                                 ("", False), ("salir", False)])
def test_confirmacion_por_terminal(respuesta, esperado):
    v = nueva_vision()
    assert confirmar_con(v, respuesta) is esperado
    assert not v.confirmando
    assert v.cola_envio.empty()  # la respuesta no se le manda a Gemini


def test_confirmacion_muestra_el_comando_completo(capsys):
    confirmar_con(nueva_vision(), "no", pregunta="⚠️ COMANDO PELIGROSO. ¿Ejecuto este comando?  ls\rrm -rf ~/x")
    salida = capsys.readouterr().out
    assert "ls\\rrm -rf ~/x" in salida  # el \r no puede tapar el rm


def test_salir_durante_la_confirmacion_responde_no_y_termina():
    async def escenario():
        v = nueva_vision()
        v.loop = asyncio.get_running_loop()
        v.respuesta_confirmacion = v.loop.create_future()
        with pytest.raises(vision.Salir):
            await v.atender_linea("salir")
        return v.respuesta_confirmacion.result()

    assert asyncio.run(escenario()) == "salir"


def test_sin_confirmacion_pendiente_el_texto_va_a_gemini():
    v = nueva_vision()
    asyncio.run(v.atender_linea("abrime el excel"))
    assert v.cola_envio.get_nowait() == ("texto", "abrime el excel")


def test_mientras_confirma_no_manda_audio_ni_pantalla():
    class SesionFalsa:
        def __init__(self):
            self.enviado = []

        async def send_realtime_input(self, **kwargs):
            self.enviado.append(next(iter(kwargs)))

    async def escenario():
        v, sesion = nueva_vision(), SesionFalsa()
        v.confirmando = True
        for item in [("audio", b"x"), ("video", b"y"), ("texto", "hola")]:
            v.cola_envio.put_nowait(item)
        tarea = asyncio.create_task(v.enviar(sesion))
        while not v.cola_envio.empty():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.01)
        v.confirmando = False
        v.cola_envio.put_nowait(("audio", b"z"))
        await asyncio.sleep(0.05)
        tarea.cancel()
        return sesion.enviado

    assert asyncio.run(escenario()) == ["text", "audio"]


def test_registra_el_confirmador_al_arrancar(monkeypatch):
    registrados = []
    monkeypatch.setattr(vision.seguridad, "registrar_confirmador", registrados.append)

    async def escenario(v):
        # correr() registra el confirmador antes de arrancar las tareas; se corta ahí.
        async def cortar():
            raise vision.Salir
        monkeypatch.setattr(v, "leer_teclado", cortar)
        for nombre in ("leer_microfono", "reproducir_audio", "capturar_pantalla", "conversar"):
            monkeypatch.setattr(v, nombre, lambda: asyncio.sleep(0))
        await v.correr()

    con = nueva_vision()
    asyncio.run(escenario(con))
    assert registrados == [con.confirmar_por_terminal]
    registrados.clear()
    asyncio.run(escenario(nueva_vision(con_herramientas=False)))
    assert registrados == []


# ---------------------------------------------------------------- llamadas asíncronas

def test_las_lentas_son_no_bloqueantes_y_el_resto_bloqueante():
    [tool] = vision.a_herramientas_genai(herramientas.esquemas())
    comportamiento = {d.name: d.behavior for d in tool.function_declarations}
    assert comportamiento.pop("buscar_archivos") == types.Behavior.NON_BLOCKING
    assert comportamiento.pop("ejecutar_comando") == types.Behavior.NON_BLOCKING
    assert set(comportamiento.values()) == {types.Behavior.BLOCKING}


def test_scheduling_interrupt_solo_en_las_no_bloqueantes():
    lenta = vision.VisionEnVivo.respuesta(types.FunctionCall(id="1", name="ejecutar_comando"), "ok")
    assert lenta.response == {"result": "ok", "scheduling": "INTERRUPT"}
    assert lenta.scheduling == types.FunctionResponseScheduling.INTERRUPT
    rapida = vision.VisionEnVivo.respuesta(types.FunctionCall(id="2", name="fecha_y_hora"), "ok")
    assert rapida.response == {"result": "ok"}
    assert rapida.scheduling is None


def test_no_bloqueante_sigue_pidiendo_confirmacion_por_terminal():
    """ejecutar_comando (NON_BLOCKING) con un comando que no es seguro: pregunta y, si decís no, no corre."""
    async def escenario():
        v = nueva_vision()
        v.loop = asyncio.get_running_loop()
        seguridad.registrar_confirmador(v.confirmar_por_terminal)
        tarea = asyncio.create_task(v.ejecutar_llamadas(llamada(
            ("c1", "ejecutar_comando", {"comando": "pip install paquete-que-no-existe"}))))
        while v.respuesta_confirmacion is None:
            await asyncio.sleep(0.01)
        assert v.confirmando
        await v.atender_linea("no")
        return await tarea

    [respuesta] = asyncio.run(asyncio.wait_for(escenario(), timeout=10))
    assert "NO autorizó" in respuesta.response["result"]
    assert respuesta.response["scheduling"] == "INTERRUPT"


def test_cancelar_la_llamada_que_espera_confirmacion_responde_no():
    async def escenario():
        v = nueva_vision()
        v.loop = asyncio.get_running_loop()
        hilo = asyncio.create_task(asyncio.to_thread(v.confirmar_por_terminal, "¿Ejecuto?  rm x"))
        while v.respuesta_confirmacion is None:
            await asyncio.sleep(0.01)
        v.id_en_curso = "c7"
        v.cancelar_llamadas(["c7"])
        return await hilo

    assert asyncio.run(asyncio.wait_for(escenario(), timeout=5)) is False


def test_llamada_cancelada_no_se_ejecuta_ni_se_responde(monkeypatch):
    ejecutadas = []
    monkeypatch.setattr(vision.herramientas, "ejecutar", lambda nombre, args: ejecutadas.append(nombre) or "ok")

    class SesionFalsa:
        enviado = None

        async def send_tool_response(self, *, function_responses):
            self.enviado = function_responses

    v, sesion = nueva_vision(), SesionFalsa()
    v.cancelar_llamadas(["x1"])
    v.herramientas_en_curso = 1
    asyncio.run(v.usar_herramientas(sesion, llamada(("x1", "ejecutar_comando", {"comando": "ls"}))))
    assert ejecutadas == []
    assert sesion.enviado is None
    assert v.herramientas_en_curso == 0


# ---------------------------------------------------------------- búsqueda, VAD, afectivo, proactivo

def test_busqueda_de_google_junto_a_las_funciones():
    v = vision.VisionEnVivo(None, None, None, con_video=True, con_busqueda=True)
    tools = v.herramientas_genai()
    assert tools[0].function_declarations
    assert tools[-1].google_search is not None
    assert "buscar en Google" in v.configuracion().system_instruction


def test_busqueda_apagada_por_defecto():
    v = nueva_vision()
    assert not v.con_busqueda
    assert all(t.google_search is None for t in v.herramientas_genai())
    assert "buscar en Google" not in v.configuracion().system_instruction


@pytest.mark.parametrize("valor, esperado", [("true", "True"), ("false", "False"), ("", "False")])
def test_variable_busqueda(valor, esperado):
    raiz = Path(vision.__file__).parent
    entorno = dict(os.environ, JARVIS_BUSQUEDA=valor)
    salida = subprocess.run([sys.executable, "-c", "import vision; print(vision.BUSQUEDA_ACTIVADA)"],
                            cwd=raiz, env=entorno, capture_output=True, text=True, timeout=60)
    assert salida.stdout.strip() == esperado


def test_cuota_agotada_con_busqueda_apunta_a_la_busqueda():
    error = Exception("1011 None. You exceeded your current quota, please check your plan")
    con = vision.explicar_error(error, con_busqueda=True)
    assert "cuota de búsqueda" in con
    assert "sin --con-busqueda" in con and "JARVIS_BUSQUEDA=false" in con
    sin = vision.explicar_error(error, con_busqueda=False)
    assert "búsqueda" not in sin


def test_sin_busqueda():
    v = vision.VisionEnVivo(None, None, None, con_video=True, con_busqueda=False)
    tools = v.herramientas_genai()
    assert all(t.google_search is None for t in tools)
    assert "buscar en Google" not in v.configuracion().system_instruction


def test_sin_herramientas_tampoco_busca():
    v = vision.VisionEnVivo(None, None, None, con_video=True, con_herramientas=False, con_busqueda=True)
    assert v.herramientas_genai() is None
    assert not v.con_busqueda


def test_configuracion_de_voz_y_opciones_apagadas_por_defecto():
    config = nueva_vision().configuracion()
    deteccion = config.realtime_input_config.automatic_activity_detection
    assert deteccion.silence_duration_ms == 700
    assert deteccion.prefix_padding_ms == 300
    # gemini-3.8-live rechaza "proactivity" (error 1007): ninguna de las dos va si no se pide.
    assert config.enable_affective_dialog is None
    assert config.proactivity is None


def test_afectivo_y_proactivo_se_prenden_desde_el_env(monkeypatch):
    monkeypatch.setattr(vision, "AFECTIVO", True)
    monkeypatch.setattr(vision, "PROACTIVO", True)
    config = nueva_vision().configuracion()
    assert config.enable_affective_dialog is True
    assert config.proactivity.proactive_audio is True


@pytest.mark.parametrize("valor, esperado", [("true", "True"), ("1", "True"), ("false", "False"),
                                             ("", "False"), ("cualquier", "False")])
def test_variables_afectivo_y_proactivo(valor, esperado):
    raiz = Path(vision.__file__).parent
    entorno = dict(os.environ, JARVIS_AFECTIVO=valor, JARVIS_PROACTIVO=valor)
    salida = subprocess.run(
        [sys.executable, "-c", "import vision; print(vision.AFECTIVO, vision.PROACTIVO)"],
        cwd=raiz, env=entorno, capture_output=True, text=True, timeout=60)
    assert salida.stdout.split() == [esperado, esperado]


# ---------------------------------------------------------------- errores al conectar

ERROR_1007 = ('Invalid JSON payload received. Unknown name "proactivity" at \'setup\': '
              'Cannot find field.')


def test_explica_opcion_no_soportada():
    mensaje = vision.explicar_error(Exception(f"received 1007 {ERROR_1007}"))
    assert mensaje == (f"El modelo {vision.MODELO} no soporta la opción 'proactivity'. "
                       "Apagala con JARVIS_PROACTIVO=false en el .env.")


def test_explica_opcion_no_soportada_sin_variable():
    mensaje = vision.explicar_error(Exception('Unknown name "otraCosa" at \'setup\''))
    assert "no soporta la opción 'otraCosa'" in mensaje
    assert "Sacala de configuracion()" in mensaje


def test_explica_cuota_agotada_y_otros_errores():
    assert "cuota" in vision.explicar_error(Exception("1011 You exceeded your current quota, please"))
    assert vision.explicar_error(Exception("algo raro")) == "Error de conexión con Gemini: algo raro"


def test_fallo_de_configuracion_al_conectar_da_un_mensaje_claro():
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    class ConexionRechazada:
        async def __aenter__(self):
            raise ConnectionClosedError(Close(1007, ERROR_1007), None)

        async def __aexit__(self, *args):
            return False

    cliente = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(
        connect=lambda model, config: ConexionRechazada())))
    v = vision.VisionEnVivo(cliente, None, None, con_video=True)
    with pytest.raises(BaseException) as atrapado:
        asyncio.run(v.conversar())
    error = vision.buscar_error(atrapado.value, vision.ErrorDeConexion)
    assert error is not None
    assert "no soporta la opción 'proactivity'" in str(error)
    assert "JARVIS_PROACTIVO=false" in str(error)


def test_buscar_error_en_grupos_anidados():
    adentro = vision.ErrorDeConexion("x")
    grupo = ExceptionGroup("afuera", [ValueError(), ExceptionGroup("medio", [adentro])])
    assert vision.buscar_error(grupo, vision.ErrorDeConexion) is adentro
    assert vision.buscar_error(ValueError(), vision.ErrorDeConexion) is None


def test_vad_se_lee_del_env():
    raiz = Path(vision.__file__).parent
    entorno = dict(os.environ, JARVIS_VAD_SILENCIO_MS="900", JARVIS_VAD_PREFIJO_MS="150")
    salida = subprocess.run(
        [sys.executable, "-c", "import vision; print(vision.VAD_SILENCIO_MS, vision.VAD_PREFIJO_MS)"],
        cwd=raiz, env=entorno, capture_output=True, text=True, timeout=60)
    assert salida.stdout.split() == ["900", "150"]


def test_v1beta_es_la_version_que_ya_usaba_el_sdk():
    por_defecto = genai.Client(api_key="clave-de-prueba")
    fijada = genai.Client(api_key="clave-de-prueba", http_options=types.HttpOptions(api_version="v1beta"))
    assert por_defecto._api_client._http_options.api_version == "v1beta"
    assert fijada._api_client._http_options.api_version == "v1beta"


# ---------------------------------------------------------------- tokens e idiomas

def test_tokens_se_suman_y_se_muestran_como_mucho_una_vez_por_minuto(capsys):
    v = nueva_vision()
    v.contar_tokens(types.UsageMetadata(total_token_count=1200))
    assert "Tokens" not in capsys.readouterr().out          # todavía no pasó un minuto
    v.tokens_informados_en -= vision.SEGUNDOS_ENTRE_INFORMES_DE_TOKENS
    v.contar_tokens(types.UsageMetadata(total_token_count=300))
    assert "Tokens consumidos en esta sesión: 1.500" in capsys.readouterr().out
    v.contar_tokens(types.UsageMetadata(total_token_count=None))
    assert v.tokens == 1500 and capsys.readouterr().out == ""


def test_instrucciones_de_idiomas_en_todos_los_modos():
    for v in (nueva_vision(), nueva_vision(con_herramientas=False)):
        texto = v.configuracion().system_instruction
        assert "clases en otros idiomas" in texto
        assert "volvé al español" in texto


# ---------------------------------------------------------------- saludo

def test_saludo_inicial():
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    class SesionFalsa:
        def __init__(self):
            self.textos = []

        async def send_realtime_input(self, **kwargs):
            if "text" in kwargs:
                self.textos.append(kwargs["text"])

        async def receive(self):
            await asyncio.sleep(0.1)  # da tiempo a que salga el saludo y después corta
            raise ConnectionClosedError(Close(1011, "fin de la prueba"), None)
            yield

    sesion = SesionFalsa()

    class Conexion:
        async def __aenter__(self):
            return sesion

        async def __aexit__(self, *args):
            return False

    cliente = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(
        connect=lambda model, config: Conexion())))
    v = vision.VisionEnVivo(cliente, None, None, con_video=True)
    with pytest.raises(BaseException):
        asyncio.run(v.conversar())
    assert sesion.textos == ["Saludá diciendo exactamente esta frase y nada más: "
                             '"Hola Johnny, soy tu amiga ideal, soy Lucy, ¿en qué te puedo ayudar?"']
