"""Pruebas unitarias para la arquitectura de Doble Cerebro y Certeza Absoluta."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

import vision
from jarvis import cerebro, herramientas


def test_dependencias_en_requirements():
    contenido = (Path(vision.__file__).parent / "requirements.txt").read_text(encoding="utf-8")
    for dep in ("trafilatura>=1.8", "psutil>=5.9", "pywinauto>=0.6.8", "pydantic>=2.0"):
        assert dep in contenido


def test_protocolo_certeza_100_en_vision_y_no_bloqueantes():
    assert "diagnosticar_y_resolver_con_certeza" in vision.NO_BLOQUEANTES
    assert "diagnosticar_y_resolver_con_certeza" in herramientas.REGISTRO

    texto = vision.instrucciones(con_video=True, con_herramientas=True)
    assert "PROTOCOLO DE CERTEZA 100% (PROHIBIDO IMPROVISAR EN VIVO):" in texto
    assert "Esperame unos segundos que lo investigo a fondo y verifico tu sistema para darte el dato exacto." in texto
    assert "diagnosticar_y_resolver_con_certeza(consulta_usuario)" in texto
    assert "Jamás uses al usuario como conejillo de indias" in texto


def test_investigar_a_fondo_usa_trafilatura_con_parametros_exactos_y_jina(monkeypatch):
    llamadas_trafilatura = []

    class TrafilaturaFalso:
        @staticmethod
        def extract(html, include_formatting=False, include_tables=False):
            llamadas_trafilatura.append({
                "include_formatting": include_formatting,
                "include_tables": include_tables,
            })
            return "## Configuración Oficial\n| Puerto | Servicio |\n| 5432 | PostgreSQL |"

    class DDGSFalso:
        def text(self, consulta, max_results=4):
            return [
                {"title": "PostgreSQL Docs", "href": "https://postgresql.org/docs/config"},
                {"title": "Doc Jina", "href": "https://gnuhealth.org/jina-doc", "usar_jina": True},
            ]

    llamadas_get = []

    class RequestsFalso:
        @staticmethod
        def get(url, timeout=None, headers=None):
            llamadas_get.append(url)
            if url == "https://postgresql.org/docs/config":
                return SimpleNamespace(
                    status_code=200,
                    text="<html><body><pre><code>listen_addresses = '*'</code></pre></body></html>",
                )
            if url == "https://gnuhealth.org/jina-doc":
                return SimpleNamespace(status_code=503, text="")
            if url == "https://r.jina.ai/https://gnuhealth.org/jina-doc":
                return SimpleNamespace(status_code=200, text="Contenido limpio leído vía r.jina.ai")
            return SimpleNamespace(status_code=404, text="")

    monkeypatch.setattr(herramientas, "trafilatura", TrafilaturaFalso)
    monkeypatch.setattr(herramientas, "DDGS", DDGSFalso)
    monkeypatch.setattr(herramientas, "requests", RequestsFalso)

    salida = herramientas.investigar_a_fondo("configurar posgre en genugel", solo_fuentes_oficiales=True)

    assert llamadas_trafilatura
    assert llamadas_trafilatura[0] == {"include_formatting": True, "include_tables": True}
    assert "Configuración Oficial" in salida
    assert "listen_addresses = '*'" in salida
    assert "https://r.jina.ai/https://gnuhealth.org/jina-doc" in llamadas_get
    assert "Contenido limpio leído vía r.jina.ai" in salida


def test_flujo_completo_diagnosticar_y_resolver_con_certeza(monkeypatch):
    # 1. Mock de pywinauto (ventana activa y controles)
    class ControlFalso:
        def __init__(self, texto, tipo="Button"):
            self._texto = texto
            self.element_info = SimpleNamespace(control_type=tipo)

        def window_text(self):
            return self._texto

    class VentanaFalsa:
        @staticmethod
        def window_text():
            return "DBeaver - Conexión a PostgreSQL"

        @staticmethod
        def descendants():
            return [
                ControlFalso("Host: 192.168.100.24", "Edit"),
                ControlFalso("Database: gnuhealth_db", "Edit"),
                ControlFalso("Probar conexión", "Button"),
            ]

    class DesktopFalso:
        def __init__(self, backend="uia"):
            self.backend = backend

        @staticmethod
        def top_window():
            return VentanaFalsa()

    monkeypatch.setattr(herramientas, "pywinauto", SimpleNamespace(Desktop=DesktopFalso))
    monkeypatch.setattr(
        herramientas,
        "inspeccionar_pantalla_hd",
        lambda: "Ventana activa: DBeaver - Conexión a PostgreSQL\nPosición del cursor: (500, 300)",
    )

    # 2. Mock de psutil (procesos y puertos)
    class PsutilFalso:
        @staticmethod
        def process_iter(attrs=None):
            return [SimpleNamespace(info={"pid": 4120, "name": "dbeaver.exe"})]

        @staticmethod
        def net_connections(kind="inet"):
            return [SimpleNamespace(status="LISTEN", laddr=SimpleNamespace(port=5432))]

    monkeypatch.setattr(herramientas, "psutil", PsutilFalso)

    # 3. Mock de verificación silenciosa SSH en VM y WSL
    comandos_vm = []
    comandos_wsl = []
    monkeypatch.setattr(
        herramientas,
        "ejecutar_ssh_vm",
        lambda cmd: comandos_vm.append(cmd) or "SSH gnuhealth@192.168.100.24 (código 0):\ngnuhealth44 | gnuhealth | UTF8",
    )
    monkeypatch.setattr(
        herramientas,
        "ejecutar_wsl",
        lambda cmd: comandos_wsl.append(cmd) or "Código de salida 0.\nLISTEN 0 128 0.0.0.0:8000",
    )

    # 4. Mock de investigación documental
    monkeypatch.setattr(
        herramientas,
        "investigar_a_fondo",
        lambda consulta, solo_fuentes_oficiales=True: "Fuente: https://docs.gnuhealth.org\n```psql -U gnuhealth -d gnuhealth44```",
    )

    # 5. Mock de litellm.completion verificando temperature=0.0 y esquema Pydantic DiagnosticoVerificado
    llamadas_llm = []

    def falsa_completion(**kwargs):
        llamadas_llm.append(kwargs)
        payload = {
            "CausaVerificada": "La base de datos real en la VM 192.168.100.24 se llama 'gnuhealth44' y no 'gnuhealth_db'.",
            "EvidenciaDelSistema": "psql -lqt en VM devolvió 'gnuhealth44'; DBeaver (pid=4120) apunta a 'gnuhealth_db'.",
            "PasosExactosNumerados": [
                "Paso 1: En la ventana 'DBeaver - Conexión a PostgreSQL', hacé clic en el campo 'Database'.",
                "Paso 2: Reemplazá 'gnuhealth_db' por el nombre real verificado 'gnuhealth44' y usuario 'gnuhealth'.",
                "Paso 3: Apretá el botón 'Probar conexión' y luego 'Finalizar'.",
            ],
            "ComandoVerificado": "psql -h 192.168.100.24 -p 5432 -U gnuhealth -d gnuhealth44",
        }
        mensaje = SimpleNamespace(content=json.dumps(payload))
        return SimpleNamespace(choices=[SimpleNamespace(message=mensaje)])

    monkeypatch.setattr(herramientas.config, "clave_configurada", lambda: True)
    monkeypatch.setattr(herramientas.litellm, "completion", falsa_completion)

    resultado = herramientas.diagnosticar_y_resolver_con_certeza(
        "no puedo conectar divi builder a posgre en la vm de genugel ni desde wsl"
    )

    # Verifica que interrogó tanto la VM por SSH como WSL antes de responder
    assert len(comandos_vm) == 1
    assert len(comandos_wsl) == 1

    # Verifica que el Agente Verificador corrió con temperature=0.0 y esquema Pydantic DiagnosticoVerificado
    assert len(llamadas_llm) == 1
    assert llamadas_llm[0]["temperature"] == 0.0
    assert llamadas_llm[0]["response_format"] is herramientas.DiagnosticoVerificado

    # Verifica el formato estricto de salida sin palabras condicionales
    assert "HECHO VERIFICADO:" in resultado
    assert "gnuhealth44" in resultado
    assert "PASOS EXACTOS (1, 2, 3):" in resultado
    assert "1. En la ventana 'DBeaver - Conexión a PostgreSQL'" in resultado
    assert "2. Reemplazá 'gnuhealth_db' por el nombre real verificado 'gnuhealth44'" in resultado
    assert "3. Apretá el botón 'Probar conexión' y luego 'Finalizar'." in resultado
    assert "COMANDO VERIFICADO: psql -h 192.168.100.24 -p 5432 -U gnuhealth -d gnuhealth44" in resultado

    for prohibida in ("quizás", "probá", "debería", "fijate si", "probablemente"):
        assert prohibida not in resultado.lower()


def test_diagnosticar_elimina_palabras_condicionales_en_modo_determinista(monkeypatch):
    monkeypatch.setattr(herramientas, "_inspeccionar_controles_pywinauto", lambda: "Ventana UIA: Terminal")
    monkeypatch.setattr(herramientas, "inspeccionar_pantalla_hd", lambda: "Ventana activa: Terminal")
    monkeypatch.setattr(herramientas, "_inspeccionar_procesos_y_puertos_psutil", lambda: "Puertos en escucha: :5432")
    monkeypatch.setattr(herramientas, "investigar_a_fondo", lambda *a, **k: "Documentación verificada")
    monkeypatch.setattr(herramientas.config, "clave_configurada", lambda: False)

    salida = herramientas.diagnosticar_y_resolver_con_certeza("verificar puerto posgre")
    assert "HECHO VERIFICADO:" in salida
    assert "PASOS EXACTOS (1, 2, 3):" in salida
    assert "1." in salida and "2." in salida and "3." in salida
    for prohibida in ("quizás", "probá", "debería"):
        assert prohibida not in salida.lower()

