"""Precisión analítica de vision.py: instrucciones de auditor y capturas legibles para el modelo."""
import io

from google.genai import types
from PIL import Image, ImageDraw, ImageFont

import vision


def nueva_vision(con_video=True, con_herramientas=True):
    return vision.VisionEnVivo(cliente=None, microfono=None, parlante=None, con_video=con_video,
                               con_herramientas=con_herramientas)


def editor_de_codigo(texto: str, ancho=1920, alto=1080) -> Image.Image:
    """Simula un editor oscuro con una línea de código en letra de 14 px (tamaño típico de VS Code)."""
    imagen = Image.new("RGB", (ancho, alto), color=(30, 30, 30))
    ImageDraw.Draw(imagen).text((100, 300), texto, fill=(220, 220, 170),
                                font=ImageFont.load_default(size=14))
    return imagen


# ---------------------------------------------------------------- instrucciones

def _texto_instruccion(config: types.LiveConnectConfig) -> str:
    assert isinstance(config.system_instruction, types.Content)
    assert config.system_instruction.role is None
    assert config.system_instruction.parts and isinstance(config.system_instruction.parts[0], types.Part)
    return config.system_instruction.parts[0].text


def test_auditor_va_primero_en_todos_los_modos():
    for v in (nueva_vision(), nueva_vision(con_video=False), nueva_vision(con_herramientas=False)):
        texto = _texto_instruccion(v.configuracion())
        assert texto.startswith("REGLA DE VISIÓN EN TIEMPO REAL (PROHIBIDO HABLAR A CIEGAS):")
        assert "TIENES PROHIBIDO decir 'debería aparecerte'" in texto
        assert "'fijate si ves'" in texto
        assert "'probablemente te salga'" in texto
        assert "'imagino que estás en'" in texto
        assert "Eres un auditor de software y código extremadamente riguroso." in texto
        assert "NUNCA des la razón al usuario por inercia" in texto
        assert "letra por letra" in texto


def test_regla_prohibido_hablar_en_condicional_en_vision_y_cerebro():
    from jarvis import cerebro

    for texto in (vision.instrucciones(True, True), cerebro.INSTRUCCIONES):
        assert texto.startswith("REGLA DE VISIÓN EN TIEMPO REAL (PROHIBIDO HABLAR A CIEGAS):")
        assert "debería aparecerte" in texto
        assert "Estoy viendo tu ventana de [Programa]" in texto


def test_auditor_no_borra_las_reglas_de_seguridad():
    texto = _texto_instruccion(nueva_vision().configuracion())
    assert "Ingeniera de Software Senior y Auditora Técnica" in texto
    assert "100% de precisión y certeza" in texto
    assert "amiga ideal" not in texto.lower()
    assert "contención" not in texto.lower()
    assert "frustrad" not in texto.lower()
    assert "datos sensibles" in texto                  # no leer contraseñas en voz alta
    assert "NUNCA digas que hiciste algo" in texto     # herramientas


# ---------------------------------------------------------------- resolución del modelo

def test_resolucion_alta_con_video_y_nada_sin_video():
    assert nueva_vision().configuracion().media_resolution == types.MediaResolution.MEDIA_RESOLUTION_HIGH
    assert nueva_vision(con_video=False).configuracion().media_resolution is None


def test_resolucion_no_soportada_explica_como_apagarla():
    mensaje = vision.explicar_error(Exception('Unknown name "media_resolution" at \'setup\''))
    assert "JARVIS_RESOLUCION_VIDEO" in mensaje


# ---------------------------------------------------------------- JPEG legible

def test_1080p_se_manda_en_resolucion_nativa_con_calidad_85():
    assert vision.ANCHO_CAPTURA == 1920
    assert vision.ALTO_CAPTURA == 1080
    assert vision.CALIDAD_JPEG == 85
    jpeg = vision.comprimir_jpeg(editor_de_codigo("def suma(a, b):"))
    imagen = Image.open(io.BytesIO(jpeg))
    assert imagen.size == (1920, 1080)
    # Sin submuestreo de color (4:4:4): el resaltado de sintaxis no se emborrona.
    assert imagen.layer[0][1:3] == imagen.layer[1][1:3] == (1, 1)


