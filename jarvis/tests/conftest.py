import pytest

from jarvis import config, seguridad


@pytest.fixture(autouse=True)
def restaurar_confirmador():
    """Cada test puede cambiar el confirmador global; al terminar se deja el original."""
    original = seguridad._confirmar
    yield
    seguridad.registrar_confirmador(original)


@pytest.fixture(autouse=True)
def sin_gemini_web(monkeypatch):
    """Ningún test abre Edge de verdad: los que prueban Gemini web lo activan con mocks."""
    monkeypatch.setattr(config, "GEMINI_WEB_ACTIVO", False)
