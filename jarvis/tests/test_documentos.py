"""Pruebas unitarias para leer_documento_local, validación de archivos sensibles y personalidad en vision.py."""
from pathlib import Path
import pytest

import vision
from jarvis import herramientas, seguridad


# ---------------------------------------------------------------- personalidad en vision.py

def test_instrucciones_rol_ingeniera_auditora_sin_amiga_ideal():
    texto = vision.instrucciones(con_video=True, con_herramientas=True)
    assert "Ingeniera de Software Senior y Auditora Técnica" in texto
    assert "100% de precisión y certeza" in texto
    assert "NUNCA" in texto
    assert "complacencia" in texto.lower()
    assert "vos" in texto.lower()
    # Verificar que se eliminó por completo la personalidad anterior y contención emocional
    for prohibido in ("amiga ideal", "contención", "frustrada", "frustrado", "triste", "135"):
        assert prohibido not in texto.lower()


# ---------------------------------------------------------------- seguridad: archivos sensibles

@pytest.mark.parametrize("ruta", [
    ".env",
    ".env.local",
    ".env.production",
    "produccion.env",
    "C:/Users/jhonn/Downloads/jarvis/.env",
    "C:\\Users\\jhonn\\Downloads\\jarvis\\.env",
    "/home/user/app/.env.development",
    "~/.ssh/id_rsa",
    "C:/Users/user/.ssh/id_ed25519",
    "certificado.pem",
    "privada.key",
    "credenciales.json",
    "credentials.txt",
])
def test_es_archivo_sensible_bloquea_rutas_criticas(ruta):
    assert seguridad.es_archivo_sensible(ruta) is True


@pytest.mark.parametrize("ruta", [
    "README.md",
    "jarvis/vision.py",
    "datos/configuracion.json",
    "notas/resumen.txt",
    "manual_tecnico.pdf",
    "C:/proyecto/environment_utils.py",
])
def test_es_archivo_sensible_permite_archivos_normales(ruta):
    assert seguridad.es_archivo_sensible(ruta) is False


def test_leer_documento_local_bloquea_env_sin_abrirlo(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=AIzaSySecreto12345678901234567890", encoding="utf-8")

    respuesta = herramientas.leer_documento_local(str(env_file))
    assert "bloqueado" in respuesta.lower() or "prohibida" in respuesta.lower()
    assert ".env" in respuesta
    assert "AIzaSySecreto" not in respuesta


def test_leer_documento_local_bloquea_variantes_sensibles():
    for ruta in (".env", ".env.local", "C:/proyecto/.env", "~/.ssh/id_rsa"):
        respuesta = herramientas.leer_documento_local(ruta)
        assert "bloqueado por seguridad" in respuesta.lower()


# ---------------------------------------------------------------- lectura de texto plano y código

@pytest.mark.parametrize("nombre,contenido", [
    ("nota.txt", "Línea 1 de prueba\nLínea 2 con acentos: áéíóú ñ"),
    ("script.py", "def sumar(a: int, b: int) -> int:\n    return a + b\n"),
    ("datos.json", '{"proyecto": "jarvis", "version": 2}'),
    ("README.md", "# Jarvis\nAuditora técnica con precisión absoluta."),
])
def test_leer_documento_local_formatos_texto(tmp_path, nombre, contenido):
    archivo = tmp_path / nombre
    archivo.write_text(contenido, encoding="utf-8")

    resultado = herramientas.leer_documento_local(str(archivo))
    assert resultado == contenido


# ---------------------------------------------------------------- lectura de PDF con fitz (PyMuPDF)

def test_leer_documento_local_pdf_con_fitz_mock(tmp_path, monkeypatch):
    pdf_falso = tmp_path / "documento.pdf"
    pdf_falso.write_bytes(b"%PDF-1.4 fake content")

    class PaginaFalsa:
        def __init__(self, texto: str):
            self._texto = texto

        def get_text(self) -> str:
            return self._texto

    class DocumentoPDF:
        def __init__(self, ruta):
            self.ruta = Path(ruta)
            self.cerrado = False
            self._paginas = [
                PaginaFalsa("Página 1: Arquitectura del sistema."),
                PaginaFalsa("Página 2: Resultados de auditoría."),
            ]

        def __iter__(self):
            return iter(self._paginas)

        def close(self):
            self.cerrado = True

    instancias = []

    class FitzFalso:
        @staticmethod
        def open(ruta):
            doc = DocumentoPDF(ruta)
            instancias.append(doc)
            return doc

    monkeypatch.setattr(herramientas, "fitz", FitzFalso)

    resultado = herramientas.leer_documento_local(str(pdf_falso))
    assert "Página 1: Arquitectura del sistema." in resultado
    assert "Página 2: Resultados de auditoría." in resultado
    assert len(instancias) == 1
    assert instancias[0].cerrado is True


def test_leer_documento_local_pdf_sin_pymupdf_instalado(tmp_path, monkeypatch):
    pdf_falso = tmp_path / "informe.pdf"
    pdf_falso.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(herramientas, "fitz", None)

    resultado = herramientas.leer_documento_local(str(pdf_falso))
    assert "PyMuPDF" in resultado


# ---------------------------------------------------------------- límite de 80.000 caracteres

def test_leer_documento_local_trunca_mas_de_80000_caracteres(tmp_path):
    largo = 85_500
    archivo = tmp_path / "largo.txt"
    archivo.write_text("A" * largo, encoding="utf-8")

    resultado = herramientas.leer_documento_local(str(archivo))
    assert resultado.startswith("A" * 80_000)
    assert "A" * 80_001 not in resultado
    assert "80000" in resultado
    assert "85500" in resultado
    assert "truncado" in resultado.lower()


def test_leer_documento_local_exactamente_80000_no_trunca(tmp_path):
    archivo = tmp_path / "exacto.md"
    contenido = "B" * 80_000
    archivo.write_text(contenido, encoding="utf-8")

    resultado = herramientas.leer_documento_local(str(archivo))
    assert resultado == contenido
    assert "truncado" not in resultado.lower()


# ---------------------------------------------------------------- manejo de errores y registro

def test_leer_documento_local_archivo_inexistente(tmp_path):
    ruta = tmp_path / "no_existe.py"
    resultado = herramientas.leer_documento_local(str(ruta))
    assert "No encontré el archivo" in resultado


def test_leer_documento_local_extension_no_soportada(tmp_path):
    binario = tmp_path / "imagen.png"
    binario.write_bytes(b"\x89PNG\r\n")
    resultado = herramientas.leer_documento_local(str(binario))
    assert "no soportado" in resultado.lower()


def test_leer_documento_local_registrada_en_herramientas():
    assert "leer_documento_local" in herramientas.REGISTRO
    nombres_esquemas = {e["function"]["name"] for e in herramientas.esquemas()}
    assert "leer_documento_local" in nombres_esquemas