def test_4k_se_achica_a_1920_y_lo_chico_no_se_agranda():
    grande = Image.open(io.BytesIO(vision.comprimir_jpeg(editor_de_codigo("x", 3840, 2160))))
    assert grande.size == (1920, 1080)
    chica = Image.open(io.BytesIO(vision.comprimir_jpeg(editor_de_codigo("x", 1366, 768))))
    assert chica.size == (1366, 768)


# ---------------------------------------------------------------- detección de cambios

def test_una_letra_corregida_se_detecta_como_cambio_chico():
    antes = editor_de_codigo("prnt('hola')")
    despues = editor_de_codigo("print('hola')")
    # No alcanza para un envío inmediato...
    assert not vision.hubo_cambio_visual(despues, antes)
    # ...pero sí para el refresco de cambios chicos, así Lucy no revisa código viejo.
    assert vision.hubo_cambio_visual(despues, antes, aceptar_cambio_chico=True)


def test_pantalla_identica_no_se_reenvia_nunca():
    pantalla = editor_de_codigo("print('hola')")
    assert not vision.hubo_cambio_visual(pantalla, pantalla.copy(), aceptar_cambio_chico=True)


def test_enviar_video_imprime_resolucion_exacta_en_consola(capsys):
    import asyncio

    class SesionFalsa:
        def __init__(self):
            self.frames = []

        async def send_realtime_input(self, **kwargs):
            if "video" in kwargs:
                self.frames.append(kwargs["video"])

    async def escenario():
        v, sesion = nueva_vision(), SesionFalsa()
        jpeg = vision.comprimir_jpeg(editor_de_codigo("print('test')"))
        v.encolar_video(jpeg)
        tarea = asyncio.create_task(v.enviar_video(sesion))
        while not sesion.frames:
            await asyncio.sleep(0.01)
        tarea.cancel()

    asyncio.run(escenario())
    salida = capsys.readouterr().out
    assert "1920x1080" in salida





def test_inspeccionar_pantalla_hd_envia_jpeg_a_sesion_y_devuelve_ventana_y_cursor(monkeypatch):
    from jarvis import herramientas

    img_real = editor_de_codigo("SELECT * FROM gnuhealth_patient;")

    class CapturaReal:
        size = (1920, 1080)
        rgb = img_real.tobytes()

    class SctFalso:
        primary_monitor = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        monitors = [primary_monitor, primary_monitor]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def grab(self, region):
            return CapturaReal()

    class MssModuloFalso:
        @staticmethod
        def mss():
            return SctFalso()

    monkeypatch.setattr(herramientas, "mss", MssModuloFalso)
    monkeypatch.setattr(
        herramientas, "_info_ventana_activa",
        lambda: ("DBeaver 24.0 - Script-1", "dbeaver.exe", {"left": 0, "top": 0, "width": 1920, "height": 1080}),
    )
    monkeypatch.setattr(herramientas, "_posicion_cursor", lambda: (640, 420))

    v = nueva_vision()
    salida = herramientas.inspeccionar_pantalla_hd()

    assert "DBeaver 24.0 - Script-1" in salida
    assert "(640, 420)" in salida
    assert "mime_type=image/jpeg" in salida
    assert "enviado a la sesión Gemini Live" in salida

    # Verifica que el frame real 1920x1080 fue encolado en la sesión de VisionEnVivo
    assert not v.cola_video.empty()
    jpeg_enviado = v.cola_video.get_nowait()
    img_decodificada = Image.open(io.BytesIO(jpeg_enviado))
    assert img_decodificada.size == (1920, 1080)
    assert img_decodificada.format == "JPEG"
